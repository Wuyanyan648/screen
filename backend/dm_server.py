#!/usr/bin/env python3
"""display-manager 本地 Web 服务：把界面和 JSON API 暴露在 127.0.0.1。

只用标准库（http.server），无需装包。固定监听 127.0.0.1:8765（被占用即报错退出），
根路径 `/` 直接返回 web/display-manager.html；界面与 JSON API 都在这一个服务里。
"""

import json
import os
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dm_core as core  # noqa: E402
import dm_brightness  # noqa: E402
import dm_hotkey  # noqa: E402
import dm_idle  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
LOCK = threading.Lock()  # 串行化 xrandr 操作，避免两个请求同时改布局


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB_DIR, **kw)

    def log_message(self, fmt, *args):  # 静默访问日志（操作日志已单独记录）
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/state":
            try:
                self._json(core.snapshot())
            except core.DMError as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return
        if path == "/api/log":
            self._json({"ok": True, "lines": core.read_log_tail(80)})
            return
        if path == "/api/brightness":
            refresh = "refresh=1" in self.path
            try:
                self._json({"ok": True, "brightness": core.brightness_state(refresh)})
            except core.DMError as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        # 根路径即界面：只认 display-manager.html 这一个入口，不再保留 index.html
        if path in ("/", "/display-manager.html"):
            self.path = "/display-manager.html"
        elif path == "/index.html":
            body = b"index.html is gone, use /display-manager.html"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
        name = (body.get("name") or "").strip()
        try:
            with LOCK:
                if path == "/api/off":
                    if not name:
                        self._json({"ok": False, "error": "缺少输出名"}, 400)
                        return
                    self._json(core.turn_off(name, "web"))
                elif path == "/api/on":
                    if not name:
                        self._json({"ok": False, "error": "缺少输出名"}, 400)
                        return
                    self._json(core.turn_on(name, "web"))
                elif path == "/api/restore":
                    self._json(core.restore_all("web"))
                elif path == "/api/plan":
                    self._json({"ok": True, "plan": core.plan_off(name)})
                elif path == "/api/config":
                    self._json({"ok": True, "config": core.save_config(body)})
                elif path == "/api/brightness":
                    if not name:
                        self._json({"ok": False, "error": "缺少输出名"}, 400)
                        return
                    if "value" in body:
                        # extra 可选：硬件到底（0）后叠加的软件层
                        extra = body.get("extra") if "extra" in body else None
                        self._json(core.set_brightness(name, body["value"], extra))
                    elif "extra" in body:
                        self._json(core.set_brightness_extra(name, body["extra"]))
                    else:
                        self._json({"ok": False, "error": "缺少亮度值"}, 400)
                else:
                    self._json({"ok": False, "error": "未知接口"}, 404)
        except core.DMError as e:
            self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:  # 兜底，避免服务因单次异常挂掉
            core.log("接口 %s 异常: %r" % (path, e))
            self._json({"ok": False, "error": "内部错误: %s" % e}, 500)


def serve(argv: list[str]) -> int:
    port = int(os.environ.get("PORT", "8765"))
    open_browser = True
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--port", "-p") and i + 1 < len(argv):
            port = int(argv[i + 1]); i += 2; continue
        if a == "--no-browser":
            open_browser = False; i += 1; continue
        i += 1

    # 固定端口，占用即退出（不再漂移到别的端口，避免浏览器书签/自启指向错地址）
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print("端口 %d 无法监听（%s）。可能已有一个实例在跑，"
              "用 `ss -lntp | grep %d` 或 `curl -s http://127.0.0.1:%d/api/state` 确认。"
              % (port, e, port, port), file=sys.stderr)
        return 3

    url = "http://127.0.0.1:%d/" % port
    core.log("启动网页界面 %s（pid=%d）" % (url, os.getpid()))

    # 亮度持久化：服务起来后把上次的值重新下发（关屏时不会应用，等开启时再补）
    def _restore_brightness():
        time.sleep(0.8)
        try:
            msgs = dm_brightness.apply_all()
            if msgs:
                print("启动恢复亮度: " + "，".join(msgs))
        except Exception as e:
            core.log("启动恢复亮度异常: %r" % e)

    threading.Thread(target=_restore_brightness, daemon=True).start()

    # 闲置检测：XDamage 逐屏计时，失败则降级为 XScreenSaver 全局空闲
    monitor = dm_idle.DamageMonitor(
        lambda: {o["name"]: (o["x"], o["y"], o["w"], o["h"]) for o in core.screen_rects()})
    monitor.start()
    core.set_idle_provider(monitor.report)
    # 等监视线程把 X 连接建起来（最多 1.5 秒），否则日志会把「刚启动」误写成「不可用」
    for _ in range(15):
        rep = monitor.report()
        if rep["damage_ok"] or rep["error"]:
            break
        time.sleep(0.1)
    rep = monitor.report()
    core.log("闲置检测启动: damage=%s screensaver=%s 依据=%s%s" % (
        rep["damage_ok"], rep["screensaver_ok"], rep["basis"],
        ("  错误: " + rep["error"]) if rep["error"] else ""))

    # 全局快捷键：Ctrl+Alt+Shift+F12 = 恢复全部（不自锁的救急入口）
    def _hotkey_restore():
        with LOCK:
            try:
                r = core.restore_all("hotkey")
                core.log("快捷键 %s 恢复全部: %s" % (dm_hotkey.KEY_COMBO, r.get("message")))
            except Exception as e:
                core.log("快捷键恢复失败: %r" % e)

    hotkey = dm_hotkey.HotkeyListener(_hotkey_restore)
    hotkey.start()
    time.sleep(0.5)
    core.log("全局快捷键注册: %s -> restore-all，ok=%s%s" % (
        dm_hotkey.KEY_COMBO, hotkey.ok, ("  错误: " + hotkey.error) if hotkey.error else ""))

    print("display-manager 界面: %s" % url)
    print("闲置检测依据: %s" % rep["basis"])
    print("全局快捷键: %s = 恢复全部输出" % dm_hotkey.KEY_COMBO)
    print("命令行同一套逻辑: python3 %s list|on|off|restore-all" %
          os.path.join(ROOT, "backend", "dm_cli.py"))
    print("按 Ctrl-C 退出（不会改动当前显示布局）")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hotkey.stop()
        monitor.stop()
        srv.server_close()
        core.log("网页界面已退出")
    return 0


if __name__ == "__main__":
    sys.exit(serve(sys.argv[1:]))
