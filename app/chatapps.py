# -*- coding: utf-8 -*-
"""Per-chat-app profile: which window to capture, which OCR engine, which bubble is mine.

Everything app-specific lives here, so adding an app is one entry. The pixel rules stay where
they were proven (see ocr.who_said); this file only names them.

Currently WeChat only. Upstream also carried a KakaoTalk profile (Windows.Media.Ocr for Korean);
this fork dropped it — see the docstring in app/i18n.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np


def _wechat_me(bg: np.ndarray) -> bool:
    """WeChat's own bubble is green: G clearly above both R and B."""
    return bool(bg[1] > bg[0] + 40 and bg[1] > bg[2] + 40)


@dataclass(frozen=True)
class ChatApp:
    key: str
    label: str
    exes: tuple[str, ...]        # process names, lowercase
    main_title: str              # preferred top-level window title; "" = pick the biggest window
    skip_titles: tuple[str, ...]  # windows that are never a conversation (roster, toasts)
    ocr: str                     # OCR backend name; "rapidocr" is the only one bundled today
    join: str                    # how OCR fragments inside one bubble are glued
    is_me: Callable[[np.ndarray], bool]
    trim_top: Callable[[np.ndarray], int] = lambda chat: 0  # rows to skip (pinned notice, ...)
    collector: str = "ocr"       # 采集引擎：app/collectors/ 里注册的名字（ocr / pixel / uia）


WECHAT = ChatApp("wechat", "微信", ("weixin.exe", "wechat.exe"), "微信", (),
                 "rapidocr", "", _wechat_me)

APPS = {a.key: a for a in (WECHAT,)}
DEFAULT = WECHAT


def by_exe(exe: str) -> ChatApp | None:
    return next((a for a in APPS.values() if exe in a.exes), None)


def get(key: str | None) -> ChatApp:
    return APPS.get(key or "", DEFAULT)


if __name__ == "__main__":  # 自测：颜色规则用实测像素锁住，改错了当场炸
    assert by_exe("weixin.exe") is WECHAT and by_exe("wechat.exe") is WECHAT
    assert by_exe("chrome.exe") is None and by_exe("kakaotalk.exe") is None
    assert get(None) is DEFAULT and get("wechat") is WECHAT
    assert WECHAT.is_me(np.array((149, 236, 105)))          # 微信自己那侧的绿泡
    assert not WECHAT.is_me(np.array((255, 255, 255)))      # 对方白泡
    assert not WECHAT.is_me(np.array((245, 245, 245)))      # 面板底色
    assert WECHAT.trim_top(np.full((20, 20, 3), 200, np.uint8)) == 0
    print("chatapps ok")
