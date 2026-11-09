#!/usr/bin/env bash
# 手动启动 display-manager：本机 HTTP 服务（127.0.0.1:8765）+ 自动打开浏览器
# 这是「前台」方式，Ctrl-C 或关掉终端服务就停了；
# 想常驻请用 ./run-service.sh（崩溃自动重启）或 ./install-autostart.sh（登录自启）。
# 可选：PORT=9000 ./start.sh    换端口（端口被占用会直接报错退出，不会漂移）
#       ./start.sh --no-browser 只起服务不自动开浏览器
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PORT="${PORT:-8765}"
exec python3 "$DIR/backend/dm_server.py" "$@"
