#!/usr/bin/env bash
# 一键启动 display-manager：本机 HTTP 服务（127.0.0.1）+ 自动打开浏览器
# Ctrl-C 退出；退出时只关闭服务与闲置监视线程，不改动任何显示布局。
# 可选：PORT=9000 ./start.sh        换端口
#       ./start.sh --no-browser     只起服务不自动开浏览器
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PORT="${PORT:-8765}"
exec python3 "$DIR/backend/dm_server.py" "$@"
