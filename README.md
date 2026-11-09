# screen —— 本机显示器输出管理 + 闲置提醒

本机自用工具：把 Ubuntu 24.04 + GNOME 46 + X11（ubuntu-xorg）+ NVIDIA 595 驱动的
三屏布局可视化出来，**逐块开关某一路输出**，并提醒「这块屏似乎闲置，要不要关掉」。
界面为中文，逻辑只有一份（`backend/dm_core.py`），网页界面和命令行共用。

形态是**常驻的本地小服务**：固定监听 `127.0.0.1:8765`，根路径 `/` 就是界面，
登录后自动启动，浏览器打开即用；页面每 2 秒自动拉一次状态，不需要手动刷新。

同仓还带一个**本机服务门户**（`portal/`，`127.0.0.1:8760`），把这台机器上几个自建服务
（包括本工具和 8787 的网络管理）收成一张卡片入口页，见下文「本机服务门户」。

- 当前实测机型：RTX 3060 Laptop，输出 `DP-0`（AOC U27G3X，3840×2160@160，133% 缩放）、
  `DP-4`（内置 BOE 面板，2560×1600@165，133%，**本机当前是 primary**）、
  `HDMI-0`（便携屏，2880×1800@100，114% 缩放）。
  注意：需求描述里写的「DP-0 primary」与本机 `xrandr` 实际输出不一致（实际是 DP-4 primary），
  本工具只如实展示、不擅自改 primary。

## 适用范围（先读这个）

- **系统**：Linux；本机验证环境为 **Ubuntu 24.04 + GNOME 46 + X11（ubuntu-xorg 会话）**，
  NVIDIA 专有驱动 595（RTX 3060 Laptop），`xrandr 1.5.2` / RandR 1.6，Python 3.12。
- **前提**：`xrandr` 可用、能列出各输出与 CRTC 几何。所有开关动作都是 `xrandr` 命令，
  没有直接碰驱动或 sysfs。
- **Wayland：不可用 / 未验证**。Wayland 会话下 `xrandr` 一般只能读不能改布局，
  「分数缩放靠 transform」这套做法也不成立；本工具若在 Wayland 下运行，只会显示状态、
  开关会失败（失败信息与 xrandr stderr 会写进日志）。请在登录时选择
  「Ubuntu on Xorg」/ `ubuntu-xorg` 会话再使用。全局快捷键也依赖 X11 的 `XGrabKey`。
- **其他发行版**：需自行确认 `xrandr` 版本、`libXdamage.so.1` / `libXss.so.1` 是否存在、
  Python ≥ 3.10（代码用了 `X | None` 写法）。GNOME 之外的桌面环境未验证，
  尤其「关屏后 mutter 会重新套用自己的显示配置」这类行为在 KDE 等环境下可能不同。

## 快速开始

```bash
cd ~/eric/wuyan_github/local_service/screen

./install-autostart.sh     # 安装 systemd 用户单元 + 登录自启（无需 sudo）
./run-service.sh           # 或前台常驻运行（旧方式，崩溃自动重启）
./start.sh                 # 或临时前台运行并自动打开浏览器
```

- 界面入口：<http://127.0.0.1:8765/>（根路径直接返回 [web/display-manager.html](web/display-manager.html)）
- 端口固定为 `8765`：被占用时服务直接报错退出（退出码 3），**不会再漂移到别的端口**。
  这样书签、自启项、快捷键都指向同一个地址。想临时换端口可以 `PORT=9000 ./start.sh`。

### 常驻与登录自启（systemd 用户单元）

| 方式 | 命令 | 特点 |
| --- | --- | --- |
| 登录自启（推荐，已安装） | `./install-autostart.sh` | 写 `~/.config/systemd/user/screen-manager.service` + 登录会话脚本；注销时由 systemd 统一停止 |
| 手动常驻（旧方式） | `./run-service.sh` | 前台循环、崩溃重启；但它是普通会话进程，注销后可能残留，仅排查时用 |
| 临时前台 | `./start.sh` | 会打开浏览器，关掉终端服务就停 |

- 状态与日志：`systemctl --user status screen-manager`、`journalctl --user -u screen-manager -n 50`。
- `Restart=on-failure` 只兜崩溃；**点停止 / `systemctl --user stop` 不会被拉起**。
- dm_server 必须跑在图形会话的 X11 环境里，所以 unit 不写 `WantedBy=default.target`：
  由 `autostart/screen-session.desktop` 调 `start-session-services.sh`，在会话就绪后
  `systemctl --user import-environment DISPLAY XAUTHORITY` 再 `systemctl --user start`。
- 为什么不继续只用 autostart：本机 `/etc/systemd/logind.conf` 里是注释状态
  `#KillUserProcesses=no`（默认即 no），autostart 拉起的普通进程在注销时不会被杀，
  会变成孤儿继续占着 8765；systemd 用户单元在 `user@1000.service` 的 cgroup 里，
  `Linger=no` 时会话结束随用户管理器一起停掉。详见下文「本机服务门户」里的生命周期说明。

**关闭自启**：

```bash
./uninstall-autostart.sh    # 停单元、删 unit 与登录项，并停掉进程（不改动显示布局）
```

## 界面说明（纯点击，不需要输入任何文字/命令）

- **顶栏**：连接状态（`● 已连接` / `● 服务未连接，自动重试中`）、画布尺寸、启用块数、
  最近更新时间、**闲置阈值下拉框**、`刷新`、`恢复全部`、`日志` 按钮。
  页面每 2 秒自动拉取状态；`刷新` 只是「立即来一次」，不是唯一手段。
- **布局图**：按 `xrandr` 里各输出的 CRTC 几何（x/y/宽/高）等比绘制，块内标出
  名字、分辨率、位置、模式与刷新率、缩放（由 `几何宽 ÷ 模式宽` 推算）、是否 primary、是否内屏。
  点块可选中；**每块屏内部自带一个「关闭」/「关闭（内屏）」按钮**，点了就弹二次确认。
- **未启用的输出**：关闭后没有几何、画不出来，会出现在布局图下方的托盘里，带「开启」按钮。
- **右侧卡片**：同一份数据的明细 + 开/关按钮；顶部有 `已启用/已关闭`、`primary`、`内屏`、
  `你关闭的`、`闲置 X:XX` 等徽章。
- **闲置提醒**：某块屏超过阈值没有画面变化时，该块整体变暗，右上角出现
  「闲置 6:32　关闭」角标（角标可点，非模态，不打断操作）；右侧卡片同时给出
  「这块屏 X 无变化，要不要关掉（判据：…）」。**只提醒，不会自动关**。
- **阈值**：下拉选择（不提醒 / 1 / 2 / 5 / 10 / 15 / 30 分钟 / 1 小时），默认 5 分钟，
  改完立即生效，并写入 `~/.config/display-manager/config.json`。
- **确认框**：显示「要关闭谁、关闭后还剩几块、分别是哪块、画布尺寸怎么变」；
  关内屏会额外红字提示「笔记本面板会黑屏，画面留在剩下哪几块屏上」；
  如果关的正是浏览器窗口所在的那块屏，会出现一个默认打开的开关
  「关掉后 20 秒自动开回来」——页面在关屏后仍在后台运行，到点自动调开启，
  这样即使界面自己消失也能自己回来（也可以手动取消这个自动恢复）。

### 关于「点刷新提示刷新失败」这个历史 bug

- 现象：浏览器里点「刷新」出现 `刷新失败：Failed to fetch`。
- 真实原因：**不是接口 500，也不是并发 xrandr**。`/api/state` 本身正常
  （并发 8 次全部 200，服务端无 traceback，`~/.cache/display-manager.log` 无异常）。
  是之前的服务是「前台启动、跟着启动它的终端一起退出」——终端一关，进程就没了，
  页面还留在浏览器里，于是任何一次 fetch 都变成连接失败。
- 修法：① 服务改为常驻（登录自启 + `run-service.sh` 崩溃自动重启）；
  ② 前端区分「服务断连」与「接口报错」，断连时顶栏显示
  `● 服务未连接，自动重试中` 并继续自动重试，不再反复弹失败提示；
  ③ 平时每 2 秒自动更新，刷新按钮不再是唯一入口。
- 复现/验证证据（本机实测）：
  - 停服务前 `ss -lntp | grep 8765` 有监听；`kill` 掉 python 后 4 秒内
    wrapper 拉起新 pid，`curl /api/state` 立即恢复 200；
  - 重复启动第二个实例会打印「端口 8765 无法监听…」并以退出码 3 退出，不漂移；
  - 页面自动更新实测：不点任何按钮，5 秒内「更新于」从 21:41:09 变为 21:41:17；
    点「刷新」后变为 21:41:21 并提示「状态已更新」。

## 命令行（同一套逻辑）

```bash
python3 backend/dm_cli.py list            # 列出所有输出与布局（含闲置判断依据）
python3 backend/dm_cli.py json            # JSON 快照（脚本化用）
python3 backend/dm_cli.py plan  HDMI-0    # 只看「关掉会怎样」，不动手
python3 backend/dm_cli.py off   HDMI-0    # 关闭
python3 backend/dm_cli.py on    HDMI-0    # 按关闭前记录的四件套恢复
python3 backend/dm_cli.py toggle HDMI-0
python3 backend/dm_cli.py restore-all     # 恢复所有有记录 / 有看门狗基线的屏
python3 backend/dm_cli.py log             # 最近的操作日志
python3 backend/dm_cli.py serve           # 等价于 start.sh
```

命令行不依赖浏览器，也不依赖界面所在的那块屏，是本工具最可靠的救急入口。

## 全局快捷键（不自锁的恢复入口）

- **`Ctrl+Alt+Shift+F12` = 恢复全部输出**（等价于 `restore-all`）。
- 由后端用 X11 `XGrabKey` 自己抓键，不装 `xbindkeys` / `xdotool`，也不改 GNOME 快捷键设置。
- 注册结果会写日志：`全局快捷键注册: Ctrl+Alt+Shift+F12 -> restore-all，ok=True/False`。
  如果组合键被别的程序占用（BadAccess），日志会写明，但主服务不受影响。
- 注意：**不要用 `Ctrl+Alt+Shift+R`**——那是 GNOME 自带的录屏快捷键，会被 Shell 先占用
  （开发过程中实测踩到过，会启动录屏而不是恢复屏幕）。所以最终选 F12。

## 开关的实现方式：模式 + 刷新率 + transform + 位置「四件套」

分数缩放靠 xrandr 的 CRTC `--transform` 实现，**只给 `--mode` 不给 `--transform` 会让缩放掉回 100%**。

- **关闭**：先读当前 `模式 / 刷新率 / transform 九元组 / pos / 几何 / primary`，
  写进 `~/.cache/display-manager.state`，再执行 `xrandr --output X --off`。
- **开启**：用一条命令同时下发
  `xrandr --output X --mode 2880x1800 --rate 100.00 --transform 1.144440,0,0,0,1.144444,0,0,0,1 --pos 4289x2880`
  （primary 屏再加 `--primary`），完成后重新读状态校验 几何=期望值，并检查是否与其他屏重叠，
  任何偏差都会写进日志、也回显在界面上。
- transform 不是几何宽高比硬算出来的，而是关闭那一刻从 `xrandr --current --verbose`
  原样抓的九元组，避免四舍五入漂移（DP-0 实测 1.333328、DP-4 1.331238、HDMI-0 1.144440）。

## 保底规则（硬性）

1. **不关最后一块亮屏**：关闭前统计 `xrandr --query` 里已启用的输出数，等于 1 时直接拒绝，
   界面按钮置灰为「最后一块亮屏不可关」。
2. **关闭前二次确认**：确认框显示「关闭后剩余几块、分别是哪几块、画布尺寸从多少变成多少」。
3. **内屏（DP-4）允许关闭，但有额外提示**：需求已明确「三块里至少一块亮着就行」，
   所以内屏不再特殊拦截。确认框会红字提示：笔记本自带面板会黑屏，
   画面会留在剩下的 N 块屏上（列出名字）；如果界面正好在内屏上，还会出现
   默认开启的「关掉后 20 秒自动开回来」保险。
4. **所有操作落日志**：`~/.cache/display-manager.log`，包含关闭前的四件套、恢复来源、
   校验结果；xrandr 失败时把原始 stderr 一并写入。
5. **恢复入口四件套**：命令行 `on` / `restore-all`；界面「恢复全部」；
   全局快捷键 `Ctrl+Alt+Shift+F12`；关掉界面自身所在屏时默认 20 秒自动开回。
   下次打开界面时该输出会以「已关闭」出现在列表和托盘里，点一下就能开回来（参数来自状态文件）。

## 与 `~/.local/bin/hdmi-watchdog.sh` 的共存（已打补丁）

读源码 + 看它的日志后的结论：

- 看门狗默认 `FORCE_RECOVER=0`，自启项 `~/.config/autostart/hdmi-watchdog.desktop`
  没有传环境变量，所以实际运行在 0。该模式下它**只在「先观测到 disconnected，之后又回来但没启用」**
  这个特征序列下才补 `xrandr`；用户主动关闭（connected 但没有几何）不会被拉起，
  它的日志实测就是这么写的：`HDMI-0 被关闭且无掉线前兆（可能是用户主动关闭），保留基线但不自动点亮`
  （2026-11-09 的多条记录）。所以**本界面的日常关闭本来就与看门狗相容**。

### 残留冲突已修复（2026-11-09 打补丁，经用户批准）

原冲突：在本界面关掉 `HDMI-0` 之后，如果**物理拔插一次线**（真实 HPD 掉线又回来），
看门狗会认为这是故障，用 `~/.cache/hdmi-watchdog.layout` 里的基线把它点亮。

- 改动文件：`~/.local/bin/hdmi-watchdog.sh`（**不在本仓库内**，这是跨项目改动，在此留档）
- 备份：`~/.local/bin/hdmi-watchdog.sh.bak-20261109-2155`（与原文件同权限，可直接回滚）
- 补丁内容：

  ```bash
  # 变量区新增
  USER_OFF="${USER_OFF:-$HOME/.cache/display-manager.off}"
  is_user_off() { [ -s "$USER_OFF" ] && grep -qxF -- "$1" "$USER_OFF"; }

  # 「掉线后又回来但未启用」分支，recover 前：
  if is_user_off "$n"; then
    log "$n 在界面主动关闭名单里（$USER_OFF），跳过自动恢复"
    WAS_ON[$n]=0; SAW_DISC[$n]=0
    continue
  fi

  # FORCE_RECOVER=1 的兜底分支同样检查（默认 0，不影响现状）：
  if is_user_off "$n"; then
    log "$n 在界面主动关闭名单里（$USER_OFF），跳过兜底还原"
    WAS_ON[$n]=0; unset 'OFF_TS[$n]'
    continue
  fi
  ```

- **名单清理逻辑已存在并实测**：`dm_core.turn_on()` 成功恢复（以及本来就已启用）时都会调用
  `off_unmark(name)`，把该输出从 `~/.cache/display-manager.off` 删除。
  实测：`dm_cli.py off HDMI-0` → 名单变为 `HDMI-0`；`dm_cli.py on HDMI-0` → 名单变回空。
  所以以后拔插线时，只有「仍被用户关着」的屏才被跳过，重新开过的屏恢复正常兜底。
- 验证证据：
  - `bash -n ~/.local/bin/hdmi-watchdog.sh` → OK。
  - 假 xrandr 构造「on → disconnected → connected 但 off」序列（两组独立临时目录）：
    - 名单为空：日志出现 `HDMI-0 已连接但未启用，按基线还原`，随后 `已启用，状态正常`，
      并实际调用 `xrandr --output HDMI-0 --mode 2880x1800 --rate 100.00+ --pos 0x0`；
    - 名单含 `HDMI-0`：日志出现 `HDMI-0 在界面主动关闭名单里（…/user.off），跳过自动恢复`，
      **没有**任何 `xrandr --output` 调用，状态保持 off。
  - 真机重启看门狗（旧 pid 512080 → 新 pid 694604）后，日志出现新的
    `start: panel=[DP-4] guarded=[auto] interval=3s`，三屏布局与重启前完全一致；
    随后真机 `off HDMI-0` / `on HDMI-0`，看门狗日志仍为
    `被关闭且无掉线前兆（可能是用户主动关闭）…` / `已启用，状态正常`，没有抢控制权。
- 生效方式：脚本改动只对**新启动**的进程生效。本次已手动重启；
  下次登录 autostart 也会用新脚本。手动重启命令（不会改布局）：

  ```bash
  pkill -f "[h]dmi-watchdog.sh"
  setsid -f ~/.local/bin/hdmi-watchdog.sh >/dev/null 2>&1
  ```

- 回滚：`cp -a ~/.local/bin/hdmi-watchdog.sh.bak-20261109-2155 ~/.local/bin/hdmi-watchdog.sh`
  后按上面的命令重启即可。
- 恢复被跳过的屏：在本界面点「开启」或 `python3 backend/dm_cli.py restore-all`
  （会同时清掉名单），看门狗随即恢复原有兜底行为。

## 与 `~/.cache/screen-off.state` 的关系

`~/.local/bin/screen-off.sh` 已有 list/off/on/toggle/restore-all，也有「拒绝内屏/拒绝最后一块」的保护。
本工具**没有调用它，而是把逻辑并在后端**，取舍如下：

- 需要原样保存 transform 九元组并做逐块几何校验、重叠检查，脚本里不好表达；
- 需要和闲置检测、Web 界面共用同一份状态与同一把锁，避免两个进程抢 xrandr；
- 策略差异：`screen-off.sh` 仍拒绝关内屏，本工具按新需求允许关内屏（只保留「不能关到 0 块」）。
  两者**不要同时操作**同一块屏。
- 信息是通用的：本工具把四件套存在 `~/.cache/display-manager.state`，
  若用本工具关屏后想用 `screen-off.sh on X` 救，它会回退到 `~/.cache/hdmi-watchdog.layout`
  的基线（同样是四件套），照样能开回来；反过来 `screen-off.sh` 关的屏，本界面也能用
  「看门狗基线」把参数猜回来。两套工具互不破坏。

## 闲置检测：原理与局限

- **原理**：X11 没有「某输出画面是否更新」的接口。本工具在根窗口上开 XDamage
  （`XDamageReportRawRectangles`，通过 ctypes 调 `libXdamage`，**不需要装任何包**），
  把每条脏矩形与各输出的 xrandr 几何求交，命中就把该输出「最后变化时刻」刷新。
  CPU 保护：一次唤醒最多消化 800 条事件，剩余下一轮；只保存「每块屏最后一个时间戳」，
  不逐事件落盘；`select` 0.5 秒超时 + 几何每 5 秒才重读一次。
- **降级**：拿不到 XDamage 时改用 XScreenSaver 的全局空闲（`libXss`，不需要 `xprintidle`），
  此时所有屏共用一个计时，界面顶部「闲置判据」会写明
  `XScreenSaver 全局空闲（无法区分是哪块屏）`。
- **局限（务必知悉，界面上直接展示判据）**：
  1. 静态画面（壁纸、静止的窗口）本来就不产生 Damage，会被判成「没变化」，从而可能误报闲置；
  2. 鼠标在别的屏上移动不会给这块屏产生 Damage（这正是想要的），但「屏亮着、没人看」也会被判闲置；
  3. 视频若走硬件叠加层 / 直接扫描输出，可能完全不产生 Damage；
  4. 本机实测：mutter 的合成器图层（GNOME 顶栏时钟、Shell 动画）**没有**产生可观测的根窗口
     Damage —— 顶栏所在的 DP-4 在 96.8 秒内计时一直增长。也就是说「明明有东西在动」的屏
     也可能被判闲置，只能靠阈值 + 人工确认兜底；
  5. 刚启动界面时，某屏此后一直没变化，计时从启动那一刻算起（不是从「上次真实变化」算）。
  所以它是**近似信号**，界面文案也写成「似乎闲置 / 要不要关掉」，且必须人工确认才关。

## 亮度调节（每块屏一条滑杆）

界面右侧每块已连接屏的卡片里有一条亮度滑杆：拖动时只更新本地百分比，**最多每 130ms 提交一次**，
松手立刻提交最终值，不会按像素发请求（`ddcutil` 单次 100~300ms，堆起来会卡）。

### 三条通道与选择顺序

| 通道 | 用于 | 判定方式 | 特点 |
| --- | --- | --- | --- |
| `硬件 DDC/CI` | 外接屏（DP-0 / HDMI-0） | 有 `ddcutil` + `/dev/i2c-*` 可读写 + EDID 能对上 DRM 连接器 | 改显示器背光，画质最好 |
| `硬件背光` | 内屏（DP-4/eDP） | `/sys/class/backlight/*` 存在且 `brightness` 可写 | 改面板背光 |
| `软件调光（影响对比度）` | 任意已启用输出 | `xrandr --output X --brightness 0.00~1.00` | 改显卡 gamma，亮度降时对比度也降 |
| `不支持调节` | 屏已关闭 / 无任何通道 | — | 滑杆禁用，卡片里写原因与所需权限 |

本机实测（2026-11-09）：

- **DP-0（AOC U27G3X）**：`ddcutil` **未安装**，`/dev/i2c-*` 是 `0600 root`、系统里也没有 `i2c` 组，
  所以现在走**软件调光**；装好 ddcutil + i2c 权限后会自动改用硬件 DDC，不用改代码。
- **DP-4（eDP 内屏）**：`/sys/class/backlight/nvidia_0` 存在，但 `brightness` 是 `root:video 0660`，
  当前用户不在 `video` 组 → 写不进去；X 上的 `Backlight` 属性是**只读**的
  （`xrandr --output DP-4 --set Backlight 40` 返回 `BadValue`）。所以现在也走软件调光。
- **HDMI-0（便携屏）**：DDC 未探测（缺 ddcutil）；开启后按软件调光处理。

### 需要你自己执行的权限配置（按需，可都不做）

外接屏走 DDC/CI（DP-0、HDMI-0）：

```bash
sudo apt install ddcutil
echo 'SUBSYSTEM=="i2c-dev", KERNEL=="i2c-[0-9]*", TAG+="uaccess"' \
  | sudo tee /etc/udev/rules.d/60-i2c-uaccess.rules
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=i2c-dev
```

`/dev/i2c-*` 本机已经存在；若 `ddcutil detect` 报打不开总线，先 `sudo modprobe i2c-dev`。
不想用 udev 也可以建 `i2c` 组把用户加进去，但 `uaccess` 只在本地登录会话生效，范围更小。

内屏走硬件背光（DP-4）：

```bash
sudo usermod -aG video "$USER"     # 重新登录后生效
```

没配权限时滑杆**不会失效**：外接屏和内屏都退到软件调光，卡片里写明
「DDC 不可用：…」/「硬件背光 65%（只读，需 video 组权限）」。

### 失败反馈

- 设置失败时，滑杆下方红字给出原因，同时右下角 toast；例如屏已关闭：
  `屏已关闭，gamma 调光无法生效`。
- DDC 打不开总线时错误里直接带 udev 命令；背光写失败时带 `usermod -aG video`。
- 单块屏探测失败不影响其它屏，也不影响布局图与开关功能。

### 持久化与恢复

- 位置：`~/.cache/display-manager.brightness.json`（每块屏最后一次的 `value` / `mode` / 时间）。
- 服务启动 0.8 秒后由后台线程对**已启用**输出重新下发一次；屏是 `off` 时不应用。
- 「关闭 → 开启」某块屏后，`on` 成功会立刻再应用一次上次的亮度（模式设置会复位 gamma）。
- 通道从「软件调光」切到硬件时会把 gamma 复位为 1.0，避免双重压暗。

### API / CLI（门户后续可直接复用）

```bash
curl -s http://127.0.0.1:8765/api/brightness          # 亮度总览
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"name":"DP-0","value":60}' http://127.0.0.1:8765/api/brightness

python3 backend/dm_cli.py brightness                  # 每块屏的通道与当前值
python3 backend/dm_cli.py brightness get  DP-0        # 走硬件通道时实时读
python3 backend/dm_cli.py brightness set  DP-0 60     # 0~100
```

### 验证记录（本机，2026-11-09）

- DP-0：`set 70` 后 `xrandr --verbose` 读到 `Brightness: 0.70`，`set 100` 回到 `1.0`。
  截图（ffmpeg x11grab）看不出明暗变化是正常的：截图读 framebuffer，不经过显卡 gamma LUT，
  视觉确认只能看物理屏。
- DP-4：开启后同样实测 `1.0 → 0.60 → 1.0`；`off` 再 `on` 后 `Brightness` 自动回到 `0.60`
  （日志：`已重新应用 DP-4 的亮度`）。测完已恢复到 100% 并重新关闭内屏，显示器状态与测试前一致。
- HDMI-0 当前是关闭状态，通道判定为软件调光，未采集画面证据。
- **未验证**：AOC 上 `setvcp 10` 的实际效果 —— 本机没有 ddcutil，我也没有 sudo 权限安装。
  装好后用 `dm_cli.py brightness get DP-0` 会走 `getvcp 10`，即可核对读数与画面。

## 界面把自己的屏关掉了怎么救

1. 最快：按全局快捷键 **`Ctrl+Alt+Shift+F12`**（恢复全部），不需要浏览器、不需要切终端。
2. 任意终端里：

   ```bash
   python3 ~/eric/wuyan_github/local_service/screen/backend/dm_cli.py on DP-4
   # 或者不知道关了哪块
   python3 ~/eric/wuyan_github/local_service/screen/backend/dm_cli.py restore-all
   ```
3. 如果连手边那块屏也黑着，用 `Ctrl+Alt+F3` 切到文本终端执行上面的命令，再 `Ctrl+Alt+F1/F2` 切回图形界面。
4. 也可以 `xrandr --output DP-4 --auto` 先亮起来，再用本工具的 `on` 校正位置/缩放。
5. 从界面关掉「界面自己所在屏」时，确认框里默认勾选「关掉后 20 秒自动开回来」，
   页面会在后台到点自己调用开启；如果不想等，也可以手动取消后用上面的方式救。
6. 界面重开后，被关掉的输出会以「已关闭」列在右侧和底部托盘里，点「开启」即可。

## 为什么做成「常驻本地服务 + 轮询」

对照本机 `http://127.0.0.1:8787/`（usbnic 上位机）的形态做了核实：

- 8787 是 `uvicorn`（FastAPI）常驻进程；根路径直接返回单页应用（`/static/style.css`、`/static/app.js`），
  前端用 `setInterval(refresh, POLL_MS)` + `fetch("/api/xxx")` 轮询，`POLL_MS = 2000`；
  通过 `~/.config/autostart/usbnic-host.desktop` 登录自启。
- 结论：**它就是「常驻本地服务 + 固定端口 + 根路径即界面 + 2 秒轮询」**，反馈及时、打开即用，
  不需要用户记启动命令。这套形态对我们同样合适，因此本工具照此改造：
  固定 8765、单页应用、登录自启、根路径即界面、2 秒轮询、状态自己更新。
- 为什么用轮询而不是 SSE/WebSocket：状态源是 `xrandr`，本来就要隔一会儿重读；
  2 秒一次的本地 HTTP 请求开销可以忽略，实现只用标准库、断线重连逻辑简单，
  也和 8787 的行为一致。真正昂贵的 `xrandr` 改布局命令只在点按钮时执行。

## 本机服务门户（127.0.0.1:8760）

`portal/` 是一个独立小服务：固定入口页，把本机几个自建服务收成卡片，显示在线/离线、
最近检测时间、延迟、端口、托管方式，并提供打开 / 启停按钮。它和 8765 同仓但互不依赖，
门户挂了不影响被管理的服务。

### 端口与启动

- 固定 `127.0.0.1:8760`，被占用时打印原因并以退出码 3 退出（不漂移）。
- 选 8760 的理由：紧邻 8765 好记；实测 8765、8787 在听、8760 空闲；
  刻意避开这两个端口，谁先起都不会撞。
- 托管方式与 8765 一致：**systemd 用户单元** `screen-portal.service`（`WantedBy=default.target`），
  `Restart=on-failure`，登录即起；纯 HTTP，不需要 X11。
- 手动前台：`./portal/run-portal.sh`（旧方式，崩溃循环重启）；临时：`python3 portal/portal_server.py`。

### 登录后自动打开门户

- `autostart/screen-portal-open.desktop` → `portal/open-portal.sh`：先轮询
  `http://127.0.0.1:8760/api/status`（最多 15 秒，每 0.5 秒一次）等门户就绪，
  再用 `xdg-open` 打开；用 `$XDG_RUNTIME_DIR/screen-portal.opened` 标记去重，
  **同一登录会话只开一次**（实测连跑两次只调用一次 `xdg-open`）。
- 不想自动打开：删掉 `~/.config/autostart/screen-portal-open.desktop`，或把其中
  `X-GNOME-Autostart-enabled=true` 改成 `false`；`./uninstall-autostart.sh` 也会删。
- 注意：`~/.config/autostart/usbnic-host.desktop` 是 8787 自己的登录项，gateway 模式下会另开
  8787 的页面；如果不想登录后出现两个标签，把那一项也禁用。

### 服务清单：portal/services.json（热加载）

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 唯一标识，前端按它更新卡片、发启停请求 |
| `name` / `desc` | 是 / 否 | 卡片标题 / 一句话用途 |
| `url` | 是 | 打开按钮跳转的真实地址，端口也从这里解析 |
| `health` | 否 | 健康检查路径，默认 `/`；以 `/` 开头按站点根拼，否则相对 `url` 目录，也可写完整 `http(s)://` |
| `icon` / `color` | 否 | 一个字符/emoji、卡片左侧色条 |
| `embed` | 否 | 是否提供「在此页内打开」，默认 `false`；**实测无 X-Frame-Options / CSP 才写 true** |
| `manage` | 否 | `user-unit` / `system-unit` / `external`（只展示、无启停按钮） |
| `unit` | 托管时必填 | systemd 单元名，如 `screen-manager.service` |
| `start_on_login` | 否 | 仅展示提示用：下次登录是否会被拉起 |
| `note` | 否 | 卡片上「单元」标签的悬停说明 |

- 改完保存即生效：探测轮每 5 秒比对 mtime 重读，卡片增删不用重启门户。
- 模板：[portal/services.example.json](portal/services.example.json)。
- 停用某个服务：删掉那一项；停整个门户：`systemctl --user disable --now screen-portal`。

### 托管方式与「随会话停止」的实际含义

| 服务 | 端口 | 托管 | 停止后会被拉起吗 |
| --- | --- | --- | --- |
| 屏幕输出管理 | 8765 | `screen-manager.service`（systemd **用户**单元） | 不会；下次登录由 `screen-session.desktop` 启动 |
| 网络管理 usbnicd | 8787 | `usbnicd.service`（systemd **系统**单元，root） | 不会；下次登录由 `usbnic-session.service` 拉起 |

**8787 为什么不是用户单元**：它的代码在 `~/eric/wuyan_github/local_service/usb_wireless_network`，官方启动/停止是
`scripts/host-start.sh` / `host-stop.sh`，都要 root（`pkexec`/sudo），因为要写 nftables、策略路由、
TUN、TPROXY，还要读 `/etc/usbnic/hotspot.pass`（0600 root）。实际进程是系统单元 `usbnicd.service`
（`User=root`、`/usr/bin/python3`、`WorkingDirectory=.../host`、配置 `host/config/host.yaml`、
日志 `host/data/usbnicd.log`、无 venv）。强行改成 `systemctl --user` 会直接丢功能，所以采用等价方式：

- **保留系统单元**，不碰它的业务代码与依赖；
- 系统 drop-in 加 `PartOf=user@1000.service`：最后一个会话结束时 `user@1000.service` 停，
  本服务一起停；`ExecStop` 调用它自己的 `usbnic_host.cleanup --proxy`（与 `host-stop.sh` 的清理等价），
  释放 nftables / 路由 / 代理；
- 用户单元 `usbnic-session.service`（oneshot）负责下次登录把它 `systemctl start` 回来；
- 门户的启停按钮走 `systemctl`（不加 `--user`），需要一条只放行 `usbnicd.service` 的 polkit 规则。

这三件事需要一次性 sudo（脚本只打印、不代跑）：

```bash
sudo install -D -m 644 ~/eric/wuyan_github/local_service/screen/systemd/usbnicd-session.conf \
  /etc/systemd/system/usbnicd.service.d/10-screen-session.conf
sudo install -D -m 644 ~/eric/wuyan_github/local_service/screen/systemd/49-screen-usbnicd.rules \
  /etc/polkit-1/rules.d/49-screen-usbnicd.rules
sudo systemctl daemon-reload
```

不装也能用：门户照常显示/打开 8787、8765 的停止也正常；只有 8787 的启停按钮会提示
polkit 认证失败（`最近操作 … 失败`），注销时 8787 也不会自动停。

**关于 `KillUserProcesses=no`（本机 logind 默认，配置文件里是注释状态）**：

- 它表示注销时**不杀会话里的普通进程**，这些进程会移到 `user-1000.slice` 下继续跑。
  旧版 `run-service.sh` / `run-portal.sh` 由 autostart 拉起，正是这类进程——注销后会变孤儿，
  这就是必须改造的原因。
- 但它**不影响** `user@1000.service`：`Linger=no`（本机实测 `Linger=no`）时，最后一个会话结束，
  logind 会停掉用户管理器；用户单元都在它的 cgroup 里（`systemctl --user show -p ControlGroup`
  显示 `user@1000.service/app.slice/...`），所以会一起被停、端口释放。`KillUserProcesses` 管的是
  会话 scope，不是用户管理器的生死。
- 若有人执行过 `loginctl enable-linger`（`Linger=yes`），用户管理器会常驻，注销后单元不会停；
  用 `loginctl disable-linger "$USER"` 恢复。
- 关机/重启：systemd 按依赖停所有单元（含 `user@1000`），同样会释放。
- 8787 的系统 drop-in 依赖 `user@1000.service` 这个名字；换用户名或 UID 要同步改 drop-in。

**资源释放怎么算**：端口从 `ss -lntp` 消失、对应 python 进程全部退出（无孤儿）、
单元状态为 `inactive`。若单元因启动失败累计触发 start limit，状态会是 `failed`：
门户的「启动/重启」会先 `systemctl reset-failed` 再启动；也可手动
`systemctl --user reset-failed <unit>`（系统单元去掉 `--user`）。

### 探测与打开方式

- 后端每 5 秒一轮、每个服务一个线程，`urllib` + 1.5 秒超时；前端每 2 秒只读 `/api/status`，
  **不在浏览器里直连被管理服务**，不受 CORS / 代理干扰。
- 有 HTTP 响应且状态码 < 500 算在线（4xx 会提示「健康路径是否配错」）；拒绝/超时算离线。
  探测失败只标红写字，不弹窗、不做任何写操作。
- 默认新标签页打开。实测 8765、8787 响应头都**没有** `X-Frame-Options` / `Content-Security-Policy`，
  所以这两项都提供「在此页内打开」（整页 iframe + 顶部「← 返回门户 / 新标签页打开」，`Esc` 也可返回）。
- 离线时「在此页内打开」置灰；启停按钮点击后会在卡片底部显示「最近操作 时间 动作 成功/失败：原因」。

### 要不要做成通用框架？

**结论：就做静态门户 + JSON 清单，不抽框架。** 新增服务已经只需加一段 JSON；
再往上做注册中心/插件/自服务注册，就要定义协议、生命周期、权限，而本机只有个位数服务。
真正重复的「探测 + 超时 + 标红」只有几十行，已集中在 `probe_round()`；托管部分也只是
`systemctl` 薄壳。等出现「十几个服务 + 分组/鉴权/告警」再抽象，那时接口才看得清。

### 验证记录（本机，2026-11-09）

- 门户由 `screen-portal.service` 启动，`/`、`/api/status` 200；当前登记两张卡片：
  8765 在线 200（unit active）、8787 在线 200（unit active）。
- 门户「停止」8765：端口 8765 立即消失、unit 变 `inactive`、8 秒后仍未被拉起；
  「启动」后 `/api/state` 回到 200。
- 停 `screen-portal`：8760 释放、无 `portal_server` 残留，re-start 后 200。
- 探测翻转：SIGSTOP 8765 后 5 秒内变「离线 / 请求超时 / 1501ms」，SIGCONT 后回到在线
  （开发期还曾临时登记 8080 做过同样的翻转验证，后来按要求把该项从清单里移除）。
- 自动打开：假 `xdg-open` 记录到一次调用，连跑两次不重复；门户未就绪时会等待重试。
- **未做/未验证**：8787 的实际「停止→恢复」没有执行——它需要上面三条 sudo 才安全可控，
  且本会话没有 sudo 密码；其余路径（在线探测、unit 状态、启动按钮）已实测。
  浏览器自动化环境不可用，页面用 curl/静态检查验证，未采集真实浏览器 console。

## 运行期文件

| 路径 | 用途 |
| --- | --- |
| `~/.cache/display-manager.state` | 关闭前保存的四件套（模式/刷新率/transform/位置/primary） |
| `~/.cache/display-manager.off` | 用户主动关闭的输出名单（供与看门狗共存、界面标注） |
| `~/.cache/display-manager.log` | 操作日志（含 xrandr 原始 stderr、快捷键注册/触发） |
| `~/.cache/display-manager.brightness.json` | 每块屏上次的亮度值与所用通道 |
| `~/.config/display-manager/config.json` | 闲置阈值（默认 300 秒，0 = 关闭提醒） |
| `~/.config/systemd/user/screen-manager.service` | 8765 的用户单元（`install-autostart.sh` 生成） |
| `~/.config/systemd/user/screen-portal.service` | 8760 门户的用户单元 |
| `~/.config/systemd/user/usbnic-session.service` | 登录时拉起 8787 的桥单元 |
| `~/.config/autostart/screen-session.desktop` | 会话就绪后导入 DISPLAY 并启动 8765 单元 |
| `~/.config/autostart/screen-portal-open.desktop` | 登录后自动打开门户（可删） |
| `portal/services.json` | 门户的服务清单（改完 5 秒内热加载） |
| `~/.cache/hdmi-watchdog.layout` | 看门狗的基线，本工具**只读**，用作没有关闭记录时的恢复兜底 |

`.gitignore` 已排除日志、状态、`__pycache__`、截图等运行时产物。

## 依赖

- Python 3.12（本机自带），**只用标准库**：`http.server` / `subprocess` / `ctypes`。
- 系统库：`libX11.so.6`、`libXdamage.so.1`、`libXss.so.1`（本机已随 GNOME 安装，无需装包）。
- 不用 sudo，不装 xscreensaver / xprintidle / xdotool / xbindkeys。
  （例外 1：8787 的「随会话停止 + 免密启停」需要一次性 sudo drop-in/polkit，见上文，可选。
  例外 2：想要外接屏走硬件 DDC 需要 `ddcutil` + i2c udev 规则，想要内屏走硬件背光需要加入
  `video` 组；两者都不装也能用，会自动降级到软件调光，见「亮度调节」一节。）

## 已知限制

- 只在 X11（ubuntu-xorg）下成立；Wayland 会话里 `xrandr` 改不了布局，全局快捷键也抓不到。
- GNOME/mutter 在 RandR 变化后有时会重新套用自己的显示配置，若发现关掉后又被拉回，
  先看 `~/.cache/display-manager.log` 与 `~/.cache/hdmi-watchdog.log` 确认是谁动的手。
- 闲置检测只覆盖「画面变化」，不区分「有变化但没人看」和「没人看但无变化」。
- 软件调光是改显卡 gamma，亮度降低时**对比度也会降低**；外接屏拿到 DDC/i2c 权限、
  内屏拿到 video 组权限后会自动改用硬件通道，画质更好。
- 亮度值只记「本工具最后一次设置的值」；用显示器实体按键或没经过本工具改动过亮度时，
  界面显示可能滞后，点「刷新」（`/api/brightness?refresh=1`）会重新读硬件值。
- 未连接（disconnected）的输出不参与开关与统计。
- 内屏（eDP）关闭后理论上存在个别 NVIDIA 机型唤不回来的风险；本机实测可正常恢复
  （见下一节），如果换机器出现唤不回，用 `Ctrl+Alt+Shift+F12` / `dm_cli.py restore-all` /
  `xrandr --output DP-4 --auto` 依次尝试，最坏情况重启图形会话。
- 8787 是 root 系统单元；不装可选 sudo drop-in 时，注销不会停它，门户里它的启停会因
  polkit 认证失败（其余服务不受影响）。

## 验证状态

已验证（本机真机，2026-11-09）：

- **界面入口与形态**：`GET /` 与 `/display-manager.html` 均 200，`/index.html` 返回 404
  （旧的 `index.html` 已删除，只保留一份）；页面里**没有文本输入框**（`<input type=text|number>`
  与 `<textarea>` 均为 0），只有 1 个 `<select>`（闲置阈值）和每块屏 1 条
  `<input type="range">` 亮度滑杆；所有操作都是点击或拖动滑杆。
- **常驻与自愈**：`run-service.sh` 启动后，`kill -9` 掉 python，4 秒内出现新 pid，
  `/api/state` 立即恢复 200；重复启动第二个实例打印端口占用并以退出码 3 退出。
  现已迁移到 `screen-manager.service`：`systemctl --user stop` 后端口释放、unit inactive、
  8 秒内不会被 `Restart=on-failure` 拉起；`start` 后 `/api/state` 200。
- **页面自动更新与刷新**：不点按钮 5 秒内「更新于」从 21:41:09 → 21:41:17；
  点「刷新」后 21:41:21 并提示「状态已更新」。
- **HDMI-0 经界面关闭再开启**（浏览器点击，非命令行）：

  ```text
  关闭前  HDMI-0 connected 3296x2060+4289+2880   2880x1800@100  transform 1.144440
         Screen 0: current 7585 x 5010
  界面关闭（确认框显示"要关闭 HDMI-0，剩余 2 块：DP-0, DP-4，画布 7585×5010 → 5120×5010"）
  关闭后  HDMI-0 connected（无几何）     Screen 0: current 5120 x 5010
  界面开启（托盘"开启"）
  恢复后  HDMI-0 connected 3296x2060+4289+2880   2880x1800@100  transform 1.144440
         overlaps = []，画布回到 7585x5010
  ```
- **内屏 DP-4 关闭再开启**（`dm_cli.py off DP-4` → `dm_cli.py on DP-4`）：

  ```text
  关闭前  DP-4 connected primary 3408x2130+881+2880  2560x1600@165  transform 1.331238
  关闭后  DP-4 connected primary（无几何）  Screen current 7585 x 4940
  恢复后  DP-4 connected primary 3408x2130+881+2880  2560x1600@165  transform 1.331238
         overlaps = []，画布回到 7585x5010；DP-0 / HDMI-0 未动
  ```
  Xorg 日志（`~/.local/share/xorg/Xorg.0.log`）在关闭/恢复期间只出现 NVIDIA 连接器重探：
  `BOE Technology Group Co., Ltd (DFP-5): connected / Internal DisplayPort / 2670.0 MHz maximum pixel clock`，
  **eDP 始终是 connected**（只是 CRTC off），没有新增 `(EE)` 行（日志里的 3 条 EE 均为 X 启动期
  与鼠标事件延迟，和本操作无关）。说明这块 eDP 关掉后能正常唤回。
- **全局快捷键**：先用 `dm_cli.py off HDMI-0` 关掉一块，再用 XTEST 合成
  `Ctrl+Alt+Shift+F12`，日志出现
  `快捷键 Ctrl+Alt+Shift+F12 恢复全部: 已启用 HDMI-0（状态文件（关闭前的四件套））`，
  `xrandr` 恢复为 `3296x2060+4289+2880`。
- **界面内屏按钮与提示**：DP-4 卡片按钮为「关闭（内屏）」且非禁用；布局图块内同样有该按钮；
  点开确认框实测红字为「这是笔记本内屏（DP-4），关掉后笔记本自带面板会黑屏；画面会留在剩下的
  2 块屏上（DP-0, HDMI-0）」+「这是 primary 屏，关闭后 primary 会转移到别的屏」，
  并显示「关闭后剩余 2 块：DP-0, HDMI-0」「画布尺寸 7585×5010 → 7585×4940」，取消后布局不变。
- **闲置提醒**：把阈值经下拉框临时改成 1 分钟，等待后 HDMI-0 `idle_seconds=116.2`、
  DP-4 `idle_seconds=341.6` 均被标记 `idle=true`，布局图出现「闲置 X:XX　关闭」角标、
  卡片出现「这块屏 X 没有画面变化，要不要关掉（XDamage 变化（逐屏近似））」，无模态遮挡；
  测完已把阈值恢复成 300 秒。
- 恢复后三屏几何：DP-0 `5120x2880+0+0`、DP-4 `3408x2130+881+2880`（primary）、
  HDMI-0 `3296x2060+4289+2880`，`overlaps = []`。
- **看门狗补丁**：`bash -n` 通过；假 xrandr 两用例分别验证「名单外仍自动恢复」「名单内跳过」；
  真机重启看门狗（512080 → 694604）后三屏布局不变；真机 `off HDMI-0` / `on HDMI-0`
  验证名单的写入与自动清理（详见上文「与 hdmi-watchdog.sh 的共存」）。

尚未验证 / 待补：

- 「拒绝关闭最后一块亮屏」没有真机触发（要先把另外两块都关掉，风险不划算），
  仅代码路径与界面置灰逻辑审阅过；
- 页面在「服务完全停掉后仍停留于已加载页面」时顶栏显示
  「服务未连接，自动重试中」的视觉效果：headless 浏览器在停服后直接跳到了 Chrome 错误页，
  没能截到应用层提示；重连所依赖的「服务已常驻且 4 秒内自愈」已单独验证。
