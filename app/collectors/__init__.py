# -*- coding: utf-8 -*-
"""采集器注册表：按 ChatApp 上的 collector 字段挑一种实现。

加一种聊天软件 = 在 app/chatapps.py 加一条 + 在这里加一个引擎名。
采集器只在采集子进程里跑（app/worker.py），父进程不碰。
"""
from __future__ import annotations

from app.collectors.base import BaseCollector, Capabilities, Collector, Emit, Media, Turn

__all__ = ["BaseCollector", "Capabilities", "Collector", "Emit", "Media", "Turn", "build", "engines"]


def engines() -> tuple[str, ...]:
    """可用的采集引擎名。"""
    return ("ocr", "pixel", "uia")


def build(app, hwnd, emit, debug_on=None) -> BaseCollector:
    """按 app.collector 造一个采集器。不认识的引擎当场报错，别静默退回。"""
    engine = getattr(app, "collector", "ocr")
    if engine == "ocr":
        from app.collectors.ocr_wechat import OcrWechatCollector

        return OcrWechatCollector(hwnd, emit, debug_on, app=app)
    if engine == "pixel":
        from app.collectors.pixel_qqnt import PixelQqntCollector

        return PixelQqntCollector(hwnd, emit, debug_on, app=app)
    raise RuntimeError(f"未知采集引擎：{engine!r}（app/chatapps.py 里的 collector 字段）")
