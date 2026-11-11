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

- **顶栏**：连接状态（`● 已连接` / `● 服务未连接，自动重试中`）、**固定布局尺寸**、
  **当前画布尺寸**、启用块数、最近更新时间、**闲置阈值下拉框**、`刷新`、`恢复全部`、`日志` 按钮。
  页面每 2 秒自动拉取状态；`刷新` 只是「立即来一次」，不是唯一手段。
- **布局图**：按**规范布局**（见下节）的坐标等比绘制，块内标出
  名字、分辨率、位置、模式与刷新率、缩放（由 `几何宽 ÷ 模式宽` 推算）、是否 primary、是否内屏。
  点块可选中；**每块屏内部自带一个「关闭」/「关闭（内屏）」按钮**，点了就弹二次确认。
- **固定布局栏**：布局图下方常驻一行，列出每块屏的固定坐标；未确认时给
  「确认这份布局」「按当前实际布局认领」，`调整固定坐标` 可改 x/y 后保存（重叠会被后端拒绝）。
- **未启用的输出**：画成**空心虚线幽灵框，停在自己的固定坐标上**，标「开启后即在此处」，
  不带开关按钮、不改变画布尺寸；托盘里另有「开启」按钮。
- **右侧卡片**：同一份数据的明细 + 开/关按钮；顶部有 `已启用/已关闭`、`primary`、`内屏`、
  `你关闭的`、`闲置 X:XX` 等徽章。
- **闲置提醒**：某块屏超过阈值没有画面变化时，该块整体变暗，右上角出现
  「闲置 6:32　关闭」角标（角标可点，非模态，不打断操作）；右侧卡片同时给出
  「这块屏 X 无变化，要不要关掉（判据：…）」。**只提醒，不会自动关**。
- **阈值**：下拉选择（不提醒 / 1 / 2 / 5 / 10 / 15 / 30 分钟 / 1 小时），默认 5 分钟，
  改完立即生效，并写入 `~/.config/display-manager/config.json`。
- **确认框**：显示「要关闭谁、关闭后还剩几块、分别是哪块、画布尺寸怎么变」；
  关内屏会额外红字提示「笔记本面板会黑屏，画面留在剩下哪几块屏上」；
  如果关的正是浏览器窗口所在的那块屏，会红字提示关掉后界面会从眼前消失，
  并告知用 `Ctrl+Alt+Shift+F12` 或命令行恢复。**关掉之后不会自己回来。**

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
- **位置会避让**：记录里的 pos 只在「不压到任何已启用输出」时才原样用；一旦相交，
  自动挪到现有布局外侧（右 → 左 → 下 → 上），日志写 `恢复位置 0,0 会压住正在使用的输出，改到 5120,0`。
  见下面「布局图几何重叠」一节。
- transform 不是几何宽高比硬算出来的，而是关闭那一刻从 `xrandr --current --verbose`
  原样抓的九元组，避免四舍五入漂移（DP-0 实测 1.333328、DP-4 1.331238、HDMI-0 1.144440）。

## 保底规则（硬性）

1. **不关最后一块亮屏**：关闭前统计 `xrandr --query` 里已启用的输出数，等于 1 时直接拒绝，
   界面按钮置灰为「最后一块亮屏不可关」。
2. **关闭前二次确认**：确认框显示「关闭后剩余几块、分别是哪几块、画布尺寸从多少变成多少」。
3. **内屏（DP-4）允许关闭，但有额外提示**：需求已明确「三块里至少一块亮着就行」，
   所以内屏不再特殊拦截。确认框会红字提示：笔记本自带面板会黑屏，
   画面会留在剩下的 N 块屏上（列出名字）；如果界面正好在内屏上，还会提示
   关掉后界面会消失、可用恢复全部或命令行救回。
4. **所有操作落日志**：`~/.cache/display-manager.log`，包含关闭前的四件套、恢复来源、
   校验结果；xrandr 失败时把原始 stderr 一并写入。
5. **恢复入口**：命令行 `on` / `restore-all`；界面「恢复全部」；全局快捷键
   `Ctrl+Alt+Shift+F12`。下次打开界面时该输出会以「已关闭」出现在列表和托盘里，
   点一下就能开回来（参数来自状态文件）。

### 缺陷修复：关闭输出后不再自动开回（2026-11-10）

- 现象：在界面里关掉某块屏，约 20 秒后它自己又亮了。
- 真实原因：**前端**的定时器，不是后端、也不是看门狗。`doOff()` 在
  `isBrowserScreen(o)` 为真时调 `scheduleAutoRestore(name)`，`setTimeout(..., 20000)`
  到点直接 `POST /api/on`。日志能对上：`20:28:09 已关闭 DP-0` → `20:28:29 恢复 DP-0`，
  `20:33:26 已关闭 HDMI-0` → `20:33:46 恢复 HDMI-0`，都正好 20 秒。
- 为什么会误判「关的是界面所在的屏」：`isBrowserScreen()` 用 `window.screenX/screenY`
  判断窗口落在哪块屏。最大化窗口的 `screenX/screenY` 是 `0,0`，于是任何一块位置在
  `0,0` 的输出都会被判成「界面所在的屏」。当时 HDMI-0 的恢复位置正是旧的 `0,0`，
  关它也触发了自动开回。
- 修法：整条自动开回逻辑删掉（`AUTO_RESTORE_MS`、`scheduleAutoRestore`、`clearAutoRestore`、
  确认框里的开关、底部提示条）。关屏后保持关闭，直到用户点「开启」或按恢复全部。
  `isBrowserScreen()` 只保留用于确认框里的红字提示。
- **不要和「切换分辨率/刷新率的 15 秒确认窗」混淆**：那是 `dm_modes.py` 的后端超时，
  目的是新模式下黑屏时自动退回上个模式，和开关输出无关，必须保留。
- 关闭时的状态仍然三处一致：`turn_off()` 先写 `display-manager.state` 的四件套、
  再 `off_mark` 写 `~/.cache/display-manager.off`、然后 `xrandr --off`；看门狗的
  `is_user_off` 读的就是这个名单，命中即跳过自动恢复。

## 固定布局（单一几何源，2026-11-11）

之前的几何是「每块屏各自记一条、关闭时快照、开启时按它恢复」，谁先开谁后开、记录新旧都会影响
最终摆位（DP-0 与 DP-4 都记在 `0,0`，一起亮就重叠）。现在改成一份**规范布局**，与启用状态解耦。

### 数据文件

`~/.config/display-manager/layout.json`：

```json
{
  "version": 1,
  "confirmed": true,
  "updated": "2026-11-11 18:50:00",
  "outputs": {
    "DP-0":   {"mode": "3840x2160", "rate": "160.00",
               "transform": [1.333313, 0, 0, 0, 1.333313, 0, 0, 0, 1],
               "pos": [0, 0], "geometry": "5120x2880", "primary": true},
    "DP-4":   {"mode": "2560x1600", "rate": "165.00",
               "transform": [1.331238, 0, 0, 0, 1.331238, 0, 0, 0, 1],
               "pos": [7585, 0], "geometry": "3408x2130", "primary": false},
    "HDMI-0": {"mode": "2880x1800", "rate": "100.00",
               "transform": [1.14444, 0, 0, 0, 1.14444, 0, 0, 0, 1],
               "pos": [4289, 2880], "geometry": "3296x2060", "primary": false}
  }
}
```

- `outputs` 收录所有连接过的输出（含当前关着的）；**关屏只改 `.off`，不动这里**。
- `pos/geometry` 是 CRTC 坐标与尺寸（`geometry = mode × transform`），`transform` 常数项固定为 0。
- `confirmed`：是否经用户确认。首次建库一定是 `false`，页面显示「待你确认」。

### 行为

| 场景 | 行为 |
| --- | --- |
| 开启某块屏 | 直接用 `layout.json` 的坐标/模式/刷新率/transform；先开谁后开谁结果一样 |
| 关闭某块屏 | 只写 `.off`，坐标原样保留；再次开启回到同一位置 |
| 画布尺寸 | 固定为所有输出的 union，不随开关组合变化（顶栏另给「当前」实际画布） |
| 已关闭的屏 | 布局图空心虚线幽灵框，停在自己的固定坐标，标「开启后即在此处」 |
| 实际偏离固定位置 | 只告警（琥珀虚线标出固定位置 + 日志），不擅自挪；关掉再开启即落回 |
| 服务内改分辨率/刷新率 | 同步 `layout.json` 的 mode/rate/transform/geometry，位置不动 |
| 规范布局被手改重叠 | 开启时仍用 `plan_place()` 挪开，宁可不点亮也不叠屏 |

### 旧数据迁移

- 旧数据（`.state` 四件套、看门狗基线）保留兼容：只有 `layout.json` 不存在时才读，读完写进规范布局。
- 迁移顺序：**当前实际几何 → 旧四件套 → 看门狗基线**；某条坐标与已收下的屏重叠时（例如 DP-0 与 DP-4
  都记在 `0,0`），改用 `MIGRATION_POS` 的实测摆位（DP-0 `0,0`、DP-4 `881,2880`、HDMI-0 `4289,2880`，
  取自 11-11 01:19 的真实记录），仍撞车才用 `plan_place()` 兜底。
- 迁移结果一律 `confirmed: false`，**不自动套用**。页面提供三种确认方式：`确认这份布局`（认可迁移结果）/
  `按当前实际布局认领`（以此刻 xrandr 为准，实际本身重叠会被拒绝）/ `调整固定坐标`（手填 x/y，重叠会被拒绝）。
- `.state` 不再是几何源，改为 `layout.json` 的**镜像**（每次保存整份重写），因此「固定布局 / `.state` /
  `.off` / 看门狗基线」四处对得上；`.off` 里却亮着的输出仍由启动对账清掉。改坐标/认领只写文件，
  不会动当前亮着的屏。

### 缺陷修复：布局图「几何重叠」与过期恢复记录（2026-11-11）

- 现象：8765 布局图上两个矩形左上角都在 `0,0`，其中一个大红框没有标题文字；
  顶部画布写着 `5120×2880`；右侧 DP-0 卡片显示「已启用 / primary / 位置 0,0 / 3840x2160@160 / 缩放 133%」。
- 先纠正一个误读：**那个大红框就是 DP-0，而且它当时是真的亮着的**（卡片徽章是绿色「已启用」，
  不是「已关闭」）。DP-4 本来就画在它上面，所以 DP-0 的五行居中文字被完全盖住，看起来像「无标题大框」。
  红色不是「已关闭输出的幽灵」，是 `overlaps` 命中的实心块描边。
- 根因（日志能对上）：
  1. 0:00 前后把 DP-4 关掉后，17:33 服务重启/系统恢复时 **DP-4 被系统（mutter）重新点亮**，
     但它还留在 `~/.cache/display-manager.off` 名单里 —— 服务之外的点亮不会走 `turn_on()`，
     也就不会 `off_unmark`。
  2. `17:36:02 恢复 DP-0（依据 状态文件）… --pos 0x0 --primary` → `17:36:03 警告: 布局存在重叠
     [{"a":"DP-0","b":"DP-4","w":3408,"h":2130}]`。DP-0 按关闭前记录的 `0,0` 原样恢复，
     正好压在已亮的 DP-4 上。**旧逻辑只告警、不拦**。
- 画布 `5120×2880` 怎么来的：当时的 `snapshot()` 里 `canvas = _bbox(enabled)`，**只统计已启用输出**
  （现已改为按规范布局计算，见上一节），从未把已关闭输出的恢复几何算进去。当时两块都亮着：DP-0 `5120×2880+0+0` 与
  DP-4 `3408×2130+0+0` 的 union 正好是 `5120×2880`。不是幽灵撑大的，是 DP-0 真的在亮。
- 恢复记录为什么没跟着更新：`display-manager.state` 里的四件套**只在 `turn_off()` 那一刻写入**
  （「关闭前的快照」），亮着的时候没有任何东西刷新它。DP-0 在 11-10 之后被别的路径改成
  4K@60/100%（`--mode` 不带 `--rate`、或系统恢复后的默认值），这份记录仍是更早的
  4K@160/150%；下次开启就按过期记录回放。若它在此期间又被服务之外的方式点亮，
  记录还会一直留着，变成越放越旧的「化石」。
- 修法（三处，都不改用户当前生效的布局）：
  1. `plan_place()`：开启时先看记录位置会不会压住任何已启用输出；会就挪到现有布局外侧
     （右 → 左 → 下 → 上，右侧在虚拟画布上总能放下）。`turn_on()` 用它，日志与界面提示
     「记录位置 0,0 会压住正在使用的屏，已改放到 5120,0」。
  2. `/api/state` 给每个「已关闭但能恢复」的输出加 `restore_preview`（落点、几何、模式、
     是否避让过），前端据此画**空心虚线幽灵框**，标注「DP-4 · 已关闭 / 开启后 3408×2130 @ 5120,0」。
     幽灵框不参与 `canvas`（`canvas` 仍只由 enabled 决定），也没有关闭/开启按钮。
     选「幽灵态」而不是「不画」：不画会让用户点「开启」前完全不知道屏会出现在哪，
     而幽灵框把避让结果提前摆出来，正好回答「会不会压住我现在的屏」。
  3. `core.reconcile()`（服务启动时）：亮着的输出不该留在 `.off` 名单或「关闭前四件套」里，
     命中就删掉并写日志（`启动对账: DP-4 在 .off 名单里但当前是亮的…已从名单移除` /
     `…有已过期的关闭前记录（2560x1600@165.00 881,2880 几何 3408x2130）…已删除`）。
     只动状态文件，不动 xrandr。
  4. 重叠可视化：真重叠时在交集上画红框 + 角标 `DP-0 ↔ DP-4 实际重叠 3408×2130`，
     并把大块先画、小块后画，不再出现「大框没名字」的情况。
- 保留不变：至少一块亮着的保底；关闭路径上的 20 秒自动开回仍然是删掉的状态；
  模式切换的 15 秒确认窗不动；亮度/色温/缩放不动。

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

本机实测（2026-11-09，已装 `ddcutil 1.4.1` + `i2c-tools`，已配 udev `uaccess`、已把用户加入 `video` 组）：

- **DP-0（AOC U27G3X）**：走**硬件 DDC/CI**（`ddcutil detect` 报 `card1-DP-1` / `/dev/i2c-3`，
  `getvcp 10` 读到 `0/100`，`setvcp 10 30` 后读回 30，实际可写）。
  注意该屏 VCP 10 当前读到 **0** 而画面正常，疑似显示器在 HDR/预设模式下锁定了亮度回报；
  写 30 再恢复 0 后读数正常，实际观感以下发为准。
- **HDMI-0（便携屏 Sculptor）**：`ddcutil detect` 把它列为 `Invalid display`
  （`card1-HDMI-A-1` / `/dev/i2c-5`，DDC 通信失败），**只能软件调光**；卡片会写
  「HDMI-0 不响应 DDC/CI（Sculptor），只能软件调光」。
- **DP-4（eDP 内屏）**：`/sys/class/backlight/nvidia_0/brightness` 是 `root:video 0660`。
  用户已加入 `video` 组，但**组成员资格要重新登录才生效**；在重新登录前仍显示软件调光，
  卡片提示会变成「已加入 video 组，注销重新登录后自动生效（本会话仍是软件调光）」。
  另外 nvidia 驱动对「写入相同值」返回 `EINVAL`，代码会先比对再写，不当作失败。

### 需要你自己执行的权限配置（本机已做完，留作换机参考）

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

### 亮度下限 20%（2026-11-10）

关掉 AOC 的 HDR 之后，DDC 写 `0%` 直接变成黑屏/灰屏，只能拿手机来中断操作。
所以亮度有了一条**无条件**下限 `MIN_VALUE = 20`：

- **为什么不区分 HDR**：OS 侧读不到显示器 OSD 的 HDR 状态（X11 没有 HDR 通路，
  AOC 的 DDC capabilities 里也没有相关 feature），做不了「只有非 HDR 才限制」的判断，
  所以一律限制。
- **拦在哪一层**：权威在后端。`dm_core.set_brightness()` 与 `dm_brightness.set_value()`
  双重校验，`< 20` 直接报错（`亮度不能低于 20%：SDR 下再低等于黑屏（当前请求 10%）`），
  **不会静默改成 20**；前端滑杆 `min=20` 只是让用户拖不到，不是唯一防线。
  覆盖的写入路径：HTTP API、`dm_cli.py brightness set`、网页滑杆、启动 `apply_all()`、
  布局变化 `reapply_software()`、通道切换 `_mode_changed()`、硬件回落重算。
- **历史 `value=0` 怎么迁移**：只有恢复路径（启动/重登/重启服务）会走 `allow_clamp=True`，
  把 `0` 抬到 `20` 并记日志 `历史亮度 0% 低于下限，按 20% 抬升后下发`，
  落盘值同步改成 20；交互式写入仍然报错，不迁就旧值。
- **关闭输出不受影响**：那是另一条路径（`xrandr --output X --off` + `enabled=false`），
  不经过亮度校验，恢复时照旧。
- **写入兜底**：硬件两条通道（DDC / sysfs 背光）写完后都读回校验；
  读回不一致或读回超时（背光重试 3 次 × 0.1s）就在同一次调用里还原上一值，并把结果
  写进异常：`DDC 写入后读回 40%（目标 30%），写入被忽略；已还原为 60%`；
  还原也失败时明确说「还原为 60% 也失败」而不是吞掉。随后沿用原有降级逻辑回落软件 gamma。

### 通道锁定：AOC 钉死软件调光（2026-11-10）

AOC（DP-0）关掉 HDR 后 **所有 VCP 写入都被显示器忽略**（读正常、写同值可“成功”），
原来「先试 DDC、失败再回落软件」会闪一下、慢半秒，还会把亮度带偏。现在给它加了
**可持久化的通道锁定**，DP-0 永久锁在 `software`：

- **存在哪**：`~/.cache/display-manager.brightness.json` 每条输出记录里的 `lock` 字段。
  和 `value/mode/extra/temp` 一起落盘，重启服务、重新登录都保留。
- **锁定时完全不碰 ddcutil**：`_pick_mode()` 先看锁定值，命中 `software` 就直接返回软件通道，
  不跑 `ddcutil detect`、不读 `getvcp 10`、更不写。`_mode_changed()` 也直接跳过，
  所以重启后**不会**再执行「采纳硬件当前值」，80% 不会被拉成硬件 100%。
- **界面不提供切回硬件的入口**：锁定是永久的，没有「重新尝试硬件通道」按钮，也没有
  自动探测/自动回落。配置里的 `lock` 字段保留，仅供排障时用 CLI 改：
  `dm_cli.py brightness lock DP-0 software`（锁定）、`... lock DP-0 auto`（解除）。
- **用户看到的差别**：AOC 卡片固定显示 `软件调光（已锁定，不影响对比度以外的东西）`，
  不会在硬件/软件之间来回切换；亮度始终按落盘值下发（gamma = 值/100），20% 下限照旧。
  内屏（重新登录后可走硬件背光）和便携屏（本来就是纯软件）行为不变。

### 亮度滑杆的拖动响应（2026-11-10）

- **拖动即生效**：`oninput` 先本地乐观更新数字与手柄，再按 **60ms** 节流发送；节流窗口内
  只保留最新值，中间值全部合并丢弃。
- **拖动中不做硬件读回**：请求带 `"live": true`，DDC 用 `--noverify`、背光跳过读回、
  锁软件通道的屏本来就没有硬件可校验。松手后的最终值用 `"live": false` 走完整校验
  （DDC/背光读回、不一致还原、失败回落），确保界面值与屏幕一致。
- **不整页刷新**：拖动期间轮询不会重建 DOM（`renderCards` 跳过），在途响应的旧值也不会
  回写手柄/数字（`paintBright` 在 `brightDragging` 时跳过），只做局部字段更新。
- **被拒回弹**：最终值被后端拒绝（例如低于 20%）时，界面立刻拉 `/api/brightness` 的
  真实值并回弹，提示「已回到 N%」，不会出现界面显示 20 而实际不是 20。
- 实测数据见下方「通道锁定/拖动验证」。

### 硬件到下限后的软件叠加（2026-11-10）

AOC 的 VCP 10 打到最低后绝对亮度仍然偏高（压缩在显示器内部映射里，见上一节的实测）。
所以硬件通道的卡片里多了一条「软件叠加」滑杆，两层可叠加，但优先级和可见性都写死：

- **主调节永远是硬件**：上面那条滑杆走 DDC / 背光，gamma 保持 `1.00`，对比度不损失。
- **只有硬件到下限 20% 才能叠加**：主滑杆不在 20% 时叠加滑杆禁用，后端也会拒绝并写日志
  （`DP-0 硬件亮度 40% 未到下限，忽略软件叠加 30%`），不会静默把对比度压下去。
- **叠加是显式的**：`extra` 0~100，`gamma = 1 - 0.70 × extra / 100`（100 → 0.30）。
- **合成亮度还有一道下限**：`MIN_TOTAL = 0.10`。硬件 20% × gamma 0.30 = 6%，已经接近看不见，
  所以按 `(硬件/100) × gamma ≥ 0.10` 反推叠加上限：硬件 20% 时最多叠 `71%`
  （gamma 0.50，合计 10%），再多会被截断并记日志、界面滑杆 `max` 同步变成 71。
  硬件 100% 时上限仍是 100（100% × 0.30 = 30%）。叠加本身是 gamma 系数，不套用 20 的亮度下限。
- **可解释**：卡片标签直接显示三种状态之一 ——「硬件 DDC/CI（纯硬件）」、
  「硬件 DDC/CI + 软件叠加 60%」、「软件调光（影响对比度）（纯软件）」；说明行写
  「硬件已到下限（20%）+ 软件叠加 60%：gamma 0.58，对比度约剩 58%」。
- **可持久化**：`value` 与 `extra` 两路都写进缓存文件；服务重启、注销重登后先恢复硬件值，
  再补叠加层（日志 `启动恢复亮度: DP-0=20%(ddc，叠加 60%)`）。任何 xrandr 布局变化都会
  复位 gamma，`reapply_software()` 会把叠加层一并补回来（硬件主值不必重发）。
- **不跳变**：叠加只动 gamma、不动硬件，拖动按 1% 一步下发；把主滑杆从 20% 拖回更高档时，
  同一请求里先把 gamma 复位 `1.0`、撤销叠加，再设硬件值（只升 1%），
  所以不会出现「叠加还在 + 硬件拉满」的突然变亮。
- **降级**：硬件写失败时同一请求内回落软件 gamma，并标注原因 —— 标签
  「软件调光（影响对比度）（硬件写失败回落）」，说明行写
  「硬件 DDC/CI 写失败，已回落软件调光（gamma 0.30）：<原因>」。
  再拖一次主滑杆会重试硬件，成功后回落记录自动清掉。

本机结论（DP-0 / AOC U27G3X）：

- **最暗能到**：硬件 20%（DDC 下限）+ `extra=71`（gamma 0.50），合成 10%；
  比「硬件 20% + gamma 1.0」再暗一半。`extra` 想拖到 100 会被截到 71。
- **对比度损失量级**：gamma 是整体乘性缩放，白场按比例下降、黑场由面板本底决定，
  所以观感对比度大约等于「原生对比度 × gamma」——`extra=50`（gamma 0.65）约损失 35%，
  `extra=71`（gamma 0.50）约损失 50%，暗部会明显发灰。建议只在夜间压到 40~70 之间。
- **内屏**（DP-4 / eDP 背光）**走同一套逻辑**：重新登录拿到 `video` 组后通道变成
  `硬件背光`，卡片同样出现叠加滑杆，硬件背光到 20% 后可继续叠 gamma；
  便携屏 HDMI-0 是纯软件通道，**不出现**叠加滑杆（主滑杆本身就是 gamma）。

### 夜间色温（2026-11-10）

AOC 实测**DDC 侧做不了暖色温**，所以这一层只能走软件 gamma 的 R/G/B 比例：

- `getvcp 0x14`（Select color preset）的 capabilities 只有 `01 sRGB / 05 6500K / 06 7500K /
  08 9300K / 0b User 1`，**没有 4000K / 5000K 暖档**；
- 写 `0x14`（6500K/7500K/sRGB/User1）、`0x16 / 0x18 / 0x1A`（RGB 增益）全部被显示器忽略，
  读回原值；加 `--noverify` 绕过校验也一样。同一时刻 `0x10` 亮度能正常写入，
  所以不是总线或权限问题，是这块屏把颜色相关 VCP 锁成只读了。
- capabilities 里没有任何低蓝光 VCP，OSD 的「低蓝光」是显示器固件内部行为，DDC 侧不可见。

映射（`TEMP_POINTS`，中间值线性插值；6500K = 原生不改）：

| 色温 | R:G:B gamma |
|---|---|
| 6500 K | 1.00 : 1.00 : 1.00 |
| 6000 K | 1.00 : 0.97 : 0.91 |
| 5500 K | 1.00 : 0.93 : 0.82 |
| 5000 K | 1.00 : 0.89 : 0.75 |
| 4500 K | 1.00 : 0.85 : 0.68 |
| 4000 K | 1.00 : 0.81 : 0.61 |
| 3500 K | 1.00 : 0.78 : 0.55 |
| 3000 K | 1.00 : 0.74 : 0.48 |

- **每块屏一条**，不设全局：gamma 是每个 CRTC 自己的查找表，三块屏可以各用各的色温；
  卡片本来就按输出分，放在同一张卡里也正好和「亮度 / 软件叠加」共用一次下发。
- **合成一次 xrandr 调用**：`_sw_apply(name, level, temp)` 一次写
  `--brightness <level> --gamma r:g:b`。`_sw_set` / `_sw_reset` / `reapply_software` /
  `set_value` / `set_extra` 全部走它，不会出现「先设 gamma 再被 brightness 复位」。
- **与软件叠加共存**：`level` 由亮度层决定——纯软件屏是 `value/100`；硬件屏是
  `_overlay_gamma(extra)`（没叠加时 1.0）。色温只改 R/G/B 比例，两者在同一次调用里相乘生效。
- **硬件 DDC / 背光模式下照常生效**：DDC 不动 gamma，写色温不用碰硬件，也不影响亮度值。
- **持久化**：`temp` 和 `value` / `mode` / `extra` 一起写进缓存；`reapply_software()` 在布局变化后
  连同色温一起补回（只有色温、没叠加的屏也会补），`apply_all()` 启动恢复。
- **与 GNOME 夜灯互斥**：夜灯改的是同一块 CRTC gamma 表，实测夜灯生效时
  `Gamma: 0.59:1.0:1.6`，我们一次 `--brightness` 就把它复位成 `1:1:1`。所以卡片只**提示**
  （读到 `night-light-enabled=true` 就显示黄字说明会互相覆盖），不会替用户关掉夜灯。
  检测走 `gsettings`，10 秒缓存，读不到就当作不可用。

### 通道切换时不沿用旧百分比

记录里保存了上次的 `mode`。如果探测结果显示通道变了（例如刚装好 ddcutil，从「软件调光 100%」
变成「硬件 DDC/CI」），启动时**不下发旧百分比**，而是采纳硬件当前值并落盘，日志写
`DP-0 亮度通道 software→ddc，采用硬件当前值 0%（不下发旧值）`。
否则会把 100 硬写进刚拿到的硬件通道，屏幕突然跳到最亮。代价是软件调光时期的百分比不会迁移过去。
通道切换时 `extra` 一并清零，并把 gamma 复位 `1.0`，避免上次的软件叠加留在新通道上。

### 失败反馈

- 设置失败时，滑杆下方红字给出原因，同时右下角 toast；例如屏已关闭：
  `屏已关闭，gamma 调光无法生效`。
- DDC 打不开总线时错误里直接带 udev 命令；背光写失败时带 `usermod -aG video`。
- 单块屏探测失败不影响其它屏，也不影响布局图与开关功能。

### 持久化与恢复

- 位置：`~/.cache/display-manager.brightness.json`（每块屏最后一次的
  `value` / `mode` / `extra` / `temp` / 时间；`extra` 是硬件到下限后的软件叠加层，0 表示不叠加；
  `temp` 是夜间色温 K，6500 表示原生不改）。
- 服务启动 0.8 秒后由后台线程对**已启用**输出重新下发一次；屏是 `off` 时不应用。
- 「关闭 → 开启」某块屏后，`on` 成功会立刻再应用一次上次的亮度（模式设置会复位 gamma）。
- 通道从「软件调光」切到硬件时会把 gamma 复位为 1.0，避免双重压暗；叠加层按记录重放。

### API / CLI（门户后续可直接复用）

```bash
curl -s http://127.0.0.1:8765/api/brightness          # 亮度总览（含 extra / gamma / label）
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"name":"DP-0","value":60}' http://127.0.0.1:8765/api/brightness
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"name":"DP-0","value":20,"extra":60}' http://127.0.0.1:8765/api/brightness  # 下限 20
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"name":"DP-0","extra":60}' http://127.0.0.1:8765/api/brightness   # 只改叠加
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"name":"DP-0","temp":4500}' http://127.0.0.1:8765/api/brightness  # 夜间色温 3000~6500K

python3 backend/dm_cli.py brightness                  # 每块屏的通道与当前值
python3 backend/dm_cli.py brightness get  DP-0        # 走硬件通道时实时读
python3 backend/dm_cli.py brightness set  DP-0 60     # 20~100
python3 backend/dm_cli.py brightness set  DP-0 20 60  # 硬件到 20% + 叠加 60
python3 backend/dm_cli.py brightness extra DP-0 60    # 只改叠加（硬件必须已在 20%）
python3 backend/dm_cli.py brightness temp  DP-0 4500  # 夜间色温（6500 = 原生）
```

### 验证记录（本机，2026-11-09）

- DP-0：`set 70` 后 `xrandr --verbose` 读到 `Brightness: 0.70`，`set 100` 回到 `1.0`。
  截图（ffmpeg x11grab）看不出明暗变化是正常的：截图读 framebuffer，不经过显卡 gamma LUT，
  视觉确认只能看物理屏。
- DP-4：开启后同样实测 `1.0 → 0.60 → 1.0`；`off` 再 `on` 后 `Brightness` 自动回到 `0.60`
  （日志：`已重新应用 DP-4 的亮度`）。测完已恢复到 100% 并重新关闭内屏，显示器状态与测试前一致。
- HDMI-0 当前是关闭状态，通道判定为软件调光，未采集画面证据。
- **AOC DDC 实测（2026-11-09，装好 ddcutil + uaccess 后）**：
  `ddcutil --bus 3 getvcp 10` → `0/100`；`setvcp 10 30` → 读回 30；`setvcp 10 0` 恢复；
  `ddcutil detect` 把 AOC 识别为 `card1-DP-1 /dev/i2c-3 Model: U27G3X`，
  便携屏列为 `Invalid display`（`card1-HDMI-A-1 /dev/i2c-5 Sculptor`）。
- **通道判定回归**：`dm_cli.py brightness` 显示 `DP-0 0% 硬件 DDC/CI /dev/i2c-3`；
  `/api/state` 的 `brightness.DP-0` 为 `mode=ddc, hardware=true, device=/dev/i2c-3`；
  无头浏览器 DOM 里卡片的标签是 `<span class="btag hw">硬件 DDC/CI</span>`。
- **内屏背光函数级验证**（内屏当时是关闭的，无视觉影响）：`sg video` 下
  `_backlight_set` 同值写返回成功（不再报 EINVAL），写 66 读回 66，再恢复 65 读回 65；
  `_pick_mode(DP-4)` 在 `sg video` 下返回 `backlight`（65%），普通进程返回 `software` 并提示重新登录。
- **本次修掉的解析缺陷**：`ddcutil detect` 的 `Invalid display` 段会重复打印
  `I2C bus / DRM connector / Model`，旧解析会把 AOC 的总线覆盖成便携屏的 `/dev/i2c-5`
  （结果：AOC 丢失、便携屏被误标成「硬件 DDC/CI」）。已按段切分修正。

叠加通道验证（2026-11-10，DP-0 真机 + 假输出单元测试）：

- `brightness extra DP-0 40` → 硬件 `getvcp 10` 仍是 `20`，`xrandr` gamma `0.72`；
  `extra 71`（20% 时的上限）→ gamma `0.50`；`brightness set DP-0 60` → gamma 回 `1.0`、
  `getvcp 10` 读回 `60`（叠加撤销、硬件主值生效）。
- 「硬件没到下限时叠加被丢弃」：`set 40 + extra 30` 实际只落 `40/extra=0`，日志
  `硬件亮度 40% 未到下限，忽略软件叠加 30%`。
- 持久化：`set 20 60` 后重启服务，日志 `启动恢复亮度: DP-0=20%(ddc，叠加 60%)`，
  gamma 仍是 `0.58`、VCP 10 仍是 `20`。
- 迁移：旧记录 `value=0` 启动时被抬到 `20`，日志
  `历史亮度 0% 低于下限，按 20% 抬升后下发`。
- 无头浏览器 DOM：标签 `硬件 DDC/CI + 软件叠加 60%`，叠加行 `60% / gamma 0.58`；
  纯软件/关闭的屏不渲染叠加行。
- 函数级用例（临时 `DM_CACHE_DIR` + 打桩硬件）：叠加 40/60/100 的 gamma 与
  对比度文本、硬件非 0 撤销叠加、纯软件屏拒绝叠加、硬件写失败回落
  （`mode=software` + `fallback` 落盘 + 标签「硬件写失败回落」）、
  再拖主滑杆重试硬件成功后清掉 `fallback`、重启时 `_mode_changed` 采纳硬件真实值
  （用 `probe_value`，不会把回落时的 gamma 当硬件值写回）。

夜间色温验证（2026-11-10，DP-0 真机 + 函数级用例）：

- `POST /api/brightness {"name":"DP-0","temp":4500}` → `xrandr --verbose` 读到
  `Gamma: 1.0:1.2:1.5`（即下发 `1.00:0.85:0.68`），`getvcp 10` 仍是 `0`；
  `temp=6500` 还原为 `Gamma: 1.0:1.0:1.0`。
- 越界 `8000` → `400 {"ok":false,"error":"色温必须在 3000~6500 K 之间"}`；
  未启用的 DP-4 → `屏已关闭，gamma 调光无法生效`。
- 函数级（临时 `DM_CACHE_DIR` + 打桩）：`set_temp` 单次调用只发一条 xrandr；
  硬件 `0 + extra 60 + 4500K` 合并成 `--brightness 0.58 --gamma 1.00:0.85:0.68`；
  硬件回到 30% 撤销叠加时仍保留色温；`3000K + extra 100` 合并成
  `--brightness 0.30 --gamma 1.00:0.74:0.48`；调亮度/叠加都不会把 `temp` 清回 6500。
- 无头浏览器：卡片渲染「夜间色温 6500K / 原生」、滑杆 `min=3000 max=6500 step=100`、
  「还原 6500K」按钮；只有 `temp_ready` 的屏出现这一块。
- 夜灯互斥：临时打开 GNOME 夜灯后 `Gamma: 0.59:1.0:1.6`，一次 `--brightness` 即被复位成
  `1:1:1`；卡片出现黄字「注意：GNOME 夜灯已开启（3169K）……请二选一」。测完已把夜灯关回
  `false`、色温还原 6500K，屏幕状态与测试前一致。

亮度下限验证（2026-11-10，DP-0 真机 + 函数级用例）：

- `POST /api/brightness {"name":"DP-0","value":10}` → `400 {"ok":false,"error":
  "亮度不能低于 20%：SDR 下再低等于黑屏（当前请求 10%）"}`；硬件未被写入（`getvcp 10`
  仍为测试前值）。
- `value=20` → `min_value=20`、`overlay_cap=71`；当前 DDC 写入被显示器忽略时回落软件 gamma，
  返回体带 `fallback` 与标签「硬件写失败回落」，不静默成功。
- 函数级（临时 `DM_CACHE_DIR` + 打桩硬件）：`set 10` 直接抛错且硬件未动；`set 20` 得
  `overlay_ready=True`、`cap=71`；`20 + extra=100` 被截断为 `extra=71`、只发一条
  `--brightness 0.50`；拖回 `60` 撤销叠加并 `_sw_reset`；`value=0` 迁移日志如上。
- 写入兜底：DDC 写后读回与目标不一致（值已写入但校验失败）时，同一调用内还原上一值并抛错，
  错误文案含 `已还原为 60%`；背光通道读回重试 3×0.1s，仍不一致则还原并在返回值/日志明示。
- 无头浏览器 DOM：主滑杆 `min="20"`，卡片出现说明行
  `下限 20%：再低在 SDR 下等于黑屏（HDR 关闭后实测）`；纯软件通道不渲染叠加行。

通道锁定 / 拖动验证（2026-11-10，DP-0 真机 + 无头浏览器）：

- 锁 DP-0 后重启服务：`mode=software / value=80 / gamma 0.8 / lock=software`，
  **没有再被 `_mode_changed` 拉成硬件 100%**；再重启一次仍是 80。
- 函数级（把 `_ddc_state` 换成「一调用就抛 AssertionError」）跑 `_entry`、
  `set_value`、`apply_all`：锁定路径 **ddcutil 调用次数 0**，且 `set_value(70)` 只发 gamma、
  落盘 `70/software` 并保留 `lock`。
- 界面：没有「重新尝试硬件通道」按钮；卡片固定 `软件调光（已锁定，不影响对比度以外的东西）`，
  主滑杆 `min="20"`，且锁定软件通道不渲染叠加滑杆。
- 拖动实测（无头 Edge + CDP 合成 26 个 input，间隔 40ms，模拟约 1 秒连续拖动）：
  **共 14 次请求**（13 次 live + 1 次松手 final），单次 5~14ms，
  最后一次 final 14ms；拖动结束滑块在 55，与后端一致。
- 后端单次耗时（本机 curl）：live **4.4~5.2ms**（首次含布局快照 13ms），
  最终值 **12.9~13.9ms**（含完整校验与落盘）。
- 回弹：把最终值替换成后端拒绝（模拟 400）后，界面自动重新拉 `/api/brightness`，
  滑块与数字从 30 回弹到实际 80，不会停留在未生效的值。
- 20% 下限仍生效：`POST value=10` → `400 亮度不能低于 20%...`，硬件未写入。

## 护眼休息（2026-11-10）

默认 **50 分钟工作 → 5 分钟休息**，两个时长都能在界面里改；开关可一键暂停（开会/演示时用），
状态落盘，服务重启后接着当前轮次。到点时只在**选定的那块屏**（默认 DP-0）上铺一层全屏置顶的
倒计时，结束自动关闭并开始下一轮。

**先说清楚它是什么、不是什么**：这 5 分钟的作用只有三个——打断近距离用眼、引导远眺、提醒眨眼。
**它不是治疗**，也不声称渐变/动画能改善视力。界面和覆盖层底部都写明了这句话。

### 行为与边界

- 目标屏可选（下拉里只列当前已启用的输出）。覆盖层是一块独立窗口，只铺目标屏，
  其他屏照常工作；它只是画面，不拦截、不暂停任何程序。
- 几何直接用 `/api/state` 里 xrandr 的 CRTC 矩形（`x/y/w/h`），**没有第二套几何推算**。
  本机 DP-0 开了 1.3333 的 xrandr transform：CRTC 是 `5120x2880+0+0`，物理面板 3840×2160，
  覆盖层按 `5120x2880+0+0` 铺满，位置与布局图完全一致（多屏、负坐标同理）。
- 跳过快：**ESC 或点画面上的「跳过（ESC）」**。mutter 会忽略 Tk 的 `focus_force`，
  所以覆盖层映射后直接用 `XSetInputFocus` 抢焦点，ESC 才稳定生效；抢不到焦点时按钮始终可点。
  跳过会写日志（`护眼休息被跳过（ESC/按钮），跳过时间 …`）并记进 JSON 的 `skips`。
- 与闲置提醒互斥：到点时如果目标屏正显示闲置提醒（该屏闲置超过阈值、界面上变暗标记），
  本轮**延后 60 秒并写日志**，两个覆盖层不会叠在一起；闲置提醒消失后自动补上。
- 目标屏在到点时是关闭的：不弹覆盖层，记一次 `screen-off` 跳过并回到工作计时（不空等 5 分钟）。
- 暂停：工作计时冻结在剩余秒数；暂停时若正在休息，会关掉覆盖层、本轮提前结束并回到完整工作计时。
- 改工作时长会从新一轮开始计时；改休息时长只影响下一次休息。

### 缺陷修复：休息不得点亮已关闭的输出（2026-11-10）

**现象**：HDMI-0 便携屏在界面里被关闭（`.off` 名单里有它）后触发休息，休息期间它被重新点亮，
休息结束后一直亮着。预期是「休息只覆盖当前亮着的输出，触发前后任何一路的启用/关闭都不变」。

**根因不在服务下发的 xrandr 布局**：休息路径只调 `core.snapshot()`（`xrandr --current` /
`--current --verbose`，纯读），没有 `apply_all` / `reapply_for` / `reapply_software` /
`_mode_changed`；`apply_all()` 与 `reapply_software()` 都显式跳过未启用输出；`_sw_apply`
对没有 CRTC 的输出只报 `Need crtc`，不会去开它。证据时间线里服务日志没有任何启用记录，
`.off` 名单也一直写着 HDMI-0——即「名单说关着、机器上却亮着」。

**真正的触发点是覆盖层的窗口形态**：本机 GNOME 46 (X11) + NVIDIA 下，一个**铺满整个 X 画布的
override-redirect 窗口**会让 mutter 走直通/重排路径，并把自己 `~/.config/monitors.xml` 里的布局
再套一遍，于是用户已关闭的 HDMI-0 被重新启用（README 里早先记的「mutter 在 RandR 变化后
有时会重新套用自己的显示配置」就是这个）。对照实验（HDMI-0 先关好，逐个变量试）：

| 变量 | 结果 |
| --- | --- |
| 静置 3 分钟 / 只跑 watchdog 的全量重探 `xrandr --query` | 保持 OFF |
| 受管全屏窗口（`_NET_WM_STATE_FULLSCREEN`） | 保持 OFF |
| override-redirect，但尺寸 3000×2000（不铺满画布） | 保持 OFF |
| **override-redirect，铺满 5120×2880 整个画布** | **2 秒内被点亮** |

**修法**：

1. 覆盖层改成**受管全屏窗口**（`attributes("-fullscreen", True)`，去掉 `overrideredirect`）。
   实测仍精确落在目标屏的 CRTC 矩形（`xwininfo`：`5120x2880+0+0`），双屏都开时也只铺目标屏，
   ESC 依旧可用（退出码 3）。
2. 休息开始前记录布局签名（已启用输出、`.off` 名单、画布尺寸），覆盖层出现后 1.5s/4s、
   休息结束后 1.5s/4s 各复核一次：**只把「休息前就在 `.off` 里、现在仍在 `.off` 里却被点亮」
   的输出关回去**，不动 `.off` 名单；用户主动开启会先把输出从 `.off` 移除，因此不会被抢回去。
   布局仍不一致、或名单被外部改动，都会写日志。
3. 休息中每 5 秒确认目标屏还有 CRTC；一旦被关掉就撤销覆盖层、放弃本轮（`screen-off`），
   **绝不为了覆盖层去点亮任何输出**。到点时目标屏无 CRTC 同样直接跳过（原有逻辑，补了显式
   `crtc` 判断和日志）。
4. watchdog 与 `.off` 名单在这条链路上没有被绕过：`hdmi-watchdog.sh` 的两条恢复分支
   （HPD 掉线恢复、`FORCE_RECOVER=1` 未掉线兜底）都先查 `is_user_off`，命中就跳过；
   默认 `FORCE_RECOVER=0`。修完也不再需要和它抢 xrandr：覆盖层不再触发 mutter 重套配置。

### 视觉规格（低刺激）

- 背景只在 `#12161A` ~ `#1B2328` 之间缓慢过渡（40 秒一个周期，**每 0.8 秒才重绘一次**），
  不用纯黑，也没有任何白色大面积背景。
- 引导文字 `#9AA6AE`、倒计时 `#C6CFD5`，**不用纯白、不加粗**；文字亮度按当前亮度值轻度跟随
  （系数 0.6~1.0，只作用在文字上，不动用户的亮度设置），所以屏幕本身很暗时它不会反而刺眼。
- 跟随当前色温：服务端把当前 `temp` 换算成暖色系数传给覆盖层（6500K→0，3000K→1），
  色温调暖后这里不会突然变回冷白；渲染本身还叠加在同一个 CRTC gamma 上，和桌面一致。
- 动画只有缓慢渐变与一条 10 秒周期的「呼吸」细线；没有高频闪烁、粒子、光晕。

### 状态与恢复

状态在 `~/.cache/display-manager.break.json`：

```json
{"enabled": true, "work_min": 50, "break_min": 5, "screen": "DP-0",
 "phase": "work", "remaining": 1234.5, "round": 3, "breaks": 2,
 "skips": [{"at": "2026-11-10 06:16:32", "reason": "esc"}], "last_tick": 1762746000.0}
```

- 每 10 秒落一次盘，重启最多丢 10 秒；**服务停着的时间不扣**（关着的时候不算「连续用眼」）。
- 重启时如果正处在休息中，会按剩余秒数重新铺一层，不会重头开始。
- 界面状态行显示：`距下次休息 50 分钟`、`休息中 · 剩余 4:12`、`已暂停 · 计时冻结在 …`；
  被闲置提醒挡住时显示 `闲置提醒正在显示 · 本轮休息延后 N 秒后再试`。

### API

```bash
curl -s 127.0.0.1:8765/api/break                                   # 只读状态
curl -s -X POST 127.0.0.1:8765/api/break -d '{"enabled": false}'   # 暂停/启用
curl -s -X POST 127.0.0.1:8765/api/break -d '{"work_min": 25, "break_min": 5}'
curl -s -X POST 127.0.0.1:8765/api/break -d '{"screen": "HDMI-0"}'
```

校验：`work_min` 1~240、`break_min` 1~60、`screen` 必须是当前已启用的输出，越界/非法返回 400。
`/api/state` 里也带一份 `break`，页面每 2 秒轮询就地更新，不重建面板 DOM。

### 验证记录（本机，2026-11-10）

- 1 分钟工作/1 分钟休息实测：`护眼休息开始：DP-0 覆盖 5120x2880+0+0，倒计时 60 秒（第 N 轮）`，
  截图确认铺满整块屏、置顶、深色低对比，没有白底。
- ESC 跳过：覆盖层退出码 3，日志 `护眼休息被跳过（ESC），跳过时间 2026-11-10 06:16:32`，
  `/api/break` 的 `skip_count=1`、`last_skip` 对得上，随后进入下一轮工作计时。
- 按钮「跳过（ESC）」同样退出码 3；倒计时到 0 自动退出码 0，日志 `倒计时完成`。
- 只跑单测（打桩 `snapshot`）：闲置提醒在显示 → `defer_until=+60s` 且不启动覆盖层；
  闲置消失 → 启动；目标屏关闭 → 记 `screen-off` 并回到工作计时；暂停后剩余秒数不动。
- 无头 Edge：面板显示 `距下次休息 50 分钟`；把工作改成 25 → 后端 `remaining=1499`；
  点暂停 → 徽标「已暂停」且计时冻结；恢复后 `50/5、enabled=true`。

**端到端回归（2026-11-10 07:23–07:30，真机 HDMI-0 关闭 + 工作 1 分钟）**

前置：界面里关掉 HDMI-0，`~/.cache/display-manager.off = DP-4, HDMI-0`，之后设 `work_min=1`。

触发前 / 连续两个完整休息周期后（07:23:39 → 07:26:39，每 2 秒采样一次）：

```
触发前   DP-0 ON  HDMI-0 OFF  DP-4 OFF  canvas 5120x2880  off=[DP-4,HDMI-0]
07:24:00 休息开始（ov=1，倒计时 60s）  HDMI-0 OFF  canvas 5120x2880
07:24:58 休息结束（倒计时完成）        HDMI-0 OFF  canvas 5120x2880
07:25:58 第二轮休息开始                HDMI-0 OFF  canvas 5120x2880
结束     DP-0 ON  HDMI-0 OFF  DP-4 OFF  canvas 5120x2880  off=[DP-4,HDMI-0]
```

逐项复核（`diff`）：

- `xrandr --current` 的输出行（含几何/位置/gamma 行）：**完全一致**。
- 各输出启用状态：`DP-0 ON / HDMI-0 OFF / DP-4 OFF`，前后一致。
- `~/.cache/display-manager.off`：**完全一致**（`DP-4, HDMI-0`）。
- `~/.config/monitors.xml` mtime：`2026-11-09 04:38:07` 前后未变（mutter 这次没有再改写它）。

补充用例：

- ESC 跳过路径（07:28:52 起休息，07:29:04 发 ESC）：覆盖层退出码 3，跳过后 HDMI-0 仍 OFF，
  布局行与 `.off` 与触发前完全一致。
- 兜底复核：休息前记签名 `{'enabled': ['DP-0'], 'off': ['DP-4','HDMI-0']}`，随后外部
  `xrandr --output HDMI-0 --auto` 点亮，调用 `_guard_layout()` 后日志
  `发现 HDMI-0 被外部重新点亮，已按休息前状态关回（用户关闭名单未改动）`，
  HDMI-0 回到 OFF、画布回到 `5120x2880`、`.off` 未变。
- 双屏都在时（HDMI-0 已启用）跑覆盖层：`xwininfo` 显示窗口仍为 `5120x2880+0+0`，只铺 DP-0，
  HDMI-0 状态不变。


## 分辨率与刷新率（2026-11-10）

界面里每块**已启用**的输出都有一张「分辨率 / 刷新率」卡片：两个下拉 + 一个「应用」。
下拉里的每一项都来自该输出 `xrandr --current` 实际打印出来的模式表（`dm_core._parse_query`
的 `modes` 字段），**没有内置任何型号表，也不会推算**；默认选中当前值，`（首选）` 标记
EDID 的 preferred 模式。

### 只改这一块屏

应用时一条 xrandr 命令同时下发 `--output NAME --mode M --rate R --transform … --pos … [--primary]`：

- `--rate` 必须显式带上。只给 `--mode 3840x2160` 时 xrandr 会选 EDID preferred 的 60Hz
  —— 这正是下面「为什么现在跑 60」的直接原因。
- `transform` 与 `--pos`/`--primary` 取自应用前的快照并原样带回。**transform 必须和 mode 同一条
  命令下发**，否则分数缩放会掉回 100%，CRTC 几何跟着变成模式尺寸，布局就错位了。
- 命令里只有目标输出，别的输出一个参数都不带。若 mutter 在 RandR 重配时顺手点亮了
  `~/.cache/display-manager.off` 名单里的屏，应用后会复查并立刻关回去（名单本身不改）。
- 内屏/便携屏一样适用；卡片只出现在已启用的输出上，关闭中的输出没有这张卡。

### 安全流程（四步）

1. **先落盘再动手**：应用前把当前时序（模式/刷新率/transform/位置/primary/几何）写进
   `~/.cache/display-manager.modes.json` 的 `last_good`，并写一条 `pending`。此时即使进程被杀，
   重启后 `start()` 也能按 `pending` 收尾。
2. **应用后复核**：重新读 `xrandr`，要求该输出仍启用、模式字符串一致、刷新率误差 ≤ 0.05Hz、
   transform 未被改动、CRTC 有效。任何一项不符就**立即回滚到 last-known-good**，并把结果
   （成功/回滚也失败）如实返回。
3. **确认窗 15 秒**：模式变了以后页面底部出现一条倒计时条「保持此设置 / 撤销」。
   15 秒内不点「保持」，**后端**自动退回上一个模式（不依赖页面还活着——黑屏时人够不到操作）；
   点「撤销」也立刻退回。前端倒计时只是提示，真正的超时回滚在服务进程里。
4. **一键恢复**：卡片上的「恢复上次可用模式（…）」直接回到 `last_good`，不需要记命令行。

`apply`/`confirm`/`revert` 都在全局 `LOCK` 里执行（含超时回滚线程），和关闭/开启/亮度写操作互斥。

### 为什么 DP-0 之前跑在 4K@60

**已证实**：

| 证据 | 结果 |
| --- | --- |
| `xrandr --query` 的 DP-0 模式表 | 3840x2160 在 160/144/120.01/100/75/60/29.98 全部暴露 |
| `~/.cache/hdmi-watchdog.layout` 旧基线 | 曾记录 `DP-0 3840x2160 160.00 0x0 5120x2880+0+0`，160 跑过 |
| `~/.config/monitors.xml`（mtime 2026-11-09 04:38） | 只有 1080p/1440p 旧组合，**没有** 4K+1.3333 → 排除 mutter 按它回退 |
| `~/.cache/display-manager.state` 与服务日志 | 没有 DP-0 的开启/mode 下发记录 → 不是本服务设的 |
| 手动 `xrandr --output DP-0 --mode 3840x2160 --rate 160.00` | rc=0，mode 切到 `0x1be`（1377.08MHz），transform 与画布不变 → 排除带宽/线材 |
| `--brightness/--gamma` | 只改 gamma ramp，不动模式（实测刷新率不变） |

**结论**：不是配置被记录成 60，也不是 mutter/monitors.xml 回退，更不是线材带宽不够。

**推测（无直接证据）**：某次布局下发只给了 `--mode 3840x2160` 而没给 `--rate`，xrandr 按 EDID
preferred 选了 60Hz。Xorg 日志对该输出的模式变更记录级别不足，指认不到具体那条命令。
现在这个入口把 `--rate` 变成必填并会复核，同类「静默掉回 60」不会再发生。

### 与亮度 / 色温 / 护眼休息 / 看门狗的共存

- RandR 重配会把 gamma 复位，应用成功后自动调 `dm_brightness.reapply_software()` 与
  `reapply_for()`，并按原通道补发；DP-0 的 `software` 锁定不受影响。
- 应用成功后把新时序写回 `~/.cache/hdmi-watchdog.layout` 里该输出的那一行（其余行不动），
  看门狗不会拿旧模式当基线又改回去。
- 护眼休息覆盖层运行期间（`phase == "break"`）拒绝切换，提示等覆盖层结束——覆盖层是按旧几何
  铺的，中途改模式会错位。
- 应用后作废实时快照缓存，避免拖动亮度时用到旧几何。

### API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | `modes.outputs[name].modes` 是模式表，`current` 是当前值；另有 `pending`、`last_good`、`confirm_seconds` |
| POST | `/api/modes/apply` | `{"name","mode","rate"}` → `{ok, changed, token, timeout, previous, applied, warnings}`；失败已自动回滚 |
| POST | `/api/modes/confirm` | `{"token"}` 保持当前模式 |
| POST | `/api/modes/revert` | `{"token"}` 撤销确认窗；不带 token（或没有 pending）时退回 `last_good` |

### 验证记录（本机，2026-11-10 08:45–08:52，真机 DP-0，HDMI-0 关闭、DP-4 关闭）

| 步骤 | 模式（mode id） | Transform | 画布 | HDMI-0 / DP-4 | 结果 |
| --- | --- | --- | --- | --- | --- |
| 起始 | 3840x2160@60 (`0x1bd`) | 1.333328 | 5120x2880 | 关闭 / 关闭 | 与用户报告一致 |
| API 切 160 + 保持 | 3840x2160@160 (`0x1be`) | 1.333313 | 5120x2880 | 关闭 / 关闭 | 复核通过，基线同步为 160 |
| API 切 60 + 保持 | 3840x2160@60 (`0x1bd`) | 1.333313 | 5120x2880 | 关闭 / 关闭 | 等 16 秒仍保持（未被超时回滚） |
| 切 60 后不确认 | 先 60，15 秒后 160 (`0x1be`) | 不变 | 5120x2880 | 关闭 / 关闭 | 超时自动退回，日志「确认窗超时…自动退回」 |
| 切 60 后点「撤销」 | 先 60，立即回 160 (`0x1be`) | 不变 | 5120x2880 | 关闭 / 关闭 | 撤销生效，pending 清空 |
| 「恢复上次可用模式」 | 60 → last_good 160 (`0x1be`) | 不变 | 5120x2880 | 关闭 / 关闭 | 一键退回生效 |
| 请求未暴露的模式 | 拒绝：`1234x567@60`、`3840x2160@91.00`（都不在模式表里）；对关闭中的 DP-4 也拒绝 | — | — | — | 返回明确错误，不改布局 |

每次切换后 `Brightness: 0.50 / Gamma: 1.0:1.0:1.0` 都自动补回，亮度锁仍是
`{"mode":"software","lock":"software"}`。字体/缩放未被改动。


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
5. 从界面关掉「界面自己所在屏」时，界面会消失且**不会自动回来**；
   按 `Ctrl+Alt+Shift+F12` 恢复全部，或按第 2 步的命令行救回。
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

### 资源占用指标（CPU / 内存）

每张卡片多一行紧凑指标（`CPU 2.4%　内存 15.8 MB`），页面顶部另有一条合计；不做图表，
两张卡片的布局不变。

**数据来源：优先 cgroup，不按进程名抓 `ps`**

| 情况 | 读法 | 界面标注 |
| --- | --- | --- |
| systemd 用户单元（8765、8760） | `systemctl --user show -p ControlGroup` 拿路径，直读 `/sys/fs/cgroup/user.slice/user-<uid>.slice/user@<uid>.service/.../<unit>/` 下的 `cpu.stat`、`memory.current` | `5s 均值` |
| systemd 系统单元（8787 `usbnicd.service`） | 同上，路径在 `/sys/fs/cgroup/system.slice/`；本机普通用户可读，不需要 sudo | `5s 均值` |
| cgroup 读不到（单元不存在 / 权限异常） | 退化为 `MainPID` + `/proc/<pid>` 整棵进程树（RSS 求和、`utime+stime` 差值） | `进程树降级` + 原因 |
| 未托管的外部服务（`manage: external` + `process: <名字>`） | 扫 `/proc`，按 `comm` 或命令行里的脚本名匹配 | `按进程名估算` |

cgroup 路径缓存 60 秒（`systemctl show` 不是每轮都跑），常态采样每 5 秒只有几次 `read()`，
既不 fork 也不扫 `ps`。

**口径**

- CPU：两次采样之间 `cpu.stat` 的 `usage_usec` 差值 ÷ 实际间隔，归一到**单核 100%**
  （与 `systemd-cgtop` 一致：跑满两个核显示 200%）。窗口就是后端探测周期，默认 5 秒，
  不用瞬时值；间隔小于 2 秒不重算（点「立即检测」不会打出尖峰），沿用上一次的读数。
- 内存：`memory.current`（cgroup，含该单元的 page cache）或进程树 RSS，单位 MB；tooltip 里有整机占比。
- **峰值**：同一个 5 秒均值的**滚动最大值**，窗口 **最近 5 分钟（300 秒）**。每个 5 秒采样点
  记一条样本，超过 300 秒的自动淘汰，所以一次尖峰最多影响 5 分钟，不会永久钉在红色。
  合计峰值取「同一轮各单元均值之和」的滚动最大值（不是各单元峰值的相加，那个数没有物理意义）。
- 第一次采样没有上一个读数，CPU 显示 `—`，下一轮（约 5 秒）出数。**任何识别不到的字段都显示 `—`，
  不会写 0**——0 会被误解成「进程在跑但没占用」。峰值的 `—` 表示窗口内还没有样本（刚启动，
  或该单元一直读不到）。

**峰值什么时候清零**

| 操作 | 峰值的变化 |
| --- | --- |
| 刷新页面 / 重新打开门户 | 不变。峰值存在后端进程内存里，前端只是显示 |
| 重启门户（`systemctl --user restart screen-portal`） | 清空，从下一次采样重新累积；第一轮显示 `—` |
| 被监控的单元重启（例如 8765 重启） | 旧样本仍在窗口里，最多 5 分钟后被淘汰；重启期间读不到值的那几轮不产生新样本 |
| 单元停止 / 读不到 | 不再产生新样本，旧峰值随窗口自然淘汰，最终变 `—` |

**阈值**（标黄 / 标红，单核 100% 口径）

| 指标 | 黄 | 红 | 为什么这么定 |
| --- | --- | --- | --- |
| 单服务 CPU 均值 | ≥50% | ≥90% | 50% 等于长期占住半个核，在这台 20 线程机器上已属「该警惕的常驻开销」；90% 基本跑满一核 |
| 单服务内存 | ≥512 MB | ≥1.5 GB | 这几个服务的正常量级是十几到几十 MB，过 512 MB 多半是泄漏或缓存堆积 |
| 合计 CPU 均值 | ≥100% | ≥200% | 合计是「所有服务 + 门户自身」，阈值放大到 1 核 / 2 核 |
| 合计内存 | ≥1 GB | ≥2 GB | 同上，取整机内存（约 15 GB）的十分之一 / 七分之一 |
| **单服务 CPU 峰值** | ≥100% | ≥200% | 峰值是 5 分钟里最高的那个 5 秒窗口，天然高于均值；直接套均值阈值会长期满屏红。取「曾经整段跑满一核 / 两核」才有提示价值 |
| **合计 CPU 峰值** | ≥200% | ≥400% | 同上，合计口径对应 2 核 / 4 核 |

**门户自身**：`screen-portal.service` 也是用户单元，指标同样按 cgroup 采，只出现在顶部合计条
（`含门户自身：0.2% / 16.1 MB`），不占一张卡片；它的峰值也计入合计峰值。

**API / CLI**

- `GET /api/status`：每个服务多一个 `metrics` 对象，顶层多一个 `metrics`（含 `portal` / `total` /
  `window` / `peak_window` / `cpu_basis`）。单服务与 `total` 都带 `cpu_peak`（窗口内最高均值）、
  `peak_window`、`peak_samples`；识别不到时 `cpu_pct`、`cpu_peak`、`mem_mb` 为 `null`，并带 `reason`。
- `python3 portal/portal_server.py --metrics` 直接打印表格（现场采两次、1.2 秒窗口，不监听端口）。
  注意 CLI 是独立进程，峰值列只覆盖本次两次采样；常驻服务的峰值窗口才是 5 分钟：

  ```text
  资源占用（CPU 为最近 1.2 秒均值，单核=100%；内存为 cgroup memory.current / RSS）
  服务                                 CPU       峰值      内存          来源
  ----------------------------------------------------------------------------------------------
  屏幕输出管理 (screen-manager.service) 3.93%     6.10%     15.8 MB       cgroup
  网络管理 (usbnicd.service)           0.71%     1.35%     44.8 MB       cgroup
  ----------------------------------------------------------------------------------------------
  门户自身 (screen-portal.service)     0.07%     0.22%     14.4 MB       cgroup
  合计（含门户自身）                   4.71%     7.67%     75.0 MB       整机内存 0.48%
  注：CLI 是独立进程，峰值列只覆盖本次两次采样；常驻服务的峰值窗口是最近 5 分钟。
  ```

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

**资源占用指标验证（2026-11-09，同一时刻对照 `systemd-cgtop` 与 `systemctl show`）**

```text
cgtop  /…/app.slice   screen-manager.service   3   3.4%   15.3M → 14.6M
                      screen-portal.service    2   0.3%   14.9M → 14.1M
cgtop  /system.slice  usbnicd.service          8   0.0%   45.3M
systemctl show -p MemoryCurrent   15691776（15.0 MiB） / 14393344（13.7 MiB） / 47603712（45.4 MiB）
门户 /api/status      screen 3.43%（窗口 5.0s）/ 15.6 MB；usbnic 1.4–2.1% / 44.4 MB；portal 0.19–0.25% / 15.4 MB
                      total 5.7% / 75.8 MB（整机 0.48%）
```

- CPU 与 cgtop 同一量级（3.43% vs 3.4%）；内存与 `MemoryCurrent` 差在 1 MB 上下
  （`memory.current` 会随 page cache 浮动，cgtop 显示同一字段的取整值）。数量级一致。
- 页面：三卡（临时加一张不存在的单元）与两卡布局都实测截图；不存在的单元显示
  `CPU —　内存 —`，备注写 `systemctl show 没返回 ControlGroup / MainPID（单元可能不存在）`，
  没有出现 0；该临时条目已从 `services.json` 还原。
- 阈值配色：用临时 mock 数据（CPU 62% / 内存 700 MB → 黄；95% / 1800 MB → 红；合计 157.9% → 红）
  截图核对像素颜色通过；真实数据未触发阈值，测完没有留下任何人为的高占用。
- 单元停止 → 重启后第一轮 CPU 显示 `—`（没有上一个 `usage_usec`），下一轮恢复出数；
  8787 重启时确实出现过一次 `usbnic offline / 连接被拒绝`，与本功能无关，是当时服务正在启动。

**CPU 峰值验证（2026-11-09）**

- 滚动淘汰（把窗口临时设为 3 秒）：推 50% → 峰值 50；1.5 秒后推 10% → 峰值仍 50；
  再过 2 秒推 10% → 峰值掉到 10；再推一轮 `None`（模拟采样失败）→ 峰值仍 10，不清零也不续命。
- 重启门户后：`total.cpu_peak` 从 `—`（第一轮无样本）→ 5.91% → 保持；单服务 `peak_samples`
  随轮次递增（1 → 2 → …），5 分钟后才可能开始淘汰。
- 配色：mock 数据核对像素——均值 12% 白 / 峰值 120% 黄 / 内存 300 MB 白；
  均值 20% 白 / 峰值 260% 红 / 内存 2400 MB 红；合计 32.5% 白 / 合计峰值 380% 黄。
  真实数据（峰值 3.7% / 合计 5.9%）全部为白，测完没有留下人为高占用。
- 两卡与窄屏（820px）布局都截图确认：指标行仍是一行，没有挤压按钮或地址栏。

## 运行期文件

| 路径 | 用途 |
| --- | --- |
| `~/.cache/display-manager.state` | 关闭前保存的四件套（模式/刷新率/transform/位置/primary） |
| `~/.cache/display-manager.off` | 用户主动关闭的输出名单（供与看门狗共存、界面标注） |
| `~/.cache/display-manager.log` | 操作日志（含 xrandr 原始 stderr、快捷键注册/触发） |
| `~/.cache/display-manager.brightness.json` | 每块屏上次的亮度值、通道与软件叠加层（`extra`） |
| `~/.cache/display-manager.break.json` | 护眼休息：开关、时长、目标屏、当前轮次与剩余秒数、跳过记录 |
| `~/.cache/display-manager.modes.json` | 分辨率/刷新率：`last_good`（上次可用时序）与未确认切换的 `pending` |
| `~/.config/display-manager/config.json` | 闲置阈值（默认 300 秒，0 = 关闭提醒） |
| `~/.config/systemd/user/screen-manager.service` | 8765 的用户单元（`install-autostart.sh` 生成） |
| `~/.config/systemd/user/screen-portal.service` | 8760 门户的用户单元 |
| `~/.config/systemd/user/usbnic-session.service` | 登录时拉起 8787 的桥单元 |
| `~/.config/autostart/screen-session.desktop` | 会话就绪后导入 DISPLAY 并启动 8765 单元 |
| `~/.config/autostart/screen-portal-open.desktop` | 登录后自动打开门户（可删） |
| `portal/services.json` | 门户的服务清单（改完 5 秒内热加载） |
| `~/.cache/hdmi-watchdog.layout` | 看门狗的基线，用作没有关闭记录时的恢复兜底；切换分辨率后本工具会把目标输出的新时序写回它那一行（其余行不动） |

`.gitignore` 已排除日志、状态、`__pycache__`、截图等运行时产物。

## 依赖

- Python 3.12（本机自带），**只用标准库**：`http.server` / `subprocess` / `ctypes`。
- 护眼休息的覆盖层用 `tkinter`（Ubuntu 的 `python3-tk`，本机已装）；它只画渐变、文字和一条线，
  不引入新的第三方库。
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
- 护眼休息的覆盖层只在 X11 下成立（依赖 `XSetInputFocus` 抢焦点）；Wayland 会话里按钮仍可点，
  但 ESC 不保证能收到。覆盖层不拦截输入、不锁定键盘，用户随时可以切走或跳过。
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
- 资源指标里的「内存」是 cgroup `memory.current`（含该单元的 page cache），与 `ps` 的 RSS
  不是一回事，数值会随文件缓存浮动；外部服务的「按进程名估算」只求和命中的进程，会漏掉
  改过名的子进程。
- cgroup 读不到时降级到 `/proc` 进程树，CPU 由 tick 换算，精度略低；单元刚重启的第一轮
  CPU 没有差值可算，会显示 `—`（下一轮自动出数）。

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
