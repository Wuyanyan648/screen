#!/usr/bin/env python3
"""按输出统计「画面多久没变化」：X11 Damage 扩展 + XScreenSaver 全局空闲 双路信号。

原理与局限（务必知悉，界面上也会展示判断依据）：
- X11 没有「某输出是否更新」的接口。这里在根窗口上开 XDamage（RawRectangles），
  把每条脏矩形按各输出的 xrandr 几何做交集，近似判定这块屏有没有画面变化。
- 局限 1：静态画面（壁纸、静止的聊天窗口）不会有 Damage，会被判为「没变化」；
- 局限 2：鼠标在别的屏上移动不会给这块屏产生 Damage（这正是我们要的效果），
  但也意味着「屏亮着、没人看」同样会被判闲置；
- 局限 3：视频若走硬件叠加层/直接扫描输出，可能完全不产生 Damage；
- 局限 4：GNOME 顶栏时钟每分钟跳一次，会给「顶栏所在那块屏」持续制造 Damage，
  所以那块屏可能永远不进入闲置提醒；
- 局限 5：mutter 的合成器如果绕过根窗口 Damage，则本机可能拿不到逐屏信号，
  此时自动降级为 XScreenSaver 全局空闲（无法区分是哪块屏），界面会标注依据。

CPU 保护：只累计「受影响输出的时间戳」，不逐事件保存；一次唤醒最多消化
MAX_EVENTS 条事件，剩余留到下一轮（配合 select 与 0.5s 超时），事件洪峰时也不会吃满 CPU。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import select
import sys
import threading
import time

# XDamage 报告级别
XDamageReportRawRectangles = 0


class _XScreenSaverInfo(ctypes.Structure):
    _fields_ = [
        ("window", ctypes.c_ulong),
        ("state", ctypes.c_int),
        ("kind", ctypes.c_int),
        ("til_or_since", ctypes.c_ulong),
        ("idle", ctypes.c_ulong),
        ("eventMask", ctypes.c_ulong),
    ]


class _XDamageNotifyEvent(ctypes.Structure):
    """XDamageNotifyEvent 的前半段（XEvent 是 192 字节的联合体，这里只取需要的字段）。"""
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("drawable", ctypes.c_ulong),
        ("level", ctypes.c_int),
        ("more", ctypes.c_int),
        ("timestamp", ctypes.c_ulong),
        ("area_x", ctypes.c_ushort),
        ("area_y", ctypes.c_ushort),
        ("area_w", ctypes.c_ushort),
        ("area_h", ctypes.c_ushort),
    ]


MAX_EVENTS = 800          # 单轮最多处理的事件数，剩余的下一轮继续
RECT_REFRESH = 5.0        # 多久重新读一次各输出的几何（秒）


class DamageMonitor(threading.Thread):
    """后台线程：维护 {输出名: 最后一次画面变化的时刻}。"""

    def __init__(self, rect_provider):
        super().__init__(daemon=True, name="dm-damage")
        self._rect_provider = rect_provider      # () -> {name: (x, y, w, h)}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._changed: dict[str, float] = {}
        self._rects: dict[str, tuple[int, int, int, int]] = {}
        self._rects_ts = 0.0
        self.damage_ok = False
        self.ss_ok = False
        self.global_idle = None                  # 秒，XScreenSaver 的全局空闲
        self.error = None
        self.started_at = time.time()

    # ---- 对外读取 ----
    def report(self) -> dict:
        now = time.time()
        with self._lock:
            changed = dict(self._changed)
        if self.damage_ok:
            basis = "XDamage 变化（逐屏近似）"
        elif self.ss_ok:
            basis = "XScreenSaver 全局空闲（无法区分是哪块屏）"
        else:
            basis = "不可用"
        return {
            "available": self.damage_ok or self.ss_ok,
            "basis": basis,
            "damage_ok": self.damage_ok,
            "screensaver_ok": self.ss_ok,
            "last_change": changed,
            "global_idle": self.global_idle,
            "error": self.error,
            "coarse": not self.damage_ok and self.ss_ok,
            "uptime": now - self.started_at,
        }

    def stop(self) -> None:
        self._stop.set()

    # ---- 内部 ----
    def _refresh_rects(self) -> None:
        if time.time() - self._rects_ts < RECT_REFRESH:
            return
        self._rects_ts = time.time()
        try:
            rects = self._rect_provider()
        except Exception:
            return
        now = time.time()
        with self._lock:
            self._rects = rects
            for n in rects:
                self._changed.setdefault(n, now)   # 新出现的输出，从现在开始计时
            for n in list(self._changed):
                if n not in rects:                 # 已关闭/断开：清掉旧时间戳
                    self._changed.pop(n, None)

    def run(self) -> None:
        try:
            self._loop()
        except Exception as e:                     # 线程内异常不能让整个服务挂掉
            self.error = "%s: %s" % (type(e).__name__, e)
            self.damage_ok = False

    def _loop(self) -> None:
        x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XConnectionNumber.restype = ctypes.c_int
        x11.XConnectionNumber.argtypes = [ctypes.c_void_p]
        x11.XPending.restype = ctypes.c_int
        x11.XPending.argtypes = [ctypes.c_void_p]
        x11.XNextEvent.restype = ctypes.c_int
        x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]

        dpy = x11.XOpenDisplay(None)
        if not dpy:
            raise RuntimeError("XOpenDisplay 失败（DISPLAY=%s）" % os.environ.get("DISPLAY", ""))

        root = x11.XDefaultRootWindow(dpy)
        ev_base = ctypes.c_int(0)
        err_base = ctypes.c_int(0)

        damage = None
        xdamage = None
        lib = ctypes.util.find_library("Xdamage")
        if lib:
            xdamage = ctypes.CDLL(lib)
            xdamage.XDamageQueryExtension.restype = ctypes.c_int
            xdamage.XDamageQueryExtension.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
            xdamage.XDamageCreate.restype = ctypes.c_ulong
            xdamage.XDamageCreate.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int]
            xdamage.XDamageSubtract.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
            if xdamage.XDamageQueryExtension(dpy, ctypes.byref(ev_base), ctypes.byref(err_base)):
                damage = xdamage.XDamageCreate(dpy, root, XDamageReportRawRectangles)
                x11.XSync(dpy, 0)
                self.damage_ok = damage != 0

        info = None
        xss = None
        lib = ctypes.util.find_library("Xss")
        if lib:
            xss = ctypes.CDLL(lib)
            xss.XScreenSaverQueryExtension.restype = ctypes.c_int
            xss.XScreenSaverQueryExtension.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
            xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(_XScreenSaverInfo)
            xss.XScreenSaverQueryInfo.restype = ctypes.c_int
            xss.XScreenSaverQueryInfo.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_XScreenSaverInfo)]
            eb, er = ctypes.c_int(0), ctypes.c_int(0)
            if xss.XScreenSaverQueryExtension(dpy, ctypes.byref(eb), ctypes.byref(er)):
                info = xss.XScreenSaverAllocInfo()
                self.ss_ok = info is not None

        fd = x11.XConnectionNumber(dpy)
        evbuf = (ctypes.c_char * 192)()

        self._refresh_rects()
        with self._lock:
            now = time.time()
            for n in self._rects:
                self._changed.setdefault(n, now)

        while not self._stop.is_set():
            self._refresh_rects()

            if self.damage_ok:
                readable, _, _ = select.select([fd], [], [], 0.5)
                if readable:
                    self._drain(x11, xdamage, dpy, damage, ev_base, evbuf)
            else:
                time.sleep(0.5)

            if info is not None:
                if xss.XScreenSaverQueryInfo(dpy, root, info):
                    self.global_idle = info.contents.idle / 1000.0

        # 退出清理
        if self.damage_ok:
            try:
                xdamage.XDamageDestroy = getattr(xdamage, "XDamageDestroy", None) or xdamage.XDamageDestroy
            except Exception:
                pass

    def _drain(self, x11, xdamage, dpy, damage, ev_base, evbuf) -> None:
        now = time.time()
        touched: set[str] = set()
        n = 0
        with self._lock:
            rects = dict(self._rects)
        while n < MAX_EVENTS and x11.XPending(dpy) > 0:
            x11.XNextEvent(dpy, ctypes.byref(evbuf))
            ev = ctypes.cast(ctypes.byref(evbuf),
                             ctypes.POINTER(_XDamageNotifyEvent)).contents
            if ev.type == ev_base.value + 0:      # XDamageNotify
                ax, ay = ev.area_x, ev.area_y
                aw, ah = ev.area_w, ev.area_h
                for name, (x, y, w, h) in rects.items():
                    if ax < x + w and x < ax + aw and ay < y + h and y < ay + ah:
                        touched.add(name)
            n += 1
        try:
            xdamage.XDamageSubtract(dpy, damage, None, None)
            x11.XFlush(dpy)
        except Exception:
            pass
        if touched:
            with self._lock:
                for name in touched:
                    self._changed[name] = now


# --------------------------------------------------------------------------- #
# 独立运行（排障用）：每隔 1 秒打印各输出的「多久没变化」
# --------------------------------------------------------------------------- #
def _watch() -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import dm_core as core

    def rects():
        return {o["name"]: (o["x"], o["y"], o["w"], o["h"])
                for o in core.screen_rects()}

    mon = DamageMonitor(rects)
    mon.start()
    try:
        while True:
            time.sleep(1.0)
            r = mon.report()
            now = time.time()
            parts = []
            for name, ts in sorted(r["last_change"].items()):
                parts.append("%s:%4.0fs" % (name, now - ts))
            print("%s | damage=%s ss=%s gidle=%s | %s" % (
                r["basis"], r["damage_ok"], r["screensaver_ok"], r["global_idle"],
                "  ".join(parts)), flush=True)
    except KeyboardInterrupt:
        mon.stop()
        return 0


if __name__ == "__main__":
    sys.exit(_watch())
