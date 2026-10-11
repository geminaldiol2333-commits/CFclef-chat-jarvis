# -*- coding: utf-8 -*-
"""Read QQ NT's UIA tree and judge it — one command, no manual window juggling.

The previous version was unusable: running it from a console makes the console the
foreground window, so QQ dropped to the background and its accessibility tree
collapsed to the ~9 nodes it builds for inactive windows. This version does the
window work itself:

  1. brings the QQ window forward (restore + SetForegroundWindow),
  2. waits for Chromium to rebuild the tree,
  3. reads it,
  4. puts the focus back where it was,
  5. prints the verdict inline.

Same sequence the runtime collector uses (`app/collectors/uia_qqnt.py`), so what
you see here is what the app will see.

If you would rather click QQ yourself, use `--countdown 5`: it waits 5 seconds and
then reads without touching the focus.

PRIVACY: nothing is launched, clicked, typed or captured. The dump/verdict record
class names, AutomationIds, control types, rectangles and text LENGTHS only — no
message text, no contact names, nothing written except the optional JSON with
`--dump`.

Usage
    python probe/probe_qq_uia.py                    # one-shot: read + verdict
    python probe/probe_qq_uia.py --countdown 5      # you click QQ, we don't steal focus
    python probe/probe_qq_uia.py --dump qq_tree.json
    python probe/probe_qq_uia.py --hwnd 123456      # force a specific window
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as w
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
u32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
u32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
u32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
u32.SetForegroundWindow.argtypes = [w.HWND]
u32.IsIconic.argtypes = [w.HWND]
u32.IsWindow.argtypes = [w.HWND]
u32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]

EXE = "qq.exe"
REVIVE_WAIT = 1.2       # 等 Chromium 把无障碍树建出来（运行时用 0.8s，这里是离线探测，给宽一点）
NODE_BUDGET = 80000


def _reconfigure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def exe_of(pid: int) -> str:
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_uint(1024)
    ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    k32.CloseHandle(h)
    return os.path.basename(buf.value).lower() if ok else ""


def qq_windows() -> list[dict]:
    out = []
    fg = u32.GetForegroundWindow()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if exe_of(pid.value) != EXE:
            return True
        title = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, title, 512)
        cls = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, cls, 256)
        rect = w.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(rect))
        out.append({"hwnd": int(hwnd), "cls": cls.value, "title_is_QQ": title.value == "QQ",
                    "title_len": len(title.value),
                    "rect": [rect.left, rect.top, rect.right, rect.bottom],
                    "area": (rect.right - rect.left) * (rect.bottom - rect.top),
                    "is_iconic": bool(u32.IsIconic(hwnd)), "is_foreground": int(hwnd) == fg})
        return True

    u32.EnumWindows(cb, 0)
    # 前台优先，其次标题为 QQ 的（效率模式主面板），再按面积从大到小
    out.sort(key=lambda x: (not x["is_foreground"], not x["title_is_QQ"], -x["area"]))
    return out


def bring_forward(hwnd: int) -> int:
    """恢复 + 置前，返回被顶掉的原前台窗口（用于还原）。"""
    prev = u32.GetForegroundWindow()
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, 9)          # SW_RESTORE
    if prev != hwnd:
        prev_tid = u32.GetWindowThreadProcessId(prev, None)
        our_tid = k32.GetCurrentThreadId()
        u32.AttachThreadInput(our_tid, prev_tid, True)
        u32.SetForegroundWindow(hwnd)
        u32.AttachThreadInput(our_tid, prev_tid, False)
    time.sleep(REVIVE_WAIT)              # 等树重建
    return prev


def restore_foreground(prev: int, we_are_reading: int) -> None:
    if not prev or prev == we_are_reading or not u32.IsWindow(prev):
        return
    if u32.GetForegroundWindow() != we_are_reading:
        return                            # 用户自己切走了，别抢
    try:
        prev_tid = u32.GetWindowThreadProcessId(prev, None)
        our_tid = k32.GetCurrentThreadId()
        u32.AttachThreadInput(our_tid, prev_tid, True)
        u32.SetForegroundWindow(prev)
        u32.AttachThreadInput(our_tid, prev_tid, False)
    except Exception:
        pass


def dump_tree(panel, with_text: bool):
    nodes: list[dict] = []

    def walk(ctrl, depth: int, parent: int) -> None:
        if depth > 60 or len(nodes) >= NODE_BUDGET:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for c in children:
            if len(nodes) >= NODE_BUDGET:
                return
            idx = len(nodes)
            try:
                r = c.BoundingRectangle
                name = c.Name or ""
                nodes.append({"i": idx, "p": parent, "d": depth,
                              "type": c.ControlTypeName or "", "cls": c.ClassName or "",
                              "id": getattr(c, "AutomationId", "") or "",
                              "rect": [r.left, r.top, r.right, r.bottom],
                              "text_len": len(name),
                              "offscreen": bool(getattr(c, "IsOffscreen", False))})
                if with_text and name:
                    nodes[idx]["text"] = name
            except Exception:
                continue
            walk(c, depth + 1, idx)

    walk(panel, 0, -1)
    return nodes


def main() -> int:
    _reconfigure_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", metavar="PATH", help="把这份树写成 JSON")
    ap.add_argument("--with-text", action="store_true", help="连消息文本一起写（仅本地调试）")
    ap.add_argument("--countdown", type=float, metavar="SEC",
                    help="不抢焦点：等这么多秒让你自己点到 QQ，然后直接读")
    ap.add_argument("--hwnd", type=int, help="强制用某个窗口")
    args = ap.parse_args()

    from probe.qq_uia_verdict import analyze, report  # noqa: E402  (同目录的离线判定)

    try:
        import uiautomation as auto
    except Exception as exc:
        print("需要 uiautomation：pip install uiautomation", f"({exc})")
        return 1
    auto.SetGlobalSearchTimeout(3)

    wins = qq_windows()
    if not wins:
        print("没找到可见的 QQ 窗口：先把 QQ 打开。")
        return 1
    print(f"可见 QQ 顶层窗口 {len(wins)} 个：")
    for x in wins[:6]:
        print(f"  hwnd={x['hwnd']:<10} class={x['cls']:<22} title_is_QQ={x['title_is_QQ']} "
              f"foreground={x['is_foreground']} minimized={x['is_iconic']} rect={x['rect']}")
    if len(wins) > 1 and not args.hwnd:
        print("  提示：多个窗口时会逐个试（经典模式下会话在独立窗口里）；"
              "要指定就加 --hwnd。")

    todo = [x for x in wins if not args.hwnd or x["hwnd"] == args.hwnd][:5]
    if not todo:
        print(f"没有 hwnd={args.hwnd} 这个窗口。")
        return 1

    if args.countdown:
        print(f"\n{args.countdown:.0f} 秒内请点到 QQ（保持会话可见）。不抢你的焦点……")
        time.sleep(args.countdown)
        print("开始读树")

    best = None
    for x in todo:
        if args.countdown:
            if not x["is_foreground"]:
                continue                      # 用户自己选中了哪个就读哪个
            prev = 0
        else:
            prev = bring_forward(x["hwnd"])
        try:
            panel = auto.ControlFromHandle(x["hwnd"])
            nodes = dump_tree(panel, args.with_text)
        finally:
            if not args.countdown:
                restore_foreground(prev, x["hwnd"])
        res = analyze({"windows": wins, "nodes": nodes})
        score = ((res["editor"] != "none") + (res["bubbles"] != "none")
                 + (res["self"] != "none"), res["bubbles"] != "none", len(nodes))
        print(f"  hwnd={x['hwnd']}: {len(nodes)} 个控件 → editor={res['editor']} "
              f"bubbles={res['bubbles']} self={res['self']}")
        if best is None or score > best[0]:
            best = (score, x, nodes, res)

    _, win, nodes, res = best
    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as f:
            json.dump({"captured_at": time.time(), "windows": wins,
                       "panel_hwnd": win["hwnd"], "nodes": nodes}, f, ensure_ascii=False)
        print(f"\n已写入 {args.dump}（{len(nodes)} 个节点；"
              f"{'含消息文本' if args.with_text else '不含消息文本'}）")

    print(f"\n用窗口 hwnd={win['hwnd']}（title_is_QQ={win['title_is_QQ']}）的结果：")
    report(res)
    print("\n需要我按真实结构对准映射的话，把上面这段（或 --dump 出来的 JSON）发我。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
