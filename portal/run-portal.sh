#!/usr/bin/env bash
# 常驻运行本机服务门户（无浏览器）：崩溃后 2 秒自动重启。
# 由登录自启（autostart/screen-portal.desktop）调用，也可以手动前台运行。
# 退出码 3 = 端口已被占用（多半已有一个实例在跑），此时不再重启，直接退出。
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PORT="${PORT:-8760}"

while true; do
  python3 "$DIR/portal_server.py" --no-browser
  code=$?
  if [ "$code" -eq 3 ]; then
    echo "[run-portal] 端口 $PORT 已被占用，视为已有实例，退出。" >&2
    exit 0
  fi
  echo "[run-portal] portal_server 退出（code=$code），2 秒后重启…" >&2
  sleep 2
done
