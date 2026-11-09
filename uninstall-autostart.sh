#!/usr/bin/env bash
# 卸载「用户级 systemd 托管」：停单元、禁用自启、删单元与登录自启项。
# 不会改动显示布局；8787 的 sudo 级 drop-in/polkit 规则需按提示手动删除。
set -u
UNIT_DIR="$HOME/.config/systemd/user"
AUTOSTART_DIR="$HOME/.config/autostart"

systemctl --user disable --now screen-portal.service 2>/dev/null || true
systemctl --user disable --now usbnic-session.service 2>/dev/null || true
systemctl --user stop screen-manager.service 2>/dev/null || true
rm -f "$UNIT_DIR/screen-manager.service" "$UNIT_DIR/screen-portal.service" \
      "$UNIT_DIR/usbnic-session.service"
systemctl --user daemon-reload 2>/dev/null || true

rm -f "$AUTOSTART_DIR/screen-session.desktop" "$AUTOSTART_DIR/screen-portal-open.desktop"
# 旧版的 wrapper 自启项
rm -f "$AUTOSTART_DIR/screen-manager.desktop" "$AUTOSTART_DIR/screen-portal.desktop"

# 旧 wrapper 方式残留（如果有）
pkill -f "[r]un-service.sh" 2>/dev/null || true
pkill -f "[r]un-portal.sh" 2>/dev/null || true
pkill -f "[d]m_server.py" 2>/dev/null || true
pkill -f "[p]ortal_server.py" 2>/dev/null || true

echo "已卸载 screen-manager / screen-portal / usbnic-session 用户单元与登录自启项。"
echo "显示布局未被改动；如需恢复输出: python3 backend/dm_cli.py restore-all"
echo
echo "如需一并移除 8787 的会话停止配置（sudo）："
echo "  sudo rm -f /etc/systemd/system/usbnicd.service.d/10-screen-session.conf"
echo "  sudo rm -f /etc/polkit-1/rules.d/49-screen-usbnicd.rules"
echo "  sudo systemctl daemon-reload"
echo "（不删也安全：usbnicd 只会在注销时被停，登录时仍由 usbnic-session 拉起。）"
