# -*- coding: utf-8 -*-
"""Probe the live QQ NT UIA tree: class names, structure and counts only.

PRIVACY: never prints message text or contact names. For content-bearing nodes it
prints only whether the Name is non-empty and its length. Class names, control
types and rectangles are UI internals, not user content. Nothing is written to disk.

Usage (QQ must be running and logged in; the script launches it if needed):
    python probe/probe_qq_uia.py            # structural report
    python probe/probe_qq_uia.py --launch   # also start QQ first, then close it
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as w
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

QQ_EXE = r"C:\Program Files\Tencent\QQNT\QQ.exe"
SEL_EDITOR = "qq-msg-editor"
SEL_HEADER = "chat-header__contact-name"
SEL_BUBBLE = ("message-container", "msg-content-container")
SEL_SELF = "container--self"

u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
u32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]


def exe_of(pid: int) -> str:
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_uint(1024)
    ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    k32.CloseHandle(h)
    return os.path.basename(buf.value).lower() if ok else ""


def qq_windows() -> list[tuple[int, str, bool]]:
    """(hwnd, title_is_QQ, has_title) for visible qq.exe top-level windows."""
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if exe_of(pid.value) != "qq.exe":
            return True
        buf = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, buf, 512)
        out.append((hwnd, buf.value == "QQ", bool(buf.value)))
        return True

    u32.EnumWindows(cb, 0)
    return out


def revive(hwnd: int) -> bool:
    """QQ 只对前台窗口构建完整 UIA 树（后台实测只有 9 个节点）。
    恢复 + 置前 + 等树重建，抄 qq-operations 的 Revive-QQPanel。返回是否动了手。"""
    u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    u32.GetForegroundWindow.restype = ctypes.c_void_p
    u32.ShowWindow(hwnd, 9)  # SW_RESTORE
    fg = u32.GetForegroundWindow()
    if fg != hwnd:
        fg_tid = u32.GetWindowThreadProcessId(fg, None)
        our_tid = k32.GetCurrentThreadId()
        u32.AttachThreadInput(our_tid, fg_tid, True)
        u32.SetForegroundWindow(hwnd)
        u32.AttachThreadInput(our_tid, fg_tid, False)
    time.sleep(1.2)  # 等 Chromium 把无障碍树建出来
    return True


def walk(ctrl, depth=0, limit=14, out=None, budget=None):
    """Depth-first dump of (class, control type, has_name, name_len, rect) up to a node budget."""
    if out is None:
        out, budget = [], [40000]
    if depth > limit or budget[0] <= 0:
        return out
    try:
        children = ctrl.GetChildren()
    except Exception:
        children = []
    for c in children:
        if budget[0] <= 0:
            break
        budget[0] -= 1
        try:
            cls = c.ClassName or ""
            typ = c.ControlTypeName or ""
            nm = c.Name or ""
            r = c.BoundingRectangle
            rect = (r.left, r.top, r.right, r.bottom)
        except Exception:
            continue
        out.append((depth, cls, typ, bool(nm), len(nm), rect))
        walk(c, depth + 1, limit, out, budget)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--launch", action="store_true", help="start QQ first and close it afterwards")
    args = ap.parse_args()

    if args.launch and os.path.isfile(QQ_EXE):
        subprocess.Popen([QQ_EXE])
        for _ in range(8):
            time.sleep(4)
            if qq_windows():
                break

    import uiautomation as auto

    wins = qq_windows()
    if not wins:
        print("no visible qq.exe window (QQ not running?)")
        return 1
    qq_hwnds = [h for h, is_qq, _ in wins if is_qq]
    print(f"qq.exe visible windows: {len(wins)}; titled 'QQ': {len(qq_hwnds)}")
    if not qq_hwnds:
        print("no window titled exactly 'QQ' — QQ may be in classic (separate chat windows) mode")
        return 1

    # 先置前再读树：后台只有 ~9 个节点（实测）
    hwnd_panel = qq_hwnds[0]
    print("reviving panel to the foreground (QQ only builds a full UIA tree for the active window) ...")
    revive(hwnd_panel)
    panel = auto.ControlFromHandle(hwnd_panel)
    print(f"panel: class={panel.ClassName!r} type={panel.ControlTypeName!r}")

    nodes = walk(panel)
    print(f"\ntree nodes walked: {len(nodes)}")
    classes: dict[str, int] = {}
    for depth, cls, typ, has_name, nlen, rect in nodes:
        classes[cls] = classes.get(cls, 0) + 1
    print("\ninteresting classes (count):")
    for key in (SEL_EDITOR, SEL_HEADER, SEL_SELF, *SEL_BUBBLE):
        hits = {c: n for c, n in classes.items() if key in c}
        if hits:
            for c, n in sorted(hits.items(), key=lambda x: -x[1])[:6]:
                print(f"  {key:28s} -> {c}  x{n}")
        else:
            print(f"  {key:28s} -> (not found)")

    bubbles = [n for n in nodes if any(k in n[1] for k in SEL_BUBBLE)]
    self_bubbles = [n for n in bubbles if SEL_SELF in n[1]]
    named = [n for n in bubbles if n[3]]
    print(f"\nbubble nodes: {len(bubbles)}  (with non-empty Name: {len(named)})")
    print(f"  of which container--self: {len(self_bubbles)}")
    if named:
        lens = sorted(n[4] for n in named)
        print(f"  Name length min/median/max: {lens[0]}/{lens[len(lens)//2]}/{lens[-1]}")
    print("\n  bubble classes seen:", sorted({n[1] for n in bubbles})[:10])

    ed = find_by_class(panel, SEL_EDITOR)
    if ed is not None:
        r = ed.BoundingRectangle
        print(f"\n{ SEL_EDITOR }: rect=({r.left},{r.top},{r.right},{r.bottom})")
    hd = find_by_class(panel, SEL_HEADER)
    if hd is not None:
        print(f"{SEL_HEADER}: found, non-empty Name={bool(hd.Name)}, len={len(hd.Name or '')}")

    # mode toggle: a control whose Name mentions 切换为经典模式 / 切换为效率模式
    modes = [n for n in nodes if n[1] and n[3] and n[4] < 20]
    print(f"\ncandidate small-named controls: {len(modes)} (names not printed)")

    if args.launch:
        subprocess.run(["taskkill", "/F", "/IM", "QQ.exe"], capture_output=True)
        print("\nclosed QQ")
    return 0


def find_by_class(root, needle: str, shallow: bool = False):
    """Depth-first search for the first descendant whose ClassName contains needle."""
    try:
        children = root.GetChildren()
    except Exception:
        return None
    for c in children:
        try:
            if needle in (c.ClassName or ""):
                return c
        except Exception:
            pass
        if not shallow:
            hit = find_by_class(c, needle)
            if hit is not None:
                return hit
    return None


if __name__ == "__main__":
    sys.exit(main())
