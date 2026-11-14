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
RATE_RE = re.compile(r"(\d+(?:\.\d+)?)([*+]*)")


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
                    "modes": [],          # 该输出实际暴露的模式/刷新率（界面切换用）
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
        # 完整模式表：同一个尺寸下可能有多条刷新率，`*`=当前、`+`=首选。
        # xrandr 里 `*` 可能贴在数字后（160.00*），`+` 可能是单独一个 token（60.00 +）
        entry = {"mode": toks[0], "rates": []}
        for t in toks[1:]:
            if t and set(t) <= {"*", "+"} and entry["rates"]:
                entry["rates"][-1]["current"] |= "*" in t
                entry["rates"][-1]["preferred"] |= "+" in t
                continue
            rm = RATE_RE.fullmatch(t)
            if not rm:
                continue
            entry["rates"].append({
                "rate": "%.2f" % float(rm.group(1)),
                "current": "*" in rm.group(2),
                "preferred": "+" in rm.group(2),
            })
        if entry["rates"]:
            cur["modes"].append(entry)
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
# 采集卡（UVC 视频输入）识别
# --------------------------------------------------------------------------- #
# 按 EDID 判定，不按输出名：采集卡换个 HDMI/DP 口后 connector 会变（HDMI-0/DP-2…），
# 但 EDID 里的 PNP 厂商号和产品名不变。UGREEN 95348 这类 USB 采集棒会伪装成一块
# 1080p 显示器，好让显卡吐出可采集的信号——它没有面板、没有背光，不是给人看的屏。
CAPTURE_PNP_IDS = {"UGR"}          # UGREEN
CAPTURE_NAME_HINTS = ("UGREEN", "CAPTURE", "GRABBER", "GAME CAPTURE")

# screen_rects() 只跑轻量 `xrandr --current`（不读 EDID），这里缓存最近一次
# 全量快照里识别出的采集卡名字，供护眼/闲置等高频率路径过滤。
_CAPTURE_NAMES: set[str] = set()


def _edid_identity(edid: bytes | None) -> dict | None:
    """从 EDID 取 PNP 厂商号 / 产品码 / 产品名（0xFC 描述符）。"""
    if not edid or len(edid) < 128:
        return None
    raw = int.from_bytes(edid[8:10], "big")
    pnp = "".join(chr(ord("A") + ((raw >> s) & 0x1F) - 1) for s in (10, 5, 0))
    name = ""
    for off in range(54, 126, 18):
        d = edid[off:off + 18]
        if len(d) < 18 or d[0:3] != b"\x00\x00\x00":
            continue
        if d[3] == 0xFC:
            name = d[5:18].split(b"\x0a")[0].decode("ascii", "replace").strip()
            break
    return {"pnp": pnp, "product": "0x%04x" % int.from_bytes(edid[10:12], "little"),
            "name": name}


def is_capture_sink(edid: bytes | None) -> bool:
    """这块输出是不是采集卡（视频输入）而不是显示器。"""
    ids = _edid_identity(edid)
    if not ids:
        return False
    if ids["pnp"].upper() in CAPTURE_PNP_IDS:
        return True
    up = ids["name"].upper()
    return any(h in up for h in CAPTURE_NAME_HINTS)


def capture_names() -> set[str]:
    return set(_CAPTURE_NAMES)


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
# 规范布局（单一几何源）
# --------------------------------------------------------------------------- #
# 每块输出的「模式 + 刷新率 + transform + 位置 + primary」长期存在这里，与启用状态解耦：
# 关屏只改 .off，不改坐标；各种开关组合都按这份整体几何摆放。
LAYOUT_FILE = os.path.join(os.path.dirname(CONFIG_FILE), "layout.json")

# 2026-11-11 首次建库用的实测摆位（用户 GNOME 面板：2 上、1 左下、3 右下）。
# 现网旧数据里 DP-0 与 DP-4 都记成 0,0（坏数据），不能直接当规范布局；这三个坐标
# 取自 11-11 01:19 关闭对应输出时记录的真实四件套（日志可查）。只在建库那一次用，
# 且 confirmed=False，等用户在页面上确认；之后一律以 layout.json 为准。
MIGRATION_POS = {"DP-0": (0, 0), "DP-4": (881, 2880), "HDMI-0": (4289, 2880)}


def _split_geom(g) -> tuple[int, int]:
    m = re.fullmatch(r"(\d+)x(\d+)", str(g).strip().lower())
    if not m:
        raise DMError("几何格式不对: %r" % g)
    return int(m.group(1)), int(m.group(2))


def _entry_of(o: dict) -> dict:
    return {
        "mode": o["mode"],
        "rate": o["rate"],
        "transform": list(o["transform"] or IDENTITY),
        "pos": [int(o["x"]), int(o["y"])],
        "geometry": "%dx%d" % (o["w"], o["h"]),
        "primary": bool(o["primary"]),
    }


def layout_load() -> dict:
    try:
        with open(LAYOUT_FILE, "r") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("outputs"), dict):
            d.setdefault("version", 1)
            d.setdefault("confirmed", False)
            d.setdefault("updated", None)
            return d
    except (OSError, ValueError):
        pass
    return {"version": 1, "confirmed": False, "updated": None, "outputs": {}}


def _state_mirror(lay: dict) -> None:
    """把规范布局镜像进 display-manager.state：两处几何永远一致，不再各说各话。"""
    lines = []
    for name, e in sorted(lay["outputs"].items()):
        lines.append("%s %s %s %d %d %s %s %d" % (
            name, e["mode"], e["rate"], e["pos"][0], e["pos"][1],
            ",".join("%.6f" % v for v in (e.get("transform") or IDENTITY)),
            e["geometry"], 1 if e.get("primary") else 0))
    _write_lines(STATE_FILE, lines)


def layout_save(lay: dict, reason: str = "") -> dict:
    lay["version"] = 1
    lay["updated"] = datetime.now().strftime("%F %T")
    os.makedirs(os.path.dirname(LAYOUT_FILE), exist_ok=True)
    tmp = LAYOUT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(lay, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, LAYOUT_FILE)
    _state_mirror(lay)
    log("规范布局已保存%s: %s" % ("（%s）" % reason if reason else "",
        "、".join("%s@%d,%d" % (n, e["pos"][0], e["pos"][1]) for n, e in sorted(lay["outputs"].items()))))
    return lay


def layout_canvas(lay: dict) -> dict | None:
    rects = []
    for e in lay["outputs"].values():
        try:
            w, h = _split_geom(e["geometry"])
        except DMError:
            continue
        rects.append({"x": e["pos"][0], "y": e["pos"][1], "w": w, "h": h})
    return _bbox(rects) if rects else None


def layout_ensure(outs: list[dict]) -> dict:
    """第一次建库：从「当前几何 / .state / 看门狗基线」拼一份规范布局，标成待确认。

    撞车的数据一律不采信：DP-0 与 DP-4 都记在 0,0 时，按 MIGRATION_POS 的实测摆位落位。
    """
    lay = layout_load()
    if lay["outputs"]:
        return lay
    taken: list[dict] = []
    entries: dict[str, dict] = {}
    for o in sorted(outs, key=lambda x: (not x["enabled"], x["name"])):
        if not o["connected"]:
            continue
        if o.get("capture"):        # 采集卡不进规范布局
            continue
        base = _entry_of(o) if o["enabled"] else None
        if base is None:
            for src in (state_get(o["name"]), wd_baseline(o["name"])):
                if src:
                    base = {"mode": src["mode"], "rate": src["rate"],
                            "transform": list(src["transform"]), "pos": list(src["pos"]),
                            "geometry": src["geometry"], "primary": bool(src["primary"])}
                    break
        if base is None:
            continue
        w, h = _split_geom(base["geometry"])
        cands: list[list[int]] = []
        # 用户现在的实际摆位优先；只有它和别的屏撞车（历史坏数据）才用实测摆位兜底
        for pos in (tuple(base["pos"]), MIGRATION_POS.get(o["name"])):
            if pos and list(pos) not in cands:
                cands.append(list(pos))
        pick = None
        for pos in cands:
            if not _hits({"x": pos[0], "y": pos[1], "w": w, "h": h}, taken):
                pick = pos
                break
        if pick is None:
            pick, _ = plan_place(w, h, base["pos"], taken)
        entries[o["name"]] = dict(base, pos=pick)
        taken.append({"x": pick[0], "y": pick[1], "w": w, "h": h})
    lay["outputs"] = entries
    lay["confirmed"] = False
    return layout_save(lay, "首次建库，等待确认")


def layout_confirm() -> dict:
    lay = layout_load()
    lay["confirmed"] = True
    return layout_save(lay, "用户确认")


def layout_claim(outs: list[dict]) -> dict:
    """把当前实际生效的几何认领为规范布局；实际布局本身重叠就拒绝（不猜）。"""
    ov = _overlaps(outs)
    if ov:
        raise DMError("当前实际布局本身就有重叠（%s），先在 GNOME 里把屏摆开、再点认领"
                      % "、".join("%s/%s" % (x["a"], x["b"]) for x in ov))
    lay = layout_load()
    entries = dict(lay["outputs"])
    for o in outs:
        if o["enabled"] and not o.get("capture"):
            entries[o["name"]] = _entry_of(o)
    lay["outputs"] = entries
    lay["confirmed"] = True
    return layout_save(lay, "按当前实际布局认领")


def layout_set_positions(pos: dict) -> dict:
    """手工改固定坐标：保存前检查互不重叠（不允许把重叠写进规范布局）。"""
    lay = layout_load()
    entries = lay["outputs"]
    if not entries:
        raise DMError("还没有规范布局，先点「按当前实际布局认领」")
    for name, xy in pos.items():
        if name not in entries:
            continue
        try:
            x, y = int(xy[0]), int(xy[1])
        except (TypeError, ValueError, IndexError):
            raise DMError("%s 的坐标必须是两个整数" % name)
        entries[name]["pos"] = [x, y]
    rects = []
    for name, e in entries.items():
        w, h = _split_geom(e["geometry"])
        rects.append({"name": name, "x": e["pos"][0], "y": e["pos"][1], "w": w, "h": h})
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            if (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
                    and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]):
                raise DMError("%s 与 %s 的固定位置重叠（%d,%d 对 %d,%d），改开一点再保存"
                              % (a["name"], b["name"], a["x"], a["y"], b["x"], b["y"]))
    lay["confirmed"] = True
    return layout_save(lay, "手动调整固定坐标")


def layout_update_mode(rec: dict) -> None:
    """服务内改了分辨率/刷新率：同步固定布局的 mode/rate/transform/geometry，位置不动。"""
    lay = layout_load()
    e = lay["outputs"].get(rec.get("name"))
    if not e:
        return
    for k in ("mode", "rate", "transform", "geometry", "primary"):
        if k in rec:
            e[k] = rec[k]
    layout_save(lay, "%s 改模式" % rec["name"])


def layout_action(body: dict) -> dict:
    """页面上的固定布局操作：确认 / 认领当前实际布局 / 手改坐标。"""
    action = (body or {}).get("action") or "confirm"
    snap = snapshot()
    if action == "confirm":
        lay = layout_confirm()
    elif action == "claim":
        lay = layout_claim(snap["outputs"])
    elif action == "set":
        lay = layout_set_positions((body or {}).get("pos") or {})
    else:
        raise DMError("未知操作: %r" % action)
    return {"layout": lay, "state": snapshot()}



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
        capture = is_capture_sink(d.get("edid"))
        outputs.append({
            **o,
            "id": o["name"],
            "panel": ct.lower() == "panel",
            "capture": capture,
            "kind": "capture" if capture else ("panel" if ct.lower() == "panel" else "display"),
            "connector_type": ct or "未知",
            "transform": tr,
            "scale": scale,
            "off_marked": o["name"] in marked,
            "crtc": [o["x"], o["y"], o["w"], o["h"]] if o["enabled"] else None,
        })

    # 采集卡是视频输入，不计入「启用的显示器」：数量、画布、重叠判定都按真屏算
    _CAPTURE_NAMES.clear()
    _CAPTURE_NAMES.update(o["name"] for o in outputs if o["capture"])
    enabled = [o for o in outputs if o["enabled"] and not o["capture"]]
    lay = layout_load()
    # 采集卡不该出现在规范布局里（历史坏数据自愈）：一旦识别出来就从 layout.json 摘掉
    stale = [o["name"] for o in outputs if o["capture"] and o["name"] in lay["outputs"]]
    if stale:
        for n in stale:
            lay["outputs"].pop(n, None)
        layout_save(lay, "采集卡 %s 不参与显示器布局，已移除" % "/".join(stale))
    # 画布按「规范布局」算：所有输出的 union，不随开关组合变化
    canvas = layout_canvas(lay) or _bbox(enabled)
    # 已关闭输出的「开启后会放这里」预告：只用于画虚线幽灵框，不进 canvas
    previews = _restore_preview(outputs, lay)
    for o in outputs:
        if o["name"] in previews:
            o["restore_preview"] = previews[o["name"]]
        e = lay["outputs"].get(o["name"])
        if not e:
            continue
        o["canonical"] = e
        if o["enabled"]:
            try:
                w, h = _split_geom(e["geometry"])
            except DMError:
                continue
            if (o["x"], o["y"], o["w"], o["h"]) != (e["pos"][0], e["pos"][1], w, h):
                o["drift"] = {"pos": e["pos"], "geometry": e["geometry"]}
    # 在关闭名单里却亮着：状态文件被服务之外的操作弄脏了（挂起/恢复、手动 xrandr）
    # 采集卡不走这套开关语义，不算冲突
    conflicts = [o["name"] for o in outputs
                 if o["enabled"] and o["off_marked"] and not o["capture"]]
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
        "capture_count": sum(1 for o in outputs if o["connected"] and o["capture"]),
        "canvas": canvas,
        "canvas_live": _bbox(enabled),
        "layout": {
            "file": LAYOUT_FILE,
            "confirmed": lay.get("confirmed", False),
            "updated": lay.get("updated"),
            "outputs": lay["outputs"],
        },
        "off_marked": marked,
        "conflicts": conflicts,
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


_LIVE_SNAP = {"ts": 0.0, "snap": None}


def _snapshot_for_live(ttl: float = 1.0) -> dict:
    """拖动中的实时亮度只读 1 秒内的布局快照，避免每次拖动都跑两遍 xrandr。

    布局状态在 1 秒内变化对亮度写入没有影响；松手后的最终值仍用全新快照。
    """
    now = time.time()
    if _LIVE_SNAP["snap"] is not None and now - _LIVE_SNAP["ts"] < ttl:
        return _LIVE_SNAP["snap"]
    snap = snapshot()
    _LIVE_SNAP.update(ts=now, snap=snap)
    return snap


def invalidate_live_snapshot() -> None:
    """布局变化后作废「拖动亮度」用的 1 秒快照缓存。"""
    _LIVE_SNAP.update(ts=0.0, snap=None)


def _bbox(outs: list[dict]) -> dict:
    if not outs:
        return {"x": 0, "y": 0, "w": 0, "h": 0}
    x0 = min(o["x"] for o in outs)
    y0 = min(o["y"] for o in outs)
    x1 = max(o["x"] + o["w"] for o in outs)
    y1 = max(o["y"] + o["h"] for o in outs)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _overlaps(outs: list[dict]) -> list[dict]:
    en = [o for o in outs if o["enabled"] and not o.get("capture")]
    res = []
    for i, a in enumerate(en):
        for b in en[i + 1:]:
            ix = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
            iy = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
            if ix > 0 and iy > 0:
                res.append({"a": a["name"], "b": b["name"], "w": ix, "h": iy})
    return res


def _hits(rect: dict, occupied: list[dict]) -> bool:
    """rect 是否与 occupied 里的任一矩形相交（边界相接不算）。"""
    for r in occupied:
        if (rect["x"] < r["x"] + r["w"] and r["x"] < rect["x"] + rect["w"]
                and rect["y"] < r["y"] + r["h"] and r["y"] < rect["y"] + rect["h"]):
            return True
    return False


def _enabled_rects(outs: list[dict]) -> list[dict]:
    return [{"x": o["x"], "y": o["y"], "w": o["w"], "h": o["h"]}
            for o in outs if o["enabled"] and not o.get("capture")]


def plan_place(w: int, h: int, want, occupied: list[dict]):
    """给将要启用的输出挑一个不会压住正在使用的屏的位置。

    优先用关闭前记录的位置（用户的直觉：还回到原来那块地方）；只有当它和现有
    屏相交时，才挪到现有布局的外侧：右 → 左 → 下 → 上。右侧在虚拟画布上总是
    放得下，所以几乎总能给出不重叠的结果（放不下才抛错，宁可不点亮也不叠屏）。
    返回 (pos, 是否挪动过)。
    """
    want = (int(want[0]), int(want[1]))
    rect = {"x": want[0], "y": want[1], "w": int(w), "h": int(h)}
    if not _hits(rect, occupied):
        return want, False

    minx = min(r["x"] for r in occupied)
    miny = min(r["y"] for r in occupied)
    maxx = max(r["x"] + r["w"] for r in occupied)
    maxy = max(r["y"] + r["h"] for r in occupied)
    for pos in ((maxx, want[1]), (minx - rect["w"], want[1]),
                (want[0], maxy), (want[0], miny - rect["h"])):
        if pos[0] < 0:
            continue
        if not _hits({"x": pos[0], "y": pos[1], "w": rect["w"], "h": rect["h"]}, occupied):
            return (int(pos[0]), int(pos[1])), True
    for k in range(1, 33):      # 兜底：一屏一屏往右挪，32767 是 X 的坐标上限
        pos = (maxx + k * rect["w"], want[1])
        if pos[0] + rect["w"] > 32767:
            break
        if not _hits({"x": pos[0], "y": pos[1], "w": rect["w"], "h": rect["h"]}, occupied):
            return pos, True
    raise DMError("虚拟画布放不下 %dx%d 的输出，为免叠屏不点亮" % (rect["w"], rect["h"]))


def _restore_preview(outs: list[dict], lay: dict | None = None) -> dict:
    """给「已关闭但能恢复」的输出算一个落点，供界面画虚线幽灵框。

    规范布局里有它的，直接停在它自己的固定位置上（开启后就是这里，与别的屏无关）；
    没有的，才按 plan_place 临时避让一次。
    """
    lay = lay or layout_load()
    occ = _enabled_rects(outs)
    res = {}
    for o in outs:
        if not o["connected"] or o["enabled"] or o.get("capture"):
            continue
        e = lay["outputs"].get(o["name"])
        if e:
            try:
                w, h = _split_geom(e["geometry"])
            except DMError:
                continue
            res[o["name"]] = {
                "source": "layout",
                "mode": e["mode"], "rate": e["rate"],
                "geometry": "%dx%d" % (w, h),
                "want": [e["pos"][0], e["pos"][1]],
                "pos": [e["pos"][0], e["pos"][1]],
                "relocated": False,
                "scale": round(w * 100.0 / (int(e["mode"].split("x")[0]) or 1), 1),
            }
            continue
        rec = state_get(o["name"])
        src = "state"
        if not rec:
            rec = wd_baseline(o["name"])
            src = "watchdog"
        if not rec:
            continue
        try:
            w, h = (int(v) for v in rec["geometry"].lower().split("x"))
        except (ValueError, AttributeError):
            continue
        try:
            pos, moved = plan_place(w, h, rec["pos"], occ)
        except DMError:
            continue
        res[o["name"]] = {
            "source": src,
            "mode": rec["mode"], "rate": rec["rate"],
            "geometry": "%dx%d" % (w, h),
            "want": [rec["pos"][0], rec["pos"][1]],
            "pos": [pos[0], pos[1]],
            "relocated": moved,
            "scale": round(w * 100.0 / (int(rec["mode"].split("x")[0]) or 1), 1),
        }
        occ.append({"x": pos[0], "y": pos[1], "w": w, "h": h})
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
    cap = capture_names()
    return [o for o in _parse_query(out)["outputs"] if o["enabled"] and o["name"] not in cap]


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
    if not o.get("capture") and snap["enabled_count"] <= 1:
        raise DMError("拒绝关闭：%s 是当前唯一启用的显示器，关掉后将没有任何显示输出" % name)
    if not o["mode"] or not o["rate"]:
        raise DMError("无法从 xrandr 读出 %s 的当前模式/刷新率，为安全起见不关闭" % name)

    rest = [x for x in snap["outputs"]
            if x["enabled"] and x["name"] != name and not x.get("capture")]
    return {
        "name": name,
        "panel": o["panel"],
        "remaining": [x["name"] for x in rest],
        "remaining_count": len(rest),
        "canvas_after": _bbox(rest),
        "canvas_now": snap["canvas"],
        "restore_from": "layout" if name in layout_load()["outputs"] else (
            "watchdog" if wd_baseline(name) else "none"),
    }


def turn_off(name: str, reason: str = "cli") -> dict:
    plan = plan_off(name)
    snap = snapshot()
    o = _find(snap, name)

    tr = o["transform"] or list(IDENTITY)
    # 关屏不改几何：规范布局里已有它就原样保留；只有第一次见到的新输出才补一条
    lay = layout_load()
    if o.get("capture"):
        # 采集卡不是显示器：不登记固定几何，历史残留也一并摘掉
        if lay["outputs"].pop(name, None) is not None:
            layout_save(lay, "关闭采集卡 %s 时移除其布局记录" % name)
        log("关闭采集卡 %s（视频输入，不参与显示器布局）(%s)" % (name, reason))
    elif name in lay["outputs"]:
        log("关闭 %s（关闭不改固定几何 %d,%d，仍按规范布局保留）(%s)" % (
            name, lay["outputs"][name]["pos"][0], lay["outputs"][name]["pos"][1], reason))
    else:
        lay["outputs"][name] = _entry_of(o)
        layout_save(lay, "关闭 %s 时补登固定几何" % name)
        log("记录 %s 的固定几何: %s@%s pos=%dx%d transform=%s 几何=%s primary=%s (%s)" % (
            name, o["mode"], o["rate"], o["x"], o["y"],
            ",".join("%.6f" % v for v in tr), "%dx%d" % (o["w"], o["h"]),
            "是" if o["primary"] else "否", reason))

    rc, out, err = xrandr(["--output", name, "--off"])
    if rc != 0:
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

    if o.get("capture"):
        # 采集卡是视频输入：只负责「亮起来」，不套规范布局、不抢 primary、不改别人的几何
        rc, out, err = xrandr(["--output", name, "--auto"])
        if rc != 0:
            detail = (err or out).strip()
            log("启用采集卡 %s 失败 rc=%d ；xrandr stderr: %s" % (name, rc, detail))
            raise DMError("xrandr 启用采集卡 %s 失败（rc=%d）：%s" % (name, rc, detail or "无输出"))
        state_drop(name)
        off_unmark(name)
        after = snapshot()
        log("启用采集卡 %s（视频输入，按 --auto 点亮，不参与布局重放）(%s)" % (name, reason))
        return {"ok": True, "action": "on", "name": name, "capture": True, "state": after,
                "message": "%s 是采集卡（视频输入），已按驱动默认模式点亮" % name}

    # 首选规范布局：每块屏在全局几何里的坐标是固定的，先开谁后开谁都不影响
    lay = layout_load()
    e = lay["outputs"].get(name)
    if e:
        rec = {"name": name, "mode": e["mode"], "rate": e["rate"],
               "pos": tuple(e["pos"]), "geometry": e["geometry"],
               "transform": list(e.get("transform") or IDENTITY),
               "primary": bool(e.get("primary"))}
        src = "规范布局 %s" % LAYOUT_FILE
    else:
        rec, src = _resolve_restore(name)
    # 兜底：规范布局本身若被手工改重叠了，也不许叠上去（正常路径不会触发）
    want = rec["pos"]
    moved = False
    try:
        rw, rh = (int(v) for v in rec["geometry"].lower().split("x"))
    except (ValueError, AttributeError):
        rw = rh = 0
    if rw and rh:
        pos, moved = plan_place(rw, rh, want, _enabled_rects(snap["outputs"]))
        rec = dict(rec, pos=pos)

    args = ["--output", name, "--mode", rec["mode"]]
    if rec["rate"] and rec["rate"] != "0.00":
        args += ["--rate", rec["rate"]]
    tr = rec["transform"]
    if any(abs(a - b) > 1e-4 for a, b in zip(tr, IDENTITY)):
        args += ["--transform", ",".join("%.6f" % v for v in tr)]
    args += ["--pos", "%dx%d" % rec["pos"]]
    if rec["primary"]:
        args += ["--primary"]

    if moved:
        log("恢复位置 %dx%d 会压住正在使用的输出，改到 %dx%d" % (want[0], want[1], rec["pos"][0], rec["pos"][1]))
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
    if moved:
        warn = "记录位置 %d,%d 会压住正在使用的屏，已改放到 %d,%d" % (
            want[0], want[1], rec["pos"][0], rec["pos"][1])
    if actual != exp:
        warn = (warn + "；" if warn else "") + "恢复后几何与记录不一致：期望 %s，实际 %s" % (exp, actual)
        log("警告: 恢复后几何与记录不一致：期望 %s，实际 %s" % (exp, actual))
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


def set_brightness(name: str, value, extra=None, live: bool = False) -> dict:
    """设置亮度（20~100）。通道选择见 dm_brightness.py：硬件背光/DDC 优先，其次软件调光。

    下限 20% 是无条件的安全线：SDR 下再低基本等于黑屏（关掉 HDR 的 AOC 实测 0% 直接
    灰屏）。OS 侧读不到显示器 OSD 的 HDR 状态，所以不做「只有非 HDR 才限制」的判断。
    extra 是硬件到下限后的软件叠加层 0~100；None 表示不改动已有叠加。
    live=True 是滑杆拖动中的实时值：跳过硬件读回校验，牺牲一点确定性换低延迟；
    松手后的最终值仍走 live=False 的完整校验。
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

    snap = _snapshot_for_live() if live else snapshot()
    o = _find(snap, name)
    if not o["connected"]:
        raise DMError("%s 当前不是 connected（%s）" % (name, o["status"]))

    try:
        entry = dm_brightness.set_value(o, v, extra=extra, live=live)
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


def set_brightness_lock(name: str, lock) -> dict:
    """通道锁定：lock="software" 钉死软件通道；None/"" 解除锁定。

    锁定值落在亮度持久化文件里，重启后仍然有效。锁成 software 后亮度链路
    不再调用 ddcutil（不探测、不写、不读）。
    """
    if lock not in (None, "", "software"):
        raise DMError("通道锁定只支持 software 或解除（null）")
    snap = snapshot()
    o = _find(snap, name)
    if not o["connected"]:
        raise DMError("%s 当前不是 connected（%s）" % (name, o["status"]))

    import dm_brightness
    try:
        if lock == "software":
            entry = dm_brightness.lock_software(o)
        else:
            entry = dm_brightness.unlock_channel(o)
    except dm_brightness.BrightError as e:
        raise DMError(str(e))
    return {"ok": True, "name": name, "brightness": entry,
            "message": ("%s 已锁定软件通道（之后不再调用 ddcutil）" % name
                        if lock == "software" else "%s 已解除通道锁定" % name)}


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


def reconcile(reason: str = "启动") -> list[str]:
    """对账：规范布局、.off 名单、.state 镜像、看门狗基线四处对齐。

    挂起/恢复、mutter 重新点亮、手动 xrandr 都可能绕过本服务：
    - 在 .off 里却亮着 → 名单失去意义（看门狗会漏判），移除；
    - 规范布局里没有的新输出 → 按当前几何补一条（几何源只有一份，不能缺）；
    - 亮着的输出偏离固定位置 → 只告警，不擅自挪（挪不挪由用户在 GNOME 里决定）。

    只改状态文件，不改任何显示布局。
    """
    snap = snapshot()
    lay = layout_load()
    notes = []
    for o in snap["outputs"]:
        if not o["connected"]:
            continue
        if o["enabled"] and o["off_marked"]:
            off_unmark(o["name"])
            notes.append("%s 在 .off 名单里但当前是亮的（多半是挂起/恢复时被系统点亮），已从名单移除"
                         % o["name"])
        if o["enabled"] and not o["capture"] and o["name"] not in lay["outputs"]:
            lay["outputs"][o["name"]] = _entry_of(o)
            layout_save(lay, "对账补登 %s" % o["name"])
            notes.append("%s 不在规范布局里，已按当前几何补登 %d,%d" % (o["name"], o["x"], o["y"]))
        if o.get("drift") and not o["capture"]:
            notes.append("%s 当前 %d,%d 偏离固定位置 %d,%d（%s）；要挪回请关掉再开启"
                         % (o["name"], o["x"], o["y"],
                            o["drift"]["pos"][0], o["drift"]["pos"][1], o["drift"]["geometry"]))
    for n in notes:
        log("%s对账: %s" % (reason, n))
    return notes


def restore_all(reason: str = "cli") -> dict:
    snap = snapshot()
    todo = [o["name"] for o in snap["outputs"]
            if o["connected"] and not o["enabled"] and not o["capture"]
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
        if o["capture"]:
            notes.append("采集卡/视频输入")
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
