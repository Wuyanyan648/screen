# screen —— 本机显示器输出管理 + 闲置提醒

本机自用工具：把 Ubuntu 24.04 + GNOME 46 + X11（ubuntu-xorg）+ NVIDIA 595 驱动的
三屏布局可视化出来，逐块开关某一路输出，并提醒「这块屏似乎闲置，要不要关掉」。
界面为中文，逻辑只有一份（`backend/dm_core.py`），网页界面和命令行共用。

- 当前实测机型：RTX 3060 Laptop，输出 `DP-0`（AOC U27G3X，3840×2160@160，133% 缩放）、
  `DP-4`（内置 BOE，2560×1600@165，133%，**本机当前是 primary**）、
  `HDMI-0`（便携屏，2880×1800@100，1.1444 倍缩放）。
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
  「Ubuntu on Xorg」/ `ubuntu-xorg` 会话再使用。
- **其他发行版**：需自行确认 `xrandr` 版本、`libXdamage.so.1` / `libXss.so.1` 是否存在、
  Python ≥ 3.10（代码用了 `X | None` 写法）。GNOME 之外的桌面环境未验证，
  尤其「关屏后 mutter 会重新套用自己的显示配置」这类行为在 KDE 等环境下可能不同。
- 内置屏识别依赖 `xrandr --verbose` 里的 `ConnectorType: Panel`；若某驱动没有这个字段，
  「不关内屏」这条保护会失效（此时会退化成只能靠「不关最后一块亮屏」兜底）。

## 快速开始

```bash
cd ~/eric/wuyan_github/screen
./start.sh                 # 起本机服务并自动打开浏览器（默认 127.0.0.1:8765）
PORT=9000 ./start.sh       # 换端口（端口被占会自动往后找 20 个）
./start.sh --no-browser    # 只起服务
```

- 前端入口文件：[web/index.html](web/index.html)
- 按 `Ctrl-C` 退出：只停服务和闲置监视线程，**不改动当前显示布局**。

## 界面说明

- **布局图**：按 `xrandr` 里各输出的 CRTC 几何（x/y/宽/高）等比绘制，块内标出
  名字、分辨率、位置、当前模式与刷新率、缩放（scale 由 `几何宽 ÷ 模式宽` 推算，
  与 transform 一致）、是否 primary、是否内屏。点块即选中。
- **右侧卡片**：同一份数据的明细 + 开/关按钮；`未连接` 的输出不显示。
- **画布**：所有已启用输出的包围盒尺寸；块之间有交集时边框标红并给出重叠像素。
- **状态刷新**：打开时读取一次，可手动「刷新」（快捷键 `r`），默认 2 秒自动刷新一次
  （只读 `xrandr --current`，不写 Xorg 日志；只在点开/关闭时才发 `xrandr` 改布局命令）。
- **闲置提醒**：某块屏超过阈值没有画面变化时，该块整体变暗，右上角出现
  「闲置 6:32　关闭此输出」角标（角标可点，非模态，不打断操作）；右侧卡片同时给出
  「这块屏 X 无变化，要不要关掉（判据：…）」。阈值在顶栏可改，默认 300 秒，改完立即生效。
  **只提醒，不会自动关**。

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

## 开关的实现方式：模式 + 刷新率 + transform + 位置「四件套」

分数缩放靠 xrandr 的 CRTC `--transform` 实现，**只给 `--mode` 不给 `--transform` 会让缩放掉回 100%**。

- **关闭**：先读当前 `模式 / 刷新率 / transform 九元组 / pos / 几何 / primary`，
  写进状态文件，再执行 `xrandr --output X --off`。
- **开启**：用一条命令同时下发
  `xrandr --output X --mode 2880x1800 --rate 100.00 --transform 1.144440,0,0,0,1.144444,0,0,0,1 --pos 4289x2880`
  （primary 屏再加 `--primary`），完成后重新读状态校验 几何=期望值，并检查是否与其他屏重叠，
  任何偏差都会写进日志、也回显在界面上。
- transform 不是几何宽高比硬算出来的，而是关闭那一刻从 `xrandr --current --verbose`
  原样抓的九元组，避免四舍五入漂移（DP-0 实测 1.333328、DP-4 1.331238、HDMI-0 1.144440）。

## 保底规则（硬性）

1. **不关内屏**：`ConnectorType: Panel` 的输出一律拒绝（与 `~/.local/bin/screen-off.sh` 同一策略）。
   界面上按钮置灰为「内屏不可关闭」，命令行返回错误。
2. **不关最后一块亮屏**：关闭前统计 `xrandr --query` 里已启用的输出数，等于 1 时直接拒绝，
   按钮置灰。
3. **关闭前二次确认**：确认框显示「关闭后剩余几块、分别是哪几块、画布尺寸从多少变成多少」，
   并给出该输出的一行恢复命令；若目标屏正是浏览器窗口所在屏、或它是 primary / 内屏，会额外红字提示。
4. **所有操作落日志**：`~/.cache/display-manager.log`，包含关闭前的四件套、恢复来源、
   校验结果；xrandr 失败时把原始 stderr 一并写入。
5. **恢复入口三件套**：命令行 `on` / `restore-all`；界面「恢复全部」；下次打开界面时该输出
   会以「已关闭」出现在列表里，点一下就能开回来（参数来自状态文件）。

## 与 `~/.local/bin/hdmi-watchdog.sh` 的共存（已核实）

读源码 + 看它的日志后的结论：

- 看门狗默认 `FORCE_RECOVER=0`，自启项 `~/.config/autostart/hdmi-watchdog.desktop`
  没有传环境变量，所以实际运行在 0。该模式下它**只在「先观测到 disconnected，之后又回来但没启用」**
  这个特征序列下才补 `xrandr`；用户主动关闭（connected 但没有几何）不会被拉起，
  它的日志实测就是这么写的：`HDMI-0 被关闭且无掉线前兆（可能是用户主动关闭），保留基线但不自动点亮`
  （2026-11-09 21:19 的多条记录）。所以**本界面的日常关闭是与看门狗相容的**，我们没有改它。
- 唯一残留冲突：在本界面关掉 `HDMI-0` 之后，如果**物理拔插一次线**（真实 HPD 掉线又回来），
  看门狗会认为这是故障，用 `~/.cache/hdmi-watchdog.layout` 里的基线把它点亮。
  这不是本工具常规路径，但确实会在「关了线又拔插」时发生。处理方式：
  - 本工具在关闭时会往 `~/.cache/display-manager.off` 写一份「用户主动关闭名单」，
    但**不改动**看门狗的脚本和基线（基线是恢复所需的参数，删了反而没法一键开回来）。
  - 如果你希望连这一步也拦住，可选补丁（**由你决定是否执行**，本仓库不自动改别人的脚本）：
    在 `hdmi-watchdog.sh` 的 `off)` 分支、`recover "$n"` 之前插入

    ```bash
    if grep -qx "$n" "$HOME/.cache/display-manager.off" 2>/dev/null; then
      log "$n 是用户在 screen 界面里主动关闭的，跳过自动恢复"
      continue
    fi
    ```

    恢复方式：把 `~/.cache/display-manager.off` 里对应名字删掉（在本界面点「开启」时会自动清），
    或直接 `python3 backend/dm_cli.py restore-all`，看门狗随即恢复原有的兜底行为。

## 与 `~/.cache/screen-off.state` 的关系

`screen-off.sh` 已有 list/off/on/toggle/restore-all，也有「拒绝内屏/拒绝最后一块」的保护。
本工具**没有调用它，而是把逻辑并在后端**，取舍如下：

- 需要原样保存 transform 九元组并做逐块几何校验、重叠检查，脚本里不好表达；
- 需要和闲置检测、Web 界面共用同一份状态与同一把锁，避免两个进程抢 xrandr；
- 但两者的信息是通用的：本工具把四件套存在 `~/.cache/display-manager.state`，
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
  4. GNOME 顶栏时钟每分钟跳一次，会给「顶栏所在那块屏」持续制造 Damage，
     那一块可能永远不进闲置提醒；
  5. 刚启动界面时，某屏此后一直没变化，计时从启动那一刻算起（不是从「上次真实变化」算）。
  所以它是**近似信号**，界面文案也写成「似乎闲置 / 要不要关掉」，且必须人工确认才关。

## 界面把自己的屏关掉了怎么救

1. 最快：任意终端里

   ```bash
   python3 ~/eric/wuyan_github/screen/backend/dm_cli.py on HDMI-0
   # 或者不知道关了哪块
   python3 ~/eric/wuyan_github/screen/backend/dm_cli.py restore-all
   ```

   `on` 不需要浏览器、不依赖界面所在屏。
2. 如果连那条屏也黑着，用 `Ctrl+Alt+F3` 切到文本终端执行上面的命令，再 `Ctrl+Alt+F1/F2` 切回图形界面。
3. 也可以 `xrandr --output HDMI-0 --auto` 先亮起来，再用本工具的 `on` 校正位置/缩放。
4. 界面重开后，被关掉的输出会以「已关闭」列在右侧和底部「未启用的输出」托盘里，点「开启」即可。

## 运行期文件

| 路径 | 用途 |
| --- | --- |
| `~/.cache/display-manager.state` | 关闭前保存的四件套（模式/刷新率/transform/位置/primary） |
| `~/.cache/display-manager.off` | 用户主动关闭的输出名单（供与看门狗共存、界面标注） |
| `~/.cache/display-manager.log` | 操作日志（含 xrandr 原始 stderr） |
| `~/.config/display-manager/config.json` | 闲置阈值（默认 300 秒，0 = 关闭提醒） |
| `~/.cache/hdmi-watchdog.layout` | 看门狗的基线，本工具**只读**，用作没有关闭记录时的恢复兜底 |

`.gitignore` 已排除日志、状态、`__pycache__`、截图等运行时产物。

## 依赖

- Python 3.12（本机自带），**只用标准库**：`http.server` / `subprocess` / `ctypes`。
- 系统库：`libX11.so.6`、`libXdamage.so.1`、`libXss.so.1`（本机已随 GNOME 安装，无需装包）。
- 不用 sudo，不装 xscreensaver / xprintidle / xdotool。

## 已知限制

- 只在 X11（ubuntu-xorg）下成立；Wayland 会话里 `xrandr` 改不了布局。
- GNOME/mutter 在 RandR 变化后有时会重新套用自己的显示配置，若发现关掉后又被拉回，
  先看 `~/.cache/display-manager.log` 与 `~/.cache/hdmi-watchdog.log` 确认是谁动的手。
- 闲置检测只覆盖「画面变化」，不区分「有变化但没人看」和「没人看但无变化」。
- 未连接（disconnected）的输出不参与开关与统计。

## 验证状态

已验证（本机真机，2026-11-09）：

- 三屏状态读取、几何/模式/刷新率/transform/缩放解析（DP-0 / DP-4 / HDMI-0 数值与需求描述一致）；
- **HDMI-0 关闭再开启的完整回归**（关闭前后 `xrandr --query` 对比）：

  ```text
  关闭前  HDMI-0 connected 3296x2060+4289+2880 (normal ...) 302mm x 189mm
  关闭后  HDMI-0 connected (normal left inverted right x axis y axis)      ← 无几何
         Screen 0: current 5120 x 5010（从 7585x5010 收缩，DP-0 / DP-4 未动）
  恢复后  HDMI-0 connected 3296x2060+4289+2880 (normal ...) 302mm x 189mm
  transform: 1.144440,0,0,0,1.144440,0,0,0,1（与关闭前一致），2880x1800@100.00
  重叠检查: overlaps = []，画布回到 7585x5010
  ```

- 恢复来源两条路径都跑通：优先用 `~/.cache/display-manager.state` 的四件套，
  没有记录时回退到 `~/.cache/hdmi-watchdog.layout` 的基线；
- 「拒绝关闭内屏」：`off DP-4` / `plan DP-4` 均返回拒绝并说明原因（退出码 2）；
- XDamage 逐屏归属：在 DP-0 区域弹出 `xmessage` 窗口时只有 DP-0 计时被重置，
  DP-4 / HDMI-0 计时继续增长；
- `dm_cli.py list`、`/api/state` 返回完整快照（含 idle 字段）。

尚未验证 / 待补：

- 「拒绝关闭最后一块亮屏」没有真机触发（要先把另外两块都关掉，风险不划算），
  仅代码路径与界面置灰逻辑审阅过；
- 浏览器界面的实际渲染与交互（布局图缩放、角标按钮、阈值保存）只做了接口验证，
  未逐一点击；如需截图请在本机打开 `./start.sh` 后自行确认。
