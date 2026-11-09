#!/usr/bin/env bash
# 登录会话就绪后（由 autostart/screen-session.desktop 调用）：
# 1) 把图形会话的 DISPLAY / XAUTHORITY 导入 systemd 用户管理器；
# 2) 启动显示器管理用户单元（dm_server 需要 X11，必须在会话里启动）。
# 门户是纯 HTTP 服务，由 systemd 直接 enable，不依赖这个脚本。
set -u
systemctl --user import-environment DISPLAY XAUTHORITY 2>/dev/null || true
systemctl --user start screen-manager.service
