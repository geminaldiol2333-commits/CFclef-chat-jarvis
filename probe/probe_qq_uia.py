# -*- coding: utf-8 -*-
"""Record QQ NT's UIA tree to JSON, read-only. Does NOT launch, click or switch anything.

Why this exists: deciding whether UIA can drive QQ needs a tree captured **with a
conversation actually open**. This tool never opens one - you open QQ and a chat
yourself, then run it. Nothing on screen is touched, no clicks, no keystrokes.

PRIVACY: message text and contact names are NOT written. Each node records whether it
has text and how long it is (that is all the structure analysis needs); no string
content, no images, nothing else on disk.

Usage
    # QQ running, a conversation open, QQ must be the foreground window:
    python probe/probe_qq_uia.py --dump qq_tree.json
    # then analyse it offline (no QQ needed):
    python probe/qq_uia_verdict.py qq_tree.json

Notes
  * QQ only builds a full UIA tree for the ACTIVE window (background = ~9 nodes).
    If the dump looks empty, click the QQ window and run it again.
  * Add --with-text only if you are debugging locally; that DOES write message text.
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

EXE = "qq.exe"


def exe_of(pid: int) -> str:
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_uint(1024)
    ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    k32.CloseHandle(h)
    return os.path.basename(buf.value).lower() if ok else ""


def top_windows() -> list[dict]:
    out = []

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
        rect = w.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(rect))
        out.append({"hwnd": int(hwnd), "title_is_QQ": title.value == "QQ",
                    "title_len": len(title.value), "rect": [rect.left, rect.top, rect.right, rect.bottom],
                    "is_iconic": bool(u32.IsIconic(hwnd)),
                    "is_foreground": int(hwnd) == u32.GetForegroundWindow()})
        return True

    u32.EnumWindows(cb, 0)
    return out


def dump_tree(panel, with_text: bool, limit: int = 60, budget: int = 80000):
    nodes: list[dict] = []

    def walk(ctrl, depth: int, parent: int) -> None:
        if depth > limit or len(nodes) >= budget:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for c in children:
            if len(nodes) >= budget:
                return
            idx = len(nodes)
            try:
                r = c.BoundingRectangle
                name = c.Name or ""
                nodes.append({
                    "i": idx, "p": parent, "d": depth,
                    "type": c.ControlTypeName or "",
                    "cls": c.ClassName or "",
                    "id": getattr(c, "AutomationId", "") or "",
                    "rect": [r.left, r.top, r.right, r.bottom],
                    "text_len": len(name),
                    "offscreen": bool(getattr(c, "IsOffscreen", False)),
                })
                if with_text and name:
                    nodes[idx]["text"] = name
            except Exception:
                continue
            walk(c, depth + 1, idx)

    walk(panel, 0, -1)
    return nodes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", metavar="PATH", default="qq_tree.json")
    ap.add_argument("--with-text", action="store_true",
                    help="ALSO write message text (only for local debugging)")
    args = ap.parse_args()

    import uiautomation as auto

    auto.SetGlobalSearchTimeout(3)
    wins = top_windows()
    if not wins:
        print("没有可见的 QQ 窗口：请先打开 QQ 并把一个会话点开。")
        return 1
    panel_win = next((x for x in wins if x["title_is_QQ"]), wins[0])
    print(f"QQ 顶层窗口 {len(wins)} 个；用 hwnd={panel_win['hwnd']} "
          f"(title_is_QQ={panel_win['title_is_QQ']}, foreground={panel_win['is_foreground']}, "
          f"minimized={panel_win['is_iconic']})")
    if not panel_win["is_foreground"]:
        print("⚠ 这个窗口不是前台窗口。QQ 只对前台窗口构建完整 UIA 树——"
              "请点一下 QQ（让会话可见）再跑一次，否则 dump 基本是空的。")

    panel = auto.ControlFromHandle(panel_win["hwnd"])
    t0 = time.time()
    nodes = dump_tree(panel, args.with_text)
    data = {"captured_at": time.time(), "took_s": round(time.time() - t0, 2),
            "windows": wins, "panel_hwnd": panel_win["hwnd"], "nodes": nodes}
    with open(args.dump, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"已写入 {args.dump}：{len(nodes)} 个节点，{data['took_s']}s，"
          f"不含任何消息文本：{not args.with_text}")
    print("接着离线分析（不需要 QQ）：python probe/qq_uia_verdict.py " + args.dump)
    return 0


if __name__ == "__main__":
    sys.exit(main())
