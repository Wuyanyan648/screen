#!/usr/bin/env python3
"""display-manager 命令行入口（与网页界面共用 backend/dm_core.py 的逻辑）。

用法:
    dm_cli.py list                  列出所有输出与布局
    dm_cli.py json                  输出 JSON 快照
    dm_cli.py off   HDMI-0          关闭某块屏（先做「不能关到 0 块」等预检）
    dm_cli.py on    HDMI-0          按关闭前记录的四件套还原
    dm_cli.py toggle HDMI-0
    dm_cli.py restore-all           恢复所有有记录/有看门狗基线的屏
    dm_cli.py plan  HDMI-0          只看关闭后的后果，不动手
    dm_cli.py brightness            列出每块屏的亮度与调光通道
    dm_cli.py brightness get  DP-0  读取某块屏当前亮度（硬件通道会实时读）
    dm_cli.py brightness set  DP-0 60   设置亮度（0-100）
    dm_cli.py brightness set  DP-0 0 60 硬件到底后再叠 60% 软件 gamma
    dm_cli.py brightness extra DP-0 60  只调软件叠加层（硬件必须已在 0）
    dm_cli.py brightness temp DP-0 4500 夜间色温（3000-6500K，6500 = 原生）
    dm_cli.py log                   打印最近的操作日志
    dm_cli.py serve [--port N]      启动网页界面
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dm_core as core  # noqa: E402


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "list"
    args = argv[2:]

    try:
        if cmd in ("list", "ls", "status"):
            print(core.describe(core.snapshot()))
            return 0

        if cmd in ("json", "--json"):
            print(json.dumps(core.snapshot(), ensure_ascii=False, indent=2))
            return 0

        if cmd == "plan":
            name = need_name(args, "plan")
            p = core.plan_off(name)
            print("关闭 %s 后：剩余 %d 块屏（%s），画布 %dx%d → %dx%d" % (
                name, p["remaining_count"], ", ".join(p["remaining"]),
                p["canvas_now"]["w"], p["canvas_now"]["h"],
                p["canvas_after"]["w"], p["canvas_after"]["h"]))
            return 0

        if cmd == "off":
            r = core.turn_off(need_name(args, "off"), "cli")
            print(r["message"])
            print(core.describe(r["state"]))
            return 0

        if cmd == "on":
            r = core.turn_on(need_name(args, "on"), "cli")
            print(r["message"])
            if r.get("warning"):
                print("警告: " + r["warning"])
            print(core.describe(r["state"]))
            return 0

        if cmd == "toggle":
            name = need_name(args, "toggle")
            snap = core.snapshot()
            out = next((o for o in snap["outputs"] if o["name"] == name), None)
            if out is None:
                raise core.DMError("找不到输出 %s" % name)
            r = core.turn_off(name, "cli") if out["enabled"] else core.turn_on(name, "cli")
            print(r["message"])
            if r.get("warning"):
                print("警告: " + r["warning"])
            print(core.describe(r["state"]))
            return 0

        if cmd == "restore-all":
            r = core.restore_all("cli")
            print(r["message"])
            print(core.describe(r["state"]))
            return 0 if r["ok"] else 1

        if cmd == "brightness":
            return brightness_cmd(args)

        if cmd == "log":
            for line in core.read_log_tail(int(args[0]) if args else 40):
                print(line)
            return 0

        if cmd == "serve":
            from dm_server import serve
            return serve(args)

        if cmd in ("-h", "--help", "help"):
            print(__doc__)
            return 0

        raise core.DMError("未知子命令: %s（试用 help）" % cmd)

    except core.DMError as e:
        print("错误: %s" % e, file=sys.stderr)
        return 2


def need_name(args: list[str], cmd: str) -> str:
    if not args:
        raise core.DMError("用法: dm_cli.py %s <输出名>（先用 list 查看输出名）" % cmd)
    return args[0]


def brightness_cmd(args: list[str]) -> int:
    sub = args[0] if args else "list"
    if sub in ("list", "ls"):
        items = core.brightness_state()
        if not items:
            print("没有已连接的输出")
            return 0
        for name, e in items.items():
            val = "-" if e["value"] is None else "%d%%" % e["value"]
            if e.get("extra"):
                val += " +%d%%" % e["extra"]
            if e.get("temp_ready"):
                val += " %s" % ("6500K" if e.get("temp") == 6500 else "%dK" % e.get("temp"))
            extra = e.get("reason") or e.get("note") or ""
            print("%-8s %-8s %-22s %s" % (name, val, e.get("label") or e["mode_label"], extra))
            if e.get("hint"):
                print("         需要权限: " + e["hint"].replace("\n", " && "))
        return 0

    if sub == "get":
        name = need_name(args[1:], "brightness get")
        items = core.brightness_state(refresh=True)
        e = items.get(name)
        if not e:
            raise core.DMError("找不到输出 %s（或它没有连接）" % name)
        val = "-" if e["value"] is None else "%d%%" % e["value"]
        if e.get("extra"):
            val += " + 软件叠加 %d%%（gamma %s）" % (e["extra"], e.get("gamma"))
        print("%s 亮度 %s（%s）" % (name, val, e.get("label") or e["mode_label"]))
        if e.get("temp_ready"):
            print("夜间色温 %s（gamma %s）" % (
                e.get("temp_label"),
                ":".join("%.2f" % v for v in (e.get("temp_gamma") or []))))
        nl = e.get("night_light") or {}
        if nl.get("enabled"):
            print("警告: " + nl.get("note"))
        if e.get("reason"):
            print("原因: " + e["reason"])
        if e.get("hint"):
            print("需要权限:\n" + e["hint"])
        return 0

    if sub == "set":
        if len(args) < 3:
            raise core.DMError("用法: dm_cli.py brightness set <输出名> <0-100> [叠加0-100]")
        extra = None
        if len(args) >= 4:
            try:
                extra = int(args[3])
            except ValueError:
                raise core.DMError("软件叠加必须是 0~100 的整数")
        r = core.set_brightness(args[1], args[2], extra)
        print(r["message"])
        return 0

    if sub in ("extra", "stack"):
        if len(args) < 3:
            raise core.DMError("用法: dm_cli.py brightness extra <输出名> <0-100>"
                               "（需硬件亮度已在 0）")
        r = core.set_brightness_extra(args[1], args[2])
        print(r["message"])
        return 0

    if sub in ("temp", "kelvin", "warm"):
        if len(args) < 3:
            raise core.DMError("用法: dm_cli.py brightness temp <输出名> <3000-6500>"
                               "（6500 = 原生不偏色）")
        r = core.set_brightness_temp(args[1], args[2])
        print(r["message"])
        return 0

    raise core.DMError("未知 brightness 子命令: %s（可用 list/get/set/extra/temp）" % sub)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
