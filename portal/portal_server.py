#!/usr/bin/env python3
"""本机服务门户：把几个自建本地服务汇总成一个固定入口页。

设计要点：
- 固定监听 127.0.0.1:8760（被占用即退出码 3），不与 8765 / 8787 / 8080 抢端口；
- 服务清单来自同目录 services.json，按 mtime 热加载（加服务改配置不用重启）；
- 状态由**后端**定时探测：每个服务一个线程，urllib 请求 + 1.5 秒超时；
- 浏览器只读 /api/status，不直接访问被管理的服务，避免 CORS / 代理干扰；
- 启停只是 systemctl 薄壳（/api/action）：user-unit 加 --user，system-unit 走系统管理器；
  门户不负责清理，注销/关机时由 systemd 会话管理器统一停止受管服务；
- CPU / 内存按 cgroup 采样（resource_monitor.py），与探测同一个 5 秒节奏，
  `/api/status` 里带 metrics；命令行用 `python3 portal_server.py --metrics` 直接打印。

只用 Python 标准库。
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import resource_monitor

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")
CONFIG_PATH = os.path.join(ROOT, "services.json")

DEFAULT_PORT = 8760
PROBE_INTERVAL = 5.0   # 后端探测周期（秒）= CPU 均值窗口
PROBE_TIMEOUT = 1.5    # 单次 HTTP 探测超时（秒）
USER_AGENT = "local-service-portal/1.0"

_LOCK = threading.Lock()
_PROBE_LOCK = threading.Lock()   # 串行化探测，避免定时轮与「立即检测」交叉
_SAMPLER = resource_monitor.Sampler()   # 资源占用采样（cgroup 直读 + 降级）
_STATE = {
    "services": [],       # 每项 = 配置字段 + status/latency_ms/last_checked/http_status/error/unit_state
    "mtime": 0.0,
    "checked_at": 0.0,
    "config_error": "",
    "actions": {},        # id -> {verb, ok, message, at, at_text}
    "metrics": {},        # {"by_key": {...}, "portal": {...}, "total": {...}, "at": ts}
}


def _now_text(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else ""


def _err_text(exc: Exception) -> str:
    """把探测异常翻成一句人话，界面上直接显示。"""
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        text = str(reason)
        if isinstance(reason, ConnectionRefusedError) or "refused" in text.lower():
            return "连接被拒绝（没在监听）"
        if isinstance(reason, TimeoutError) or "timed out" in text.lower():
            return "请求超时"
        return text
    if isinstance(exc, TimeoutError):
        return "请求超时"
    return "%s: %s" % (type(exc).__name__, exc)


def _health_url(base: str, health: str) -> str:
    """health 以 / 开头 → 相对站点根；否则相对 url 所在目录。"""
    if health.startswith("http://") or health.startswith("https://"):
        return health
    parts = urllib.parse.urlsplit(base)
    origin = "%s://%s" % (parts.scheme, parts.netloc)
    if health.startswith("/"):
        return origin + health
    return urllib.parse.urljoin(base.rstrip("/") + "/", health)


def _normalize(raw: dict) -> list[dict]:
    services = []
    for i, item in enumerate(raw.get("services") or []):
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        sid = str(item.get("id") or "svc-%d" % (i + 1))
        health = str(item.get("health") or "/").strip() or "/"
        manage = str(item.get("manage") or "external").strip()
        if manage not in ("user-unit", "system-unit"):
            manage = "external"
        try:
            port = urllib.parse.urlsplit(url).port
        except ValueError:
            port = None
        services.append({
            "id": sid,
            "name": str(item.get("name") or sid),
            "desc": str(item.get("desc") or ""),
            "url": url,
            "health": health,
            "health_url": _health_url(url, health),
            "port": port,
            "icon": str(item.get("icon") or "•"),
            "color": str(item.get("color") or "#58a6ff"),
            # 是否允许 iframe：由配置显式声明（实测过 X-Frame-Options / CSP 才写 true）
            "embed": bool(item.get("embed", False)),
            # 托管方式：user-unit / system-unit / external（只展示）
            "manage": manage,
            "unit": str(item.get("unit") or ""),
            # 外部服务没有 systemd 单元时，用它按进程名估资源占用
            "process": str(item.get("process") or ""),
            "note": str(item.get("note") or ""),
            "start_on_login": bool(item.get("start_on_login", manage != "external")),
            "unit_state": "",
            "status": "unknown",
            "latency_ms": None,
            "last_checked": 0.0,
            "last_checked_text": "",
            "http_status": None,
            "error": "",
            "metrics": None,
        })
    return services


def _reload_if_changed(force: bool = False) -> bool:
    """配置有变化才重读；保留 id 相同的旧状态，避免每次重载都闪一下「检测中」。"""
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError as e:
        with _LOCK:
            _STATE["config_error"] = "读不到配置文件 %s：%s" % (CONFIG_PATH, e)
            _STATE["mtime"] = 0.0
        return False

    with _LOCK:
        if not force and mtime == _STATE["mtime"]:
            return False

    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        services = _normalize(raw)
    except (OSError, ValueError) as e:
        with _LOCK:
            _STATE["config_error"] = "services.json 解析失败：%s" % e
            _STATE["mtime"] = mtime
        return False

    with _LOCK:
        old = {s["id"]: s for s in _STATE["services"]}
        for s in services:
            prev = old.get(s["id"])
            if prev and prev["url"] == s["url"] and prev["health_url"] == s["health_url"] \
                    and prev["manage"] == s["manage"] and prev["unit"] == s["unit"]:
                for k in ("status", "latency_ms", "last_checked", "last_checked_text",
                          "http_status", "error", "unit_state"):
                    s[k] = prev[k]
        _STATE["services"] = services
        _STATE["mtime"] = mtime
        _STATE["config_error"] = ""
    return True


def _unit_state(manage: str, unit: str) -> str:
    """只读查询单元状态；失败返回空串，不影响 HTTP 探测结论。"""
    if not unit:
        return ""
    cmd = ["/usr/bin/systemctl"]
    if manage == "user-unit":
        cmd.append("--user")
    cmd += ["is-active", unit]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else ""
    except Exception:  # noqa: BLE001
        return ""


def run_action(svc: dict, verb: str) -> dict:
    """通过 systemctl 做 start/stop/restart。user-unit 加 --user，system-unit 走系统管理器。"""
    if svc["manage"] not in ("user-unit", "system-unit") or not svc["unit"]:
        return {"ok": False, "message": "该服务未托管，无法操作"}
    base = ["/usr/bin/systemctl"] + (["--user"] if svc["manage"] == "user-unit" else [])
    try:
        if verb in ("start", "restart"):
            # 清掉上次失败累计的 start-limit，否则「启动」会立刻被 refused
            subprocess.run(base + ["reset-failed", svc["unit"]],
                           capture_output=True, text=True, timeout=5)
        r = subprocess.run(base + [verb, svc["unit"]],
                           capture_output=True, text=True, timeout=25)
        text = (r.stdout.strip() or r.stderr.strip()
                or {"start": "已启动", "stop": "已停止", "restart": "已重启"}[verb])
        return {"ok": r.returncode == 0, "message": text, "code": r.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "systemctl 超时（25 秒）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": "执行失败: %s" % e}


def _probe_one(svc: dict) -> dict:
    started = time.time()
    status = "offline"
    code = None
    error = ""
    req = urllib.request.Request(
        svc["health_url"],
        headers={"User-Agent": USER_AGENT, "Cache-Control": "no-store"},
    )
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            code = resp.status
            resp.read(4096)   # 读一点就走，不关心内容
        status = "online"
    except urllib.error.HTTPError as e:
        code = e.code
        if e.code < 500:
            # 服务在跑，只是健康路径可能配错：仍算在线，但把状态码带出来
            status = "online"
            error = "健康路径返回 HTTP %d（服务在，检查 health 是否配错）" % e.code
        else:
            error = "HTTP %d" % e.code
    except Exception as e:  # noqa: BLE001 —— 探测失败只记录，不能把门户带崩
        error = _err_text(e)

    done = time.time()
    return {
        "id": svc["id"],
        "status": status,
        "latency_ms": int((done - started) * 1000),
        "last_checked": done,
        "last_checked_text": _now_text(done),
        "http_status": code,
        "error": error,
        "unit_state": _unit_state(svc["manage"], svc["unit"]),
    }


def probe_round() -> None:
    with _PROBE_LOCK:
        with _LOCK:
            services = [dict(s) for s in _STATE["services"]]
        if not services:
            return
        with ThreadPoolExecutor(max_workers=len(services)) as pool:
            results = list(pool.map(_probe_one, services))
        by_id = {r["id"]: r for r in results}
        now = time.time()
        with _LOCK:
            for svc in _STATE["services"]:
                hit = by_id.get(svc["id"])
                if hit:
                    svc.update(hit)
            _STATE["checked_at"] = now
        # 资源占用：与探测同一节奏采样，CPU 窗口≈探测周期，不需要额外定时器
        try:
            by_key, meta = _SAMPLER.poll(services)
            with _LOCK:
                _STATE["metrics"] = dict(meta, by_key=by_key, at=now)
        except Exception as e:  # noqa: BLE001 —— 采样失败只让指标显示「—」
            print("[portal] 资源采样异常: %r" % e, file=sys.stderr)


def probe_loop() -> None:
    while True:
        _reload_if_changed()
        try:
            probe_round()
        except Exception as e:  # noqa: BLE001 —— 单轮失败不能让守护线程死掉
            print("[portal] 探测轮异常: %r" % e, file=sys.stderr)
        time.sleep(PROBE_INTERVAL)


def snapshot() -> dict:
    with _LOCK:
        checked = _STATE["checked_at"]
        metrics = _STATE["metrics"] or {}
        by_key = metrics.get("by_key") or {}
        services = []
        for s in _STATE["services"]:
            item = dict(s)
            item["last_action"] = _STATE["actions"].get(s["id"])
            item["metrics"] = by_key.get(resource_monitor.service_key(s))
            services.append(item)
        return {
            "ok": True,
            "now": time.time(),
            "interval": PROBE_INTERVAL,
            "timeout": PROBE_TIMEOUT,
            "checked_at": checked,
            "checked_text": _now_text(checked),
            "config_error": _STATE["config_error"],
            "services": services,
            "metrics": {
                "at": metrics.get("at", 0.0),
                "at_text": _now_text(metrics.get("at", 0.0)),
                "window": PROBE_INTERVAL,
                "peak_window": resource_monitor.PEAK_WINDOW,
                "cpu_basis": "单核 100%",
                "mem_total_bytes": resource_monitor.mem_total_bytes(),
                "portal": metrics.get("portal"),
                "total": metrics.get("total"),
            },
        }


def _width(text: str) -> int:
    """终端显示宽度：CJK 字符按 2 列算，用来对齐中文名。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _fmt_pct(v) -> str:
    return "—" if v is None else "%.2f%%" % v


def _fmt_mb(v) -> str:
    return "—" if v is None else "%.1f MB" % v


def metrics_cli(window: float = 1.2) -> int:
    """命令行打印各服务占用（现场采两次，不依赖常驻门户）。"""
    if not _reload_if_changed(force=True):
        print("读不到配置 %s：%s" % (CONFIG_PATH, _STATE["config_error"]), file=sys.stderr)
        return 1
    with _LOCK:
        services = [dict(s) for s in _STATE["services"]]

    by_key, meta = _SAMPLER.sample_now(services, window=window)
    total = meta["total"]
    win = meta["window"]
    print("资源占用（CPU 为最近 %.1f 秒均值，单核=100%%；内存为 cgroup memory.current / RSS）" % win)
    print("%s %s %s %s  %s" % (_pad("服务", 36), _pad("CPU", 9), _pad("峰值", 9),
                               _pad("内存", 12), "来源"))
    print("-" * 94)
    rows = 0
    for svc in services:
        m = by_key.get(resource_monitor.service_key(svc)) or {}
        label = "%s (%s)" % (svc["name"], svc["unit"] or svc["id"])
        src = m.get("source_label") or m.get("reason") or "—"
        print("%s %s %s %s  %s" % (_pad(label, 36), _pad(_fmt_pct(m.get("cpu_pct")), 9),
                                   _pad(_fmt_pct(m.get("cpu_peak")), 9),
                                   _pad(_fmt_mb(m.get("mem_mb")), 12), src))
        rows += 1
    pm = meta.get("portal") or {}
    print("-" * 94)
    print("%s %s %s %s  %s" % (_pad("门户自身 (screen-portal.service)", 36),
                               _pad(_fmt_pct(pm.get("cpu_pct")), 9),
                               _pad(_fmt_pct(pm.get("cpu_peak")), 9),
                               _pad(_fmt_mb(pm.get("mem_mb")), 12),
                               pm.get("source_label") or pm.get("reason") or "—"))
    print("%s %s %s %s  %s" % (_pad("合计（含门户自身）", 36), _pad(_fmt_pct(total.get("cpu_pct")), 9),
                               _pad(_fmt_pct(total.get("cpu_peak")), 9),
                               _pad(_fmt_mb(total.get("mem_mb")), 12),
                               "整机内存 %s" % _fmt_pct(total.get("mem_pct"))))
    print("注：CLI 是独立进程，峰值列只覆盖本次两次采样；常驻服务的峰值窗口是最近 %d 分钟。"
          % round(resource_monitor.PEAK_WINDOW / 60))
    return 0 if rows else 1


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB_DIR, **kw)

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/api/action":
            self._json({"ok": False, "error": "not found"}, 404)
            return
        body = self._read_json()
        if not isinstance(body, dict):
            body = {}
        sid = str(body.get("id") or "")
        verb = str(body.get("verb") or "")
        if verb not in ("start", "stop", "restart"):
            self._json({"ok": False, "error": "verb 只允许 start/stop/restart"}, 400)
            return
        with _LOCK:
            svc = next((dict(s) for s in _STATE["services"] if s["id"] == sid), None)
        if not svc:
            self._json({"ok": False, "error": "没有这个服务: %s" % sid}, 404)
            return
        result = run_action(svc, verb)
        rec = {
            "verb": verb, "ok": result["ok"], "message": result["message"],
            "at": time.time(), "at_text": _now_text(time.time()),
        }
        with _LOCK:
            _STATE["actions"][sid] = rec
        print("[portal] %s %s -> %s（%s）" % (sid, verb, "ok" if result["ok"] else "fail",
                                             result["message"]), file=sys.stderr)
        time.sleep(0.8)   # 给服务一点起来 / 退出的时间，再统一探测
        try:
            probe_round()
        except Exception:  # noqa: BLE001
            pass
        self._json(snapshot())

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/status":
            self._json(snapshot())
            return
        if path == "/api/refresh":
            # 同步重探一轮（并行，最坏约 1.5 秒），前端「立即检测」用
            try:
                probe_round()
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": "探测失败: %s" % e}, 500)
                return
            self._json(snapshot())
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path in ("/", "/portal.html"):
            self.path = "/portal.html"
        elif path == "/index.html":
            body = b"index.html is gone, use /"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def serve(argv: list[str]) -> int:
    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    open_browser = True
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--port", "-p") and i + 1 < len(argv):
            port = int(argv[i + 1])
            i += 2
            continue
        if a == "--no-browser":
            open_browser = False
            i += 1
            continue
        if a == "--metrics":
            # 命令行查看各服务占用（不启动服务、不占端口）
            return metrics_cli()
        i += 1

    # 固定端口，占用即退出（不漂移，避免书签 / 自启指向错地址）
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print("端口 %d 无法监听（%s）。可能已有一个门户实例在跑，"
              "用 `ss -lntp | grep %d` 确认。" % (port, e, port), file=sys.stderr)
        return 3

    # 先同步探一轮，保证首屏就有状态，而不是「检测中」等 5 秒
    _reload_if_changed(force=True)
    probe_round()
    threading.Thread(target=probe_loop, daemon=True).start()

    url = "http://127.0.0.1:%d/" % port
    with _LOCK:
        ids = [s["id"] for s in _STATE["services"]]
    print("本机服务门户: %s（pid=%d，%d 项服务）" % (url, os.getpid(), len(ids)))
    print("配置: %s（改完保存自动热加载，无需重启）" % CONFIG_PATH)
    print("按 Ctrl-C 退出（不会影响被管理的服务）")
    if open_browser:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(serve(sys.argv[1:]))
