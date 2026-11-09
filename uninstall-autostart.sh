#!/usr/bin/env bash
# 关闭「登录后自动常驻」：删除自启项，并停掉当前正在跑的服务进程（不会改动显示布局）。
set -u
DST="$HOME/.config/autostart/screen-manager.desktop"
rm -f "$DST"
echo "已删除自启项: $DST"
# 注意用 [d]m_server.py 的写法，避免把执行本命令的 shell 自己也匹配掉
pkill -f "[d]m_server.py" 2>/dev/null && echo "已停止正在运行的服务" || echo "当前没有运行中的服务"
echo "显示布局未被改动；如需恢复输出，用: python3 backend/dm_cli.py restore-all"
