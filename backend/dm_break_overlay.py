#!/usr/bin/env python3
"""护眼休息覆盖层（独立进程）：一块屏、无边框、置顶、低刺激。

几何由服务端从 xrandr 的 CRTC 矩形直接传入（--x/--y/--w/--h），
这里不推算缩放/布局；多屏与分数缩放下位置由调用方保证。

视觉规格（刻意做得「无聊」，见 README）：
- 背景只在 #12161A ~ #1B2328 之间极缓慢过渡（40 秒一个周期，0.8 秒才重绘一次），
  不用纯黑、不用白色大面积背景；
- 引导文字 #9AA6AE、倒计时 #C6CFD5，不用纯白、不加粗；文字亮度随当前屏幕亮度
  轻度下调（--dim），不会比桌面更亮；
- 跟随当前色温（--warm）：用户把色温调暖后，这里不会突然变回冷白；
- 动画只有缓慢渐变与一条 10 秒周期的「呼吸」细线，无闪烁、无粒子、无光晕；
- ESC 或「跳过」按钮都能结束，并在退出码里区分（3=跳过）。

退出码：0 = 倒计时结束；3 = 用户跳过（ESC 或按钮）；2 = 参数/显示环境异常。
作用仅是打断近距离用眼、引导远眺与眨眼，不是治疗，也不承诺改善视力。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import math
import os
import sys
import time
import tkinter as tk

BANDS = 90
BG_TOP = (18, 22, 26)       # #12161A
BG_BOTTOM = (27, 35, 40)    # #1B2328
C_COUNT = (198, 207, 213)   # #C6CFD5
C_GUIDE = (154, 166, 174)   # #9AA6AE
C_DIM = (122, 133, 141)     # #7A858D
GUIDES = [
    "看向 6 米外的东西，随便看什么都行",
    "记得眨眼，别一直盯着屏幕",
    "肩膀放松，慢慢呼吸",
    "让眼睛休息，不用盯着这行字",
]
FADE_MS = 800               # 渐变重绘间隔（1.25fps，肉眼是缓慢流动而不是闪烁）
TICK_MS = 200
BREATHE_S = 10.0            # 「呼吸」节奏：10 秒一个来回


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple:
    return tuple(
        max(0, min(255, round(c1 + (c2 - c1) * t))) for c1, c2 in zip(a, b))


def _tone(rgb: tuple[int, int, int], dim: float = 1.0,
          warm: float = 0.0) -> tuple:
    """按当前亮度轻微压暗（dim）并向暖色偏移（warm），不产生纯白/纯黑。"""
    r, g, b = rgb
    return (max(0, min(255, round(r * dim * (1.0 + 0.06 * warm)))),
            max(0, min(255, round(g * dim))),
            max(0, min(255, round(b * dim * (1.0 - 0.26 * warm)))))


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)


def _font(px: int, bold: bool = False) -> tuple:
    return ("Noto Sans CJK SC", -max(9, int(px)), "bold" if bold else "normal")


def _x_set_focus(wid: int) -> bool:
    """mutter 会忽略 Tk 的 focus_force；直接 XSetInputFocus 才能让 ESC 生效。"""
    if not os.environ.get("DISPLAY"):
        return False
    try:
        x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                       ctypes.c_int, ctypes.c_ulong]
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        dpy = x11.XOpenDisplay(None)
        if not dpy:
            return False
        x11.XSetInputFocus(dpy, wid, 1, 0)      # RevertToPointerRoot
        x11.XFlush(dpy)
        x11.XCloseDisplay(dpy)
        return True
    except Exception:
        return False


class Overlay:
    def __init__(self, x: int, y: int, w: int, h: int, seconds: int,
                 screen: str, dim: float = 1.0, warm: float = 0.0):
        self.w, self.h = w, h
        self.screen = screen
        self.dim = max(0.6, min(1.0, dim))
        self.warm = max(0.0, min(1.0, warm))
        self.deadline = time.monotonic() + seconds
        self.total = float(seconds)
        self.result = 0
        self.closed = False
        self.bands: list[int] = []
        self.guide_i = 0
        self.next_guide = 0.0

        self.root = tk.Tk()
        self.root.title("护眼休息")
        self.root.overrideredirect(True)          # X11 override-redirect：WM 不管它，位置精确
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.root.configure(bg=_hex(_tone(BG_TOP, 1.0, self.warm)))
        self.root.attributes("-topmost", True)

        self.canvas = tk.Canvas(self.root, width=w, height=h, highlightthickness=0,
                                bd=0, bg=_hex(_tone(BG_TOP, 1.0, self.warm)))
        self.canvas.pack(fill="both", expand=True)

        band_h = h / float(BANDS)
        for i in range(BANDS):
            self.bands.append(self.canvas.create_rectangle(
                0, i * band_h - 1, w, (i + 1) * band_h + 1, outline="", width=0))

        cx = w / 2.0
        count_fg = _hex(_tone(C_COUNT, self.dim, self.warm))
        guide_fg = _hex(_tone(C_GUIDE, self.dim, self.warm))
        dim_fg = _hex(_tone(C_DIM, self.dim, self.warm))
        self.count = self.canvas.create_text(
            cx, h * 0.42, text="", fill=count_fg, font=_font(h * 0.15))
        self.sub = self.canvas.create_text(
            cx, h * 0.42 + h * 0.105, text="休息开始 · 到点自动结束，可按 ESC 跳过",
            fill=dim_fg, font=_font(h * 0.022))
        self.guide = self.canvas.create_text(
            cx, h * 0.60, text=GUIDES[0], fill=guide_fg, font=_font(h * 0.036))

        # 跳过按钮（画布上的胶囊，不依赖 WM 按钮，也不依赖焦点）
        bw, bh = max(200, w * 0.16), max(52, h * 0.055)
        by = h * 0.76
        self.pill = self.canvas.create_rectangle(
            cx - bw / 2, by, cx + bw / 2, by + bh,
            fill="#1a2429", outline="#33424a", width=2)
        self.pill_text = self.canvas.create_text(
            cx, by + bh / 2, text="跳过（ESC）", fill=guide_fg, font=_font(h * 0.024))
        for item in (self.pill, self.pill_text):
            self.canvas.tag_bind(item, "<Button-1>", lambda e: self.skip("button"))

        self.canvas.create_text(
            cx, h * 0.90,
            text="覆盖 %s · 其他屏不受影响 · 覆盖层只是画面，不打断正在运行的程序" % screen,
            fill=dim_fg, font=_font(h * 0.018))
        self.canvas.create_text(
            cx, h * 0.94,
            text="作用：打断近距离用眼、提醒远眺与眨眼。不是治疗，也不承诺改善视力。",
            fill=dim_fg, font=_font(h * 0.018))

        self.root.bind("<Escape>", lambda e: self.skip("esc"))
        self.root.bind_all("<Escape>", lambda e: self.skip("esc"))
        self.root.protocol("WM_DELETE_WINDOW", lambda: self.skip("esc"))
        self.root.update_idletasks()
        self.root.lift()
        # 映射后再抢焦点（mutter 对 override-redirect 窗口只认 XSetInputFocus）
        for delay in (120, 400, 1200):
            self.root.after(delay, self._take_focus)

        self.paint_gradient()
        self.paint_countdown()
        self.root.after(TICK_MS, self.tick)

    def _take_focus(self) -> None:
        if self.closed:
            return
        try:
            self.root.focus_force()
            self.canvas.focus_set()
        except tk.TclError:
            pass
        try:
            _x_set_focus(self.root.winfo_id())
        except tk.TclError:
            pass

    # ---- 画面 ----
    def paint_gradient(self) -> None:
        if self.closed:
            return
        # 40 秒一个周期，只在两个规格色之间来回，幅度 ±4
        phase = math.sin((time.monotonic() / 40.0) * 2 * math.pi) * 4.0
        top = tuple(int(max(0, min(255, c + phase))) for c in BG_TOP)
        bot = tuple(int(max(0, min(255, c - phase))) for c in BG_BOTTOM)
        for i, item in enumerate(self.bands):
            color = _mix(top, bot, i / float(BANDS - 1))
            self.canvas.itemconfigure(item, fill=_hex(_tone(color, 1.0, self.warm)))
        self.root.after(FADE_MS, self.paint_gradient)

    def paint_countdown(self) -> None:
        if self.closed:
            return
        left = max(0.0, self.deadline - time.monotonic())
        secs = int(math.ceil(left))
        self.canvas.itemconfigure(self.count, text="%d:%02d" % (secs // 60, secs % 60))
        bar_w = self.w * 0.22
        bar_h = max(4, self.h * 0.004)
        progress = 0.0 if self.total <= 0 else max(0.0, min(1.0, left / self.total))
        if not hasattr(self, "bar"):
            self.bar = self.canvas.create_rectangle(
                self.w / 2 - bar_w / 2, self.h * 0.68,
                self.w / 2 - bar_w / 2 + 1, self.h * 0.68 + bar_h,
                outline="", fill="#2e3a40")
        self.canvas.coords(
            self.bar, self.w / 2 - bar_w / 2, self.h * 0.68,
            self.w / 2 - bar_w / 2 + bar_w * progress, self.h * 0.68 + bar_h)
        # 呼吸：细线在两个暗色之间 10 秒一个来回（不闪烁、不发光）
        breath = 0.5 + 0.5 * math.sin((time.monotonic() / BREATHE_S) * 2 * math.pi)
        self.canvas.itemconfigure(
            self.bar, fill=_hex(_mix((38, 48, 54), (62, 76, 84), breath)))
        now = time.monotonic()
        if now >= self.next_guide:
            self.guide_i = (self.guide_i + 1) % len(GUIDES)
            self.next_guide = now + 20.0
            self.canvas.itemconfigure(self.guide, text=GUIDES[self.guide_i])
        self.root.after(TICK_MS, self.paint_countdown)

    def skip(self, reason: str) -> None:
        if self.closed:
            return
        self.result = 3
        print("skip:%s" % reason, flush=True)
        self._close()

    def tick(self) -> None:
        if self.closed:
            return
        if time.monotonic() >= self.deadline:
            self.result = 0
            self._close()
            return
        self.root.after(TICK_MS, self.tick)

    def _close(self) -> None:
        """只退出 mainloop，不在这里 destroy：在按键/点击回调里销毁画布会让 Tk 崩溃。"""
        if self.closed:
            return
        self.closed = True
        try:
            self.root.quit()
        except tk.TclError:
            pass

    def run(self) -> int:
        self.root.mainloop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        return self.result


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="护眼休息覆盖层")
    ap.add_argument("--x", type=int, required=True)
    ap.add_argument("--y", type=int, required=True)
    ap.add_argument("--w", type=int, required=True)
    ap.add_argument("--h", type=int, required=True)
    ap.add_argument("--seconds", type=int, required=True)
    ap.add_argument("--screen", default="?")
    ap.add_argument("--dim", type=float, default=1.0,
                    help="文字亮度系数（0.6~1.0，跟随当前屏幕亮度，不改用户的亮度设置）")
    ap.add_argument("--warm", type=float, default=0.0,
                    help="暖色程度 0~1（跟随当前色温，0=6500K）")
    a = ap.parse_args(argv)
    if a.w < 100 or a.h < 100 or a.seconds < 1:
        print("参数不合法: %dx%d+%d+%d seconds=%d" % (a.w, a.h, a.x, a.y, a.seconds),
              file=sys.stderr)
        return 2
    try:
        return Overlay(a.x, a.y, a.w, a.h, a.seconds, a.screen, a.dim, a.warm).run()
    except tk.TclError as e:
        print("无法创建覆盖窗口（DISPLAY=%s）: %s" % (os.environ.get("DISPLAY", ""), e),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
