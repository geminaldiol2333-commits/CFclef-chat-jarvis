# -*- coding: utf-8 -*-
"""QQ NT 的 UIA 树 → 结构化消息。**纯函数，不依赖 uiautomation、不碰窗口**，可离线自测。

分层的原因：真正会坏的是「怎么从一棵树里认出输入框/气泡/己方」，那部分是纯数据变换，
用合成树就能测；拿活窗口的那层（app/collectors/uia_qqnt.py）只负责抓树和发事件。

选择器来自社区（treers2/qq-operations）实测：输入框 `qq-msg-editor`、头部联系人
`chat-header__contact-name`、气泡 `message-container*` / `msg-content-container*`、
己方标记 `container--self`。全部可在 config.json 里覆盖（见 Selectors）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

from app.collectors.base import Turn


@dataclass(frozen=True)
class Node:
    """UIA 树里的一个节点。index 就是树的先序序号（= DOM 顺序 = 消息先后）。"""

    index: int
    parent: int
    depth: int
    type: str
    cls: str = ""
    aid: str = ""
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    text: str = ""

    @property
    def key(self) -> str:
        return f"{self.cls} {self.aid}".lower()

    @property
    def has_geo(self) -> bool:
        l, t, r, b = self.rect
        return (r - l) > 0 and (b - t) > 0

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])

    @property
    def cx(self) -> int:
        return (self.rect[0] + self.rect[2]) // 2


@dataclass(frozen=True)
class Selectors:
    """可覆盖的选择器。默认值就是社区实测的那套。"""

    editor: tuple[str, ...] = ("qq-msg-editor",)
    header: tuple[str, ...] = ("chat-header__contact-name",)
    bubble: tuple[str, ...] = ("message-container", "msg-content-container")
    self_mark: tuple[str, ...] = ("container--self",)
    # 界面模式开关：控件名字里出现这些就说明「当前是另一种模式」
    mode_to_classic: str = "切换为经典模式"     # 看到它 → 现在是效率模式
    mode_to_efficiency: str = "切换为效率模式"  # 看到它 → 现在是经典模式
    # 群聊发言人名的容器类名（不同版本可能不同；空 = 只靠位置推断）
    sender: tuple[str, ...] = ("nickname", "sender", "member-name")


SEL_DEFAULT = Selectors()


def _children_index(nodes: Sequence[Node]) -> dict[int, list[Node]]:
    out: dict[int, list[Node]] = {}
    for n in nodes:
        out.setdefault(n.parent, []).append(n)
    for v in out.values():
        v.sort(key=lambda n: n.index)
    return out


def descendants(nodes: Sequence[Node], root: Node) -> list[Node]:
    kids = _children_index(nodes)
    out: list[Node] = []
    stack = list(kids.get(root.index, []))
    while stack:
        n = stack.pop(0)
        out.append(n)
        stack = kids.get(n.index, []) + stack
    return out


def pane_bounds(nodes: Sequence[Node], panel_rect: tuple[int, int, int, int]) -> tuple[int, int]:
    """聊天区（右半区）的左右边界。

    QQ 的布局是 [竖排图标栏][会话列表][聊天区]。会话列表和聊天区都是一整条「窄高容器」，
    所以取这些窄高容器里最宽的那个当会话列表，它的右边界 +1 就是聊天区左边界。
    找不到就退回面板宽度的 22%（QQ 默认会话列表宽度就在这个量级）。
    """
    pl, pt, pr, pb = panel_rect
    if pb <= pt or pr <= pl:
        return pl, pr
    tall = [n for n in nodes
            if n.type == "GroupControl" and n.has_geo
            and n.height > 0.6 * (pb - pt) and n.width < 0.6 * (pr - pl)]
    if tall:
        left = max(tall, key=lambda n: n.width)
        return left.rect[2] + 1, pr
    return pl + int((pr - pl) * 0.22), pr


def detect_mode(nodes: Sequence[Node]) -> str:
    """效率模式（聊天内嵌在主面板）还是经典模式（一个会话一个窗口）。"""
    for n in nodes:
        t = n.text or ""
        if not t or len(t) > 24:
            continue
        if "切换为经典模式" == t:
            return "efficiency"
        if "切换为效率模式" == t:
            return "classic"
    return "unknown"


def find_first(nodes: Sequence[Node], needles: Iterable[str], *, with_geo: bool = False) -> Node | None:
    for n in nodes:
        if with_geo and not n.has_geo:
            continue
        key = n.key
        if any(x in key for x in needles):
            return n
    return None


def find_editor(nodes: Sequence[Node], pane_left: int, sel: Selectors = SEL_DEFAULT) -> Node | None:
    """消息输入框。类名命中优先；没有类名就退回「聊天区里的 EditControl」。"""
    by_class = find_first(nodes, sel.editor)
    if by_class is not None:
        return by_class
    edits = [n for n in nodes if n.type == "EditControl" and n.has_geo and n.rect[0] >= pane_left]
    return max(edits, key=lambda n: n.width * n.height) if edits else None


def find_session(nodes: Sequence[Node], pane_left: int, sel: Selectors = SEL_DEFAULT) -> str:
    """当前会话名 = 头部联系人节点的文本。"""
    hit = find_first(nodes, sel.header)
    if hit is not None and hit.text.strip():
        return hit.text.strip()
    return ""


def _sender_name(nodes: Sequence[Node], bubble: Node, sel: Selectors) -> str | None:
    """群聊里气泡上方的发言人名。

    候选：同一个父节点下、排在气泡之前的短文本，**排除头部联系人/输入框/气泡本身**
    （否则头部那个联系人会被当成发言人），且几何上要紧贴在气泡上方。
    显式类名（sel.sender）优先；没有类名就取紧邻的那个。
    认不出宁可返回 None——挂错人比少一个名字坏得多。
    """
    excluded = set(sel.header) | set(sel.editor) | set(sel.bubble) | set(sel.self_mark)
    sibs = [n for n in nodes
            if n.parent == bubble.parent and n.index < bubble.index
            and n.text.strip() and n.text.strip() != bubble.text.strip()
            and not any(x in n.key for x in excluded)]
    if not sibs:
        return None
    sibs.sort(key=lambda n: n.index)
    for n in reversed(sibs):
        if any(x in n.key for x in sel.sender):
            return n.text.strip()[:32]
    prev = sibs[-1]
    t = prev.text.strip()
    if prev.type != "TextControl" or not (0 < len(t) <= 16) or ":" in t or "：" in t:
        return None
    if bubble.has_geo and prev.has_geo:
        gap = bubble.rect[1] - prev.rect[3]      # 正数 = 名字在气泡上方
        if gap < -8 or gap > 80:                  # 离太远就不是这条气泡的名字
            return None
    return t


def build_turns(nodes: Sequence[Node], sel: Selectors = Selectors(), *,
                pane_left: int = 0) -> list[Turn]:
    """气泡节点 → Turn 列表（按 DOM 顺序，也就是时间顺序）。"""
    bubbles = [n for n in nodes if any(x in n.key for x in sel.bubble)]
    bubbles.sort(key=lambda n: n.index)
    turns: list[Turn] = []
    for b in bubbles:
        text = (b.text or "").strip()
        if not text:                      # 文本可能在子节点里
            parts = [d.text.strip() for d in descendants(nodes, b) if d.text.strip()]
            text = "".join(parts).strip()
        if not text:
            continue
        who = "me" if any(x in b.key for x in sel.self_mark) else "her"
        name = _sender_name(nodes, b, sel) if who == "her" else None
        turns.append(Turn(who=who, text=text, name=name))
    return turns


def new_turns(seen: list[tuple[str, str, str | None]], turns: Sequence[Turn],
              similar) -> list[Turn]:
    """增量：只留下没见过的（滚动/重绘不重复上报）。similar 传 app.ocr.similar，避免循环依赖。"""
    fresh = []
    for t in turns:
        hit = any(w == t.who and similar(old, t.text) for w, old, _ in seen)
        if not hit:
            fresh.append(t)
            seen.append((t.who, t.text, t.name))
    del seen[:-500]
    return fresh


def sel_from(overrides: dict | None) -> Selectors:
    """从 config.json 的覆盖项造一份选择器（只认已知字段，值转成 tuple/str）。"""
    if not overrides:
        return SEL_DEFAULT
    out = {}
    for k, v in overrides.items():
        if not hasattr(SEL_DEFAULT, k) or not v:
            continue
        cur = getattr(SEL_DEFAULT, k)
        out[k] = tuple(v) if isinstance(cur, tuple) else str(v)
    return replace(SEL_DEFAULT, **out) if out else SEL_DEFAULT


if __name__ == "__main__":
    # 自测：不碰窗口、不依赖 uiautomation/numpy。三份合成树分别模拟「类名可用」「只有几何」「空树」。
    from app.textsim import similar

    def n(i, p, d, t, rect, cls="", aid="", text=""):
        return Node(i, p, d, t, cls, aid, rect, text)

    PANEL = (450, 210, 2110, 1170)
    rich = [
        n(0, -1, 0, "PaneControl", PANEL),
        n(1, 0, 1, "GroupControl", (455, 260, 520, 1165)),          # 图标栏
        n(2, 0, 1, "GroupControl", (520, 260, 833, 1165)),          # 会话列表
        n(3, 0, 1, "GroupControl", (833, 260, 2105, 1165)),         # 聊天区
        n(4, 3, 2, "TextControl", (900, 280, 1200, 310), cls="chat-header__contact-name", text="甲方"),
        n(5, 3, 2, "TextControl", (900, 340, 1000, 365), cls="nickname", text="小明"),
        n(6, 3, 2, "GroupControl", (900, 370, 1300, 420), cls="message-container", text="在吗"),
        n(7, 3, 2, "GroupControl", (1500, 430, 2000, 480),
          cls="message-container container--self", text="在的"),
        n(8, 3, 2, "EditControl", (900, 1000, 2000, 1080), cls="qq-msg-editor"),
        n(9, 0, 1, "ButtonControl", (1905, 210, 2110, 260), text="切换为经典模式"),
    ]
    pane_left, _ = pane_bounds(rich, PANEL)
    assert pane_left == 834, pane_left
    assert detect_mode(rich) == "efficiency", detect_mode(rich)
    assert find_session(rich, pane_left) == "甲方"
    ed = find_editor(rich, pane_left)
    assert ed is not None and ed.rect == (900, 1000, 2000, 1080)
    turns = build_turns(rich, pane_left=pane_left)
    assert [t.who for t in turns] == ["her", "me"], turns
    assert turns[0].text == "在吗" and turns[0].name == "小明", turns[0]
    assert turns[1].text == "在的" and turns[1].name is None, turns[1]

    # 增量去重
    seen: list = []
    assert len(new_turns(seen, turns, similar)) == 2
    assert new_turns(seen, turns, similar) == []
    assert len(new_turns(seen, [Turn("her", "在吗", "小明"), Turn("her", "新的一句")], similar)) == 1

    # 只有几何、没有类名：气泡认不出（这正是要提前知道的事）
    plain = [n(0, -1, 0, "PaneControl", PANEL),
             n(1, 0, 1, "GroupControl", (520, 260, 833, 1165)),
             n(2, 0, 1, "GroupControl", (833, 260, 2105, 1165)),
             n(3, 2, 2, "TextControl", (900, 400, 1100, 430), text="在吗"),
             n(4, 2, 2, "EditControl", (900, 1000, 2000, 1080))]
    assert build_turns(plain, pane_left=834) == []
    assert find_editor(plain, 834) is not None       # 输入框还能靠几何找到
    assert find_session(plain, 834) == ""

    # 空树 / 后台状态
    assert build_turns([], pane_left=0) == []
    assert find_editor([], 0) is None
    print("uia_map ok")
