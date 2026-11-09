#!/usr/bin/env bash
# 安装「登录后自动常驻」：把 desktop 自启项写入 ~/.config/autostart（无需 sudo）。
# 卸载用 uninstall-autostart.sh。
set -eu
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$DIR/autostart/screen-manager.desktop"
DST="$HOME/.config/autostart/screen-manager.desktop"

chmod +x "$DIR/run-service.sh"
mkdir -p "$HOME/.config/autostart"
sed "s|@DIR@|$DIR|g" "$SRC" > "$DST"
chmod 644 "$DST"

echo "已安装开机自启: $DST"
echo "本次立即启动: $DIR/run-service.sh >/dev/null 2>&1 &   （或注销重新登录）"
echo "界面地址: http://127.0.0.1:${PORT:-8765}/"
echo "关闭自启: $DIR/uninstall-autostart.sh"
