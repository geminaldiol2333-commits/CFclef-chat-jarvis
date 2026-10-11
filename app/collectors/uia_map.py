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
import re
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

from app.collectors.base import Turn


@dataclass(frozen=True)
class Node:
    """UIA 树里的一个节点。index 就是树的先序序号（= DOM 顺序 = 消息先后）。

    text 是节点文本；text_len 是文本长度（dump 里只存长度、不存内容，
    所以离线分析用 text_len，活体采集用 text；两者都通过 has_text 判断「有没有文字」）。
    """

    index: int
    parent: int
    depth: int
    type: str
    cls: str = ""
    aid: str = ""
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    text: str = ""
    text_len: int = 0

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip()) or self.text_len > 0

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
    """可覆盖的选择器。前四条的默认值来自社区实测的类名；后两条是本机实测的**结构 id**
    （QQ 9.9.x 已经把 CSS 类名去掉了，但 `ml-root` / `func-bar-*` 这类 id 还在）。"""

    editor: tuple[str, ...] = ("qq-msg-editor",)
    header: tuple[str, ...] = ("chat-header__contact-name",)
    bubble: tuple[str, ...] = ("message-container", "msg-content-container")
    self_mark: tuple[str, ...] = ("container--self",)
    message_list: tuple[str, ...] = ("ml-root",)          # 消息列表根节点（实测 id）
    composer: tuple[str, ...] = ("msg-editor", "input-area", "editor")  # 输入区（类名/id 片段）
    # 界面模式开关：控件名字里出现这些就说明「当前是另一种模式」
    mode_to_classic: str = "切换为经典模式"     # 看到它 → 现在是效率模式
    mode_to_efficiency: str = "切换为效率模式"  # 看到它 → 现在是经典模式
    # 群聊发言人名的容器类名（不同版本可能不同；空 = 只靠位置推断）
    sender: tuple[str, ...] = ("nickname", "sender", "member-name")


SEL_DEFAULT = Selectors()
PANE_TOL = 8   # 会话列表和聊天区之间有几像素边框/分隔，比边界时给点容差


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
    """聊天区的左右边界 `(left, right)`，**与窗口尺寸/缩放无关**。

    QQ 的布局是 [竖排图标栏][会话列表][聊天区]，每层都是一个「整高容器」。
    其中宽度占面板 >40% 的值有两个：把会话列表+聊天区一起包住的容器，和聊天区自己。
    两者取**左边缘更靠右的那个**就是聊天区（实测：包裹容器 left=520，聊天区 left=832）。
    窗口拉伸/最大化时这些容器跟着变，但这是相对判断，不受影响。
    实在找不到就按比例切（面板宽度的 22%，QQ 默认会话列表就在这个量级）。
    """
    pl, pt, pr, pb = panel_rect
    if pb <= pt or pr <= pl:
        return pl, pr
    wide_enough = 0.4 * (pr - pl)
    full_height = [n for n in nodes
                  if n.type in ("GroupControl", "WindowControl", "PaneControl") and n.has_geo
                  and n.height > 0.55 * (pb - pt) and n.width >= wide_enough
                  and n.rect[0] > pl]
    if full_height:
        chat = max(full_height, key=lambda n: n.rect[0])   # 左边缘最靠右的那个 = 聊天区
        return chat.rect[0], chat.rect[2]
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


def find_message_list(nodes: Sequence[Node], pane_left: int) -> Node | None:
    """消息列表的根容器。

    本机实测：QQ 9.9.x 把 CSS 类名从无障碍树里拿掉了，但保留了 `ml-root` 这个 id，
    它下面的子树装了所有消息（386 个节点 / 134 个文本节点）。
    没有这个 id 时的兜底：聊天区里“子树文本节点最多”的那个容器。
    """
    hit = find_first(nodes, SEL_DEFAULT.message_list, with_geo=True)
    if hit is not None:
        return hit
    cands = [n for n in nodes if n.has_geo and n.rect[0] >= pane_left - PANE_TOL
             and n.type in ("GroupControl", "WindowControl", "PaneControl")]
    best, best_score = None, 0
    for n in cands:
        texts = [d for d in descendants(nodes, n) if d.type == "TextControl" and d.has_text]
        if len(texts) > best_score:
            best, best_score = n, len(texts)
    return best if best_score >= 3 else None


def _pad(pane_width: int) -> int:
    """边界容差：按面板宽度取比例，别写死像素（拉伸/缩放/高 DPI 下才准）。"""
    return max(6, int(pane_width * 0.012))


def _rows_by_y(nodes: Sequence[Node], gap: int) -> list[list[Node]]:
    """把一批节点按 y 聚成行（相邻行距 ≤ gap）。"""
    rows: list[list[Node]] = []
    for n in sorted(nodes, key=lambda n: (n.rect[1], n.rect[0])):
        if rows and n.rect[1] - rows[-1][0].rect[1] <= gap:
            rows[-1].append(n)
        else:
            rows.append([n])
    return rows


def find_session_row(nodes: Sequence[Node], pane_left: int, sel: Selectors = SEL_DEFAULT,
                     list_node: Node | None = None, panel_right: int = 0) -> list[Node]:
    """头部那一行的所有文本节点（按 x 排好）。

    为什么要「一行」而不是「一条」：QQ 的头部会被切成好几个文本节点
    （实测同一行上有 len=12 / 1 / 21 / 5 四段），只取最左边那条会只拿到一个碎片，
    而且随头部状态（成员数、按钮）飘。会话名最终取这一行拼起来的结果。
    """
    hit = find_first(nodes, sel.header)
    if hit is not None and hit.has_text:
        return [hit]
    if list_node is None or not list_node.has_geo:
        return []
    above = [n for n in nodes if n.type == "TextControl" and n.has_text
             and n.has_geo and n.rect[1] < list_node.rect[1]
             and n.rect[0] >= pane_left - PANE_TOL and n.rect[2] <= (panel_right or 10 ** 9)]
    if not above:
        return []
    # 取「最靠近消息列表顶部的那一行」——与缩放/最大化无关，
    # 比写死「上方 80px」稳：再往上是标题栏和窗口按钮（实测那里有个 1 字符按钮）。
    heights = sorted(n.height for n in above)
    gap = max(4, int(0.6 * heights[len(heights) // 2]))
    rows = _rows_by_y(above, gap)
    return sorted(rows[-1], key=lambda n: n.rect[0])


def find_session_node(nodes: Sequence[Node], pane_left: int, sel: Selectors = SEL_DEFAULT,
                      list_node: Node | None = None, panel_right: int = 0) -> Node | None:
    """头部行的第一个节点（给「定位到了吗」用）。"""
    row = find_session_row(nodes, pane_left, sel, list_node=list_node, panel_right=panel_right)
    return row[0] if row else None


def find_session(nodes: Sequence[Node], pane_left: int, sel: Selectors = SEL_DEFAULT,
                 list_node: Node | None = None, panel_right: int = 0) -> str:
    """当前会话名（当历史记录的 key）：头部行拼接，去尾部的群成员数。

    拼一整行而不是取一条：一来不会只拿到碎片，二来这个 key 越完整越不会两个会话碰在一起。
    ponytail: 头部显示内容变了（如成员数增加）会当成新会话；所以去掉尾部的 `(1234)`。
    """
    row = find_session_row(nodes, pane_left, sel, list_node=list_node, panel_right=panel_right)
    if not row:
        return ""
    text = "".join(n.text.strip() for n in row).strip()
    return re.sub(r"[（(]\d+[)）]\s*$", "", text).strip()


def find_editor(nodes: Sequence[Node], pane_left: int, sel: Selectors = SEL_DEFAULT,
                list_node: Node | None = None, panel_right: int = 0) -> Node | None:
    """消息输入区（填候选要点它）。

    类名/id 命中优先（`qq-msg-editor` 之类）；本机实测输入区**没有 id 也没类名**，
    但结构很确定：消息列表下方、聊天区内、横跨整个宽度的那块大容器
    （实测 rect=(832,956,1880,1095)，比它更靠下的 (…,1095,1165) 是工具栏）。"""
    by_class = find_first(nodes, tuple(sel.editor) + tuple(sel.composer))
    if by_class is not None and by_class.has_geo:
        return by_class
    if list_node is None or not list_node.has_geo:
        return None
    right = panel_right or 10 ** 9
    pane_width = max(1, right - pane_left) if panel_right else max(1, list_node.width)
    tol = _pad(pane_width)
    # 输入区高度按「消息列表高度」取相对区间（实测：列表高 582 → 输入区 139、工具栏 70）
    min_h = max(24, int(0.10 * list_node.height))
    max_h = max(min_h + 1, int(0.50 * list_node.height))
    below = [n for n in nodes
             if n.type == "GroupControl" and n.has_geo
             and n.rect[1] >= list_node.rect[3] - tol and n.rect[0] >= pane_left - tol
             and n.rect[2] <= right + tol
             and n.height >= min_h and n.height <= max_h
             and n.width >= 0.55 * pane_width]        # 工具栏只有半宽，输入区是整宽
    # 紧贴在消息列表下面那块整宽容器就是输入区（它下面那行是工具栏）。
    # 更准的信号：输入区里含「已输入的文字」，所以优先取含文本的那块（取了其中面积最小的，
    # 避开把输入区和工具栏一起包住的父容器）。
    with_text = [n for n in below
                 if any(d.type == "TextControl" and d.text.strip()
                        for d in descendants(nodes, n))]
    if with_text:
        return min(with_text, key=lambda n: n.width * n.height)
    return min(below, key=lambda n: n.rect[1]) if below else None


def _text_rows(nodes: Sequence[Node], box: tuple[int, int, int, int], gap: int = 0):
    """把矩形范围内的文本节点按 y 聚成行；gap 和边界容差都按尺寸算，不写死像素。"""
    l, t, r, b = box
    pad = _pad(max(1, r - l))
    inside = [n for n in nodes if n.type == "TextControl" and n.has_text and n.has_geo
              and n.rect[0] >= l - pad and n.rect[2] <= r + pad
              and n.rect[1] >= t - pad and n.rect[3] <= b + pad]
    if not gap and inside:
        hs = sorted(n.height for n in inside)
        gap = max(4, int(0.6 * hs[len(hs) // 2]))
    return _rows_by_y(inside, gap or 8)


def build_turns_from_rows(nodes: Sequence[Node], list_node: Node,
                          sel: Selectors = SEL_DEFAULT) -> list[Turn]:
    """几何兜底：没有气泡类名时，用消息列表里的「文本行」当消息，靠左右对齐判己方/对方。

    本机实测（QQ 9.9.36）：`ml-root` 子树里有 20 个数字 id 的消息容器，但它们的子节点
    （头像/气泡）几何为 0，只有文本节点带真实矩形。所以在拿不到类名时只能这么读：
    按 y 聚行 → 一行的文本拼起来 = 一条消息；行中心在列表中线右侧 = 我发的。

    ponytail: 「发言人名」「已读」「时间戳」这些也会被当成行，所以只能算可用但不精确；
    一旦 QQ 又把类名暴露回来，`build_turns` 的类名路径优先，不走这里。
    """
    if not list_node.has_geo:
        return []
    mid = (list_node.rect[0] + list_node.rect[2]) / 2
    turns: list[Turn] = []
    for row in _text_rows(nodes, list_node.rect):
        row.sort(key=lambda n: n.rect[0])
        text = "".join(n.text.strip() for n in row).strip()
        if not text:
            continue
        center = (min(n.rect[0] for n in row) + max(n.rect[2] for n in row)) / 2
        turns.append(Turn(who="me" if center > mid else "her", text=text))
    return turns


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
                pane_left: int = 0, list_node: Node | None = None) -> list[Turn]:
    """气泡节点 → Turn 列表（按 DOM 顺序 = 时间顺序）。

    类名路径（气泡/己方标记都存在）优先；本机 QQ 9.9.x 上没有这些类名，
    所以退回 `build_turns_from_rows` 的几何路径。两条都不行就返回空（调用方会提示）。"""
    bubbles = [n for n in nodes if any(x in n.key for x in sel.bubble)]
    if bubbles:
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
    if list_node is not None:
        return build_turns_from_rows(nodes, list_node, sel)
    return []


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
    assert pane_left == 833, pane_left          # 这份 fixture 里聊天区容器从 833 开始
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

    # 只有几何、没有类名：气泡类名路径走不通，但**结构 id 还在**（ml-root + 整宽输入区）
    # 这份 fixture 的形状是照本机真实 dump（QQ 9.9.36）画的。
    real = [
        n(0, -1, 0, "PaneControl", PANEL),
        n(1, 0, 1, "GroupControl", (520, 260, 833, 1165)),           # 会话列表
        n(2, 0, 1, "GroupControl", (832, 260, 2105, 1165)),          # 聊天区
        n(3, 2, 2, "TextControl", (855, 281, 1094, 307), text="甲方"),      # 头部（最靠左）
        n(4, 2, 2, "TextControl", (1121, 281, 1387, 307), text="群聊(423)"),  # 靠右，不能被当会话名
        n(5, 2, 2, "GroupControl", (832, 325, 1880, 907), aid="ml-root"),     # 消息列表
        n(6, 5, 3, "TextControl", (920, 383, 1726, 410), text="对方发的一句话"),
        n(7, 5, 3, "TextControl", (907, 530, 1178, 552), text="对方又一句"),
        n(8, 5, 3, "TextControl", (1746, 560, 1844, 580), text="我发的"),      # 靠右 = 我
        n(9, 2, 2, "GroupControl", (852, 916, 1218, 957), aid="func-bar-shortcuts-left"),
        n(10, 2, 2, "GroupControl", (832, 956, 1880, 1095)),                  # 输入区（整宽）
        n(11, 2, 2, "GroupControl", (832, 1095, 1880, 1165)),                 # 工具栏
    ]
    pane_left, _ = pane_bounds(real, PANEL)
    assert pane_left == 832, pane_left          # 聊天区自己那块容器的左边缘（实测 832）
    lst = find_message_list(real, pane_left)
    assert lst is not None and lst.rect == (832, 325, 1880, 907), lst
    assert find_session(real, pane_left, list_node=lst, panel_right=PANEL[2]) == "甲方群聊", \
        find_session(real, pane_left, list_node=lst, panel_right=PANEL[2])   # 整行拼接 + 去掉尾部成员数
    ed = find_editor(real, pane_left, list_node=lst, panel_right=PANEL[2])
    assert ed is not None and ed.rect == (832, 956, 1880, 1095), ed      # 不是工具栏、也不是半宽工具条
    turns = build_turns(real, pane_left=pane_left, list_node=lst)
    assert [t.who for t in turns] == ["her", "her", "me"], turns
    assert [t.text for t in turns] == ["对方发的一句话", "对方又一句", "我发的"], turns

    # 没有类名、没有 ml-root、也没有整宽输入区 → 什么都认不出（不能静默猜）
    bare = [n(0, -1, 0, "PaneControl", PANEL),
            n(1, 0, 1, "GroupControl", (520, 260, 833, 1165)),
            n(2, 0, 1, "GroupControl", (833, 260, 2105, 1165)),
            n(3, 2, 2, "TextControl", (900, 400, 1100, 430), text="在吗")]
    assert find_message_list(bare, 834) is None
    assert find_editor(bare, 834, list_node=None) is None
    assert find_session(bare, 834) == ""
    assert build_turns(bare, pane_left=834, list_node=None) == []

    # ── 拉伸 / 最大化 / 不同缩放：同一套规则必须都定位对 ──────────────────
    def layout(win, scale=1.0, session_frac=0.24, n_her=5, n_me=3):
        """按给定窗口尺寸/缩放，造一棵与真实结构同形的树（app 根 / 包裹容器 / 会话列表 / 聊天区）。"""
        l, t, r, b = win
        W = r - l
        S = lambda v: int(round(v * scale))                      # noqa: E731
        rail_w, top, head = S(70), S(50), S(45)
        rail = (l + S(5), t + top, l + S(5) + rail_w, b - S(5))
        sess = (rail[2], t + top, rail[2] + int(W * session_frac), b - S(5))
        chat = (sess[2], t + top, r - S(5), b - S(5))
        content = (sess[0], t + top, r - S(5), b - S(5))
        list_t = chat[1] + head + S(15)
        composer_h, toolbar_h = S(140), S(70)
        list_b = chat[3] - composer_h - toolbar_h - S(20)
        comp_t = list_b + S(49)
        last = max(1, n_her + n_me)
        pitch = max(S(30), ((list_b - list_t) - S(16)) // last)   # 行距按可用高度算，保证不越界
        text_h = min(S(24), pitch - S(4))
        out = [n(0, -1, 0, "PaneControl", (l, t, r, b)),
               n(1, 0, 1, "GroupControl", (l, t, r, b)),            # app 根：贴着面板左边缘
               n(2, 0, 1, "GroupControl", rail),
               n(3, 0, 1, "GroupControl", content),                  # 包裹容器：很宽但 left 更小
               n(4, 0, 1, "GroupControl", sess),
               n(5, 0, 1, "GroupControl", chat),                     # 聊天区：left 最大
               n(6, 5, 2, "TextControl", (chat[0] + S(20), t + S(12), chat[0] + S(60), t + S(38)),
                 text="QQ"),                                         # 标题栏（应被排除）
               n(7, 5, 2, "TextControl", (chat[0] + S(23), chat[1] + S(8), chat[0] + S(163), chat[1] + S(38)),
                 text="会话名甲乙丙"),                                  # 头部（最靠近消息列表上方）
               n(8, 5, 2, "GroupControl", (chat[0], list_t, chat[2], list_b), aid="ml-root"),
               n(9, 5, 2, "GroupControl", (chat[0] + S(20), list_b + S(9), chat[0] + S(360), list_b + S(50)),
                 aid="func-bar-shortcuts-left"),                      # 半宽工具条
               n(10, 5, 2, "GroupControl", (chat[0], comp_t, chat[2], comp_t + composer_h)),
               n(11, 10, 3, "TextControl", (chat[0] + S(20), comp_t + S(40), chat[0] + S(300), comp_t + S(62)),
                 text="草稿"),                                        # 输入区里的草稿
               n(12, 5, 2, "GroupControl", (chat[0], comp_t + composer_h, chat[2], chat[3]))]
        for k in range(n_her):
            y = list_t + S(16) + k * pitch
            out.append(n(20 + k, 8, 3, "TextControl",
                         (chat[0] + S(88), y, chat[0] + S(400), y + text_h), text=f"对方第{k}句"))
        for k in range(n_me):
            y = list_t + S(16) + (n_her + k) * pitch
            out.append(n(30 + k, 8, 3, "TextControl",
                         (chat[2] - S(320), y, chat[2] - S(20), y + text_h), text=f"我第{k}句"))
        return out, chat, list_t, list_b, comp_t, composer_h

    cases = [
        ("125% 缩放（本机实测）", {"win": (450, 210, 2110, 1170), "scale": 1.25}),
        ("100% 缩放", {"win": (0, 0, 1440, 900), "scale": 1.0}),
        ("最大化 2560x1400", {"win": (0, 0, 2560, 1400), "scale": 1.0}),
        ("最大化 @150%", {"win": (0, 0, 2560, 1400), "scale": 1.5}),
        ("窄窗口 1000x700", {"win": (100, 100, 1100, 800), "scale": 1.0}),
        ("会话列表被拖宽", {"win": (0, 0, 1920, 1080), "scale": 1.0, "session_frac": 0.42}),
    ]
    for label, kw in cases:
        nodes, chat, list_t, list_b, comp_t, composer_h = layout(**kw)
        pl, prx = pane_bounds(nodes, (kw["win"][0], kw["win"][1], kw["win"][2], kw["win"][3]))
        assert pl == chat[0], f"{label}: pane_left={pl} 期望 {chat[0]}"
        assert prx == chat[2], f"{label}: pane_right={prx} 期望 {chat[2]}"
        lst = find_message_list(nodes, pl)
        assert lst is not None and lst.rect == (chat[0], list_t, chat[2], list_b), f"{label}: {lst}"
        ed = find_editor(nodes, pl, list_node=lst, panel_right=prx)
        assert ed is not None, f"{label}: 输入区没找到"
        assert ed.rect[0] >= chat[0] - 20 and ed.rect[3] <= comp_t + composer_h, \
            f"{label}: 输入区跑到工具栏里了 {ed.rect}"
        click_y = (ed.rect[1] + ed.rect[3]) // 2
        assert comp_t <= click_y <= comp_t + composer_h, f"{label}: 点击点 y={click_y} 不在输入区"
        assert find_session(nodes, pl, list_node=lst, panel_right=prx) == "会话名甲乙丙", label
        turns = build_turns(nodes, pane_left=pl, list_node=lst)
        assert [t.who for t in turns] == ["her"] * kw.get("n_her", 5) + ["me"] * kw.get("n_me", 3), \
            f"{label}: {[t.who for t in turns]}"

    # 空树 / 后台状态
    assert build_turns([], pane_left=0) == []
    assert find_editor([], 0) is None
    assert find_message_list([], 0) is None
    print("uia_map ok（含 6 种尺寸/缩放/最大化用例）")
