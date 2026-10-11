# -*- coding: utf-8 -*-
"""枚举 QQ NT 的顶层窗口：类名、尺寸、标题是否恰好是 "QQ"。

**隐私**：绝不打印窗口标题（标题可能是联系人/群名）。只报告「标题是否等于 QQ」这个
布尔值、类名和窗口矩形。不做截图、不读消息、不落盘。

用法（先自己把 QQ 打开，本脚本不启动 QQ）：
    python probe/probe_qq_windows.py

适配 QQ NT 时先跑它确认两件事：
 1. 顶层窗口的类名（Electron 是 Chrome_WidgetWin_1）
 2. 主窗口标题是不是恰好 "QQ"——决定 app/chatapps.py 里 main_title 怎么填
 3. 有没有「独立聊天窗口」（标题是联系人名的那些）
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import os
import sys

u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
u32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]

EXE = "qq.exe"


def exe_of(pid: int) -> str:
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_uint(1024)
    ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    k32.CloseHandle(h)
    return os.path.basename(buf.value).lower() if ok else ""


def class_of(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def main() -> int:
    rows = []

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
        rect = w.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(rect))
        rows.append((class_of(hwnd), buf.value == "QQ", len(buf.value),
                     rect.right - rect.left, rect.bottom - rect.top))
        return True

    u32.EnumWindows(cb, 0)
    if not rows:
        print(f"没找到可见的 {EXE} 顶层窗口（QQ 开了吗？）")
        return 1
    print(f"可见 {EXE} 顶层窗口 {len(rows)} 个：")
    for cls, is_qq, tlen, ww, hh in rows:
        print(f"  class={cls!r:26} title_is_QQ={is_qq}  title_len={tlen:3}  {ww}x{hh}")
    print("\n提示：QQ NT 默认把聊天区嵌在标题为「QQ」的主窗口里；如果上面出现"
          "标题不是 QQ 的窗口，那是被拖出来的独立聊天窗口。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
