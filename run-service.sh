#!/usr/bin/env bash
# 常驻运行 display-manager 服务（无浏览器）：崩溃后 2 秒自动重启。
# 由开机自启（autostart/screen-manager.desktop）调用，也可以手动前台运行。
# 退出码 3 = 端口已被占用（多半已有一个实例在跑），此时不再重启，直接退出。
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PORT="${PORT:-8765}"

while true; do
  python3 "$DIR/backend/dm_server.py" --no-browser
  code=$?
  if [ "$code" -eq 3 ]; then
    echo "[run-service] 端口 $PORT 已被占用，视为已有实例，退出。" >&2
    exit 0
  fi
  echo "[run-service] dm_server 退出（code=$code），2 秒后重启…" >&2
  sleep 2
done
