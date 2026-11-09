#!/usr/bin/env bash
# 安装并启用「用户级 systemd 托管」：
#   1. ~/.config/systemd/user/screen-manager.service  显示器输出管理（8765）
#   2. ~/.config/systemd/user/screen-portal.service   本机服务门户（8760，登录自启）
#   3. ~/.config/systemd/user/usbnic-session.service  登录时拉起 usbnicd（8787，系统级）
#   4. ~/.config/autostart/screen-session.desktop     会话就绪后启动 8765 的单元
#      ~/.config/autostart/screen-portal-open.desktop 登录后自动打开门户（可删）
# 无需 sudo。8787 的「随会话停止」需要一次性 sudo 配置，见脚本末尾提示。
set -eu
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
AUTOSTART_DIR="$HOME/.config/autostart"

mkdir -p "$UNIT_DIR" "$AUTOSTART_DIR"
chmod +x "$DIR/start-session-services.sh" "$DIR/portal/open-portal.sh" \
         "$DIR/run-service.sh" "$DIR/portal/run-portal.sh"

# 1) 迁移：先停掉旧的 wrapper 方式（run-service.sh / run-portal.sh 及其 python 子进程），
#    否则端口被占用，systemd 单元会以退出码 3 退出。
pkill -f "[r]un-service.sh" 2>/dev/null || true
pkill -f "[r]un-portal.sh" 2>/dev/null || true
pkill -f "[d]m_server.py" 2>/dev/null || true
pkill -f "[p]ortal_server.py" 2>/dev/null || true
# 已装过 unit 的情况：先停再重装
systemctl --user stop screen-manager.service 2>/dev/null || true
systemctl --user stop screen-portal.service 2>/dev/null || true
sleep 0.5

# 2) 写 unit（把 @DIR@ 换成绝对路径）
for unit in screen-manager screen-portal usbnic-session; do
  sed "s|@DIR@|$DIR|g" "$DIR/systemd/$unit.service" > "$UNIT_DIR/$unit.service"
  chmod 644 "$UNIT_DIR/$unit.service"
  echo "已安装用户单元: $UNIT_DIR/$unit.service"
done
systemctl --user daemon-reload

# 3) 门户登录自启；8787 的登录拉起；8765 立即启动
systemctl --user enable screen-portal.service >/dev/null
systemctl --user enable usbnic-session.service >/dev/null
systemctl --user start screen-portal.service
systemctl --user import-environment DISPLAY XAUTHORITY 2>/dev/null || true
systemctl --user start screen-manager.service
# usbnic-session 只是「登录后拉起 8787」的桥；没有 polkit 规则时 start 会被拒，
# unit 里用了 `-` 前缀忽略失败，不影响登录，门户会显示 8787 真实状态。
systemctl --user start usbnic-session.service 2>/dev/null || true

# 4) 登录自启项（桌面会话）
for name in screen-session screen-portal-open; do
  sed "s|@DIR@|$DIR|g" "$DIR/autostart/$name.desktop" > "$AUTOSTART_DIR/$name.desktop"
  chmod 644 "$AUTOSTART_DIR/$name.desktop"
  echo "已安装登录自启: $AUTOSTART_DIR/$name.desktop"
done
# 清掉旧的两份（由 wrapper 启动，注销后不会被 systemd 收走）
rm -f "$AUTOSTART_DIR/screen-manager.desktop" "$AUTOSTART_DIR/screen-portal.desktop"

echo
echo "门户:      http://127.0.0.1:8760/   （systemd 用户单元 screen-portal.service）"
echo "显示器管理: http://127.0.0.1:8765/   （systemd 用户单元 screen-manager.service）"
echo "查看状态:  systemctl --user status screen-portal screen-manager"
echo "停止自启:  $DIR/uninstall-autostart.sh"
echo
echo "── 8787（usbnicd，系统级 root 服务）需要一次性 sudo 配置 ──"
echo "原因：它要 nftables / 策略路由 / 读取 /etc/usbnic/hotspot.pass，无法降权到用户单元。"
echo "下面两条命令让门户能免密 start/stop/restart，并让它在注销时随会话停止："
echo "  sudo install -D -m 644 $DIR/systemd/usbnicd-session.conf /etc/systemd/system/usbnicd.service.d/10-screen-session.conf"
echo "  sudo install -D -m 644 $DIR/systemd/49-screen-usbnicd.rules /etc/polkit-1/rules.d/49-screen-usbnicd.rules"
echo "  sudo systemctl daemon-reload"
echo "不执行也不影响门户显示/打开 8787，只是门户里的启动/停止按钮会报 polkit 认证失败。"
