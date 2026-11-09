#!/usr/bin/env python3
"""display-manager 本地 Web 服务：把界面和 JSON API 暴露在 127.0.0.1。

只用标准库（http.server），无需装包。启动后浏览器打开 http://127.0.0.1:<port>/
"""

import json
import os
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dm_core as core  # noqa: E402
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
        if path == "/":
            self.path = "/index.html"
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

    srv = None
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        print("端口 %d~%d 都被占用，无法启动" % (port, port + 19), file=sys.stderr)
        return 1

    url = "http://127.0.0.1:%d/" % port
    core.log("启动网页界面 %s（pid=%d）" % (url, os.getpid()))

    # 闲置检测：XDamage 逐屏计时，失败则降级为 XScreenSaver 全局空闲
    monitor = dm_idle.DamageMonitor(
        lambda: {o["name"]: (o["x"], o["y"], o["w"], o["h"]) for o in core.screen_rects()})
    monitor.start()
    core.set_idle_provider(monitor.report)
    rep = monitor.report()
    core.log("闲置检测启动: damage=%s screensaver=%s 依据=%s%s" % (
        rep["damage_ok"], rep["screensaver_ok"], rep["basis"],
        ("  错误: " + rep["error"]) if rep["error"] else ""))

    print("display-manager 界面: %s" % url)
    print("闲置检测依据: %s" % rep["basis"])
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
        monitor.stop()
        srv.server_close()
        core.log("网页界面已退出")
    return 0


if __name__ == "__main__":
    sys.exit(serve(sys.argv[1:]))
