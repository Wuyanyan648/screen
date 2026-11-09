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


if __name__ == "__main__":
    sys.exit(main(sys.argv))
