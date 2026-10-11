# -*- coding: utf-8 -*-
"""界面文案层。目前只有简体中文，`T()` 原样返回。

这一层保留下来是为了以后加语言时不用改上百个调用点：界面文案统一包一层 `T("…")`
或 `bind(widget, "…")`，真有第二种语言时只要在这里挂一张表、再加个切换开关。
`bind()` 会把「文案 → setter」记在控件上，`retranslate()` 语言变化时重放一遍，
避免把聊天内容或用户输入当成翻译键。

上游那套韩文界面是给 KakaoTalk 的（见 docs/KOREAN.md 的历史版本）；本 fork 不再适配
KakaoTalk，所以韩文表和语言切换一起删了。
"""
from __future__ import annotations


def T(text: str) -> str:
    """界面文案：查得到就翻译，查不到原样返回。现在没有表，永远原样返回。"""
    return text


def language() -> str:
    """当前界面语言。永远 "zh"；留给以后加语言。"""
    return "zh"


def bind(widget, source, setter="setText"):
    """保留文案 key 或动态文案函数，并记下 setter，方便语言变化时重放。"""
    bindings = getattr(widget, "_i18n_bindings", None)
    if bindings is None:
        bindings = widget._i18n_bindings = {}
    bindings[setter] = source
    getattr(widget, setter)(source() if callable(source) else T(source))
    return widget


def retranslate(root) -> None:
    """只刷新显式绑定的界面文案，避免把聊天内容或用户输入当成翻译键。"""
    from PySide6.QtCore import QObject

    for widget in [root, *root.findChildren(QObject)]:
        for setter, source in getattr(widget, "_i18n_bindings", {}).items():
            getattr(widget, setter)(source() if callable(source) else T(source))


if __name__ == "__main__":  # 自测
    assert T("采集中") == "采集中" and T("没有这条") == "没有这条"
    assert language() == "zh"
    print("i18n ok")
