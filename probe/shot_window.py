# -*- coding: utf-8 -*-
"""按窗口标题截一张图（PrintWindow，窗口被别的窗口盖住也能截）。

诊断用的小工具，不在产品路径上：验证实机效果时，把悬浮窗、QQ 窗口截下来看，
比来回问「你看到什么」可靠。

    python probe/shot_window.py "CFclef-chat-jarvis" C:/tmp/overlay.png
    python probe/shot_window.py QQ C:/tmp/qq.png --list     # 先列出所有含这个词的窗口

用 -X utf8 跑，标题里有中文时才不会乱码。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as w
import sys

u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
u32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
u32.GetWindowTextLengthW.argtypes = [w.HWND]
u32.SetProcessDPIAware()

PW_RENDERFULLCONTENT = 2


def windows(needle: str) -> list[tuple[int, str, tuple[int, int, int, int]]]:
    """所有可见顶层窗口里，标题含 needle 的（返回 hwnd、标题、矩形）。"""
    found: list[tuple[int, str, tuple[int, int, int, int]]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 2)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        if needle.lower() in buf.value.lower() and not u32.GetWindow(hwnd, 4):  # 4 = GW_OWNER
            r = w.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(r))
            found.append((hwnd, buf.value, (r.left, r.top, r.right, r.bottom)))
        return True

    u32.EnumWindows(cb, 0)
    return found


def shot(hwnd: int, rect: tuple[int, int, int, int], path: str) -> None:
    """PrintWindow 取像素，再用 Qt 存 PNG（Qt 已经在环境里，省得自己拼 BMP 头）。"""
    from PySide6.QtGui import QImage

    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    src = u32.GetWindowDC(hwnd)
    mem = g32.CreateCompatibleDC(src)
    bmp = g32.CreateCompatibleBitmap(src, width, height)
    g32.SelectObject(mem, bmp)
    try:
        if not u32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT):
            u32.PrintWindow(hwnd, mem, 0)          # 老系统/某些窗口只认 0

        class BMIH(ctypes.Structure):
            _fields_ = [("biSize", w.DWORD), ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long), ("biPlanes", w.WORD),
                        ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                        ("biSizeImage", w.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", w.DWORD),
                        ("biClrImportant", w.DWORD)]

        info = BMIH()
        info.biSize = ctypes.sizeof(BMIH)
        info.biWidth, info.biHeight = width, -height   # 负数 = 自上而下
        info.biPlanes, info.biBitCount, info.biCompression = 1, 32, 0
        buf = ctypes.create_string_buffer(width * height * 4)
        if not g32.GetDIBits(mem, bmp, 0, height, buf, ctypes.byref(info), 0):
            raise OSError("GetDIBits 失败")
        img = QImage(bytes(buf), width, height, width * 4, QImage.Format_RGB32)
        if not img.save(path, "PNG"):
            raise OSError(f"存不下来：{path}")
    finally:
        g32.DeleteObject(bmp)
        g32.DeleteDC(mem)
        u32.ReleaseDC(hwnd, src)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("title", help="窗口标题的一部分（不分大小写）")
    ap.add_argument("out", nargs="?", help="存到哪个 PNG")
    ap.add_argument("--list", action="store_true", help="只列出匹配的窗口")
    args = ap.parse_args()

    found = windows(args.title)
    if not found:
        print(f"没有标题含「{args.title}」的可见窗口")
        return 1
    for hwnd, title, rect in found:
        print(f"hwnd={hwnd} rect={rect} title={title!r}")
    if args.list or not args.out:
        return 0
    shot(found[0][0], found[0][2], args.out)
    print(f"已保存：{args.out}（{found[0][2][2] - found[0][2][0]}x{found[0][2][3] - found[0][2][1]}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
