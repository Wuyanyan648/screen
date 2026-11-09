# screen —— 本机显示器输出管理 + 闲置提醒

本机自用工具：把 Ubuntu 24.04 + GNOME 46 + X11（ubuntu-xorg）+ NVIDIA 595 驱动的
三屏布局可视化出来，**逐块开关某一路输出**，并提醒「这块屏似乎闲置，要不要关掉」。
界面为中文，逻辑只有一份（`backend/dm_core.py`），网页界面和命令行共用。

形态是**常驻的本地小服务**：固定监听 `127.0.0.1:8765`，根路径 `/` 就是界面，
登录后自动启动，浏览器打开即用；页面每 2 秒自动拉一次状态，不需要手动刷新。

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
cd ~/eric/wuyan_github/screen

./install-autostart.sh     # 安装登录自启（写 ~/.config/autostart，无需 sudo）
./run-service.sh           # 或前台常驻运行（崩溃自动重启，Ctrl-C 退出）
./start.sh                 # 或临时前台运行并自动打开浏览器
```

- 界面入口：<http://127.0.0.1:8765/>（根路径直接返回 [web/display-manager.html](web/display-manager.html)）
- 端口固定为 `8765`：被占用时服务直接报错退出（退出码 3），**不会再漂移到别的端口**。
  这样书签、自启项、快捷键都指向同一个地址。想临时换端口可以 `PORT=9000 ./start.sh`。

### 常驻与开机自启

| 方式 | 命令 | 特点 |
| --- | --- | --- |
| 登录自启（推荐，已安装） | `./install-autostart.sh` | 写 `~/.config/autostart/screen-manager.desktop`，登录桌面后自动起，随会话环境（DISPLAY/XAUTHORITY）正常 |
| 手动常驻 | `./run-service.sh` | 前台循环，进程崩溃 2 秒后自动重启；端口已被占用则直接退出 |
| 临时前台 | `./start.sh` | 会打开浏览器，关掉终端服务就停 |

**关闭自启**：

```bash
./uninstall-autostart.sh    # 删除自启项，并停掉当前服务（不改动显示布局）
```

为什么选 autostart 而不是用户级 systemd：本工具所有动作都是 `xrandr`，必须跑在
图形会话的 `DISPLAY=:0` / `Xauthority` 环境里；autostart 由 gnome-session 启动，
天然带对这套环境，也和本机 8787 那个上位机服务的做法一致。systemd --user 需要手工
hardcode `DISPLAY` 与 gdm 的 `Xauthority` 路径，换机器/换登录方式容易失效。
崩溃自愈由 `run-service.sh` 的循环兜底。

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

## 界面把自己的屏关掉了怎么救

1. 最快：按全局快捷键 **`Ctrl+Alt+Shift+F12`**（恢复全部），不需要浏览器、不需要切终端。
2. 任意终端里：

   ```bash
   python3 ~/eric/wuyan_github/screen/backend/dm_cli.py on DP-4
   # 或者不知道关了哪块
   python3 ~/eric/wuyan_github/screen/backend/dm_cli.py restore-all
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

## 运行期文件

| 路径 | 用途 |
| --- | --- |
| `~/.cache/display-manager.state` | 关闭前保存的四件套（模式/刷新率/transform/位置/primary） |
| `~/.cache/display-manager.off` | 用户主动关闭的输出名单（供与看门狗共存、界面标注） |
| `~/.cache/display-manager.log` | 操作日志（含 xrandr 原始 stderr、快捷键注册/触发） |
| `~/.config/display-manager/config.json` | 闲置阈值（默认 300 秒，0 = 关闭提醒） |
| `~/.config/autostart/screen-manager.desktop` | 登录自启项（`uninstall-autostart.sh` 可删） |
| `~/.cache/hdmi-watchdog.layout` | 看门狗的基线，本工具**只读**，用作没有关闭记录时的恢复兜底 |

`.gitignore` 已排除日志、状态、`__pycache__`、截图等运行时产物。

## 依赖

- Python 3.12（本机自带），**只用标准库**：`http.server` / `subprocess` / `ctypes`。
- 系统库：`libX11.so.6`、`libXdamage.so.1`、`libXss.so.1`（本机已随 GNOME 安装，无需装包）。
- 不用 sudo，不装 xscreensaver / xprintidle / xdotool / xbindkeys。

## 已知限制

- 只在 X11（ubuntu-xorg）下成立；Wayland 会话里 `xrandr` 改不了布局，全局快捷键也抓不到。
- GNOME/mutter 在 RandR 变化后有时会重新套用自己的显示配置，若发现关掉后又被拉回，
  先看 `~/.cache/display-manager.log` 与 `~/.cache/hdmi-watchdog.log` 确认是谁动的手。
- 闲置检测只覆盖「画面变化」，不区分「有变化但没人看」和「没人看但无变化」。
- 未连接（disconnected）的输出不参与开关与统计。
- 内屏（eDP）关闭后理论上存在个别 NVIDIA 机型唤不回来的风险；本机实测可正常恢复
  （见下一节），如果换机器出现唤不回，用 `Ctrl+Alt+Shift+F12` / `dm_cli.py restore-all` /
  `xrandr --output DP-4 --auto` 依次尝试，最坏情况重启图形会话。

## 验证状态

已验证（本机真机，2026-11-09）：

- **界面入口与形态**：`GET /` 与 `/display-manager.html` 均 200，`/index.html` 返回 404
  （旧的 `index.html` 已删除，只保留一份）；页面内 `<input>` / `<textarea>` 数量为 0，
  只有 1 个 `<select>`（闲置阈值），全部操作都是点击。
- **常驻与自愈**：`run-service.sh` 启动后，`kill -9` 掉 python，4 秒内出现新 pid，
  `/api/state` 立即恢复 200；重复启动第二个实例打印端口占用并以退出码 3 退出。
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

尚未验证 / 待补：

- 「拒绝关闭最后一块亮屏」没有真机触发（要先把另外两块都关掉，风险不划算），
  仅代码路径与界面置灰逻辑审阅过；
- 页面在「服务完全停掉后仍停留于已加载页面」时顶栏显示
  「服务未连接，自动重试中」的视觉效果：headless 浏览器在停服后直接跳到了 Chrome 错误页，
  没能截到应用层提示；重连所依赖的「服务已常驻且 4 秒内自愈」已单独验证。
