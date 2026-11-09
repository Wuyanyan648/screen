#!/usr/bin/env python3
"""display-manager 核心逻辑：读取 xrandr 状态、开关输出、写日志。

Web 服务（dm_server.py）和命令行（dm_cli.py）共用这一份实现，保证两边行为一致。

本机要点（实测）：
- 分数缩放靠 CRTC --transform 实现，下令时 --mode 必须与 --transform 成对下发，
  否则缩放会掉回 100%；
- 恢复一块屏要同时还原「模式 + 刷新率 + transform + 位置」，位置错会压屏；
- 状态判断只用 xrandr，不看 sysfs（NVIDIA 下 enabled/dpms 与真实状态不符）；
  轮询用 --current（读服务端缓存），避免每次触发驱动全量重探刷 Xorg 日志。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime

HOME = os.path.expanduser("~")
CACHE_DIR = os.environ.get("DM_CACHE_DIR", os.path.join(HOME, ".cache"))

STATE_FILE = os.path.join(CACHE_DIR, "display-manager.state")   # 关闭前保存的「四件套」
OFF_MARK = os.path.join(CACHE_DIR, "display-manager.off")       # 用户主动关闭的输出名单
LOG_FILE = os.path.join(CACHE_DIR, "display-manager.log")
WD_LAYOUT = os.path.join(CACHE_DIR, "hdmi-watchdog.layout")     # 看门狗基线（只读兜底）
WD_LOG = os.path.join(CACHE_DIR, "hdmi-watchdog.log")

CONFIG_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")),
    "display-manager", "config.json")

LOG_MAX = 1_000_000
IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
DEFAULT_IDLE_THRESHOLD = 300      # 秒；0 = 关闭闲置提醒

# 由 dm_server 注入的闲置监测器（CLI 里为空，界面里才有逐屏计时）
IDLE_PROVIDER = None


def set_idle_provider(fn) -> None:
    global IDLE_PROVIDER
    IDLE_PROVIDER = fn


def load_config() -> dict:
    cfg = {"idle_threshold": DEFAULT_IDLE_THRESHOLD}
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, dict) and "idle_threshold" in data:
            cfg["idle_threshold"] = int(data["idle_threshold"])
    except (OSError, ValueError, TypeError):
        pass
    return cfg


def save_config(cfg: dict) -> dict:
    cur = load_config()
    if "idle_threshold" in cfg:
        try:
            v = int(cfg["idle_threshold"])
        except (TypeError, ValueError):
            raise DMError("闲置阈值必须是整数秒")
        if v != 0 and not (30 <= v <= 86400):
            raise DMError("闲置阈值需在 30~86400 秒之间（0 表示关闭提醒）")
        cur["idle_threshold"] = v
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)
    log("配置已更新: %s" % json.dumps(cur, ensure_ascii=False))
    return cur

HEADER_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9._-]+)\s+"
    r"(?P<status>connected|disconnected|unknown connection)\b(?P<rest>.*)$"
)
GEOM_RE = re.compile(r"(\d+)x(\d+)\+(\d+)\+(\d+)")
SCREEN_RE = re.compile(
    r"Screen \d+: minimum (\d+) x (\d+), current (\d+) x (\d+), maximum (\d+) x (\d+)"
)
MODE_RE = re.compile(r"^\d+x\d+$")


class DMError(Exception):
    """带可直接展示给用户的中文说明的错误。"""


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        if os.path.getsize(LOG_FILE) > LOG_MAX:
            with open(LOG_FILE, "r", errors="replace") as f:
                tail = f.readlines()[-200:]
            with open(LOG_FILE, "w", errors="replace") as f:
                f.writelines(tail)
    except OSError:
        pass
    line = "%s %s" % (datetime.now().strftime("%F %T"), msg)
    with open(LOG_FILE, "a", errors="replace") as f:
        f.write(line + "\n")


def read_log_tail(n: int = 60) -> list[str]:
    try:
        with open(LOG_FILE, "r", errors="replace") as f:
            return [l.rstrip("\n") for l in f.readlines()[-n:]]
    except OSError:
        return []


# --------------------------------------------------------------------------- #
# xrandr 调用与解析
# --------------------------------------------------------------------------- #
def xrandr(args: list[str], verbose: bool = False) -> tuple[int, str, str]:
    cmd = ["xrandr", "--current"] + (["--verbose"] if verbose else []) + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", "无法执行 xrandr: %s" % e
    return p.returncode, p.stdout, p.stderr


def _parse_query(text: str) -> dict:
    """解析 `xrandr --current`：屏幕尺寸、各输出状态、几何、当前模式与刷新率。"""
    screen = {"w": 0, "h": 0, "min": [0, 0], "max": [0, 0]}
    m = SCREEN_RE.search(text)
    if m:
        screen = {
            "min": [int(m.group(1)), int(m.group(2))],
            "w": int(m.group(3)),
            "h": int(m.group(4)),
            "max": [int(m.group(5)), int(m.group(6))],
        }

    outputs: list[dict] = []
    cur = None
    for line in text.splitlines():
        if line and not line[0].isspace():
            mm = HEADER_RE.match(line)
            if mm:
                rest = mm.group("rest")
                cur = {
                    "name": mm.group("name"),
                    "status": mm.group("status"),
                    "connected": mm.group("status") == "connected",
                    "primary": " primary" in " " + rest,
                    "x": None, "y": None, "w": None, "h": None,
                    "mode": None, "rate": None,
                    "physical_mm": None,
                }
                gm = GEOM_RE.search(line)
                if gm:
                    cur["w"], cur["h"] = int(gm.group(1)), int(gm.group(2))
                    cur["x"], cur["y"] = int(gm.group(3)), int(gm.group(4))
                pm = re.search(r"(\d+)mm x (\d+)mm", line)
                if pm:
                    cur["physical_mm"] = [int(pm.group(1)), int(pm.group(2))]
                outputs.append(cur)
            else:
                cur = None
            continue

        if cur is None or not line[:1].isspace():
            continue
        toks = line.split()
        if not toks or not MODE_RE.match(toks[0]):
            continue
        hit = [t for t in toks[1:] if "*" in t]
        if hit:
            cur["mode"] = toks[0]
            raw = hit[0].replace("*", "").replace("+", "")
            if re.fullmatch(r"\d+(\.\d+)?", raw):
                cur["rate"] = "%.2f" % float(raw)

    for o in outputs:
        o["enabled"] = o["w"] is not None
    return {"screen": screen, "outputs": outputs}


def _parse_details(text: str) -> dict:
    """解析 `xrandr --current --verbose`：Transform、ConnectorType、Brightness、EDID。"""
    details: dict[str, dict] = {}
    name = None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line and not line[0].isspace():
            mm = HEADER_RE.match(line)
            name = mm.group("name") if mm else None
            if name:
                details.setdefault(name, {"transform": None, "connector_type": None,
                                          "brightness": None, "edid": None})
            continue
        if not name:
            continue
        s = line.strip()
        if s.startswith("ConnectorType:"):
            details[name]["connector_type"] = s.split(":", 1)[1].strip()
        elif s.startswith("Brightness:"):
            try:
                details[name]["brightness"] = float(s.split(":", 1)[1])
            except ValueError:
                pass
        elif s.startswith("EDID:"):
            buf = []
            for chunk in lines[i + 1:]:
                t = chunk.strip()
                if re.fullmatch(r"[0-9a-fA-F]{32}", t):
                    buf.append(t)
                else:
                    break
            if buf:
                details[name]["edid"] = bytes.fromhex("".join(buf))
        elif s.startswith("Transform:"):
            nums: list[float] = []
            for chunk in text.splitlines()[i:i + 4]:
                body = chunk.strip()
                if body.startswith("Transform:"):
                    body = body.split(":", 1)[1]
                elif not body.startswith("filter:") and not re.fullmatch(r"[-0-9.\s]+", body):
                    break
                if body.startswith("filter:"):
                    break
                nums += [float(v) for v in re.findall(r"-?\d+\.\d+", body)]
                if len(nums) >= 9:
                    break
            if len(nums) >= 9:
                details[name]["transform"] = nums[:9]
    return details


# --------------------------------------------------------------------------- #
# 状态文件
# --------------------------------------------------------------------------- #
def _read_lines(path: str) -> list[str]:
    try:
        with open(path, "r", errors="replace") as f:
            return [l.rstrip("\n") for l in f if l.strip()]
    except OSError:
        return []


def _write_lines(path: str, lines: list[str]) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("".join(l + "\n" for l in lines))
    os.replace(tmp, path)


def state_get(name: str) -> dict | None:
    """状态文件字段: 名字 模式 刷新率 位置X 位置Y transform(9项逗号) geometry primary"""
    for line in _read_lines(STATE_FILE):
        p = line.split()
        if p[0] != name or len(p) < 8:
            continue
        try:
            return {
                "name": name,
                "mode": p[1],
                "rate": p[2],
                "pos": (int(p[3]), int(p[4])),
                "transform": [float(v) for v in p[5].split(",")],
                "geometry": p[6],
                "primary": p[7] == "1",
            }
        except (ValueError, IndexError):
            return None
    return None


def state_put(rec: dict) -> None:
    line = "%s %s %s %d %d %s %s %d" % (
        rec["name"], rec["mode"], rec["rate"], rec["pos"][0], rec["pos"][1],
        ",".join("%.6f" % v for v in rec["transform"]),
        rec["geometry"], 1 if rec["primary"] else 0,
    )
    lines = [l for l in _read_lines(STATE_FILE) if l.split()[0] != rec["name"]] + [line]
    _write_lines(STATE_FILE, lines)


def state_drop(name: str) -> None:
    lines = [l for l in _read_lines(STATE_FILE) if l.split()[0] != name]
    _write_lines(STATE_FILE, lines)


def off_marked() -> list[str]:
    return [l.split()[0] for l in _read_lines(OFF_MARK)]


def off_mark(name: str) -> None:
    if name not in off_marked():
        _write_lines(OFF_MARK, off_marked() + [name])


def off_unmark(name: str) -> None:
    _write_lines(OFF_MARK, [n for n in off_marked() if n != name])


def wd_baseline(name: str) -> dict | None:
    """看门狗基线：名字 模式 刷新率 位置(WxH) 几何(WxH+X+Y)。只读，不改动它。"""
    for line in _read_lines(WD_LAYOUT):
        p = line.split()
        if p[0] != name or len(p) < 5:
            continue
        gm = GEOM_RE.search(p[4])
        pos = re.fullmatch(r"(\d+)x(\d+)", p[3])
        if not gm or not pos:
            return None
        gw, gh = int(gm.group(1)), int(gm.group(2))
        mw = int(p[1].split("x")[0]) or 1
        mh = int(p[1].split("x")[1]) or 1
        return {
            "name": name,
            "mode": p[1],
            "rate": p[2],
            "pos": (int(pos.group(1)), int(pos.group(2))),
            "transform": [gw / mw, 0.0, 0.0, 0.0, gh / mh, 0.0, 0.0, 0.0, 1.0],
            "geometry": "%dx%d" % (gw, gh),
            "primary": False,
        }
    return None


# --------------------------------------------------------------------------- #
# 快照（界面与 CLI 共用）
# --------------------------------------------------------------------------- #
def snapshot() -> dict:
    rc, out, err = xrandr([])
    if rc != 0:
        raise DMError("xrandr 执行失败（DISPLAY=%s）: %s" % (os.environ.get("DISPLAY", ""), (err or out).strip()))

    q = _parse_query(out)
    srcs = {"query": "xrandr --current"}
    rc2, out2, _ = xrandr([], verbose=True)
    details = _parse_details(out2) if rc2 == 0 else {}
    if rc2 == 0:
        srcs["details"] = "xrandr --current --verbose"

    marked = off_marked()
    outputs = []
    for o in q["outputs"]:
        d = details.get(o["name"], {})
        tr = d.get("transform")
        scale = None
        if o["enabled"] and o["mode"]:
            mw = int(o["mode"].split("x")[0]) or 1
            scale = round(o["w"] * 100.0 / mw, 1)
            if tr is None:
                mh = int(o["mode"].split("x")[1]) or 1
                tr = [o["w"] / mw, 0.0, 0.0, 0.0, o["h"] / mh, 0.0, 0.0, 0.0, 1.0]
        ct = d.get("connector_type") or ""
        outputs.append({
            **o,
            "id": o["name"],
            "panel": ct.lower() == "panel",
            "connector_type": ct or "未知",
            "transform": tr,
            "scale": scale,
            "off_marked": o["name"] in marked,
            "crtc": [o["x"], o["y"], o["w"], o["h"]] if o["enabled"] else None,
        })

    enabled = [o for o in outputs if o["enabled"]]
    canvas = _bbox(enabled)
    idle_info = _attach_idle(outputs, time.time())
    try:
        import dm_brightness
        bright = dm_brightness.summary(outputs, details)
    except Exception as e:  # 亮度探测失败不影响布局功能
        bright = {}
        log("亮度状态探测失败: %r" % e)
    return {
        "ok": True,
        "server_screen": q["screen"],
        "outputs": outputs,
        "brightness": bright,
        "enabled_count": len(enabled),
        "canvas": canvas,
        "off_marked": marked,
        "idle": idle_info,
        "config": load_config(),
        "state_records": [r.split()[0] for r in _read_lines(STATE_FILE)],
        "watchdog": {
            "layout_file": WD_LAYOUT,
            "baseline": [r.split()[0] for r in _read_lines(WD_LAYOUT)],
            "force_recover": os.environ.get("FORCE_RECOVER"),
        },
        "log_file": LOG_FILE,
        "time": datetime.now().strftime("%F %T"),
        "sources": srcs,
        "overlaps": _overlaps(outputs),
    }


def _bbox(outs: list[dict]) -> dict:
    if not outs:
        return {"x": 0, "y": 0, "w": 0, "h": 0}
    x0 = min(o["x"] for o in outs)
    y0 = min(o["y"] for o in outs)
    x1 = max(o["x"] + o["w"] for o in outs)
    y1 = max(o["y"] + o["h"] for o in outs)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _overlaps(outs: list[dict]) -> list[dict]:
    en = [o for o in outs if o["enabled"]]
    res = []
    for i, a in enumerate(en):
        for b in en[i + 1:]:
            ix = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
            iy = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
            if ix > 0 and iy > 0:
                res.append({"a": a["name"], "b": b["name"], "w": ix, "h": iy})
    return res


def _find(snap: dict, name: str) -> dict:
    for o in snap["outputs"]:
        if o["name"] == name:
            return o
    raise DMError("找不到输出 %s（可用输出: %s）" % (
        name, ", ".join(o["name"] for o in snap["outputs"])))


def screen_rects() -> list[dict]:
    """只取各已启用输出的几何，供闲置监视线程高频调用（一次 xrandr --current）。"""
    rc, out, _ = xrandr([])
    if rc != 0:
        return []
    return [o for o in _parse_query(out)["outputs"] if o["enabled"]]


def _attach_idle(outputs: list[dict], now: float) -> dict:
    """把闲置计时挂到每块屏上。依据与局限见 dm_idle.py 顶部说明。"""
    cfg = load_config()
    threshold = cfg["idle_threshold"]
    info = {"available": False, "basis": "未启用（命令行模式不采集画面变化）",
            "threshold": threshold, "damage_ok": False, "coarse": False,
            "global_idle": None, "error": None}
    if IDLE_PROVIDER is None:
        for o in outputs:
            o.update(idle_seconds=None, idle=False, idle_text=None,
                     idle_basis=info["basis"])
        return info

    try:
        info.update(IDLE_PROVIDER())
    except Exception as e:
        info["error"] = str(e)
    info["threshold"] = threshold

    last = info.get("last_change") or {}
    gidle = info.get("global_idle")
    for o in outputs:
        secs = None
        if o["enabled"]:
            if info.get("damage_ok") and o["name"] in last:
                secs = max(0.0, now - last[o["name"]])
            elif info.get("coarse") and gidle is not None:
                secs = float(gidle)
            elif o["name"] in last:
                secs = max(0.0, now - last[o["name"]])
        o["idle_seconds"] = None if secs is None else round(secs, 1)
        o["idle"] = bool(secs is not None and threshold > 0 and secs >= threshold)
        o["idle_basis"] = info["basis"]
    return info


# --------------------------------------------------------------------------- #
# 操作
# --------------------------------------------------------------------------- #
def plan_off(name: str) -> dict:
    """关闭前预检：返回关闭后的剩余屏数与画布尺寸，不合法直接抛 DMError。"""
    snap = snapshot()
    o = _find(snap, name)
    if not o["connected"]:
        raise DMError("%s 当前不是 connected（%s），无法关闭" % (name, o["status"]))
    if not o["enabled"]:
        raise DMError("%s 已经是关闭状态" % name)
    if snap["enabled_count"] <= 1:
        raise DMError("拒绝关闭：%s 是当前唯一启用的输出，关掉后将没有任何显示输出" % name)
    if not o["mode"] or not o["rate"]:
        raise DMError("无法从 xrandr 读出 %s 的当前模式/刷新率，为安全起见不关闭" % name)

    rest = [x for x in snap["outputs"] if x["enabled"] and x["name"] != name]
    return {
        "name": name,
        "panel": o["panel"],
        "remaining": [x["name"] for x in rest],
        "remaining_count": len(rest),
        "canvas_after": _bbox(rest),
        "canvas_now": snap["canvas"],
        "restore_from": "state" if state_get(name) else (
            "watchdog" if wd_baseline(name) else "none"),
    }


def turn_off(name: str, reason: str = "cli") -> dict:
    plan = plan_off(name)
    snap = snapshot()
    o = _find(snap, name)

    tr = o["transform"] or list(IDENTITY)
    rec = {
        "name": name,
        "mode": o["mode"],
        "rate": o["rate"],
        "pos": (o["x"], o["y"]),
        "transform": tr,
        "geometry": "%dx%d" % (o["w"], o["h"]),
        "primary": o["primary"],
    }
    state_put(rec)
    log("记录 %s 的四件套: %s@%s pos=%dx%d transform=%s 几何=%s primary=%s (%s)" % (
        name, rec["mode"], rec["rate"], rec["pos"][0], rec["pos"][1],
        ",".join("%.6f" % v for v in rec["transform"]), rec["geometry"],
        "是" if rec["primary"] else "否", reason))

    rc, out, err = xrandr(["--output", name, "--off"])
    if rc != 0:
        state_drop(name)
        detail = (err or out).strip()
        log("关闭 %s 失败 rc=%d ；xrandr stderr: %s" % (name, rc, detail))
        raise DMError("xrandr 关闭 %s 失败（rc=%d）：%s" % (name, rc, detail or "无输出"))

    off_mark(name)
    after = snapshot()
    log("已关闭 %s；剩余启用: %s；画布 %sx%d" % (
        name, ", ".join(x["name"] for x in after["outputs"] if x["enabled"]) or "无",
        after["canvas"]["w"], after["canvas"]["h"]))
    # RandR 重新配置会把所有输出的 gamma 复位，把还在用软件调光的屏补回来
    try:
        import dm_brightness
        dm_brightness.reapply_software()
        dm_brightness.schedule_software_reapply()   # mutter 可能稍后再复位一次
    except Exception as e:
        log("关闭 %s 后恢复其它屏软件调光失败: %r" % (name, e))
    return {"ok": True, "action": "off", "name": name, "plan": plan, "state": after,
            "message": "已关闭 %s，剩余 %d 块屏" % (name, plan["remaining_count"])}


def _resolve_restore(name: str) -> tuple[dict, str]:
    rec = state_get(name)
    if rec:
        return rec, "状态文件（关闭前的四件套）"
    rec = wd_baseline(name)
    if rec:
        return rec, "看门狗基线 %s" % WD_LAYOUT
    raise DMError("%s 没有可用的恢复参数（既无关闭记录，也无看门狗基线）。"
                  "可手动执行: xrandr --output %s --auto" % (name, name))


def turn_on(name: str, reason: str = "cli") -> dict:
    snap = snapshot()
    o = _find(snap, name)
    if not o["connected"]:
        raise DMError("%s 当前未连接（%s），等接上线再开" % (name, o["status"]))
    if o["enabled"]:
        state_drop(name)
        off_unmark(name)
        return {"ok": True, "action": "on", "name": name, "state": snap,
                "message": "%s 已经处于启用状态，无需操作" % name}

    rec, src = _resolve_restore(name)
    args = ["--output", name, "--mode", rec["mode"]]
    if rec["rate"] and rec["rate"] != "0.00":
        args += ["--rate", rec["rate"]]
    tr = rec["transform"]
    if any(abs(a - b) > 1e-4 for a, b in zip(tr, IDENTITY)):
        args += ["--transform", ",".join("%.6f" % v for v in tr)]
    args += ["--pos", "%dx%d" % rec["pos"]]
    if rec["primary"]:
        args += ["--primary"]

    log("恢复 %s（依据 %s）: %s" % (name, src, " ".join(args)))
    rc, out, err = xrandr(args)
    if rc != 0:
        detail = (err or out).strip()
        log("恢复 %s 失败 rc=%d ；xrandr stderr: %s" % (name, rc, detail))
        raise DMError("xrandr 恢复 %s 失败（rc=%d）：%s" % (name, rc, detail or "无输出"))

    state_drop(name)
    off_unmark(name)

    after = snapshot()
    got = _find(after, name)
    exp = "%dx%d+%d+%d" % (int(rec["geometry"].split("x")[0]), int(rec["geometry"].split("x")[1]),
                           rec["pos"][0], rec["pos"][1])
    actual = "%dx%d+%d+%d" % (got["w"], got["h"], got["x"], got["y"]) if got["enabled"] else "(未启用)"
    warn = None
    if actual != exp:
        warn = "恢复后几何与记录不一致：期望 %s，实际 %s" % (exp, actual)
        log("警告: " + warn)
    ov = after["overlaps"]
    if ov:
        warn = (warn + "；" if warn else "") + "存在重叠: " + ", ".join(
            "%s/%s %dx%d" % (x["a"], x["b"], x["w"], x["h"]) for x in ov)
        log("警告: 布局存在重叠 " + json.dumps(ov, ensure_ascii=False))

    log("已启用 %s: %s@%s 几何 %s" % (name, got["mode"], got["rate"], actual))
    # 重新打开后 gamma 会被模式设置复位，把上次的亮度再下发一次
    try:
        import dm_brightness
        if dm_brightness.reapply_for(got):
            log("已重新应用 %s 的亮度" % name)
        dm_brightness.reapply_software()   # 开启动作同样会让其它屏的 gamma 复位
        dm_brightness.schedule_software_reapply()
    except Exception as e:
        log("重新应用 %s 亮度失败: %r" % (name, e))
    return {"ok": True, "action": "on", "name": name, "state": after,
            "restored_from": src, "geometry": actual, "warning": warn,
            "message": "已启用 %s（%s）" % (name, src)}


def set_brightness(name: str, value, extra=None) -> dict:
    """设置亮度（20~100）。通道选择见 dm_brightness.py：硬件背光/DDC 优先，其次软件调光。

    下限 20% 是无条件的安全线：SDR 下再低基本等于黑屏（关掉 HDR 的 AOC 实测 0% 直接
    灰屏）。OS 侧读不到显示器 OSD 的 HDR 状态，所以不做「只有非 HDR 才限制」的判断。
    extra 是硬件到下限后的软件叠加层 0~100；None 表示不改动已有叠加。
    """
    import dm_brightness
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise DMError("亮度必须是 %d~100 的整数" % dm_brightness.MIN_VALUE)
    if not 0 <= v <= 100:
        raise DMError("亮度必须在 0~100 之间")
    if v < dm_brightness.MIN_VALUE:
        raise DMError("亮度不能低于 %d%%：SDR 下再低等于黑屏（当前请求 %d%%）"
                      % (dm_brightness.MIN_VALUE, v))
    if extra is not None:
        try:
            extra = int(extra)
        except (TypeError, ValueError):
            raise DMError("软件叠加必须是 0~100 的整数")
        if not 0 <= extra <= 100:
            raise DMError("软件叠加必须在 0~100 之间")

    snap = snapshot()
    o = _find(snap, name)
    if not o["connected"]:
        raise DMError("%s 当前不是 connected（%s）" % (name, o["status"]))

    try:
        entry = dm_brightness.set_value(o, v, extra=extra)
    except dm_brightness.BrightError as e:
        raise DMError(str(e))
    return {"ok": True, "name": name, "brightness": entry,
            "message": "%s 亮度已设为 %s%%（%s）" % (
                name, entry["value"], entry.get("label") or entry["mode_label"])}


def set_brightness_extra(name: str, extra) -> dict:
    """只调软件叠加层（硬件必须已经在最低档 20%）。

    叠加值会被合成下限（MIN_TOTAL）截断，见 dm_brightness._extra_cap；
    返回的 entry["extra"] 才是真正生效的值。
    """
    try:
        x = int(extra)
    except (TypeError, ValueError):
        raise DMError("软件叠加必须是 0~100 的整数")
    if not 0 <= x <= 100:
        raise DMError("软件叠加必须在 0~100 之间")

    snap = snapshot()
    o = _find(snap, name)
    if not o["connected"]:
        raise DMError("%s 当前不是 connected（%s）" % (name, o["status"]))

    import dm_brightness
    try:
        entry = dm_brightness.set_extra(o, x)
    except dm_brightness.BrightError as e:
        raise DMError(str(e))
    return {"ok": True, "name": name, "brightness": entry,
            "message": "%s 软件叠加已设为 %d%%（%s）" % (
                name, entry["extra"], entry.get("label") or entry["mode_label"])}


def set_brightness_temp(name: str, kelvin) -> dict:
    """设置夜间色温（3000~6500K，6500 = 原生）。走软件 gamma，与硬件亮度不冲突。"""
    try:
        k = int(kelvin)
    except (TypeError, ValueError):
        raise DMError("色温必须是 3000~6500 的整数（单位 K）")
    if not 3000 <= k <= 6500:
        raise DMError("色温必须在 3000~6500 K 之间")

    snap = snapshot()
    o = _find(snap, name)
    if not o["connected"]:
        raise DMError("%s 当前不是 connected（%s）" % (name, o["status"]))

    import dm_brightness
    try:
        entry = dm_brightness.set_temp(o, k)
    except dm_brightness.BrightError as e:
        raise DMError(str(e))
    return {"ok": True, "name": name, "brightness": entry,
            "message": "%s 夜间色温已设为 %s（gamma %s）" % (
                name, entry["temp_label"],
                ":".join("%.2f" % v for v in entry["temp_gamma"]))}


def brightness_state(refresh: bool = False) -> dict:
    """亮度总览；refresh=True 时强制重读硬件值（ddcutil getvcp / sysfs / gamma）。"""
    snap = snapshot()
    if not refresh:
        return snap.get("brightness") or {}
    details = {}
    rc, out, _ = xrandr([], verbose=True)
    if rc == 0:
        details = _parse_details(out)
    import dm_brightness
    return dm_brightness.summary(snap["outputs"], details, refresh=True)


def restore_all(reason: str = "cli") -> dict:
    snap = snapshot()
    todo = [o["name"] for o in snap["outputs"]
            if o["connected"] and not o["enabled"]
            and (state_get(o["name"]) or wd_baseline(o["name"]))]
    if not todo:
        return {"ok": True, "message": "没有需要恢复的输出", "state": snap}
    msgs = []
    ok = True
    for n in todo:
        try:
            r = turn_on(n, reason)
            msgs.append(r["message"])
        except DMError as e:
            ok = False
            msgs.append(str(e))
    return {"ok": ok, "message": "；".join(msgs), "state": snapshot()}


def describe(snap: dict) -> str:
    """CLI 的文本表格。"""
    lines = [
        "画布 %dx%d（%d 块启用）  时间 %s" % (
            snap["canvas"]["w"], snap["canvas"]["h"], snap["enabled_count"], snap["time"]),
        "%-8s %-13s %-20s %-11s %-7s %s" % ("输出", "状态", "几何", "模式", "缩放", "备注"),
    ]
    for o in snap["outputs"]:
        if o["status"] == "disconnected":
            state = "未连接"
        elif o["enabled"]:
            state = "启用"
        else:
            state = "已关闭"
        geom = "%dx%d+%d+%d" % (o["w"], o["h"], o["x"], o["y"]) if o["enabled"] else "-"
        mode = "%s@%s" % (o["mode"], o["rate"]) if o["enabled"] else "-"
        scale = ("%.0f%%" % o["scale"]) if o["scale"] else "-"
        notes = []
        if o["primary"]:
            notes.append("primary")
        if o["panel"]:
            notes.append("内屏")
        if o["off_marked"]:
            notes.append("你关闭的")
        if o["idle"]:
            notes.append("闲置 %s" % _fmt_secs(o["idle_seconds"]))
        if o["enabled"] and state_get(o["name"]):
            notes.append("有恢复记录")
        if not o["enabled"] and not o["panel"] and o["connected"] and wd_baseline(o["name"]):
            notes.append("可按基线恢复")
        lines.append("%-8s %-13s %-20s %-11s %-7s %s" % (
            o["name"], state, geom, mode, scale, ",".join(notes)))
    if snap.get("idle"):
        lines.append("闲置判据: %s（阈值 %s 秒）" % (
            snap["idle"]["basis"], snap["idle"]["threshold"]))
    for ov in snap["overlaps"]:
        lines.append("!! 重叠: %s 与 %s 相交 %dx%d" % (ov["a"], ov["b"], ov["w"], ov["h"]))
    return "\n".join(lines)


def _fmt_secs(s: float | None) -> str:
    if s is None:
        return "-"
    s = int(s)
    return "%d:%02d" % (s // 60, s % 60)
