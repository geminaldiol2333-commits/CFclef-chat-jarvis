# -*- coding: utf-8 -*-
"""文本相似度：判断「这两条是不是同一条消息」。

采集层（OCR 和 UIA 两条路）都要做滚动/重绘去重，所以放在这个无依赖的小模块里：
它不能引入 numpy / rapidocr / uiautomation，否则 UIA 那条纯粹的逻辑就没法离线自测了。
"""
from __future__ import annotations

import difflib


def similar(a: str, b: str) -> bool:
    """同一段文字换个位置/引擎识别，允许有抖动（「傻逼了」↔「傻逼」、「不好意思」↔「不好竟思」）。"""
    if a == b:
        return True
    if difflib.SequenceMatcher(None, a, b).ratio() >= 0.75:
        return True
    return len(a) == len(b) >= 3 and sum(x != y for x, y in zip(a, b)) <= 1  # 短句错一个字


if __name__ == "__main__":
    assert similar("在吗", "在吗")
    assert similar("不好意思", "不好竟思")
    assert similar("傻逼了", "傻逼")
    assert not similar("在吗", "今天几点")
    assert not similar("好", "好的")
    print("textsim ok")
