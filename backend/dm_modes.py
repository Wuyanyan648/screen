#!/usr/bin/env python3
"""分辨率 / 刷新率切换：只改目标输出的 mode+rate，其余参数原样保留。

安全流程（四步，缺一不可）：
1. 应用前把当前时序记为 last-known-good 并落盘（modes.json），同时写一条 pending；
2. 应用后重新读 xrandr 复核：输出仍启用、模式与刷新率真的生效、transform 没被改、
   CRTC 有效；任何一项不符立即回滚到 last-known-good；
3. 成功后开一个确认窗（默认 15 秒）：用户点「保持」才确认，超时或点「撤销」自动退回
   —— 黑屏时人够不到页面，所以超时回滚放在后端，不依赖前端；
4. 一键「恢复上次可用模式」直接回到 last_good。

与其它模块的配合：
- 只发目标输出的参数，别的输出一个都不带；若 mutter 趁机点亮了
  ~/.cache/display-manager.off 名单里的输出，立刻关回去（名单本身不改）；
- RandR 变化会把 gamma 复位，应用后调用 dm_brightness.reapply_software() 补回亮度/色温；
- 同步 ~/.cache/hdmi-watchdog.layout，避免看门狗把旧模式当基线又改回去；
- 护眼休息覆盖层运行期间拒绝切换，避免覆盖层几何错位。

模式与刷新率一律来自 `xrandr --current` 实际暴露的列表（dm_core._parse_query 的
`modes` 字段），不接受客户端臆造的值。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime

import dm_core as core

MODES_FILE = os.path.join(core.CACHE_DIR, "display-manager.modes.json")
CONFIRM_SECONDS = 15                       # 确认窗时长：超时未确认自动退回
RATE_TOL = 0.05                            # 刷新率复核容差（xrandr 两位小数）
TRANSFORM_TOL = 1e-3

_LOCK: threading.Lock | None = None
_TIMER: threading.Timer | None = None


def init(lock: threading.Lock) -> None:
    """由 dm_server 注入全局 LOCK，保证和 off/on/亮度等写操作串行。"""
    global _LOCK
    _LOCK = lock


def _log(msg: str) -> None:
    core.log("模式切换: " + msg)


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
def _read() -> dict:
    try:
        with open(MODES_FILE, "r") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(d: dict) -> None:
    os.makedirs(core.CACHE_DIR, exist_ok=True)
    tmp = MODES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MODES_FILE)


def last_good() -> dict | None:
    rec = _read().get("last_good")
    return rec if isinstance(rec, dict) and rec.get("mode") else None


def pending() -> dict | None:
    p = _read().get("pending")
    if not isinstance(p, dict) or not p.get("token"):
        return None
    return p


def _set_pending(p: dict | None) -> None:
    d = _read()
    if p is None:
        d.pop("pending", None)
    else:
        d["pending"] = p
    _write(d)


def _set_last_good(rec: dict) -> None:
    d = _read()
    d["last_good"] = rec
    _write(d)


# --------------------------------------------------------------------------- #
# 模式枚举（只读 xrandr 实际暴露的列表）
# --------------------------------------------------------------------------- #
def summary(snap: dict) -> dict:
    """给 /api/state 用的精简视图：每块 connected 输出的模式表 + 当前值 + 记录。"""
    outs = {}
    for o in snap["outputs"]:
        if not o["connected"]:
            continue
        outs[o["name"]] = {
            "enabled": o["enabled"],
            "current": {"mode": o["mode"], "rate": o["rate"]},
            "modes": o.get("modes") or [],
        }
    return {
        "outputs": outs,
        "pending": pending(),
        "last_good": last_good(),
        "confirm_seconds": CONFIRM_SECONDS,
    }


def _find_mode(o: dict, mode: str, rate: str) -> dict:
    """在 xrandr 实际列表里核对请求值，返回规范化的 {mode, rate}。"""
    for m in o.get("modes") or []:
        if m["mode"] != mode:
            continue
        for r in m["rates"]:
            try:
                if abs(float(r["rate"]) - float(rate)) <= RATE_TOL:
                    return {"mode": m["mode"], "rate": r["rate"]}
            except (TypeError, ValueError):
                continue
    raise core.DMError("%s 没有暴露 %s @ %s Hz 这个模式（只接受 xrandr 实际列出的值）"
                       % (o["name"], mode, rate))


def _rec_of(o: dict) -> dict:
    return {
        "name": o["name"],
        "mode": o["mode"],
        "rate": o["rate"],
        "pos": [o["x"], o["y"]],
        "transform": list(o["transform"] or core.IDENTITY),
        "geometry": "%dx%d" % (o["w"], o["h"]),
        "primary": bool(o["primary"]),
    }


def _args(rec: dict) -> list[str]:
    """组 xrandr 参数：mode/rate/transform/pos/primary 一次性下发。

    只带目标输出。transform 必须和 mode 同一条命令下发，否则分数缩放会掉回 100%，
    几何跟着变成模式尺寸，布局就错乱了。
    """
    args = ["--output", rec["name"], "--mode", rec["mode"]]
    if rec.get("rate") and rec["rate"] != "0.00":
        args += ["--rate", rec["rate"]]
    tr = rec.get("transform") or list(core.IDENTITY)
    if any(abs(a - b) > 1e-4 for a, b in zip(tr, core.IDENTITY)):
        args += ["--transform", ",".join("%.6f" % v for v in tr)]
    pos = rec.get("pos") or [0, 0]
    args += ["--pos", "%dx%d" % (int(pos[0]), int(pos[1]))]
    if rec.get("primary"):
        args += ["--primary"]
    return args


def _apply_rec(rec: dict, reason: str) -> bool:
    """执行一条记录并复核；失败返回 False（调用方决定是否再回滚）。"""
    args = _args(rec)
    _log("%s: xrandr %s" % (reason, " ".join(args)))
    rc, out, err = core.xrandr(args)
    if rc != 0:
        _log("%s失败 rc=%d: %s" % (reason, rc, (err or out).strip()))
        return False
    snap = core.snapshot()
    return _verify(snap, rec, reason)


def _verify(snap: dict, rec: dict, reason: str) -> bool:
    got = None
    for o in snap["outputs"]:
        if o["name"] == rec["name"]:
            got = o
            break
    if got is None:
        _log("%s复核失败：找不到输出 %s" % (reason, rec["name"]))
        return False
    problems = []
    if not got["enabled"]:
        problems.append("输出没有启用（没有 CRTC）")
    if got["mode"] != rec["mode"]:
        problems.append("模式实际是 %s，期望 %s" % (got["mode"], rec["mode"]))
    try:
        if abs(float(got["rate"]) - float(rec["rate"])) > RATE_TOL:
            problems.append("刷新率实际是 %s，期望 %s" % (got["rate"], rec["rate"]))
    except (TypeError, ValueError):
        problems.append("读不到刷新率")
    tr = got["transform"]
    if tr and any(abs(a - b) > TRANSFORM_TOL for a, b in zip(tr, rec["transform"])):
        problems.append("transform 被改成 %s" % ",".join("%.4f" % v for v in tr[:5]))
    if problems:
        _log("%s复核失败：%s" % (reason, "；".join(problems)))
        return False
    _log("%s已生效: %s@%s 几何 %dx%d+%d+%d" % (
        reason, got["mode"], got["rate"], got["w"], got["h"], got["x"], got["y"]))
    return True


# --------------------------------------------------------------------------- #
# 应用 / 确认 / 回退
# --------------------------------------------------------------------------- #
def apply(name: str, mode: str, rate: str) -> dict:
    if _LOCK is None:
        raise core.DMError("内部错误：模式模块未初始化")
    with _LOCK:
        return _apply_locked(name, mode, rate)


def _apply_locked(name: str, mode: str, rate: str) -> dict:
    # 护眼休息覆盖层按旧几何铺的，改模式会错位；等它结束再改
    try:
        import dm_break
        if dm_break.status().get("phase") == "break":
            raise core.DMError("护眼休息正在进行，覆盖层结束后再改分辨率（避免覆盖层几何错位）")
    except core.DMError:
        raise
    except Exception:
        pass

    snap = core.snapshot()
    o = None
    for x in snap["outputs"]:
        if x["name"] == name:
            o = x
            break
    if o is None:
        raise core.DMError("找不到输出 %s" % name)
    if not o["connected"]:
        raise core.DMError("%s 当前未连接（%s）" % (name, o["status"]))
    if not o["enabled"]:
        raise core.DMError("%s 当前是关闭状态，先开启再改分辨率" % name)
    if not o["mode"] or not o["rate"]:
        raise core.DMError("读不出 %s 的当前模式/刷新率，为安全起见不改" % name)

    want = _find_mode(o, str(mode or ""), str(rate or ""))
    if want["mode"] == o["mode"] and abs(float(want["rate"]) - float(o["rate"])) <= RATE_TOL:
        return {"ok": True, "changed": False, "name": name, "state": snap,
                "message": "%s 已经是 %s @ %s Hz" % (name, want["mode"], want["rate"])}

    prev = _rec_of(o)
    target = dict(prev)
    target["mode"] = want["mode"]
    target["rate"] = want["rate"]
    # transform 原样保留：CRTC 尺寸随模式等比变化，缩放比例不掉
    target["geometry"] = "%dx%d" % (
        int(round(int(want["mode"].split("x")[0]) * prev["transform"][0])),
        int(round(int(want["mode"].split("x")[1]) * prev["transform"][4])))

    token = uuid.uuid4().hex
    # 先落盘再动 xrandr：此刻崩溃也能从 pending / last_good 里恢复
    _set_last_good(prev)
    _set_pending({
        "token": token, "name": name,
        "prev": prev, "applied": dict(target),
        "expire": time.time() + CONFIRM_SECONDS,
        "time": datetime.now().strftime("%F %T"),
    })
    _log("记录 last-known-good %s@%s，准备切到 %s@%s" % (
        prev["mode"], prev["rate"], target["mode"], target["rate"]))

    if not _apply_rec(target, "应用 %s" % name):
        _log("新模式没生效，立即回滚到 last-known-good")
        ok = _apply_rec(prev, "应用失败回滚")
        _set_pending(None)
        _after_change(prev)
        raise core.DMError(
            "切换 %s 到 %s@%s 失败，%s" % (
                name, target["mode"], target["rate"],
                "已回滚到 %s@%s" % (prev["mode"], prev["rate"]) if ok else
                "回滚也失败了，请用「恢复上次可用模式」或命令行处理"))

    # 其他输出一律不动；若 mutter 趁机点亮了关闭名单里的屏幕，关回去
    warnings = _guard_others(snap, name)
    _after_change(target)
    _arm_timer(CONFIRM_SECONDS)
    return {
        "ok": True, "changed": True, "name": name, "token": token,
        "timeout": CONFIRM_SECONDS, "previous": prev, "applied": target,
        "warnings": warnings, "state": core.snapshot(),
        "message": "%s 已切到 %s @ %s Hz，%d 秒内确认否则自动退回" % (
            name, target["mode"], target["rate"], CONFIRM_SECONDS),
    }


def _guard_others(before: dict, name: str) -> list[str]:
    """复核其他输出没被带歪；关闭名单里的输出被点亮就关回去。"""
    warns: list[str] = []
    marked = set(core.off_marked())
    old = {o["name"]: o for o in before["outputs"]}
    after = core.snapshot()
    for o in after["outputs"]:
        if o["name"] == name:
            continue
        b = old.get(o["name"])
        if not b:
            continue
        if not b["enabled"] and o["enabled"]:
            if o["name"] in marked:
                core.xrandr(["--output", o["name"], "--off"])
                _log("%s 被外部点亮，已按关闭名单关回" % o["name"])
                warns.append("%s 在关闭名单里，切换时被外部点亮，已关回" % o["name"])
            else:
                warns.append("%s 被外部点亮（不在关闭名单，未处理）" % o["name"])
                _log("警告: " + warns[-1])
        elif b["enabled"] and not o["enabled"]:
            warns.append("%s 被外部关闭" % o["name"])
            _log("警告: " + warns[-1])
        elif b["enabled"] and (o["x"], o["y"], o["w"], o["h"]) != (b["x"], b["y"], b["w"], b["h"]):
            warns.append("%s 几何从 %dx%d+%d+%d 变成 %dx%d+%d+%d" % (
                o["name"], b["w"], b["h"], b["x"], b["y"], o["w"], o["h"], o["x"], o["y"]))
            _log("警告: " + warns[-1])
    return warns


def _after_change(rec: dict) -> None:
    """布局变化后的收尾：亮度补发 + 看门狗基线同步 + 失效实时快照缓存。"""
    try:
        core.invalidate_live_snapshot()
    except AttributeError:
        pass
    try:
        import dm_brightness
        dm_brightness.reapply_software()
        dm_brightness.schedule_software_reapply()
        snap = core.snapshot()
        for o in snap["outputs"]:
            if o["name"] == rec["name"] and o["enabled"]:
                dm_brightness.reapply_for(o)
                break
    except Exception as e:
        _log("补发亮度失败: %r" % e)
    _sync_watchdog(rec)


def _sync_watchdog(rec: dict) -> None:
    """把新时序写进看门狗基线，别让它拿旧模式兜底。"""
    name = rec["name"]
    try:
        if any(o["name"] == name and o.get("panel") for o in core.snapshot()["outputs"]):
            return                      # 内屏不参与看门狗
    except Exception:
        pass
    try:
        lines = core._read_lines(core.WD_LAYOUT)
    except AttributeError:
        return
    keep = [l for l in lines if l.split()[0] != name] if lines else []
    pos = rec.get("pos") or [0, 0]
    keep.append("%s %s %s %dx%d %s+%d+%d" % (
        name, rec["mode"], rec["rate"], int(pos[0]), int(pos[1]),
        rec["geometry"], int(pos[0]), int(pos[1])))
    try:
        os.makedirs(core.CACHE_DIR, exist_ok=True)
        tmp = core.WD_LAYOUT + ".tmp"
        with open(tmp, "w") as f:
            f.write("".join(l + "\n" for l in keep))
        os.replace(tmp, core.WD_LAYOUT)
        _log("已同步看门狗基线: %s %s@%s" % (name, rec["mode"], rec["rate"]))
    except OSError as e:
        _log("同步看门狗基线失败: %r" % e)


def _cancel_timer() -> None:
    global _TIMER
    if _TIMER is not None:
        _TIMER.cancel()
        _TIMER = None


def _arm_timer(seconds: float) -> None:
    global _TIMER
    _cancel_timer()
    t = threading.Timer(max(0.2, seconds), _timer_fire)
    t.daemon = True
    _TIMER = t
    t.start()


def _timer_fire() -> None:
    if _LOCK is None:
        return
    with _LOCK:
        p = pending()
        if not p:
            return
        left = float(p.get("expire") or 0) - time.time()
        if left > 0:                      # 被重新排过，按新到期时间继续等
            _arm_timer(left)
            return
        _log("确认窗超时（%d 秒）未操作，自动退回 %s@%s" % (
            CONFIRM_SECONDS, p["prev"]["mode"], p["prev"]["rate"]))
        _set_pending(None)
        _apply_rec(p["prev"], "超时回滚")
        _after_change(p["prev"])


def confirm(token: str) -> dict:
    if _LOCK is None:
        raise core.DMError("内部错误：模式模块未初始化")
    with _LOCK:
        p = pending()
        if not p:
            raise core.DMError("没有待确认的切换（可能已超时自动退回）")
        if token and token != p["token"]:
            raise core.DMError("确认凭据不匹配（可能已被新的切换覆盖）")
        _cancel_timer()
        _set_pending(None)
        _log("用户确认保持 %s@%s" % (p["applied"]["mode"], p["applied"]["rate"]))
        return {"ok": True, "name": p["name"], "applied": p["applied"],
                "message": "已保持 %s @ %s Hz" % (p["applied"]["mode"], p["applied"]["rate"])}


def revert(token: str = "") -> dict:
    """撤销当前确认窗；没有 pending 时退回 last-known-good。"""
    if _LOCK is None:
        raise core.DMError("内部错误：模式模块未初始化")
    with _LOCK:
        p = pending()
        if p and token and token != p["token"]:
            raise core.DMError("凭据不匹配（可能已被新的切换覆盖）")
        target = p["prev"] if p else last_good()
        if not target:
            raise core.DMError("没有可退回的模式记录")
        _cancel_timer()
        _set_pending(None)
        _log("撤销：退回 %s@%s（%s）" % (
            target["mode"], target["rate"], "确认窗内撤销" if p else "上次可用模式"))
        ok = _apply_rec(target, "撤销回退")
        if not ok:
            raise core.DMError("退回失败，请手动执行: xrandr --output %s --mode %s --rate %s"
                               % (target["name"], target["mode"], target["rate"]))
        _after_change(target)
        return {"ok": True, "name": target["name"], "restored": target,
                "state": core.snapshot(),
                "message": "已退回 %s @ %s Hz" % (target["mode"], target["rate"])}


def start() -> None:
    """服务启动时收尾上一次没确认完的切换（进程被杀/服务重启都不会把屏留在新模式下）。"""
    p = pending()
    if not p:
        lg = last_good()
        if lg:
            _log("上次可用模式记录：%s@%s" % (lg["mode"], lg["rate"]))
        return
    left = float(p.get("expire") or 0) - time.time()
    if left <= 0:
        _log("发现过期的未确认切换，启动时回滚到 %s@%s" % (p["prev"]["mode"], p["prev"]["rate"]))
        try:
            revert(p.get("token") or "")
        except core.DMError as e:
            _log("启动回滚失败: %s" % e)
    else:
        _log("发现未确认的切换，%d 秒后自动回滚" % int(left))
        _arm_timer(left)
