#!/usr/bin/env python3
"""按 cgroup 给门户里登记的每个单元采样 CPU / 内存。只用标准库。

口径（页面与 README 都按这个写）：
- CPU：两次采样之间 `cpu.stat` 的 `usage_usec` 差值 / 实际间隔，归一到「单核 100%」
  （与 systemd-cgtop 一致：跑满两个核显示 200%）。默认窗口约 5 秒，不用瞬时值。
- 内存：`memory.current`（cgroup）或进程树 RSS 求和，单位 MB；另附整机占比。

数据来源优先级：
1. `systemctl show -p ControlGroup` 拿到 cgroup 路径后直接读 /sys/fs/cgroup 下的文件
   （不 fork、不扫 ps，单次采样只有几次 read）；
2. cgroup 读不到（路径不存在 / 权限不足）时，退化为 MainPID + /proc 进程树，
   结果标注「进程树降级」并带上原因；
3. 未托管的外部服务按进程名扫 /proc 估，标注「按进程名估算」（可能漏掉子进程/同名进程）。
"""

import os
import subprocess
import threading
import time

CGROUP_ROOT = "/sys/fs/cgroup"
SYSTEMCTL = "/usr/bin/systemctl"
RESOLVE_TTL = 60.0          # 单元 → cgroup 路径的缓存时间（路径出错会提前重查）
MIN_WINDOW = 2.0            # 小于这个间隔不重算 CPU（避免手动刷新造成的尖峰）
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

_MEM_TOTAL = None


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def mem_total_bytes():
    """整机内存总量（缓存，来自 /proc/meminfo）。"""
    global _MEM_TOTAL
    if _MEM_TOTAL is None:
        total = 0
        for line in (_read("/proc/meminfo") or "").splitlines():
            if line.startswith("MemTotal:"):
                try:
                    total = int(line.split()[1]) * 1024
                except (IndexError, ValueError):
                    total = 0
                break
        _MEM_TOTAL = total
    return _MEM_TOTAL


def unit_key(manage, unit):
    return "%s:%s" % (manage or "", unit or "")


def service_key(svc):
    """同一份配置在「采样」与「快照」两处要算出同一个 key。"""
    return unit_key(svc.get("manage") or "external",
                    svc.get("unit") or svc.get("process") or svc.get("id") or "")


def _metrics_empty(reason, source=None, label=""):
    return {
        "source": source,
        "source_label": label,
        "cpu_pct": None,
        "cpu_window": None,
        "mem_bytes": None,
        "mem_mb": None,
        "mem_pct": None,
        "reason": reason,
        "detail": "",
    }


def _build(usage_usec, mem_bytes, window, source, label, detail, reason=""):
    """把一次采样结果拼成给前端的指标字典。"""
    total = mem_total_bytes()
    mem_mb = round(mem_bytes / 1048576.0, 1) if mem_bytes is not None else None
    return {
        "source": source,
        "source_label": label,
        "cpu_pct": round(usage_usec / (window * 1e6) * 100.0, 2) if (usage_usec is not None and window) else None,
        "cpu_window": round(window, 1) if window else None,
        "mem_bytes": mem_bytes,
        "mem_mb": mem_mb,
        "mem_pct": round(mem_bytes * 100.0 / total, 2) if (mem_bytes is not None and total) else None,
        "reason": reason,
        "detail": detail,
    }


class Sampler:
    """有状态采样器：CPU 需要前后两次读数，所以缓存上一次的用量。"""

    def __init__(self, self_unit="screen-portal.service"):
        self._lock = threading.Lock()
        self._resolved = {}   # (manage, unit) -> {t, cg, pid, error}
        self._prev = {}       # key -> {usage, t, source}
        self._last = {}       # key -> metrics
        self.self_unit = self_unit
        self.self_manage = "user-unit"
        self.last_at = 0.0

    # ---------- 单元 → cgroup 路径 / 主进程 ----------
    def _resolve(self, manage, unit, force=False):
        key = (manage, unit)
        now = time.time()
        with self._lock:
            hit = self._resolved.get(key)
            if hit and not force and now - hit["t"] < RESOLVE_TTL:
                return hit
        cmd = [SYSTEMCTL] + (["--user"] if manage == "user-unit" else []) + \
              ["show", unit, "-p", "ControlGroup", "-p", "MainPID"]
        info = {"t": now, "cg": None, "pid": None, "error": ""}
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=4)
            vals = {}
            for line in (r.stdout or "").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip()
            cg = vals.get("ControlGroup") or ""
            if cg and cg != "/":
                info["cg"] = os.path.join(CGROUP_ROOT, cg.lstrip("/"))
            pid = vals.get("MainPID") or "0"
            if pid.isdigit() and int(pid) > 0:
                info["pid"] = int(pid)
            if not info["cg"] and not info["pid"]:
                info["error"] = "systemctl show 没返回 ControlGroup / MainPID（单元可能不存在）"
        except Exception as e:  # noqa: BLE001 —— 采样失败只降级，不能影响门户
            info["error"] = "systemctl show 失败: %s" % e
        with self._lock:
            self._resolved[key] = info
        return info

    # ---------- cgroup 直读 ----------
    @staticmethod
    def _cgroup_usage(cg):
        """返回 (usage_usec, memory.current)；读不到的部分为 None。"""
        usage = None
        mem = None
        text = _read(os.path.join(cg, "cpu.stat"))
        if text:
            for line in text.splitlines():
                if line.startswith("usage_usec"):
                    try:
                        usage = float(line.split()[1])
                    except (IndexError, ValueError):
                        usage = None
                    break
        raw = _read(os.path.join(cg, "memory.current"))
        if raw:
            try:
                mem = int(raw.strip())
            except ValueError:
                mem = None
        return usage, mem

    # ---------- /proc 进程树（降级路径）----------
    @staticmethod
    def _proc_scan():
        """扫一遍 /proc，返回 (children, ticks, rss)。只读文件，不 fork。"""
        children = {}
        ticks = {}
        rss = {}
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            data = _read("/proc/%d/stat" % pid)
            if not data:
                continue
            i = data.rfind(")")   # comm 里可能有空格和括号
            if i < 0:
                continue
            f = data[i + 2:].split()
            try:
                ppid = int(f[1])
                utime = float(f[11])
                stime = float(f[12])
            except (IndexError, ValueError):
                continue
            children.setdefault(ppid, []).append(pid)
            ticks[pid] = utime + stime
            statm = _read("/proc/%d/statm" % pid)
            if statm:
                parts = statm.split()
                if len(parts) > 1:
                    try:
                        rss[pid] = int(parts[1]) * PAGE_SIZE
                    except ValueError:
                        pass
        return children, ticks, rss

    @classmethod
    def _proc_tree(cls, root_pid):
        """主进程 + 全部子孙的 (cpu 微秒, RSS 字节)；进程不在时返回 None。"""
        children, ticks, rss = cls._proc_scan()
        if root_pid not in ticks:
            return None
        seen = set()
        stack = [root_pid]
        total_ticks = 0.0
        total_rss = 0
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            total_ticks += ticks.get(pid, 0.0)
            total_rss += rss.get(pid, 0)
            stack.extend(children.get(pid, ()))
        return total_ticks / CLK_TCK * 1e6, total_rss, len(seen)

    @classmethod
    def _proc_by_name(cls, name):
        """按进程名匹配：comm、cmdline[0] 的 basename，或任意参数里的脚本名。

        python 服务的 comm 是 `python3`，所以还要看命令行里的脚本 / 模块名
        （例如 `dm_server.py`、`usbnic_host.daemon`）。
        """
        children, ticks, rss = cls._proc_scan()
        hits = []
        for pid in ticks:
            comm = (_read("/proc/%d/comm" % pid) or "").strip()
            if comm == name:
                hits.append(pid)
                continue
            cmdline = (_read("/proc/%d/cmdline" % pid) or "")
            for token in cmdline.split("\0"):
                if not token or token.startswith("-"):
                    continue
                if os.path.basename(token) == name or token == name:
                    hits.append(pid)
                    break
        if not hits:
            return None
        return (sum(ticks.get(p, 0.0) for p in hits) / CLK_TCK * 1e6,
                sum(rss.get(p, 0) for p in hits), len(hits))

    # ---------- 单个单元 ----------
    def _sample(self, manage, unit, process="", min_window=MIN_WINDOW, key=None):
        key = key or unit_key(manage, unit or process)
        now = time.time()
        usage = None
        mem = None
        source = None
        label = ""
        detail = ""
        reason = ""

        if manage in ("user-unit", "system-unit") and unit:
            info = self._resolve(manage, unit)
            if info["cg"]:
                usage, mem = self._cgroup_usage(info["cg"])
                if usage is not None or mem is not None:
                    source, label = "cgroup", "cgroup"
                    detail = "cgroup: %s" % info["cg"].replace(CGROUP_ROOT, "")
            if source is None:
                pid = info["pid"]
                tree = self._proc_tree(pid) if pid else None
                if tree:
                    usage, mem, n = tree
                    source, label = "proc", "进程树降级"
                    reason = info["error"] or "cgroup 读不到，退化为 MainPID %d 的 /proc 进程树" % pid
                    detail = "/proc 进程树（%d 个进程，主进程 %d）" % (n, pid)
                else:
                    reason = info["error"] or "单元未运行（没有 cgroup，也找不到主进程）"
        elif process:
            got = self._proc_by_name(process)
            if got:
                usage, mem, n = got
                source, label = "procname", "按进程名估算"
                reason = "未托管服务，按进程名 %r 匹配 /proc（可能漏掉子进程）" % process
                detail = "按进程名估算（命中 %d 个进程）" % n
            else:
                reason = "外部服务未托管，也没找到名为 %r 的进程" % process
        else:
            reason = "未托管且没有配置 process 字段，无法采样"

        if source is None:
            out = _metrics_empty(reason)
            with self._lock:
                self._last[key] = out
            return out

        # CPU 差值
        window = None
        usage_delta = None
        with self._lock:
            prev = self._prev.get(key)
        if prev and prev.get("source") == source and prev.get("usage") is not None \
                and usage is not None and usage >= prev["usage"]:
            dt = now - prev["t"]
            if dt >= min_window:
                window = dt
                usage_delta = usage - prev["usage"]
        with self._lock:
            self._prev[key] = {"usage": usage, "t": now, "source": source}

        out = _build(usage_delta, mem, window, source, label, detail, reason)
        if out["cpu_pct"] is None:
            # 首次采样 / 间隔太短：沿用上一次算好的 CPU，避免出现瞬时尖峰或空值闪烁
            with self._lock:
                old = self._last.get(key)
            if old and old.get("cpu_pct") is not None and old.get("source") == source:
                out["cpu_pct"] = old["cpu_pct"]
                out["cpu_window"] = old["cpu_window"]
        with self._lock:
            self._last[key] = out
        return out

    def poll(self, services):
        """采样一轮：services 为配置项（含 manage / unit / process）。返回 (by_key, meta)。"""
        by_key = {}
        for svc in services:
            manage = svc.get("manage") or "external"
            unit = svc.get("unit") or ""
            key = service_key(svc)
            try:
                by_key[key] = self._sample(manage, unit, svc.get("process") or "", key=key)
            except Exception as e:  # noqa: BLE001
                by_key[key] = _metrics_empty("采样异常: %r" % e)
        portal = None
        try:
            portal = self._sample(self.self_manage, self.self_unit, key="portal")
        except Exception as e:  # noqa: BLE001
            portal = _metrics_empty("采样异常: %r" % e)
        self.last_at = time.time()
        return by_key, {"portal": portal, "total": _total(list(by_key.values()) + [portal])}

    def sample_now(self, services, window=1.2):
        """CLI 用：现场采两次（默认间隔 1.2 秒）后返回结果，不依赖常驻服务。"""
        pairs = [(svc, service_key(svc)) for svc in services]
        for svc, key in pairs:
            self._sample(svc.get("manage") or "external", svc.get("unit") or "",
                         svc.get("process") or "", min_window=0.5, key=key)
        self._sample(self.self_manage, self.self_unit, min_window=0.5, key="portal")
        time.sleep(window)
        by_key = {}
        for svc, key in pairs:
            by_key[key] = self._sample(svc.get("manage") or "external", svc.get("unit") or "",
                                       svc.get("process") or "", min_window=0.5, key=key)
        portal = self._sample(self.self_manage, self.self_unit, min_window=0.5, key="portal")
        return by_key, {"portal": portal, "total": _total(list(by_key.values()) + [portal]),
                        "window": window}


def _total(items):
    """合计：只累加采样成功的项，并记录参与统计的条数。"""
    cpu = 0.0
    cpu_n = 0
    mem = 0
    mem_n = 0
    for it in items:
        if not it:
            continue
        if it.get("cpu_pct") is not None:
            cpu += it["cpu_pct"]
            cpu_n += 1
        if it.get("mem_bytes") is not None:
            mem += it["mem_bytes"]
            mem_n += 1
    total = mem_total_bytes()
    return {
        "cpu_pct": round(cpu, 2) if cpu_n else None,
        "mem_bytes": mem if mem_n else None,
        "mem_mb": round(mem / 1048576.0, 1) if mem_n else None,
        "mem_pct": round(mem * 100.0 / total, 2) if (mem_n and total) else None,
        "cpu_count": cpu_n,
        "mem_count": mem_n,
        "count": len([i for i in items if i]),
    }
