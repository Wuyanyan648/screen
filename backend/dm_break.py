#!/usr/bin/env python3
"""护眼休息：工作 N 分钟 → 在指定屏上铺 N 分钟低刺激倒计时，结束后自动进入下一轮。

边界说明（界面上也会写）：
- 覆盖层的 5 分钟只做三件事：打断近距离用眼、引导远眺、提醒眨眼。它不是治疗，
  也不声称动图/渐变能改善视力。
- 覆盖层是独立进程（dm_break_overlay.py），几何直接用 xrandr 的 CRTC 矩形
  （x/y/w/h，分数缩放下也是这套坐标），本模块不另算几何。
- 与闲置提醒互斥：到点时如果目标屏正显示闲置提醒，本轮延后 60 秒并写日志，
  两个覆盖层不会同时出现。
- 状态落盘到 ~/.cache/display-manager.break.json，服务重启后接着当前轮次。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

import dm_core as core

BREAK_FILE = os.path.join(core.CACHE_DIR, "display-manager.break.json")
OVERLAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dm_break_overlay.py")

DEFAULTS = {"enabled": True, "work_min": 50, "break_min": 5, "screen": "DP-0"}
WORK_RANGE = (1, 240)
BREAK_RANGE = (1, 60)
IDLE_RETRY = 60.0        # 被闲置提醒挡住后，多久再试一次
TICK = 1.0
SAVE_EVERY = 10.0        # 倒计时期间每 10 秒落一次盘，重启最多丢 10 秒
DT_CAP = 5.0             # 单次最多扣 5 秒，防止休眠醒来后瞬间跳到休息

_LOCK = threading.RLock()
_STATE: dict | None = None
_PROC: subprocess.Popen | None = None
_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_LAST_SAVE = 0.0
_BASE: dict | None = None      # 休息开始前的布局签名，用于前后比对/兜底
_LAST_LAYOUT_CHECK = 0.0       # 休息中核对目标屏是否还在的节流时间戳
LAYOUT_CHECK_EVERY = 5.0       # 休息中每 5 秒确认一次目标屏仍有 CRTC


def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime("%F %T")


def _default_state() -> dict:
    now = time.time()
    return {
        "enabled": DEFAULTS["enabled"],
        "work_min": DEFAULTS["work_min"],
        "break_min": DEFAULTS["break_min"],
        "screen": DEFAULTS["screen"],
        "phase": "work",
        "remaining": float(DEFAULTS["work_min"] * 60),
        "round": 0,
        "breaks": 0,
        "skips": [],
        "last_tick": now,
        "defer_until": 0.0,
        "updated": _iso(now),
    }


def _clamp_int(v, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))


def _sanitize(raw: dict) -> dict:
    st = _default_state()
    if not isinstance(raw, dict):
        return st
    for k in ("enabled",):
        if isinstance(raw.get(k), bool):
            st[k] = raw[k]
    for k, (lo, hi) in (("work_min", WORK_RANGE), ("break_min", BREAK_RANGE)):
        try:
            st[k] = _clamp_int(raw[k], lo, hi)
        except (KeyError, TypeError, ValueError):
            pass
    if isinstance(raw.get("screen"), str) and raw["screen"].strip():
        st["screen"] = raw["screen"].strip()
    if raw.get("phase") in ("work", "break"):
        st["phase"] = raw["phase"]
    for k in ("remaining",):
        try:
            st[k] = max(0.0, float(raw[k]))
        except (KeyError, TypeError, ValueError):
            pass
    for k in ("round", "breaks"):
        try:
            st[k] = max(0, int(raw[k]))
        except (KeyError, TypeError, ValueError):
            pass
    skips = raw.get("skips")
    if isinstance(skips, list):
        st["skips"] = [x for x in skips if isinstance(x, dict)][-50:]
    # 离线期间不补扣时间：服务关着的时候不算「工作」，只从启动这一刻继续
    st["last_tick"] = time.time()
    st["defer_until"] = 0.0
    if st["phase"] == "work":
        st["remaining"] = min(st["remaining"], st["work_min"] * 60.0)
    else:
        st["remaining"] = min(st["remaining"], st["break_min"] * 60.0)
    return st


def _load() -> dict:
    try:
        with open(BREAK_FILE, "r") as f:
            return _sanitize(json.load(f))
    except (OSError, ValueError, TypeError):
        return _default_state()


def _save(st: dict | None = None) -> None:
    global _LAST_SAVE
    st = st or _STATE
    if st is None:
        return
    st["updated"] = _iso()
    os.makedirs(os.path.dirname(BREAK_FILE), exist_ok=True)
    tmp = BREAK_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, BREAK_FILE)
        _LAST_SAVE = time.time()
    except OSError as e:
        core.log("护眼休息状态写入失败: %r" % e)


def _save_maybe() -> None:
    if time.time() - _LAST_SAVE >= SAVE_EVERY:
        _save()


def status() -> dict:
    """给界面/API 用的只读快照，不跑 xrandr（2 秒轮询也不会有负担）。"""
    with _LOCK:
        st = _STATE or _default_state()
        rem = max(0.0, float(st["remaining"]))
        now = time.time()
        defer = max(0.0, float(st.get("defer_until") or 0) - now)
        total = (st["break_min"] if st["phase"] == "break" else st["work_min"]) * 60.0
        skips = st.get("skips") or []
        return {
            "enabled": bool(st["enabled"]),
            "work_min": int(st["work_min"]),
            "break_min": int(st["break_min"]),
            "screen": st["screen"],
            "phase": st["phase"],
            "remaining": round(rem, 1),
            "phase_total": total,
            "round": int(st["round"]),
            "breaks": int(st["breaks"]),
            "skip_count": len(skips),
            "last_skip": (skips[-1].get("at") if skips else None),
            "overlay": bool(_PROC is not None and _PROC.poll() is None),
            "defer": round(defer, 1),
            "idle_postponed": defer > 0,
            "updated": st.get("updated"),
        }


def configure(patch: dict) -> dict:
    """启停、改时长、改目标屏。返回最新状态；非法输入抛 core.DMError。"""
    global _PROC
    if not isinstance(patch, dict):
        raise core.DMError("请求体必须是 JSON 对象")
    with _LOCK:
        st = _STATE
        now = time.time()

        if "work_min" in patch:
            try:
                v = int(patch["work_min"])
            except (TypeError, ValueError):
                raise core.DMError("工作分钟数必须是整数")
            if not WORK_RANGE[0] <= v <= WORK_RANGE[1]:
                raise core.DMError("工作分钟数需在 %d~%d 之间" % WORK_RANGE)
            if v != st["work_min"]:
                st["work_min"] = v
                if st["phase"] == "work":
                    st["remaining"] = v * 60.0      # 改时长后从新一轮开始
                core.log("护眼休息：工作时长改为 %d 分钟" % v)

        if "break_min" in patch:
            try:
                v = int(patch["break_min"])
            except (TypeError, ValueError):
                raise core.DMError("休息分钟数必须是整数")
            if not BREAK_RANGE[0] <= v <= BREAK_RANGE[1]:
                raise core.DMError("休息分钟数需在 %d~%d 之间" % BREAK_RANGE)
            if v != st["break_min"]:
                st["break_min"] = v
                core.log("护眼休息：休息时长改为 %d 分钟" % v)

        if "screen" in patch:
            name = str(patch["screen"] or "").strip()
            names = [o["name"] for o in core.screen_rects()]
            if name not in names:
                raise core.DMError("目标屏 %s 不在当前已启用的输出里（%s）" % (
                    name or "（空）", ", ".join(names) or "无"))
            if name != st["screen"]:
                st["screen"] = name
                if st["phase"] == "break":
                    # 正在休息时换屏：先撤掉旧覆盖层，下一秒按剩余时间铺到新屏
                    _stop_overlay()
                core.log("护眼休息：覆盖屏改为 %s" % name)

        if "enabled" in patch:
            want = bool(patch["enabled"])
            if want != st["enabled"]:
                st["enabled"] = want
                st["last_tick"] = now
                st["defer_until"] = 0.0
                if want:
                    if st["remaining"] <= 0:
                        st["remaining"] = st["work_min"] * 60.0
                    core.log("护眼休息：已启用（%d 分钟工作 / %d 分钟休息，覆盖 %s）" % (
                        st["work_min"], st["break_min"], st["screen"]))
                else:
                    core.log("护眼休息：已暂停（计时冻结在 %.0f 秒）" % st["remaining"])
                    if st["phase"] == "break":
                        _stop_overlay()
                        st["phase"] = "work"
                        st["remaining"] = st["work_min"] * 60.0
                        core.log("护眼休息：暂停时正在休息，本轮提前结束，回到工作计时")
        _save(st)
        return status()


def skip_current(reason: str = "api") -> dict:
    """供外部（测试/排障）提前结束当前休息，等同于点覆盖层上的「跳过」。"""
    with _LOCK:
        if _STATE["phase"] != "break":
            raise core.DMError("当前不在休息中")
        _stop_overlay()
        _end_break(reason)
        return status()


# --------------------------------------------------------------------------- #
# 覆盖层
# --------------------------------------------------------------------------- #
def _stop_overlay() -> None:
    global _PROC
    p = _PROC
    if p is not None and p.poll() is None:
        p._dm_killed = True                  # 让监视线程知道是主动结束的
        try:
            p.terminate()
        except OSError:
            pass
    _PROC = None


def _signature(snap: dict) -> dict:
    """布局签名：已启用输出、用户关闭名单、画布尺寸。用于休息前后逐项比对。"""
    return {
        "enabled": sorted(o["name"] for o in snap["outputs"] if o["enabled"]),
        "off": sorted(snap.get("off_marked") or []),
        "canvas": [snap["canvas"]["w"], snap["canvas"]["h"]],
    }


def _guard_layout(label: str, base: dict | None = None) -> list[str]:
    """复核休息前后布局：把「本该关闭却被外部重新点亮」的输出关回去。

    本服务在休息路径上从不下发 xrandr 布局（只读快照）。但本机实测：
    mutter 会在特定窗口形态下重套 ~/.config/monitors.xml，把用户已关闭的屏点亮
    （见 dm_break_overlay.py 头部说明）。这里按休息开始时的快照做兜底，
    只处理「休息前就在 .off 名单里、现在仍在名单里」的输出——用户主动开启会
    把它从 .off 移除，因此不会被这里抢回去。
    """
    base = base if base is not None else _BASE
    if not base:
        return []
    try:
        snap = core.snapshot()
    except Exception as e:
        core.log("护眼休息：%s 复核布局失败 %r" % (label, e))
        return []
    now_off = set(snap.get("off_marked") or [])
    before_on = set(base["enabled"])
    fixed = []
    for o in snap["outputs"]:
        n = o["name"]
        if not o["enabled"] or n in before_on:
            continue
        if n not in base["off"] or n not in now_off:
            core.log("护眼休息：%s 发现 %s 被外部启用（不在休息前关闭名单里），"
                     "按用户设置不动它" % (label, n))
            continue
        rc, out, err = core.xrandr(["--output", n, "--off"])
        if rc == 0:
            fixed.append(n)
            core.log("护眼休息：%s 发现 %s 被外部重新点亮，已按休息前状态关回"
                     "（用户关闭名单未改动）" % (label, n))
        else:
            core.log("护眼休息：%s 把 %s 关回失败 rc=%d：%s" % (
                label, n, rc, (err or out).strip()))
    after = _signature(core.snapshot())
    if after["enabled"] != base["enabled"] or after["canvas"] != base["canvas"]:
        core.log("护眼休息：%s 复核后布局仍与休息前不一致；休息前 %s，现在 %s" % (
            label, base, after))
    if after["off"] != base["off"]:
        core.log("护眼休息：%s 关闭名单发生变化：休息前 %s，现在 %s" % (
            label, base["off"], after["off"]))
    return fixed


def _schedule_guard(label: str, delays=(1.5, 4.0), base: dict | None = None) -> None:
    """mutter 重套配置是异步的，休息开始/结束后各查两拍。"""
    base = base if base is not None else _BASE
    if not base:
        return

    def worker():
        last = 0.0
        for d in delays:
            time.sleep(max(0.0, d - last))
            last = d
            try:
                _guard_layout(label, base)
            except Exception as e:      # 兜底线程不能把异常抛出去
                core.log("护眼休息：%s 布局复核异常 %r" % (label, e))
    threading.Thread(target=worker, daemon=True, name="dm-break-guard").start()


def _target_alive(st: dict) -> bool:
    """休息中目标屏被关掉时放弃覆盖（绝不为了覆盖层去点亮任何输出）。"""
    global _LAST_LAYOUT_CHECK
    now = time.time()
    if now - _LAST_LAYOUT_CHECK < LAYOUT_CHECK_EVERY:
        return True
    _LAST_LAYOUT_CHECK = now
    names = [o["name"] for o in core.screen_rects()]
    if st["screen"] in names:
        return True
    core.log("护眼休息：目标屏 %s 在休息期间被关闭，撤销覆盖层并放弃本轮"
             "（不会重新点亮它）" % st["screen"])
    _stop_overlay()
    _end_break("screen-off")
    return False


def _visual(snap: dict | None, name: str) -> tuple[float, float]:
    """覆盖层的文字亮度与暖色程度：跟随当前亮度/色温，只影响这一层画面。"""
    b = ((snap or {}).get("brightness") or {}).get(name) or {}
    val = b.get("value")
    dim = 1.0
    if isinstance(val, (int, float)) and val > 0:
        dim = max(0.6, min(1.0, val / 100.0))    # 不低于 60%，保住可读性
    temp = b.get("temp")
    warm = 0.0
    if isinstance(temp, (int, float)) and temp:
        warm = max(0.0, min(1.0, (6500 - temp) / 3500.0))
    return dim, warm


def _launch(st: dict, o: dict, secs: int, snap: dict | None = None) -> None:
    global _PROC
    dim, warm = _visual(snap, o["name"])
    cmd = [sys.executable, OVERLAY,
           "--x", str(o["x"]), "--y", str(o["y"]),
           "--w", str(o["w"]), "--h", str(o["h"]),
           "--seconds", str(max(1, int(round(secs)))),
           "--screen", o["name"],
           "--dim", "%.2f" % dim, "--warm", "%.2f" % warm]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, close_fds=True)
    except OSError as e:
        core.log("护眼休息：覆盖层启动失败 %r，本轮跳过（回到工作计时）" % e)
        st["phase"] = "work"
        st["remaining"] = st["work_min"] * 60.0
        _save(st)
        return
    proc._dm_killed = False
    _PROC = proc
    st["phase"] = "break"
    st["round"] += 1
    st["remaining"] = max(1, int(round(secs)))
    _save(st)
    core.log("护眼休息开始：%s 覆盖 %dx%d+%d+%d，倒计时 %d 秒（第 %d 轮）" % (
        o["name"], o["w"], o["h"], o["x"], o["y"], st["remaining"], st["round"]))
    threading.Thread(target=_watch, args=(proc,), daemon=True,
                     name="dm-break-overlay").start()


def _watch(proc: subprocess.Popen) -> None:
    """等覆盖层退出：0=倒计时结束，3=ESC/按钮跳过，其它=异常。"""
    global _PROC
    try:
        out, err = proc.communicate()
    except Exception:
        out, err = "", ""
    rc = proc.returncode
    killed = getattr(proc, "_dm_killed", False)
    if killed:
        return                                  # configure()/stop() 已经处理过状态
    if rc == 3:
        reason = "esc"
    elif rc == 0:
        reason = "done"
    else:
        reason = "error"
        core.log("护眼休息：覆盖层异常退出 rc=%s %s" % (
            rc, (err or out or "").strip()[:300]))
    with _LOCK:
        if _PROC is proc:
            _PROC = None
        _end_break(reason)


def _end_break(reason: str) -> None:
    """结束当前休息并回到工作计时。reason: done/esc/button/paused/screen-off/error。"""
    global _PROC, _BASE
    st = _STATE
    if st is None or st["phase"] != "break":
        return
    if reason == "done":
        st["breaks"] += 1
        core.log("护眼休息结束：倒计时完成，进入下一轮 %d 分钟工作" % st["work_min"])
    elif reason in ("esc", "button"):
        st["skips"] = (st.get("skips") or [])[-49:] + [{"at": _iso(), "reason": reason}]
        core.log("护眼休息被跳过（%s），跳过时间 %s；进入下一轮 %d 分钟工作" % (
            "ESC" if reason == "esc" else "按钮", _iso(), st["work_min"]))
    elif reason == "screen-off":
        core.log("护眼休息：目标屏已关闭，本轮不弹覆盖层，回到工作计时")
    else:
        core.log("护眼休息：本轮异常结束（%s），回到工作计时" % reason)
    st["phase"] = "work"
    st["remaining"] = st["work_min"] * 60.0
    st["defer_until"] = 0.0
    st["last_tick"] = time.time()
    _save(st)
    # 结束后再核一遍：mutter 的重套配置是异步的，覆盖层消失后也可能晚一步生效
    _schedule_guard("休息结束", base=_BASE)
    _BASE = None


# --------------------------------------------------------------------------- #
# 调度
# --------------------------------------------------------------------------- #
def _begin_break(st: dict, now: float) -> None:
    """到点：先看目标屏是否可用、闲置提醒是否在显示，再决定弹出还是延后。"""
    global _BASE, _LAST_LAYOUT_CHECK
    try:
        snap = core.snapshot()
    except Exception as e:
        core.log("护眼休息：读取布局失败 %r，延后 %d 秒再试" % (e, IDLE_RETRY))
        st["defer_until"] = now + IDLE_RETRY
        return
    o = next((x for x in snap["outputs"] if x["name"] == st["screen"]), None)
    if o is None or not o["connected"]:
        core.log("护眼休息：目标屏 %s 未连接，本轮跳过，回到工作计时" % st["screen"])
        _end_work_skip(st, "screen-off")
        return
    if not o["enabled"] or not o.get("crtc"):
        core.log("护眼休息：目标屏 %s 已关闭（没有 CRTC），本轮跳过，"
                 "不会为了覆盖而点亮它" % st["screen"])
        _end_work_skip(st, "screen-off")
        return
    if o.get("idle"):
        st["defer_until"] = now + IDLE_RETRY
        _save(st)
        core.log("护眼休息：闲置提醒正在显示（%s 已 %.0f 秒无变化），本轮休息延后 %d 秒，"
                 "避免两个覆盖层叠在一起" % (st["screen"], o.get("idle_seconds") or 0, IDLE_RETRY))
        return
    _BASE = _signature(snap)
    _LAST_LAYOUT_CHECK = 0.0
    rem = st["break_min"] * 60.0
    _launch(st, o, rem, snap)
    core.log("护眼休息：休息前布局 %s（休息期间只覆盖已启用输出，不改任何开关）" % (_BASE,))
    _schedule_guard("覆盖层出现后")


def _end_work_skip(st: dict, reason: str) -> None:
    st["skips"] = (st.get("skips") or [])[-49:] + [{"at": _iso(), "reason": reason}]
    st["remaining"] = st["work_min"] * 60.0
    st["defer_until"] = 0.0
    _save(st)


def _resume_break(st: dict, now: float) -> None:
    """服务重启时正处在休息中：按剩余时间重新铺一层，而不是重头开始。"""
    global _BASE, _LAST_LAYOUT_CHECK
    try:
        snap = core.snapshot()
    except Exception as e:
        core.log("护眼休息：恢复休息失败 %r，回到工作计时" % e)
        _end_break("error")
        return
    o = next((x for x in snap["outputs"] if x["name"] == st["screen"]), None)
    if o is None or not o["enabled"] or not o.get("crtc"):
        core.log("护眼休息：恢复时目标屏 %s 不可用，本轮跳过" % st["screen"])
        _end_break("screen-off")
        return
    _BASE = _signature(snap)
    _LAST_LAYOUT_CHECK = 0.0
    _launch(st, o, st["remaining"], snap)
    _schedule_guard("恢复覆盖层后")


def _tick() -> None:
    global _PROC
    now = time.time()
    with _LOCK:
        st = _STATE
        if st is None:
            return
        dt = min(DT_CAP, max(0.0, now - float(st.get("last_tick") or now)))
        st["last_tick"] = now
        if not st["enabled"]:
            _save_maybe()
            return
        proc = _PROC
        running = proc is not None and proc.poll() is None
        if running:
            # 覆盖层在跑：服务端同步扣时，只作为「覆盖层卡死」的兜底
            if st["phase"] == "break":
                if not _target_alive(st):
                    return
                st["remaining"] = max(0.0, st["remaining"] - dt)
                if st["remaining"] <= 0:
                    _stop_overlay()
                    _end_break("done")
                else:
                    _save_maybe()
            return
        if st["phase"] == "break":
            st["remaining"] = max(0.0, st["remaining"] - dt)
            if st["remaining"] <= 0:
                _end_break("done")
            else:
                _resume_break(st, now)
            return
        st["remaining"] = max(0.0, st["remaining"] - dt)
        if st["remaining"] <= 0 and now >= float(st.get("defer_until") or 0):
            _begin_break(st, now)
        _save_maybe()


def _loop() -> None:
    while not _STOP.is_set():
        try:
            _tick()
        except Exception as e:                  # 线程不能因为单次异常就停掉
            core.log("护眼休息调度异常: %r" % e)
        _STOP.wait(TICK)


def start() -> None:
    global _STATE, _THREAD, _STOP
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STATE = _load()
        _STOP = threading.Event()
        _THREAD = threading.Thread(target=_loop, daemon=True, name="dm-break")
        _THREAD.start()
        st = _STATE
        core.log("护眼休息调度启动：%s，%d 分钟工作 / %d 分钟休息，覆盖 %s，"
                 "当前 %s 剩余 %.0f 秒（第 %d 轮）" % (
                     "启用" if st["enabled"] else "已暂停",
                     st["work_min"], st["break_min"], st["screen"],
                     "休息" if st["phase"] == "break" else "工作",
                     st["remaining"], st["round"]))


def stop() -> None:
    global _STATE
    _STOP.set()
    with _LOCK:
        if _PROC is not None and _PROC.poll() is None:
            _PROC._dm_killed = True
            try:
                _PROC.terminate()
            except OSError:
                pass
            core.log("护眼休息：服务退出，覆盖层已关闭（%s 剩余 %.0f 秒，重启后接着走）" % (
                "休息" if _STATE and _STATE["phase"] == "break" else "工作",
                (_STATE or {}).get("remaining", 0)))
            _save()
        _STATE = None
