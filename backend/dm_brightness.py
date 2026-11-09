#!/usr/bin/env python3
"""亮度调节：能力探测（DDC/CI、sysfs 背光、软件 gamma）、读写与持久化。

本机实测结论（2026-11-09，已装 ddcutil 1.4.1 + i2c-tools）：
- DP-0（AOC U27G3X，DisplayPort，ddcutil 报 card1-DP-1 / /dev/i2c-3）：DDC/CI 可读可写，
  `getvcp 10` 返回 0/100（0 疑似显示器 HDR/预设锁定，写 30 后读回 30 正常），走硬件 DDC。
- HDMI-0（便携屏 Sculptor）：ddcutil 列为 "Invalid display"，DDC 通信失败，只能软件调光。
- DP-4（eDP 内屏）：无 DDC；`/sys/class/backlight/nvidia_0/brightness` 是 root:video 0660。
  用户已加入 video 组并通过 udev `uaccess` 拿到 `/dev/i2c-*`，但**背光权限要重新登录**才生效；
  注意 nvidia 驱动对「写入相同值」返回 EINVAL，写前先比对（见 `_backlight_set`）。
- `/dev/i2c-*` 由 `/etc/udev/rules.d/60-i2c-uaccess.rules` 的 `TAG+="uaccess"` 授权，无需 sudo。

三条通道的优先级：硬件背光（内屏）/ DDC（外接）→ 软件 gamma → 不支持。
「软件调光」改的是显卡 gamma，亮度下降时对比度也会变，界面必须单独标注。

叠加（2026-11-10 起）：硬件通道仍是主调节，但 AOC 硬件 VCP 10 打到 0 后绝对亮度
仍偏高。所以硬件通道下额外允许一层「软件叠加」`extra`（0~100），只有硬件已经到 0
时才生效，gamma = 1 - 0.70 * extra/100（extra=100 → 0.30，即最低约压到 30% 白光）。
- 纯软件通道（便携屏）不叠加：主滑杆本身就是 gamma，再叠一层没有意义。
- 硬件非 0 时叠加一律撤销并把 gamma 复位 1.0，避免白损失对比度。
- 硬件写失败回落软件，记录里写 `fallback`，界面/CLI 明示原因。

夜间色温（2026-11-10 起）：AOC 实测 DDC 侧做不了暖色温——capabilities 只给
01 sRGB / 05 6500K / 06 7500K / 08 9300K / 0b User1，没有 4000K/5000K 档，且
0x14 / 0x16 / 0x18 / 0x1A 写入全部被显示器忽略（`--noverify` 也一样），capabilities
里也没有低蓝光 VCP。所以色温只能走软件 gamma 的 R/G/B 三通道比。
色温与「软件叠加」共用同一块 CRTC gamma 查找表，必须合成**一次** xrandr 调用
（`--brightness B --gamma r:g:b`）；分两次写会互相覆盖（实测 `--brightness` 会把
刚设好的 gamma 复位成 1:1:1）。硬件 DDC / 背光通道下色温照常生效（DDC 不动 gamma）。
GNOME 夜灯改的也是同一块表，两者互斥，界面只警告不覆盖用户设置。

持久化：~/.cache/display-manager.brightness.json，每块屏记
`{value, mode, extra, temp, time}`，硬件、叠加、色温三路值都落盘，
服务启动和「关→开」后都会重新下发。
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

DDC_TTL = 300         # ddcutil detect 结果缓存秒数（detect 约 0.6 秒，别每次都跑）
VALUE_TTL = 300       # 硬件读值缓存秒数，避免每 2 秒轮询都去读 DDC

I2C_HINT = (
    "sudo apt install ddcutil\n"
    "echo 'SUBSYSTEM==\"i2c-dev\", KERNEL==\"i2c-[0-9]*\", TAG+=\"uaccess\"' | "
    "sudo tee /etc/udev/rules.d/60-i2c-uaccess.rules\n"
    "sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=i2c-dev"
)
VIDEO_HINT = "sudo usermod -aG video $USER（重新登录后生效；之后自动改用硬件背光）"


def video_hint() -> str:
    """已经把用户加进 video 组时，提示改成「重新登录即可」，别让人重复执行。"""
    try:
        import grp
        g = grp.getgrnam("video")
        user = os.environ.get("USER") or ""
        groups = os.getgroups()
        if g.gr_gid in groups or (user and user in g.gr_mem):
            return "已加入 video 组，注销重新登录后自动生效（本会话仍是软件调光）"
    except (KeyError, OSError):
        pass
    return VIDEO_HINT


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


# 通道锁定：把某块屏钉死在某个通道。锁成 software 后整条亮度链路都不再调用 ddcutil。
# 背景（2026-11-10）：AOC 关掉 HDR 后所有 VCP 写入都被显示器忽略，原来「先试硬件、
# 失败再回落」会闪一下、慢半秒，还会把亮度带偏。DP-0 现在永久锁在 software，
# 界面不提供切回硬件的入口；配置里的 lock 字段保留，需要时可由 CLI 改动。
LOCK_SOFTWARE = "software"
_LOCK_MODES = ("software", "ddc", "backlight")
_KEEP = object()


def lock_of(name: str) -> str | None:
    """读取该输出的通道锁定值；没有锁定返回 None。"""
    st = _stored().get(name) or {}
    lock = st.get("lock")
    return lock if lock in _LOCK_MODES else None


def _store(name: str, value: int, mode: str, extra: int = 0,
           fallback: dict | None = None, temp: int | None = None,
           lock=_KEEP) -> None:
    """落盘。value 是主通道值（硬件百分比；纯软件时是 gamma 百分比），
    extra 是硬件到底后叠加的软件层（0 = 不叠加），temp 是夜间色温（K，6500 = 原生）。
    temp 不传时沿用上一次记录，避免调亮度顺手把色温清掉。
    lock 不传时沿用上一次的锁定；显式传 None 表示解除锁定。"""
    data = _stored()
    prev = data.get(name) or {}
    rec = {"value": int(value), "mode": mode, "extra": int(extra),
           "temp": _int_temp({"temp": temp}) if temp is not None else _int_temp(prev),
           "time": datetime.now().strftime("%F %T")}
    if fallback:
        rec["fallback"] = fallback
    lock_val = prev.get("lock") if lock is _KEEP else lock
    if lock_val in _LOCK_MODES:
        rec["lock"] = lock_val
    data[name] = rec
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
    if info.get("cur") == raw:
        # nvidia 驱动对「写入相同值」返回 EINVAL，先比对再写，避免误报失败
        return pct
    try:
        with open(info["path"], "w") as f:
            f.write(str(raw))
    except PermissionError:
        raise BrightError("没有写 %s 的权限（需要 video 组或 udev 规则）：%s"
                          % (info["path"], VIDEO_HINT))
    except OSError as e:
        # 个别驱动接受写入后会在 close/flush 上报 EIO/EINVAL，值已生效就按成功处理
        try:
            with open(info["path"]) as f:
                now = int(f.read().strip())
        except (OSError, ValueError):
            now = None
        if now != raw:
            raise BrightError("写 %s 失败：%s" % (info["path"], e))
    return pct


# --------------------------------------------------------------------------- #
# DDC/CI（ddcutil）
# --------------------------------------------------------------------------- #
def _parse_detect(text: str) -> tuple[list[dict], list[dict]]:
    """解析 `ddcutil detect`，返回 (可用显示器, 无效显示器)。

    注意 "Invalid display" 段同样会打印 I2C bus / DRM connector / Model，
    必须在这里结束上一段，否则会把有效屏的总线覆盖成无效屏的（本机实测踩过：
    AOC 被便携屏的 /dev/i2c-5 顶掉，界面还把便携屏误标成「硬件 DDC」）。
    """
    valid: list[dict] = []
    invalid: list[dict] = []
    cur = None
    for line in text.splitlines():
        m = re.match(r"^Display (\d+)\s*$", line)
        if m:
            cur = {"num": int(m.group(1)), "bus": None, "connector": None, "model": None}
            valid.append(cur)
            continue
        if line.startswith("Invalid display"):
            cur = {"num": None, "bus": None, "connector": None, "model": None}
            invalid.append(cur)
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
    return valid, invalid


def _ddc_state() -> dict:
    now = time.time()
    with _DDC_LOCK:
        if _DDC["info"] is not None and now - _DDC["ts"] < DDC_TTL:
            return _DDC["info"]
        info = {"ok": False, "reason": "", "hint": "", "displays": [], "invalid": []}
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
                valid, invalid = _parse_detect(out)
                info["displays"] = valid
                info["invalid"] = invalid
                if valid:
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


def _bus_num(bus: str) -> str:
    """ddcutil 的 --bus 只收数字，detect 打印的是 /dev/i2c-3 这样的路径。"""
    m = re.search(r"i2c-(\d+)", bus or "")
    return m.group(1) if m else str(bus or "")


def _ddc_get(bus: str) -> tuple[int, int]:
    rc, out, err = _run(["ddcutil", "--bus", _bus_num(bus), "--sleep-multiplier", "0.3",
                         "getvcp", "10", "--brief"], 8)
    if rc != 0:
        raise _ddc_error(err or out)
    m = re.search(r"VCP\s+10\s+\w+\s+(\d+)\s+(\d+)", out)
    if not m:
        raise BrightError("无法解析 getvcp 10 输出：%s" % out.strip())
    return int(m.group(1)), int(m.group(2))


def _ddc_set(bus: str, pct: int, verify: bool = True) -> None:
    cmd = ["ddcutil"]
    if not verify:
        # 拖动中的实时值不做读回校验（ddcutil 自带 verify 也会多一次读），
        # 松手后的最终值仍走完整校验。锁软件通道的屏根本不会走到这里。
        cmd.append("--noverify")
    cmd += ["--bus", _bus_num(bus), "--sleep-multiplier", "0.3",
            "setvcp", "10", str(pct)]
    rc, out, err = _run(cmd, 12)
    if rc != 0:
        raise _ddc_error(err or out)


# --------------------------------------------------------------------------- #
# 夜间色温 → RGB gamma
# --------------------------------------------------------------------------- #
# 6500K 为原生（1:1:1）；往下越暖，逐档压绿和蓝。前三档由实测观感推算，
# 3000K 为外推值，中间值线性插值。xrandr 的 --gamma 每个通道越小越暗。
TEMP_MIN, TEMP_MAX = 3000, 6500
TEMP_POINTS = [
    (6500, (1.00, 1.00, 1.00)),
    (5500, (1.00, 0.93, 0.82)),
    (4500, (1.00, 0.85, 0.68)),
    (3500, (1.00, 0.78, 0.55)),
    (3000, (1.00, 0.74, 0.48)),
]


def _clamp_temp(k) -> int:
    try:
        k = int(round(float(k)))
    except (TypeError, ValueError):
        return TEMP_MAX
    return max(TEMP_MIN, min(TEMP_MAX, k))


def _temp_gamma(k) -> tuple[float, float, float]:
    """色温（K）→ (R, G, B) gamma 三元组。"""
    k = _clamp_temp(k)
    for (k1, g1), (k2, g2) in zip(TEMP_POINTS, TEMP_POINTS[1:]):
        if k2 <= k <= k1:
            t = (k1 - k) / float(k1 - k2)
            return tuple(round(a + (b - a) * t, 2) for a, b in zip(g1, g2))
    return TEMP_POINTS[-1][1]


def _gamma_arg(k) -> str:
    r, g, b = _temp_gamma(k)
    return "%.2f:%.2f:%.2f" % (r, g, b)


def _int_temp(st: dict) -> int:
    try:
        return _clamp_temp((st or {}).get("temp", TEMP_MAX))
    except (TypeError, ValueError):
        return TEMP_MAX


def _temp_of(name: str) -> int:
    return _int_temp(_stored().get(name) or {})


# --------------------------------------------------------------------------- #
# 软件调光（xrandr gamma）
# --------------------------------------------------------------------------- #
def _sw_apply(name: str, level: float, temp: int | None = None) -> None:
    """一次 xrandr 调用同时下发亮度与色温。

    两者写的是同一块 CRTC gamma 查找表，分两次调用会互相复位（实测
    `--brightness` 会把刚设好的色温抹回 1:1:1），所以必须合成一次。
    level 是整体亮度系数（0.00~1.00），temp 是色温（K，None 表示沿用已落盘的值）。
    """
    if temp is None:
        temp = _temp_of(name)
    rc, out, err = _run(["xrandr", "--output", name,
                         "--brightness", "%.2f" % level,
                         "--gamma", _gamma_arg(temp)], 10)
    if rc != 0:
        detail = (err or out).strip()
        if "Need crtc" in detail:
            raise BrightError("%s 当前没有启用（没有 CRTC），gamma 调光无法生效" % name)
        raise BrightError("xrandr 设置 gamma 失败：%s" % (detail or "无输出"))


def _sw_set(name: str, pct: int, temp: int | None = None) -> None:
    _sw_apply(name, pct / 100.0, temp)


def _sw_reset(name: str, temp: int | None = None) -> None:
    """撤销软件压暗（亮度系数回 1.0），但保留夜间色温。"""
    try:
        _sw_apply(name, 1.0, temp)
    except BrightError:
        pass


# 叠加层最多把白光压到 30%（gamma 1.0 → 0.30），对应的对比度损失见 README
OVERLAY_DROP = 0.70

# 亮度下限（百分比）。SDR 下再往下基本等于黑屏：2026-11-10 关掉 AOC 的 HDR 后，
# DDC 写 0% 实测就是整块灰/黑，用户只能拿手机来中断操作。这里无条件限制——
# OS 侧读不到显示器 OSD 的 HDR 状态（X11 无 HDR 通路，DDC capabilities 里也没有
# 相关 feature），没法做「只有非 HDR 才限制」的判断。
MIN_VALUE = 20
# 硬件主值 × 叠加 gamma 的合成亮度下限（相对满亮度）。20% × 0.30 = 6% 太暗，
# 所以叠加层按这个下限反推一个上限，保证「硬件 20 + 叠加 100」不会压到看不见。
MIN_TOTAL = 0.10


def _overlay_gamma(extra: int) -> float:
    """extra 0~100 → gamma 1.00~0.30。"""
    extra = max(0, min(100, int(extra or 0)))
    return round(1.0 - OVERLAY_DROP * extra / 100.0, 2)


def _extra_cap(hw_pct) -> int:
    """当前硬件主值下叠加层的上限，保证合成亮度不低于 MIN_TOTAL。

    硬件 20% 时 gamma 最低只能到 0.50（额外叠加约 71%），再低整体就 <10% 了。
    """
    try:
        v = max(MIN_VALUE, min(100, int(hw_pct if hw_pct is not None else 100))) / 100.0
    except (TypeError, ValueError):
        v = 1.0
    gmin = MIN_TOTAL / v
    if gmin >= 1.0:
        return 0
    return max(0, min(100, int((1.0 - gmin) / OVERLAY_DROP * 100)))


def _int_extra(st: dict) -> int:
    try:
        return max(0, min(100, int((st or {}).get("extra") or 0)))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# 能力与读值
# --------------------------------------------------------------------------- #
def _pick_mode(o: dict) -> tuple[str, dict, str | None, str | None, str | None]:
    """返回 (mode, 附加信息, note, reason, hint)。mode ∈ ddc/backlight/software/none。"""
    name = o["name"]
    if lock_of(name) == LOCK_SOFTWARE:
        # 锁定后连 ddcutil detect 都不跑：不闪、不慢、不改动硬件值
        if not o.get("enabled"):
            return "none", {"locked": True}, None, "屏已关闭，gamma 调光无法生效", None
        return ("software", {"locked": True},
                "已锁定软件调光：固定使用 gamma，不再调用 ddcutil",
                None, None)

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
            hint = video_hint()
        elif not panel:
            bad = _map_displays([o], {name: details.get("edid")},
                                ddc.get("invalid") or []).get(name)
            if bad:
                note = "%s 不响应 DDC/CI（%s），只能软件调光" % (
                    name, bad.get("model") or "未知型号")
            elif ddc["ok"]:
                note = "DDC/CI 没匹配到这台显示器，暂用软件调光"
            else:
                note = "DDC 不可用：%s，暂用软件调光" % ddc["reason"]
                hint = ddc["hint"]
        return "software", {"bl": bl, "ddc": ddc}, note, None, hint

    # 屏已关闭：没有可用通道
    reason = "屏已关闭，gamma 调光无法生效"
    if panel and bl and not bl["writable"]:
        hint = video_hint()
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

# GNOME 夜灯读的是同一块 gamma 表，同时开就会互相覆盖。只读探测 + 10 秒缓存，
# 只提示、不替用户关。
_NL = {"info": None, "ts": 0.0}
_NL_LOCK = threading.Lock()


def night_light() -> dict:
    info = {"available": False, "enabled": False, "temperature": None,
            "note": "未检测到 GNOME 夜灯（gsettings 不可用）"}
    exe = shutil.which("gsettings")
    if not exe:
        return info
    rc, out, _ = _run([exe, "get", "org.gnome.settings-daemon.plugins.color",
                       "night-light-enabled"], 3)
    if rc != 0:
        return info
    info["available"] = True
    info["enabled"] = out.strip().lower() == "true"
    rc, out, _ = _run([exe, "get", "org.gnome.settings-daemon.plugins.color",
                       "night-light-temperature"], 3)
    # gsettings 输出形如 "uint32 3169"，别把类型前缀当成数值
    m = re.search(r"uint\d+\s+(\d+)", out or "")
    if m:
        info["temperature"] = int(m.group(1))
    if info["enabled"]:
        info["note"] = ("GNOME 夜灯已开启（%sK）。它和本页色温改的是同一块 gamma 表，"
                        "两者会互相覆盖，请二选一：要么关掉夜灯用这里，要么把这里还原成 6500K"
                        % (info["temperature"] or "?"))
    else:
        info["note"] = "GNOME 夜灯未开启，色温由本页独享"
    return info


def _night_light_cached() -> dict:
    now = time.time()
    with _NL_LOCK:
        if _NL["info"] is not None and now - _NL["ts"] < 10:
            return _NL["info"]
        info = night_light()
        _NL["info"] = info
        _NL["ts"] = now
        return info


def _entry(o: dict, details: dict, refresh: bool) -> dict:
    name = o["name"]
    o = dict(o, _details=details.get(name) or {})
    lock = lock_of(name)
    mode, info, note, reason, hint = _pick_mode(o)
    stored = _stored().get(name) or {}
    value = None
    fallback = None

    if mode == "ddc":
        if stored and not refresh and stored.get("mode") == "ddc":
            value = stored.get("value")
        else:
            cached = _VALUES.get(name)
            if cached and not refresh and time.time() - cached[1] < VALUE_TTL:
                value = cached[0]
            else:
                try:
                    value, _max = _ddc_get(info["ddc"]["bus"])
                    _VALUES[name] = (value, time.time())
                except BrightError as e:
                    mode, reason, hint = "none", str(e), None
    elif mode == "backlight":
        bl = info["bl"]
        cur = bl["actual"] if bl["actual"] is not None else bl["cur"]
        value = None if cur is None else int(round(cur * 100.0 / bl["max"]))
    elif mode == "software":
        # 锁定时只认落盘值，读也不去碰硬件
        if lock == LOCK_SOFTWARE or (stored and not refresh and stored.get("mode") == "software"):
            value = stored.get("value")
        else:
            g = (details.get(name) or {}).get("brightness")
            value = None if g is None else int(round(g * 100))
        if lock != LOCK_SOFTWARE and stored.get("mode") == "software" and stored.get("fallback"):
            fallback = stored["fallback"]

    if value is None and stored and mode != "none":
        value = stored.get("value")
    if value is None and mode != "none":
        value = 100
    if value is not None:
        value = max(0, min(100, int(value)))

    # 探测到的硬件能力（用于「下次是否重试硬件」）；mode 之后可能被回落改成 software
    capability = mode
    probe_value = value       # 硬件实际读到的值，回落时报告的实际通道值会覆盖 value

    # 夜间色温：和亮度共用同一块 gamma 表，最终由 _sw_apply 合成一次下发
    temp = _int_temp(stored)
    nl = _night_light_cached()
    if mode == "none":
        temp_note = None
    elif temp == TEMP_MAX:
        temp_note = "6500K 原生，不改色偏"
    else:
        temp_note = ("%dK → gamma %s（只改 R/G/B 比例，对比度影响很小）"
                     % (temp, _gamma_arg(temp)))

    # 叠加层：只有硬件通道、记录里的通道与当前探测一致、且主值在最低档时才回放
    extra = 0
    extra_cap = _extra_cap(value)
    if mode in ("ddc", "backlight") and stored.get("mode") == mode:
        extra = _int_extra(stored)
        if value is not None and value > MIN_VALUE:
            extra = 0          # 硬件不在最低档，叠加本来就不生效，别在界面上暗示它在起作用
        elif extra > extra_cap:
            note = "叠加 %d%% 会把整体压到 %.0f%%（低于 %d%% 下限），已按 %d%% 生效" % (
                extra, (MIN_VALUE / 100.0) * _overlay_gamma(extra) * 100,
                MIN_TOTAL * 100, extra_cap)
            extra = extra_cap

    # 上一次硬件写失败、实际走软件 gamma 的屏：照实报告当前通道，别让界面以为还在硬件上
    if mode in ("ddc", "backlight") and stored.get("fallback") \
            and stored["fallback"].get("from") == mode:
        fallback = stored["fallback"]
        extra = 0
        mode = "software"
        value = stored.get("value") if stored.get("value") is not None else value

    if mode == "none":
        label, gamma = MODE_LABEL["none"], None
    elif mode == "software":
        gamma = None if value is None else round(value / 100.0, 2)
        if lock == LOCK_SOFTWARE:
            label = "软件调光（已锁定，不影响对比度以外的东西）"
            note = "已锁定软件通道：固定使用 gamma，不再调用 ddcutil"
        else:
            label = MODE_LABEL["software"] + ("（硬件写失败回落）" if fallback else "（纯软件）")
            if fallback:
                note = "硬件 %s 写失败，已回落软件调光%s：%s" % (
                    fallback.get("from") or "通道",
                    "" if gamma is None else "（gamma %.2f）" % gamma,
                    fallback.get("reason") or "未知原因")
            elif not note:
                note = "纯软件通道：gamma 直接压暗，亮度越低对比度越差"
    else:
        gamma = _overlay_gamma(extra)
        if extra:
            label = "%s + 软件叠加 %d%%" % (MODE_LABEL[mode], extra)
            note = "硬件已到下限（%d%%）+ 软件叠加 %d%%：gamma %.2f，对比度约剩 %d%%%s" % (
                MIN_VALUE, extra, gamma, round(gamma * 100),
                "" if not note else "；" + note)
        else:
            label = MODE_LABEL[mode] + "（纯硬件）"
            if value is not None and value <= MIN_VALUE:
                note = "硬件已到下限 %d%%，还想更暗可拖动「软件叠加」（会压低对比度）" % MIN_VALUE
            elif note is None:
                note = "纯硬件调节，不压 gamma（对比度不受影响）"

    device = None
    if capability == "ddc":
        device = (info.get("ddc") or {}).get("bus")
    elif capability == "backlight":
        device = (info.get("bl") or {}).get("path")

    mode_label = label if lock == LOCK_SOFTWARE else MODE_LABEL[mode]
    return {
        "name": name,
        "mode": mode,
        "capability": capability,
        "mode_label": mode_label,
        "label": label,
        "hardware": mode in ("ddc", "backlight"),
        "available": mode != "none",
        "value": value,
        "probe_value": probe_value,
        "extra": extra,
        "gamma": gamma,
        "overlay_ready": mode in ("ddc", "backlight")
                          and value is not None and value <= MIN_VALUE,
        "overlay_cap": extra_cap,
        "min_value": MIN_VALUE,
        "floor_note": "下限 %d%%：再低在 SDR 下等于黑屏（HDR 关闭后实测）" % MIN_VALUE,
        "lock": lock,
        "temp": temp,
        "temp_label": "%dK" % temp if temp != TEMP_MAX else "6500K（原生）",
        "temp_gamma": list(_temp_gamma(temp)),
        "temp_ready": mode != "none",
        "temp_note": temp_note,
        "night_light": nl,
        "fallback": fallback,
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
                "name": o["name"], "mode": "none", "capability": "none",
                "mode_label": MODE_LABEL["none"],
                "label": MODE_LABEL["none"], "hardware": False, "available": False,
                "value": None, "probe_value": None, "extra": 0, "gamma": None,
                "overlay_ready": False, "overlay_cap": 0,
                "min_value": MIN_VALUE,
                "floor_note": "下限 %d%%：再低在 SDR 下等于黑屏（HDR 关闭后实测）" % MIN_VALUE,
                "lock": None,
                "temp": TEMP_MAX, "temp_label": "6500K（原生）",
                "temp_gamma": [1.0, 1.0, 1.0], "temp_ready": False,
                "temp_note": None, "night_light": _night_light_cached(),
                "fallback": None, "enabled": bool(o.get("enabled")), "device": None,
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


def _mode_changed(o: dict, details: dict) -> dict | None:
    """记录的通道与当前探测结果不一致时，以硬件当前值为准并更新记录。

    典型场景：刚装好 ddcutil / 刚拿到 video 组权限，上次记录还是「软件调光 100%」。
    如果照旧下发，会把 100 硬写进硬件通道，屏幕突然跳到最亮。这里改成采纳硬件
    当前值（并落盘），返回变化摘要；没变化返回 None。

    切到硬件通道时顺手把 gamma 复位，避免上一次的软件层（纯软件或叠加）留在硬件上。
    """
    st = _stored().get(o["name"]) or {}
    if not st:
        return None
    if lock_of(o["name"]):
        # 锁定通道后不再「采纳硬件当前值」，否则重启会把软件 80% 拉成硬件 100%
        return None
    # refresh=True 才会真的去读硬件；否则 _entry 会直接回放记录里的旧值
    entry = _entry(o, details, refresh=True)
    cap = entry.get("capability") or entry["mode"]
    if cap == "none" or st.get("mode") == cap:
        return None
    value = entry.get("probe_value") if cap in ("ddc", "backlight") else entry.get("value")
    stored_value = 100 if value is None else int(value)
    if stored_value < MIN_VALUE:
        # 采纳硬件当前值，但不能把屏幕留在下限以下（HDR 关掉后 0% 就是黑屏）
        _log("%s 硬件当前 %d%% 低于下限，抬到 %d%%" % (o["name"], stored_value, MIN_VALUE))
        stored_value = MIN_VALUE
        try:
            _apply_main(o, cap, stored_value, details)
        except BrightError as e:
            _log("%s 抬升到下限 %d%% 失败：%s" % (o["name"], MIN_VALUE, e))
    _store(o["name"], stored_value, cap, 0)
    if cap in ("ddc", "backlight") and (
            st.get("mode") == "software" or _int_extra(st)):
        _sw_reset(o["name"])
        _log("%s 切到 %s，gamma 复位 1.0（撤销上一次的软件层）" % (o["name"], cap))
    _log("%s 亮度通道 %s→%s，采用硬件当前值 %d%%（不下发旧值）"
         % (o["name"], st.get("mode"), cap, stored_value))
    return {"mode": cap, "value": stored_value, "prev": st.get("mode")}


def _apply_main(o: dict, mode: str, pct: int, details: dict,
                verify: bool = True) -> None:
    """下发主通道值（硬件 DDC / 硬件背光 / 纯软件 gamma）。

    硬件两条通道写完都读回校验：面板固件可能收下指令却不改值（AOC 关 HDR 后
    就出现过整条 DDC 写不进去的情况），只发不验会让界面显示「已设置」而屏幕没动。
    读回不一致时在同一次调用里还原上一值，并把还原结果写进异常信息。

    verify=False 只给拖动中的实时值用：跳过读回，追求低延迟；松手后的最终值
    仍走 verify=True，由它负责校验、还原与回落。
    """
    if mode == "ddc":
        disp = _map_displays([o], {o["name"]: (details.get(o["name"]) or {}).get("edid")},
                             _ddc_state()["displays"]).get(o["name"])
        if not disp or not disp.get("bus"):
            raise BrightError("没找到 %s 对应的 ddcutil 显示器编号" % o["name"])
        bus = disp["bus"]
        if not verify:
            _ddc_set(bus, pct, verify=False)
            _VALUES[o["name"]] = (pct, time.time())
            return
        before = None
        try:
            before, _mx = _ddc_get(bus)
        except BrightError:
            before = None                 # 读不到就算了，后面按「未知原值」处理
        try:
            _ddc_set(bus, pct)
        except BrightError as e:
            cur = _ddc_quiet(bus)
            if before is not None and cur is not None and cur != before:
                raise BrightError("%s；读回 %d%%，%s" % (e, cur, _ddc_restore(bus, before)))
            raise
        got = _ddc_quiet(bus)
        if got is None:
            raise BrightError("DDC 写入后读回超时（目标 %d%%），无法确认是否生效" % pct)
        if got != pct:
            raise BrightError("DDC 写入后读回 %d%%（目标 %d%%），写入被忽略；%s"
                              % (got, pct, _ddc_restore(bus, before)))
        _VALUES[o["name"]] = (pct, time.time())
    elif mode == "backlight":
        bl = backlight_info()
        if not bl:
            raise BrightError("没有可用的 /sys/class/backlight 设备")
        _backlight_set(bl, pct)
        if not verify:
            return
        raw_before = bl.get("cur")
        before = (int(round(raw_before * 100.0 / bl["max"]))
                  if raw_before is not None else None)
        got = _backlight_read(bl)
        if got is None:
            raise BrightError("背光写入后读回超时（目标 %d%%），无法确认是否生效" % pct)
        if got != pct:
            raise BrightError("背光写入后读回 %d%%（目标 %d%%）；%s"
                              % (got, pct, _backlight_restore(bl, before)))
    else:
        _sw_set(o["name"], pct)


def _ddc_quiet(bus: str) -> int | None:
    """读回当前亮度，失败返回 None（用于校验，不再抛异常）。"""
    try:
        cur, _mx = _ddc_get(bus)
        return cur
    except BrightError:
        return None


def _ddc_restore(bus: str, before: int | None) -> str:
    if before is None:
        return "原值未知，未做还原"
    if _ddc_set_quiet(bus, before):
        return "已还原为 %d%%" % before
    return "还原为 %d%% 也失败（屏幕可能仍停在写入后的状态）" % before


def _ddc_set_quiet(bus: str, pct: int) -> bool:
    try:
        _ddc_set(bus, pct)
        return True
    except BrightError:
        return False


def _backlight_read(bl: dict) -> int | None:
    """读回背光百分比；nvidia 驱动偶尔要等一拍，最多重试 3 次。"""
    for i in range(3):
        try:
            with open(bl["path"]) as f:
                raw = int(f.read().strip())
            return int(round(raw * 100.0 / bl["max"]))
        except (OSError, ValueError):
            pass
        if i < 2:
            time.sleep(0.1)
    return None


def _backlight_restore(bl: dict, before: int | None) -> str:
    if before is None:
        return "原值未知，未做还原"
    try:
        _backlight_set(bl, int(before))
        return "已还原为 %d%%" % before
    except BrightError as e:
        return "还原为 %d%% 也失败：%s" % (before, e)


def _fallback_to_software(o: dict, pct: int, mode: str, details: dict,
                          err: BrightError, hw_value) -> dict:
    """硬件写失败：就地回落软件 gamma，并把原因写进记录，界面/CLI 明示。"""
    try:
        _sw_set(o["name"], pct)
    except BrightError as e2:
        raise BrightError("%s；软件回落也失败：%s" % (err, e2))
    _store(o["name"], pct, "software", 0,
           fallback={"from": mode, "reason": str(err), "hardware": hw_value,
                     "time": datetime.now().strftime("%F %T")})
    _log("%s %s 写失败（%s），已回落软件调光 %d%%" % (
        o["name"], MODE_LABEL.get(mode, mode), err, pct))
    return _entry(o, details, refresh=False)


def set_value(o: dict, pct: int, details: dict | None = None,
              extra: int | None = None, allow_clamp: bool = False,
              live: bool = False) -> dict:
    """设置亮度（MIN_VALUE~100）。

    extra 语义：None = 不改动已有的软件叠加（硬件回到非最低档时会自动撤销），
    0~100 = 显式指定叠加值；只有硬件已经到下限 MIN_VALUE 才生效，否则忽略并写日志。
    allow_clamp=True 只给启动/重登的恢复路径用：历史记录里可能存着低于下限的值
    （HDR 关闭前 0% 并不黑），那时抬到 MIN_VALUE 并记日志，而不是报错。
    live=True 是拖动中的实时值：跳过硬件读回与失败回落，只保证低延迟；
    松手后的最终值用 live=False，仍然完整校验并负责回弹。
    """
    if not isinstance(pct, int) or not (0 <= pct <= 100):
        raise BrightError("亮度必须是 0~100 的整数")
    if pct < MIN_VALUE:
        if not allow_clamp:
            raise BrightError(
                "亮度不能低于 %d%%：SDR 下再低等于黑屏（当前请求 %d%%）" % (MIN_VALUE, pct))
        _log("%s 历史亮度 %d%% 低于下限，按 %d%% 抬升后下发" % (o["name"], pct, MIN_VALUE))
        pct = MIN_VALUE
    if extra is not None and (not isinstance(extra, int) or not (0 <= extra <= 100)):
        raise BrightError("软件叠加必须是 0~100 的整数")
    if details is None:
        # 锁软件通道的屏不需要 xrandr --verbose 的色温/EDID 细节，拖动时省掉这一下
        details = {} if lock_of(o["name"]) == LOCK_SOFTWARE else _details()
    entry = _entry(o, details, refresh=False)
    # 用「探测到的能力」决定怎么下发：上一次硬件写失败（当前 mode=software）时，
    # 用户再拖一次应该重试硬件，而不是永久留在软件回落里。
    mode = entry.get("capability") or entry["mode"]
    if mode == "none":
        raise BrightError(entry["reason"] or "这块屏不支持亮度调节")

    stored = _stored().get(o["name"]) or {}
    prev_mode = stored.get("mode")
    prev_extra = _int_extra(stored)

    try:
        _apply_main(o, mode, pct, details, verify=not live)
    except BrightError as e:
        if live or mode == "software":
            # 实时值失败/纯软件通道再回落就没得落了：交给最终值或界面报错
            raise
        return _fallback_to_software(o, pct, mode, details, e, entry.get("value"))

    # 从纯软件/回落通道切到硬件：先把上一次的软件 gamma 清掉（叠加层随后会重新下发）
    if prev_mode == "software" and mode != "software":
        _sw_reset(o["name"])
        _log("亮度通道从软件 gamma 切到 %s，已把 gamma 复位为 1.0" % mode)

    # 叠加层：硬件通道、且主值已在最低档（MIN_VALUE）时才生效
    new_extra = 0
    extra_cap = _extra_cap(pct)
    if mode in ("ddc", "backlight"):
        if extra is not None:
            if pct <= MIN_VALUE:
                new_extra = min(extra, extra_cap)
                if new_extra != extra:
                    _log("%s 硬件 %d%% 下叠加最多 %d%%（再高整体低于 %d%%），%d%% 按 %d%% 生效"
                         % (o["name"], pct, extra_cap, MIN_TOTAL * 100, extra, new_extra))
            elif extra:
                _log("%s 硬件亮度 %d%% 未到下限，忽略软件叠加 %d%%"
                     % (o["name"], pct, extra))
        elif pct <= MIN_VALUE and prev_mode == mode:
            new_extra = min(prev_extra, extra_cap)

    try:
        if new_extra:
            _sw_apply(o["name"], _overlay_gamma(new_extra))
        elif mode in ("ddc", "backlight") and prev_extra and pct > MIN_VALUE:
            _sw_reset(o["name"])
            _log("%s 硬件亮度回到 %d%%，撤销软件叠加，gamma 复位 1.0" % (o["name"], pct))
    except BrightError as e:
        if extra:                       # 用户显式要叠加，失败必须让他看到
            raise
        _log("%s 恢复软件叠加失败（按无叠加处理）：%s" % (o["name"], e))
        new_extra = 0

    _store(o["name"], pct, mode, new_extra)
    if not live:                        # 拖动中的实时值不刷日志，避免每 60ms 一行
        _log("亮度 %s = %d%%（%s%s）" % (
            o["name"], pct, mode,
            "，软件叠加 %d%%（gamma %.2f）" % (new_extra, _overlay_gamma(new_extra))
            if new_extra else ""))
    return _entry(o, details, refresh=False)


def set_extra(o: dict, extra: int, details: dict | None = None) -> dict:
    """只调软件叠加层（硬件必须已经在最低档 MIN_VALUE）。"""
    if not isinstance(extra, int) or not (0 <= extra <= 100):
        raise BrightError("软件叠加必须是 0~100 的整数")
    details = details if details is not None else _details()
    entry = _entry(o, details, refresh=False)
    if entry["mode"] == "none":
        raise BrightError(entry["reason"] or "这块屏不支持亮度调节")
    if entry["mode"] == "software":
        if entry.get("fallback"):
            raise BrightError("%s 上一次硬件写失败，当前在软件回落通道；"
                              "先拖一下主滑杆重试硬件，成功后才能叠加" % o["name"])
        raise BrightError("%s 当前是纯软件调光，主滑杆本身就是 gamma，无需叠加"
                          % o["name"])
    hw_value = MIN_VALUE if entry.get("value") is None else int(entry["value"])
    if hw_value > MIN_VALUE:
        raise BrightError("%s 硬件亮度还有 %d%%，先把主滑杆拖到 %d%% 再叠加"
                          % (o["name"], hw_value, MIN_VALUE))
    cap = _extra_cap(hw_value)
    if extra > cap:
        _log("%s 硬件 %d%% 下叠加最多 %d%%（再高整体低于 %d%%），%d%% 按 %d%% 生效"
             % (o["name"], hw_value, cap, MIN_TOTAL * 100, extra, cap))
        extra = cap
    if extra:
        _sw_apply(o["name"], _overlay_gamma(extra))
    else:
        _sw_reset(o["name"])
    _store(o["name"], hw_value, entry["mode"], extra)
    _log("%s 软件叠加 = %d%%（gamma %.2f，硬件 %d%%）"
         % (o["name"], extra, _overlay_gamma(extra), hw_value))
    return _entry(o, details, refresh=False)


def set_temp(o: dict, kelvin, details: dict | None = None) -> dict:
    """设置夜间色温（3000~6500K，6500 = 原生）。

    色温走软件 gamma，和硬件 DDC / 背光通道不冲突；与「软件叠加」共用一块
    gamma 表，所以这里按当前的亮度层算出整体系数，合成一次 xrandr 调用下发。
    """
    temp = _clamp_temp(kelvin)
    details = details if details is not None else _details()
    entry = _entry(o, details, refresh=False)
    if entry["mode"] == "none":
        raise BrightError(entry["reason"] or "这块屏不支持色温调节")

    mode = entry.get("capability") or entry["mode"]
    value = entry.get("value")
    extra = int(entry.get("extra") or 0)
    if mode == "software":
        level = (100 if value is None else max(0, min(100, value))) / 100.0
    else:
        level = _overlay_gamma(extra) if extra else 1.0
    _sw_apply(o["name"], level, temp)
    _store(o["name"], 100 if value is None else int(value), mode, extra, temp=temp)
    _log("%s 夜间色温 = %dK（gamma %s，亮度层 %.2f）"
         % (o["name"], temp, _gamma_arg(temp), level))
    return _entry(o, details, refresh=False)


def lock_software(o: dict, details: dict | None = None) -> dict:
    """把输出钉死在软件通道：立即按落盘值下发 gamma，之后不再调用 ddcutil。

    锁定是用户显式动作，允许在这一刻按需探测一次当前值；锁上以后连 detect 都不跑。
    """
    name = o["name"]
    details = details if details is not None else _details()
    st = _stored().get(name) or {}
    value = st.get("value")
    if value is None:
        try:
            value = _entry(o, details, refresh=True).get("value")
        except BrightError:
            value = None
    value = max(MIN_VALUE, min(100, int(value if value is not None else 100)))
    temp = _int_temp(st)
    _sw_apply(name, value / 100.0, temp)
    _store(name, value, "software", 0, temp=temp, lock=LOCK_SOFTWARE)
    _log("%s 已锁定软件调光 %d%%（此后不再调用 ddcutil）" % (name, value))
    return _entry(o, details, refresh=False)


def unlock_channel(o: dict, details: dict | None = None) -> dict:
    """解除通道锁定，恢复自动探测；不主动写硬件。"""
    name = o["name"]
    details = details if details is not None else _details()
    st = _stored().get(name) or {}
    value = max(MIN_VALUE, min(100, int(st.get("value", 100))))
    mode = st.get("mode") if st.get("mode") in ("software", "ddc", "backlight") else "software"
    _store(name, value, mode, _int_extra(st), lock=None)
    _log("%s 已解除通道锁定，恢复自动判断（下次探测按硬件能力决定）" % name)
    return _entry(o, details, refresh=False)


def reapply_for(o: dict) -> dict | None:
    """屏幕重新打开后把上次的值再下发一次（gamma 会随模式设置被复位）。"""
    st = _stored().get(o["name"])
    if not st or not o.get("enabled"):
        return None
    details = _details()
    if _mode_changed(o, details):
        return None
    try:
        return set_value(o, int(st.get("value", 100)), details,
                         extra=_int_extra(st), allow_clamp=True)
    except BrightError as e:
        _log("重新应用 %s 亮度失败：%s" % (o["name"], e))
        return None


def reapply_software() -> list[str]:
    """任何 xrandr 布局变化都会把 gamma 复位；给「软件调光」和「硬件 + 叠加」的屏重新下发。

    硬件通道的主值（DDC / 背光）不随布局变化，不用重发（DDC 单次上百毫秒，能省则省），
    但叠加层走的是 gamma，必须补回来，否则一次布局变化就把叠加花掉了。
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
        if not st:
            continue
        extra = _int_extra(st)
        temp = _int_temp(st)
        raw = int(st.get("value", 100))
        val = max(MIN_VALUE, min(100, raw))
        if val != raw:
            _log("%s 历史亮度 %d%% 低于下限，按 %d%% 恢复" % (o["name"], raw, val))
            _store(o["name"], val, st.get("mode") or "software", extra, temp=temp)
        cap = _extra_cap(val)
        if extra > cap:
            _log("%s 硬件 %d%% 下叠加上限 %d%%，%d%% 按 %d%% 恢复"
                 % (o["name"], val, cap, extra, cap))
            extra = cap
            _store(o["name"], val, st.get("mode") or "software", extra, temp=temp)
        try:
            if st.get("mode") == "software":
                _sw_set(o["name"], val, temp)
                done.append("%s %d%%%s" % (
                    o["name"], val, "，%dK" % temp if temp != TEMP_MAX else ""))
            elif extra:
                _sw_apply(o["name"], _overlay_gamma(extra), temp)
                done.append("%s 叠加 %d%%%s" % (
                    o["name"], extra, "，%dK" % temp if temp != TEMP_MAX else ""))
            elif temp != TEMP_MAX:
                _sw_apply(o["name"], 1.0, temp)
                done.append("%s 色温 %dK" % (o["name"], temp))
        except BrightError as e:
            _log("布局变化后恢复 %s 软件层失败：%s" % (o["name"], e))
    if done:
        _log("布局变化后重新应用软件层: " + "，".join(done))
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
        changed = _mode_changed(dict(o, _details=details.get(o["name"]) or {}), details)
        if changed:
            msgs.append("%s 通道→%s，用当前值 %d%%" % (o["name"], changed["mode"], changed["value"]))
            continue
        try:
            r = set_value(dict(o, _details=details.get(o["name"]) or {}),
                          int(st.get("value", 100)), details,
                          extra=_int_extra(st), allow_clamp=True)
            msgs.append("%s=%d%%(%s%s%s)" % (
                o["name"], r["value"], r["mode"],
                "，叠加 %d%%" % r["extra"] if r.get("extra") else "",
                "，色温 %dK" % r["temp"] if r.get("temp", TEMP_MAX) != TEMP_MAX else ""))
        except BrightError as e:
            msgs.append("%s 失败: %s" % (o["name"], e))
    if msgs:
        _log("启动恢复亮度: " + "，".join(msgs))
    return msgs
