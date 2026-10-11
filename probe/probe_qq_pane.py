# -*- coding: utf-8 -*-
"""看 QQ NT 主窗口上 chat_area() 认出了什么，并统计聊天区的实际配色。

**隐私**：不跑 OCR、不打印任何消息文本、不落盘。只统计像素颜色和边界坐标——
也就是 QQ 自己的界面配色，不是聊天内容。抓到的帧只在内存里用完就丢。

用法（先自己把 QQ 打开，并且把要测的那个会话点开）：
    python probe/probe_qq_pane.py

它回答三个问题：
 1. chat_area() 有没有正确定位消息区/输入框（现在是没定到，见 docs/QQNT.md）
 2. 聊天区里最常见的几个颜色是什么（面板底色 / 对方气泡 / 自己的气泡）
 3. 按 app/chatapps.py 里那种「哪种底色算自己」的规则，各自占多少
"""
from __future__ import annotations

import collections
import ctypes
import ctypes.wintypes as w
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.capture import Capture, chat_area  # noqa: E402

u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
u32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
EXE = "qq.exe"


def exe_of(pid: int) -> str:
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_uint(1024)
    ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    k32.CloseHandle(h)
    return os.path.basename(buf.value).lower() if ok else ""


def find_main() -> int | None:
    """标题恰好是 "QQ" 的最大窗口。"""
    hits = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if exe_of(pid.value) != EXE:
            return True
        buf = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, buf, 512)
        if buf.value == "QQ":
            r = w.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(r))
            hits.append((hwnd, (r.right - r.left) * (r.bottom - r.top)))
        return True

    u32.EnumWindows(cb, 0)
    return max(hits, key=lambda x: x[1])[0] if hits else None


def main() -> int:
    hwnd = find_main()
    if hwnd is None:
        print("没找到标题为「QQ」的窗口（QQ 开了吗？）")
        return 1
    print("找到 QQ 主窗口，抓帧中 …")
    cap = Capture(hwnd, settle=0.35, max_wait=3.0)
    full = None
    for _ in range(40):
        time.sleep(0.25)
        full = cap.settled()
        if full is not None:
            break
    cap.stop()
    if full is None:
        print("没等到稳定的帧（窗口最小化？）")
        return 1

    H, W = full.shape[:2]
    print(f"帧尺寸 {W}x{H}（窗口 1328x768 时这里是 1660x960，说明系统缩放 125%）")

    area = chat_area(full)
    if area is None:
        print("chat_area() -> None（认不出布局）")
        return 1
    x0, y_top, x1, y_in, bg, y_pane = area
    print(f"chat_area() -> x0={x0} y_top={y_top} x1={x1} y_in={y_in} bg={tuple(bg)} y_pane={y_pane}")
    if x0 == 0 and x1 == W:
        print("  ⚠ 覆盖了整个宽度 = 没认出来（QQ NT 的聊天区是壁纸/渐变，像素锚点失效）")

    pane = full[y_top:y_in, x0:x1]
    flat = pane.reshape(-1, 3)
    cnt = collections.Counter(map(tuple, flat[::3]))
    total = sum(cnt.values())
    print("\n聊天区最常见颜色（RGB / 占比）：")
    for rgb, c in cnt.most_common(12):
        print(f"  rgb{rgb}  {c / total * 100:5.2f}%")

    arr = np.array(list(cnt.keys()), dtype=int)
    shares = np.array(list(cnt.values())) / total
    b, g, r = arr[:, 2], arr[:, 1], arr[:, 0]
    for label, mask in (
        ("蓝色主导 b>r+40,b>=g+20（候选「自己的气泡」）", (b > r + 40) & (b >= g + 20) & (b > 120)),
        ("高蓝低红 b>180,r<120,b>r+120,b>=g+30（建议规则）",
         (b > 180) & (r < 120) & (b > r + 120) & (b >= g + 30)),
    ):
        hit = shares[mask].sum() * 100
        print(f"\n{label}\n  占聊天区 {hit:.2f}%")
        if mask.any():
            best = arr[np.argsort(-(shares * mask))[0]]
            print(f"  里面最多的那个：rgb{tuple(best)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
