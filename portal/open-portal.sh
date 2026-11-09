#!/usr/bin/env bash
# 登录时自动打开门户页面（由 autostart/screen-portal-open.desktop 调用）。
# - 先等门户起来（最多 15 秒，每 0.5 秒重试一次），避免开出浏览器错误页；
# - 同一个登录会话只开一次：用 $XDG_RUNTIME_DIR 下的标记文件去重（注销后自动清空）；
# - 不想自动打开：删掉 ~/.config/autostart/screen-portal-open.desktop 即可。
set -u
URL="http://127.0.0.1:8760/"
FLAG="${XDG_RUNTIME_DIR:-/tmp}/screen-portal.opened"

[ -e "$FLAG" ] && exit 0

ready=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 1 "${URL}api/status" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.5
done
[ "$ready" -eq 1 ] || exit 0

touch "$FLAG" 2>/dev/null || true
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 || true
fi
