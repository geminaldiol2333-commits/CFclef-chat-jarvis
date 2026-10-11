# -*- coding: utf-8 -*-
"""采集器接口：把「怎么看到聊天」和「怎么判/怎么写/怎么填」分开。

一条 Turn = 一条消息（谁说的、文本、可选的媒体）。形状跟 core.questions.build_state
要的完全一致，所以 engine 及以上一行都不用改。

现在有两种实现（见 app/collectors/）：
  ocr_wechat —— 微信：WGC 抓帧 + 像素定位 + 离线 OCR（原有逻辑搬过来的）
  （QQ NT 走哪条路见 docs/QQNT.md：UIA 在 QQ 9.9.36 上读不到聊天内容，
    实测证据写在那里；像素路线待实现）

采集器跑在采集子进程里（app/worker.py），只通过队列和父进程说话，
所以 emit() 出去的东西必须是纯 tuple/str/bytes（不能带 numpy 或 COM 对象）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

# emit(kind, *payload) → 父进程队列里的一条消息。父进程认得的 kind 见 main.py::drain
Emit = Callable[..., None]


@dataclass(frozen=True)
class Media:
    """一条消息附带的媒体。图像给判断模型看（/v1/systemone 的 files），**只在内存里**。"""

    kind: str                    # "image"（将来 "audio" / "video"）
    data: bytes                  # 已编码的字节（JPEG/PNG/WAV…）
    mime: str = "image/jpeg"
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class Turn:
    """一条消息。who 只认 core.questions.build_state 认识的 "me" / "her"。"""

    who: str
    text: str
    name: str | None = None      # 群聊发言人
    media: tuple[Media, ...] = ()


@dataclass(frozen=True)
class Capabilities:
    """采集器能提供什么。用来和判断模型的能力做协商（决定要不要附截图）。"""

    provides_text: bool = True
    provides_media: bool = False
    can_insert: bool = True
    needs_foreground: bool = False   # UIA 类采集器为 True


class BaseCollector:
    """采集器基类。子类至少要实现 start / stop / poll。

    事件约定（都通过 emit 发出去，父进程 main.py::drain 处理）：
      emit("resumed")                    开始采集
      emit("paused")                     暂停
      emit("status", text)               一句提示（失败/警告，不致命）
      emit("chat", name)                 当前会话名变了
      emit("area", (x0,y0,x1,y1))        消息区变了（父进程据此判断"能不能填")
      emit("input", (x0,y0,x1,y1)|None)  输入框位置；None = 用消息区推算（微信那样）
      emit("lines", name, [Turn...], area)  这一拍新出现的消息
      emit("debug", packet)              调试视图的一帧（只有 OCR 采集器有）
    """

    key = "base"
    capabilities = Capabilities()

    def __init__(self, hwnd, emit: Emit, debug_on=None):
        self.hwnd = hwnd
        self.emit = emit
        self.debug_on = debug_on

    def start(self) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def poll(self, emit: Emit) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def alive(self) -> bool:
        return True

    def wait(self) -> None:
        """采集底层若是异步的（WGC），在这里把线程错误抛出来。"""


class Collector(Protocol):
    key: str
    capabilities: Capabilities

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def poll(self, emit: Emit) -> None: ...
    def alive(self) -> bool: ...
    def wait(self) -> None: ...
