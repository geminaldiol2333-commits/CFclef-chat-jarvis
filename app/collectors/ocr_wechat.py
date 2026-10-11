# -*- coding: utf-8 -*-
"""微信采集器：WGC 抓帧 → 像素定位消息区 → 离线 OCR → 按会话去重。

这段逻辑原来整个写在 app/worker.py 里；搬进来是为了让 worker 只当「跑哪一拍、怎么发消息」
的壳，好让 QQ NT 那种完全不同的采集方式（UIA / 像素定位）能挂在同一个位置上。

行为与搬之前完全一致：去重状态（readers）跨暂停/恢复保留，会话名 OCR 抖动用 similar 归并，
消息区认不出来只在第一次报一句 status。帧全程内存，绝不落盘。
"""
from __future__ import annotations

import time

import numpy as np

from app import chatapps
from app.capture import Capture, chat_area, unminimize
from app.collectors.base import BaseCollector, Capabilities
from app.ocr import Reader, read_title, similar


def _packet(full, area, title, reader, lines):
    """调试视图的一帧：整帧缩到长边 ≤1100 再走队列（原帧 2560 宽裸传要 20MB），只在内存里传，不落盘。
    整数步长切片够用，不引新依赖；框和坐标照发原始值，画的那边按 scale 折算。"""
    k = max(1, -(-max(full.shape[:2]) // 1100))
    small = np.ascontiguousarray(full[::k, ::k])
    return {"w": small.shape[1], "h": small.shape[0], "rgb": small.tobytes(), "scale": k,
            "area": tuple(int(v) for v in area[:4]) if area else None,
            "pane_top": int(area[5]) if area else 0, "title": title,
            "boxes": reader.last_boxes if reader else [],
            "lines": [(w, n, t) for w, n, t, _ in lines],
            "ocr_ms": reader.last_ms if reader else 0, "ts": time.time()}


class OcrWechatCollector(BaseCollector):
    """微信：截图 + OCR。视觉上不抢焦点，可以只读（被遮挡也能截）。"""

    key = "wechat"
    capabilities = Capabilities(provides_text=True, provides_media=True,
                                can_insert=True, needs_foreground=False)

    def __init__(self, hwnd, emit, debug_on=None, app=None):
        super().__init__(hwnd, emit, debug_on)
        self.app = app or chatapps.get("wechat")
        self.cap = None
        self.readers = {}   # {会话名: Reader}，一个会话一套去重状态
        self.title = ""     # 当前会话名
        self.head = None    # 上一帧的头部像素
        self.last_area = None
        self.warned = False

    def start(self) -> None:
        self.cap = Capture(self.hwnd)   # 失败就往外抛，worker 会报 dead
        self.emit("resumed")

    def stop(self) -> None:
        if self.cap is not None:
            self.cap.stop()
            self.cap = None
            self.emit("paused")

    def alive(self) -> bool:
        return self.cap is not None and self.cap.alive()

    def wait(self) -> None:
        if self.cap is not None:
            self.cap.wait()

    def poll(self, emit) -> None:
        cap, app = self.cap, self.app
        unminimize(self.hwnd)
        full = cap.settled()
        if full is None:
            return
        reader, lines = None, []   # 调试视图要用，消息区没认出来时就是空的
        # 每次停稳都重算：拖完窗口微信布局会晚一拍才铺好，只按尺寸变化算一次会锁死
        area = chat_area(full)
        if area is None:
            if not self.warned:
                emit("status", "消息区认不出来（窗口太小？）")
                self.warned = True
            return
        self.warned = False
        cap.area = area           # 采集线程拿它做 diff
        x0, y0, x1, y1, bg, y_pane = area
        rect = (x0, y0, x1, y1)
        if rect != self.last_area:
            emit("area", rect)
            self.last_area = rect
        crop = full[y_pane:y0, x0:x1]   # 头部：会话名在这里
        if self.head is None or not np.array_equal(crop, self.head):  # 名字没动就别白跑一次 OCR
            self.head = crop
            name = read_title(crop, app)
            # OCR 抖一下（「小分队」↔「小分认」）不能分裂出一个新会话
            name = next((k for k in self.readers if similar(k, name)), name) if name else ""
            # ponytail: 认不出就沿用上次；开头就认不出给个占位名，总比把消息全丢了强
            name = name or self.title or "当前会话"
            if name != self.title:
                self.title = name
                emit("chat", self.title)
        reader = self.readers.setdefault(self.title, Reader(app))
        lines = reader.read(full[y0:y1, x0:x1], bg)
        new = reader.new_lines(lines)
        if new:
            emit("lines", self.title, new, rect)
        if self.debug_on is not None and self.debug_on.is_set():
            emit("debug", _packet(full, area, self.title, reader, lines))
