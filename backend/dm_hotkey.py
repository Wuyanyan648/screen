#!/usr/bin/env python3
"""全局快捷键：Ctrl+Alt+Shift+F12 → 恢复全部输出。

用 X11 的 XGrabKey 实现，不装 xbindkeys / xdotool，也不改 GNOME 的快捷键设置。
注意：不要用 Ctrl+Alt+Shift+R —— 那是 GNOME 自带的录屏快捷键，会被 Shell 先占用。
- 只在 X11 会话下有效；Wayland 下抓不到键，会把原因写进日志。
- 组合键已被别的程序占用时 Grab 会失败（BadAccess），记日志但不影响主服务。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import select
import sys
import threading

# X11 修饰键常量
ShiftMask = 1 << 0
LockMask = 1 << 1
ControlMask = 1 << 2
Mod1Mask = 1 << 3          # Alt
Mod2Mask = 1 << 4          # NumLock
KEY_COMBO = "Ctrl+Alt+Shift+F12"
KEY_SYM = b"F12"
MODS = ControlMask | Mod1Mask | ShiftMask
KEY_PRESS = 2


class _XKeyEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("x_root", ctypes.c_int),
        ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("keycode", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


class _XErrorEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("resourceid", ctypes.c_ulong),
        ("serial", ctypes.c_ulong),
        ("error_code", ctypes.c_ubyte),
        ("request_code", ctypes.c_ubyte),
        ("minor_code", ctypes.c_ubyte),
    ]


_XErrorHandler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                  ctypes.POINTER(_XErrorEvent))


class HotkeyListener(threading.Thread):
    def __init__(self, callback):
        super().__init__(daemon=True, name="dm-hotkey")
        self._callback = callback
        self._stop = threading.Event()
        self.ok = False
        self.error = None
        self.combo = KEY_COMBO

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            self._loop()
        except Exception as e:
            self.error = "%s: %s" % (type(e).__name__, e)
            self.ok = False

    def _loop(self) -> None:
        x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XStringToKeysym.restype = ctypes.c_ulong
        x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
        x11.XKeysymToKeycode.restype = ctypes.c_ubyte
        x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XGrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint,
                                 ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        x11.XUngrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong]
        x11.XConnectionNumber.restype = ctypes.c_int
        x11.XConnectionNumber.argtypes = [ctypes.c_void_p]
        x11.XPending.restype = ctypes.c_int
        x11.XPending.argtypes = [ctypes.c_void_p]
        x11.XNextEvent.restype = ctypes.c_int
        x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]

        dpy = x11.XOpenDisplay(None)
        if not dpy:
            raise RuntimeError("XOpenDisplay 失败（快捷键需要 X11 会话）")

        root = x11.XDefaultRootWindow(dpy)
        keycode = x11.XKeysymToKeycode(dpy, x11.XStringToKeysym(KEY_SYM))
        if not keycode:
            raise RuntimeError("找不到 %s 键的 keycode" % KEY_COMBO.split("+")[-1])

        # 临时接管 X 错误回调，用来发现「组合键已被别的程序占用」（BadAccess = 10）
        grab_errors = []

        def _on_x_error(_dpy, ev):
            grab_errors.append(int(ev.contents.error_code))
            return 0

        err_cb = _XErrorHandler(_on_x_error)
        x11.XSetErrorHandler.restype = ctypes.c_void_p
        x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]
        prev = x11.XSetErrorHandler(ctypes.cast(err_cb, ctypes.c_void_p))

        # 同时抓 NumLock / CapsLock 的各种组合，否则开着大写锁就按不出来
        masks = [MODS, MODS | LockMask, MODS | Mod2Mask, MODS | LockMask | Mod2Mask]
        for m in masks:
            x11.XGrabKey(dpy, keycode, m, root, 1, 1, 1)
        x11.XSync(dpy, 0)
        x11.XSetErrorHandler(prev)
        if grab_errors:
            raise RuntimeError("XGrabKey 失败，组合键可能已被占用（error_code=%s）"
                               % ",".join(str(e) for e in grab_errors))
        self.ok = True

        fd = x11.XConnectionNumber(dpy)
        evbuf = (ctypes.c_char * 192)()

        while not self._stop.is_set():
            readable, _, _ = select.select([fd], [], [], 0.5)
            if not readable:
                continue
            while x11.XPending(dpy) > 0:
                x11.XNextEvent(dpy, ctypes.byref(evbuf))
                ev = ctypes.cast(ctypes.byref(evbuf), ctypes.POINTER(_XKeyEvent)).contents
                if ev.type == KEY_PRESS:
                    try:
                        self._callback()
                    except Exception:
                        pass  # 回调内部自己记日志，这里绝不让监听线程挂掉


def _main() -> int:
    """排障用：前台运行，按下快捷键时打印一行。"""
    import time

    def cb():
        print("%.0f 快捷键触发 %s" % (time.time(), KEY_COMBO), flush=True)

    hk = HotkeyListener(cb)
    hk.start()
    time.sleep(0.6)
    print("注册 %s: ok=%s error=%s（Ctrl-C 退出）" % (KEY_COMBO, hk.ok, hk.error), flush=True)
    try:
        while True:
            time.sleep(1)
            if not hk.is_alive():
                print("监听线程已退出: %s" % hk.error, flush=True)
                return 1
    except KeyboardInterrupt:
        hk.stop()
        return 0


if __name__ == "__main__":
    sys.exit(_main())
