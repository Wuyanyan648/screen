#!/usr/bin/env python3
"""亮度调节：能力探测（DDC/CI、sysfs 背光、软件 gamma）、读写与持久化。

本机实测结论（2026-11-09）：
- DP-4（eDP 内屏）：`/sys/class/backlight/nvidia_0` 存在但 brightness 是 root:video 0660，
  当前用户不在 video 组，直接写会 PermissionError；X 上的 `Backlight` 属性只读
  （`xrandr --set Backlight` 返回 BadValue）。因此默认走软件调光，拿到权限后自动切硬件背光。
- DP-0（AOC U27G3X，DisplayPort）：DDC/CI 需要 ddcutil + /dev/i2c-* 权限；本机未安装
  ddcutil 且 i2c 设备是 0600 root，暂时只能软件调光，装好权限后自动改用 setvcp 10。
- HDMI-0（便携屏）：同样走 DDC 探测；探测不到就软件调光（gamma）。

三条通道的优先级：硬件背光（内屏）/ DDC（外接）→ 软件 gamma → 不支持。
「软件调光」改的是显卡 gamma，亮度下降时对比度也会变，界面必须单独标注。

持久化：~/.cache/display-manager.brightness.json，服务启动和「关→开」后都会重新下发。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime

HOME = os.path.expanduser("~")
CACHE_DIR = os.environ.get("DM_CACHE_DIR", os.path.join(HOME, ".cache"))
BRIGHT_FILE = os.path.join(CACHE_DIR, "display-manager.brightness.json")
CONFIG_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")),
    "display-manager", "config.json")

BACKLIGHT_DIR = "/sys/class/backlight"
DRM_DIR = "/sys/class/drm"

DDC_TTL = 60          # ddcutil detect 结果缓存秒数（detect 本身要 1~2 秒）
VALUE_TTL = 300       # 硬件读值缓存秒数，避免每 2 秒轮询都去读 DDC

I2C_HINT = (
    "sudo apt install ddcutil\n"
    "echo 'SUBSYSTEM==\"i2c-dev\", KERNEL==\"i2c-[0-9]*\", TAG+=\"uaccess\"' | "
    "sudo tee /etc/udev/rules.d/60-i2c-uaccess.rules\n"
    "sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=i2c-dev"
)
VIDEO_HINT = "sudo usermod -aG video $USER（重新登录后生效；之后自动改用硬件背光）"

_DDC = {"info": None, "ts": 0.0}
_DDC_LOCK = threading.Lock()
_VALUES: dict[str, tuple[int, float]] = {}


class BrightError(Exception):
    """带中文说明的亮度错误，可直接展示到界面。"""


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)


def _log(msg: str) -> None:
    try:
        import dm_core
        dm_core.log(msg)
    except Exception:
        pass


def _stored() -> dict:
    try:
        with open(BRIGHT_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _store(name: str, value: int, mode: str) -> None:
    data = _stored()
    data[name] = {"value": int(value), "mode": mode,
                  "time": datetime.now().strftime("%F %T")}
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = BRIGHT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BRIGHT_FILE)


def _override_map() -> dict:
    """config.json 里可选的手工映射： {"brightness": {"ddc_display": {"DP-0": 1}}}"""
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
        m = (data.get("brightness") or {}).get("ddc_display") or {}
        return {str(k): int(v) for k, v in m.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


# --------------------------------------------------------------------------- #
# 硬件背光（sysfs）
# --------------------------------------------------------------------------- #
def backlight_info() -> dict | None:
    try:
        devs = sorted(os.listdir(BACKLIGHT_DIR))
    except OSError:
        return None
    for dev in devs:
        base = os.path.join(BACKLIGHT_DIR, dev)

        def rd(fname: str):
            try:
                with open(os.path.join(base, fname)) as f:
                    return int(f.read().strip())
            except (OSError, ValueError):
                return None

        mx = rd("max_brightness")
        if not mx:
            continue
        path = os.path.join(base, "brightness")
        return {
            "dev": dev,
            "path": path,
            "max": mx,
            "cur": rd("brightness"),
            "actual": rd("actual_brightness"),
            "writable": os.access(path, os.W_OK),
        }
    return None


def _backlight_set(info: dict, pct: int) -> int:
    raw = int(round(info["max"] * pct / 100.0))
    try:
        with open(info["path"], "w") as f:
            f.write(str(raw))
    except PermissionError:
        raise BrightError("没有写 %s 的权限（需要 video 组或 udev 规则）：%s"
                          % (info["path"], VIDEO_HINT))
    except OSError as e:
        raise BrightError("写 %s 失败：%s" % (info["path"], e))
    return pct


# --------------------------------------------------------------------------- #
# DDC/CI（ddcutil）
# --------------------------------------------------------------------------- #
def _parse_detect(text: str) -> list[dict]:
    res: list[dict] = []
    cur = None
    for line in text.splitlines():
        m = re.match(r"^Display (\d+)\s*$", line)
        if m:
            cur = {"num": int(m.group(1)), "bus": None, "connector": None, "model": None}
            res.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("I2C bus:"):
            cur["bus"] = s.split(":", 1)[1].strip()
        elif s.startswith("DRM connector:"):
            cur["connector"] = s.split(":", 1)[1].strip()
        elif s.startswith("Model:"):
            cur["model"] = s.split(":", 1)[1].strip()
    return res


def _ddc_state() -> dict:
    now = time.time()
    with _DDC_LOCK:
        if _DDC["info"] is not None and now - _DDC["ts"] < DDC_TTL:
            return _DDC["info"]
        info = {"ok": False, "reason": "", "hint": "", "displays": []}
        exe = shutil.which("ddcutil")
        if not exe:
            info["reason"] = "未安装 ddcutil（本机当前没有 DDC/CI 工具）"
            info["hint"] = I2C_HINT
        else:
            rc, out, err = _run([exe, "detect"], 20)
            if rc != 0 and not out.strip():
                text = (err or "").strip()
                low = text.lower()
                if "permission" in low or "failed to open" in low or "/dev/i2c" in low:
                    info["reason"] = "打不开 /dev/i2c-*（权限不足）"
                    info["hint"] = I2C_HINT
                else:
                    info["reason"] = "ddcutil detect 失败：%s" % (
                        text.splitlines()[-1] if text else "无输出")
            else:
                info["displays"] = _parse_detect(out)
                if info["displays"]:
                    info["ok"] = True
                else:
                    info["reason"] = "ddcutil 没发现支持 DDC/CI 的显示器"
                    info["hint"] = "确认显示器菜单里打开了 DDC/CI；外接屏用 DP/HDMI 直连"
        _DDC["info"] = info
        _DDC["ts"] = now
        return info


def _norm_conn(s: str) -> str:
    s = (s or "").strip()
    return s.split("-", 1)[1] if re.match(r"^card\d+-", s) else s


def _drm_edids() -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    try:
        names = sorted(os.listdir(DRM_DIR))
    except OSError:
        return out
    for n in names:
        if "-" not in n or not n.startswith("card"):
            continue
        try:
            with open(os.path.join(DRM_DIR, n, "edid"), "rb") as f:
                data = f.read()
        except OSError:
            continue
        if data:
            out[n] = data
    return out


def _map_displays(outputs: list[dict], edids: dict[str, bytes | None],
                  displays: list[dict]) -> dict:
    """X 输出名 → ddcutil Display 条目。

    优先用 config.json 的 brightness.ddc_display；否则把 xrandr 里的 EDID 与
    /sys/class/drm/<connector>/edid 对上，找到 DRM 连接器名，再和 ddcutil detect
    打印的 "DRM connector: cardN-XXX" 匹配。
    """
    override = _override_map()
    by_num = {d["num"]: d for d in displays}
    drm = _drm_edids()
    by_edid: dict[bytes, list[str]] = {}
    for conn, edid in drm.items():
        by_edid.setdefault(edid, []).append(conn)

    res: dict[str, dict] = {}
    for o in outputs:
        name = o["name"]
        if name in override and override[name] in by_num:
            res[name] = by_num[override[name]]
            continue
        edid = edids.get(name)
        if not edid:
            continue
        want = {_norm_conn(c) for c in by_edid.get(edid, [])}
        if not want:
            continue
        for d in displays:
            if d.get("connector") and _norm_conn(d["connector"]) in want:
                res[name] = d
                break
    return res


def _ddc_error(text: str) -> BrightError:
    low = (text or "").lower()
    if "permission" in low or "failed to open" in low or "i2c" in low and "open" in low:
        return BrightError("没有 /dev/i2c-* 的读写权限：" + I2C_HINT)
    last = (text or "").strip().splitlines()
    return BrightError("ddcutil 失败：%s" % (last[-1] if last else "无输出"))


def _ddc_get(bus: str) -> tuple[int, int]:
    rc, out, err = _run(["ddcutil", "--bus", bus, "--sleep-multiplier", "0.3",
                         "getvcp", "10", "--brief"], 8)
    if rc != 0:
        raise _ddc_error(err or out)
    m = re.search(r"VCP\s+10\s+\w+\s+(\d+)\s+(\d+)", out)
    if not m:
        raise BrightError("无法解析 getvcp 10 输出：%s" % out.strip())
    return int(m.group(1)), int(m.group(2))


def _ddc_set(bus: str, pct: int) -> None:
    rc, out, err = _run(["ddcutil", "--bus", bus, "--sleep-multiplier", "0.3",
                         "setvcp", "10", str(pct)], 12)
    if rc != 0:
        raise _ddc_error(err or out)


# --------------------------------------------------------------------------- #
# 软件调光（xrandr gamma）
# --------------------------------------------------------------------------- #
def _sw_set(name: str, pct: int) -> None:
    rc, out, err = _run(["xrandr", "--output", name, "--brightness",
                         "%.2f" % (pct / 100.0)], 10)
    if rc != 0:
        detail = (err or out).strip()
        if "Need crtc" in detail:
            raise BrightError("%s 当前没有启用（没有 CRTC），gamma 调光无法生效" % name)
        raise BrightError("xrandr 设置 gamma 失败：%s" % (detail or "无输出"))


def _sw_reset(name: str) -> None:
    """切回硬件通道时把 gamma 复位，避免软件调光叠加在硬件亮度上。"""
    _run(["xrandr", "--output", name, "--brightness", "1.0"], 10)


# --------------------------------------------------------------------------- #
# 能力与读值
# --------------------------------------------------------------------------- #
def _pick_mode(o: dict) -> tuple[str, dict, str | None, str | None, str | None]:
    """返回 (mode, 附加信息, note, reason, hint)。mode ∈ ddc/backlight/software/none。"""
    panel = bool(o.get("panel"))
    bl = backlight_info()
    ddc = _ddc_state()
    details = o.get("_details") or {}
    name = o["name"]

    if panel and bl and bl["writable"]:
        return "backlight", {"bl": bl}, None, None, None

    if not panel and ddc["ok"]:
        disp = _map_displays([o], {name: details.get("edid")}, ddc["displays"]).get(name)
        if disp and disp.get("bus"):
            return "ddc", {"ddc": disp}, None, None, None

    if o.get("enabled"):
        note = None
        hint = None
        if panel and bl:
            cur = bl["actual"] if bl["actual"] is not None else bl["cur"]
            note = "硬件背光 %d%%（只读，需 video 组权限）" % round((cur or 0) * 100.0 / bl["max"])
            hint = VIDEO_HINT
        elif not panel:
            if ddc["ok"]:
                note = "DDC/CI 没匹配到这台显示器，暂用软件调光"
            else:
                note = "DDC 不可用：%s，暂用软件调光" % ddc["reason"]
                hint = ddc["hint"]
        return "software", {"bl": bl, "ddc": ddc}, note, None, hint

    # 屏已关闭：没有可用通道
    reason = "屏已关闭，gamma 调光无法生效"
    if panel and bl and not bl["writable"]:
        hint = VIDEO_HINT
    elif not panel:
        hint = (ddc.get("hint") or "") if not ddc["ok"] else None
    else:
        hint = None
    return "none", {"bl": bl, "ddc": ddc}, None, reason, hint


MODE_LABEL = {
    "ddc": "硬件 DDC/CI",
    "backlight": "硬件背光",
    "software": "软件调光（影响对比度）",
    "none": "不支持调节",
}


def _entry(o: dict, details: dict, refresh: bool) -> dict:
    name = o["name"]
    o = dict(o, _details=details.get(name) or {})
    mode, extra, note, reason, hint = _pick_mode(o)
    stored = _stored().get(name) or {}
    value = None

    if mode == "ddc":
        if stored and not refresh:
            value = stored.get("value")
        else:
            cached = _VALUES.get(name)
            if cached and not refresh and time.time() - cached[1] < VALUE_TTL:
                value = cached[0]
            else:
                try:
                    value, _max = _ddc_get(extra["ddc"]["bus"])
                    _VALUES[name] = (value, time.time())
                except BrightError as e:
                    mode, reason, hint = "none", str(e), None
    elif mode == "backlight":
        bl = extra["bl"]
        cur = bl["actual"] if bl["actual"] is not None else bl["cur"]
        value = None if cur is None else int(round(cur * 100.0 / bl["max"]))
    elif mode == "software":
        if stored and not refresh:
            value = stored.get("value")
        else:
            g = (details.get(name) or {}).get("brightness")
            value = None if g is None else int(round(g * 100))

    if value is None and stored and mode != "none":
        value = stored.get("value")
    if value is None and mode != "none":
        value = 100

    device = None
    if mode == "ddc":
        device = (extra.get("ddc") or {}).get("bus")
    elif mode == "backlight":
        device = (extra.get("bl") or {}).get("path")

    return {
        "name": name,
        "mode": mode,
        "mode_label": MODE_LABEL[mode],
        "hardware": mode in ("ddc", "backlight"),
        "available": mode != "none",
        "value": None if value is None else max(0, min(100, int(value))),
        "enabled": bool(o.get("enabled")),
        "device": device,
        "note": note,
        "reason": reason,
        "hint": hint,
    }


def summary(outputs: list[dict], details: dict | None = None, refresh: bool = False) -> dict:
    details = details or {}
    res = {}
    for o in outputs:
        if o.get("status") != "connected":
            continue
        try:
            res[o["name"]] = _entry(o, details, refresh)
        except Exception as e:  # 单块屏探测失败不影响其它屏
            res[o["name"]] = {
                "name": o["name"], "mode": "none", "mode_label": MODE_LABEL["none"],
                "hardware": False, "available": False, "value": None,
                "enabled": bool(o.get("enabled")), "device": None,
                "note": None, "reason": "探测异常：%s" % e, "hint": None,
            }
    return res


# --------------------------------------------------------------------------- #
# 设置与持久化恢复
# --------------------------------------------------------------------------- #
def _details() -> dict:
    try:
        import dm_core
        rc, out, _ = dm_core.xrandr([], verbose=True)
        return dm_core._parse_details(out) if rc == 0 else {}
    except Exception:
        return {}


def set_value(o: dict, pct: int, details: dict | None = None) -> dict:
    if not isinstance(pct, int) or not (0 <= pct <= 100):
        raise BrightError("亮度必须是 0~100 的整数")
    details = details if details is not None else _details()
    entry = _entry(o, details, refresh=False)
    mode = entry["mode"]
    if mode == "none":
        raise BrightError(entry["reason"] or "这块屏不支持亮度调节")

    prev_mode = (_stored().get(o["name"]) or {}).get("mode")
    if mode == "ddc":
        disp = _map_displays([o], {o["name"]: (details.get(o["name"]) or {}).get("edid")},
                             _ddc_state()["displays"]).get(o["name"])
        if not disp or not disp.get("bus"):
            raise BrightError("没找到 %s 对应的 ddcutil 显示器编号" % o["name"])
        _ddc_set(disp["bus"], pct)
        _VALUES[o["name"]] = (pct, time.time())
    elif mode == "backlight":
        bl = backlight_info()
        if not bl:
            raise BrightError("没有可用的 /sys/class/backlight 设备")
        _backlight_set(bl, pct)
    else:
        _sw_set(o["name"], pct)

    if prev_mode == "software" and mode != "software":
        _sw_reset(o["name"])
        _log("亮度通道从软件 gamma 切到 %s，已把 gamma 复位为 1.0" % mode)

    _store(o["name"], pct, mode)
    _log("亮度 %s = %d%%（%s）" % (o["name"], pct, mode))
    return _entry(o, details, refresh=False)


def reapply_for(o: dict) -> dict | None:
    """屏幕重新打开后把上次的值再下发一次（gamma 会随模式设置被复位）。"""
    st = _stored().get(o["name"])
    if not st or not o.get("enabled"):
        return None
    try:
        return set_value(o, int(st.get("value", 100)), _details())
    except BrightError as e:
        _log("重新应用 %s 亮度失败：%s" % (o["name"], e))
        return None


def reapply_software() -> list[str]:
    """任何 xrandr 布局变化都会把 gamma 复位；只给「软件调光」且已启用的屏重新下发。

    硬件通道（DDC / 背光）不随布局变化，不必重发（DDC 单次上百毫秒，能省则省）。
    """
    try:
        import dm_core
        snap = dm_core.snapshot()
    except Exception as e:
        _log("布局变化后恢复软件调光失败（取快照出错）：%r" % e)
        return []
    done = []
    for o in snap["outputs"]:
        if not o.get("enabled") or o.get("status") != "connected":
            continue
        st = _stored().get(o["name"])
        if not st or st.get("mode") != "software":
            continue
        try:
            _sw_set(o["name"], int(st.get("value", 100)))
            done.append(o["name"])
        except BrightError as e:
            _log("布局变化后恢复 %s 软件调光失败：%s" % (o["name"], e))
    if done:
        _log("布局变化后重新应用软件调光: " + "，".join(done))
    return done


def schedule_software_reapply(delay: float = 1.2) -> None:
    """GNOME/mutter 会在 RandR 变化后异步再套一次自己的配置，把 gamma 复位。

    所以除了当场补一次，再延迟补一次；只在常驻进程（web 服务）里有意义，CLI 退出就没了。
    """
    t = threading.Timer(delay, reapply_software)
    t.daemon = True
    t.start()


def apply_all() -> list[str]:
    """服务启动时恢复所有已启用输出上次的亮度。返回日志行。"""
    try:
        import dm_core
        snap = dm_core.snapshot()
    except Exception as e:
        _log("启动恢复亮度失败（取快照出错）：%r" % e)
        return []
    details = _details()
    msgs = []
    for o in snap["outputs"]:
        if not o.get("enabled") or o.get("status") != "connected":
            continue
        st = _stored().get(o["name"])
        if not st:
            continue
        try:
            r = set_value(dict(o, _details=details.get(o["name"]) or {}),
                          int(st.get("value", 100)), details)
            msgs.append("%s=%d%%(%s)" % (o["name"], r["value"], r["mode"]))
        except BrightError as e:
            msgs.append("%s 失败: %s" % (o["name"], e))
    if msgs:
        _log("启动恢复亮度: " + "，".join(msgs))
    return msgs
