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


COUNT_ONLY = re.compile(r"^[（(]\d+[)）]$")   # 群成员数，不是会话名（实测过：(423) 这种）


def _ancestor_of_kind(nodes: Sequence[Node], node: Node, kinds: tuple[str, ...]) -> Node | None:
    """往上找最近的某一类祖先。"""
    by_index = {n.index: n for n in nodes}
    i = node.parent
    while i >= 0:
        a = by_index.get(i)
        if a is None:
            return None
        if a.type in kinds:
            return a
        i = a.parent
    return None


REGION_LABELS = ("消息列表", "会话列表", "群成员列表", "联系人", "窗口控制区域", "搜索",
                 "消息", "聊天")

# 右栏成员面板里的角色标签，不是人名
ROLE_WORDS = ("群主", "管理员", "群聊成员")


def _is_region_label(text: str) -> bool:
    """是不是 QQ 自己的区域标签（不是会话名）。

    实测：主面板里 ml-root 外面那层 WindowControl 的 Name 就是「消息列表」，
    同层还有「会话列表」（左栏）、「群成员列表」（右栏）、「窗口控制区域」。
    这些都不是会话名；拿它们当 key 就会把不同会话撞成一个（切会话就不会重置了）。"""
    return text.strip() in REGION_LABELS


def group_fingerprint(nodes: Sequence[Node], list_node: Node | None,
                      panel_right: int = 0) -> str:
    """群名读不到时，给这个群一个**稳定的**键：成员数 + 前几个成员名。

    为什么需要（全部实测）：某个 521 人群在主面板视图里根本不给群名——
      · ml-root 外面那层 WindowControl 的 Name 是区域标签「消息列表」；
      · 头部行只有成员数 `(521)`（会被尾数正则滤掉）；
      · 会话列表不实现 UIA 选中模式（容器无 SelectionPattern，21 个子节点全无
        SelectionItemPattern），不能反查「当前选中的是哪个会话」；
      · 树里只有按**消息**变的雪花 id，没有按会话变的 id。
    但同一个群里「成员数 + 前几个成员名」是稳定的，又足以把不同的群分开。
    连群特征都读不到（没右栏也没成员数）就返回空串——调用方宁可不换 key，
    也不要拿一个会变的字符串当会话名。
    """
    if list_node is None or not list_node.has_geo:
        return ""
    count, member_top = "", None
    for n in nodes:
        if n.type != "TextControl" or not n.has_text:
            continue
        m = re.search(r"群聊成员\s*(\d+)", n.text)
        if m:
            count, member_top = m.group(1), n.rect[1]
            break
        if (not count and n.rect[1] < list_node.rect[1]
                and COUNT_ONLY.match(n.text.strip())):
            count = re.sub(r"\D", "", n.text)          # 头部那个 (521)
    names: list[str] = []
    left = list_node.rect[2]
    for n in nodes:
        if (n.type != "TextControl" or not n.has_text or not n.has_geo
                or n.rect[0] < left - PANE_TOL or "群聊成员" in n.text
                or n.text.strip() in ROLE_WORDS or COUNT_ONLY.match(n.text.strip())):
            continue
        # 右栏最上面是「群公告 + 公告正文」，也是从 x≈2015 开始的短/长文本——
        # 实测把「群公告」「兑换码：93TJY3…」当成了成员名。成员名单在「群聊成员 N」那一行
        # **下面**，按这个分界；再加上“短、不带冒号”两条，公告行就进不来了。
        if member_top is not None and n.rect[1] < member_top:
            continue
        text = n.text.strip()
        if len(text) > 16 or "：" in text or ":" in text:
            continue
        if n.rect[0] - left < 0.4 * max(1, (panel_right or n.rect[2]) - left) \
                and text not in names:
            names.append(text)
    if not count and not names:
        return ""
    head = f"群 {count} 人" if count else "群聊"
    return head + (" · " + "、".join(names[:2]) if names else "")


def find_session_row(nodes: Sequence[Node], pane_left: int, sel: Selectors = SEL_DEFAULT,
                     list_node: Node | None = None, panel_right: int = 0) -> list[Node]:
    """会话名所在的那一行。三个来源，从可靠到不可靠：

      1. 类名命中（`chat-header__contact-name`，别的 QQ 构建/以后）；
      2. **消息列表外面那层 `WindowControl` 的 Name** —— 本机实测它就是会话标题
         （实测：群名 4 字→那层 Name 也是 4 字），不要几何、不受头部重绘影响；
      3. 头部行文本（几何兜底）——实测头部会时隐时现，而且**成员数 `(423)` 会混进来**，
         所以这里把“纯成员数”和单字符的 UI 碎片滤掉。
    """
    hit = find_first(nodes, sel.header)
    if hit is not None and hit.has_text:
        return [hit]
    if list_node is None or not list_node.has_geo:
        return []
    wrap = _ancestor_of_kind(nodes, list_node, ("WindowControl",))
    if (wrap is not None and wrap.has_text and not COUNT_ONLY.match(wrap.text.strip())
            and not _is_region_label(wrap.text)):
        return [wrap]
    above = [n for n in nodes if n.type == "TextControl" and n.has_text
             and n.has_geo and n.rect[1] < list_node.rect[1]
             and n.rect[0] >= pane_left - PANE_TOL and n.rect[2] <= (panel_right or 10 ** 9)]
    # 成员数、单字符 UI 碎片不是名字（除非实在没别的）
    useful = [n for n in above if not COUNT_ONLY.match(n.text.strip()) and len(n.text.strip()) > 1]
    above = useful or above
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
    text = "".join(n.text.strip() for n in row).strip() if row else ""
    text = re.sub(r"[（(]\d+[)）]\s*$", "", text).strip()
    return text or group_fingerprint(nodes, list_node, panel_right)


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


def message_containers(nodes: Sequence[Node], list_node: Node | None) -> list[Node]:
    """消息列表里的「消息容器」，按 DOM 顺序（= 时间顺序）。

    本机实测（群聊）：每个消息容器的 id 是 15+ 位数字，其子树结构非常规整：

        d13 时间戳（可选） · d14 昵称 · d15 徽标（群等级） · d15 徽标（群头衔） · d16 正文

    名字行与正文行是分开的两行（实测 y 差 35px）。这里只负责把容器挑出来。
    """
    if list_node is None:
        return []
    out = [n for n in descendants(nodes, list_node) if re.fullmatch(r"\d{15,}", n.aid or "")]
    out.sort(key=lambda n: n.index)
    return out


def _glyph_gap(texts: Sequence[Node]) -> int:
    hs = sorted(n.height for n in texts if n.height > 0)
    return max(4, int(0.6 * hs[len(hs) // 2])) if hs else 8


# 非文字消息的占位文案。顺序有意义（先匹配到的先用）；关键词来自 QQ 给附件节点起的名字。
NONTEXT_LABELS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("语音", "语音消息"), "[语音]"),
    (("视频",), "[视频]"),
    (("聊天记录", "合并转发"), "[聊天记录]"),
    (("文件", "文档"), "[文件]"),
    (("红包",), "[红包]"),
    (("转账",), "[转账]"),
    (("位置", "位置分享"), "[位置]"),
    (("音乐", "歌曲"), "[音乐]"),
    (("名片",), "[名片]"),
    (("链接", "分享"), "[链接]"),
)
STICKER_MAX_FRAC = 0.25      # 图片宽度不到列表宽度的这个比例且接近方形 → 当表情包


def placeholder_for(sub: Sequence[Node], media: Sequence[Node], list_width: int) -> str | None:
    """非文字消息的占位文案；认不出是哪种就 `[非文字消息]`；什么都没有（只有名字行）返回 None。"""
    for n in sub:
        t = (n.text or "").strip()
        if not t or len(t) > 16:
            continue
        for keys, label in NONTEXT_LABELS:
            if any(k in t for k in keys):
                return label
    if media:
        widest = max(n.width for n in media)
        tallest = max(n.height for n in media)
        if (widest <= STICKER_MAX_FRAC * max(1, list_width)
                and abs(widest - tallest) <= 0.35 * max(widest, tallest, 1)):
            return "[表情包]"
        return "[图片]"
    return None


def _anchor_center(primary: Sequence[Node], fallback: Sequence[Node]) -> float | None:
    """一组节点的水平中心（优先用有几何的；都没有再试 fallback）。"""
    geo = [n for n in primary if n.has_geo] or [n for n in fallback if n.has_geo]
    if not geo:
        return None
    return (min(n.rect[0] for n in geo) + max(n.rect[2] for n in geo)) / 2


def turns_from_containers(nodes: Sequence[Node], list_node: Node,
                          sel: Selectors = SEL_DEFAULT,
                          anchors: list | None = None) -> list[Turn]:
    """按消息容器读消息——有群等级/群头衔这类高特征文本时最可靠的一条路。

    每个容器里的文本按 y 聚行：
      · 名字行：同一行上深度最小的那串是昵称，剩下的短串是徽标（群等级/群头衔）；
      · 其余行是正文。

    **己方/对方只用几何判（靠右 = 我）**，不靠“有没有名字行”——实测自己发消息时
    QQ 同样会把昵称+徽标打在上面（自己那条的 d14 是 28 字的昵称），用名字行判会把
    自己的消息当成对方的。

    名字行的另一个作用：QQ 只在**发言人变化时**才显示昵称，所以没名字行的容器就继承
    上一个容器的发言人（几何对得上才继承，对不上就按几何算）。
    居中的（±8% 宽）当系统提示丢掉。

    **非文字消息给占位**：图片/表情包/语音/视频/文件/红包/转账/位置…… 都变成一条
    `[图片]` 这类占位消息，而不是直接消失——群里上下文不会因此断掉。
    anchors 非 None 时，会按 turn 顺序把「这条消息对应的媒体节点」（图片控件；没有就是 None）
    填进去，给上层去截图交给多模态模型（Clef-Omni 预留）。

    ponytail: 只读“全部子文本都有几何”的容器（已渲染的）；虚拟列表滑出去的项几何全为 0，
    直接跳过，不猜。
    """
    if list_node is None or not list_node.has_geo:
        return []
    width = max(1, list_node.rect[2] - list_node.rect[0])
    mid = list_node.rect[0] + width / 2
    turns: list[Turn] = []
    prev_who: str | None = None
    prev_name: str | None = None
    for c in message_containers(nodes, list_node):
        sub = descendants(nodes, c)
        texts = [t for t in sub if t.type == "TextControl" and t.has_text and t.has_geo]
        media = [t for t in sub if t.type == "ImageControl" and t.has_geo]
        if not texts and not media:
            continue                     # 没布局的容器整条跳过，不猜
        rows = _rows_by_y(texts, _glyph_gap(texts)) if texts else []
        name, body_rows = None, rows
        # 只有一行、而这行上除了头那串还有更短、更深的段（徽标）→ 这是「名字行 + 徽标」
        # 而没有单独的正文行：图片/表情包/合并转发就长这样，不能当正文读。
        if len(rows) == 1:
            row = rows[0]
            shallow = min(n.depth for n in row)
            deeper = [n for n in row if n.depth > shallow]
            if deeper and all(len(n.text.strip()) <= 4 for n in deeper):
                name = "".join(n.text.strip() for n in sorted(
                    (n for n in row if n.depth == shallow), key=lambda n: n.rect[0])).strip() or None
                body_rows = []
        for k, row in enumerate(rows):
            if k == len(rows) - 1:
                break                    # 最后一行留给正文
            shallow = min(n.depth for n in row)
            head = "".join(n.text.strip() for n in sorted(
                (n for n in row if n.depth == shallow), key=lambda n: n.rect[0])).strip()
            if not head or re.fullmatch(r"\d{1,2}[:：]\d{2}.*", head):
                continue                 # 时间戳那一行不是名字行
            # 名字行的特征：同行还有更深的短段（徽标），或者下面那行比它深（正文在更深一层）
            deeper_in_row = any(n.depth > shallow for n in row)
            deeper_below = all(n.depth > shallow for n in rows[k + 1])
            if deeper_in_row or deeper_below:
                name, body_rows = head, rows[k + 1:]
                break
        body = "".join(n.text.strip() for r in body_rows
                       for n in sorted(r, key=lambda n: n.rect[0])).strip()
        media_source = max(media, key=lambda n: n.width * n.height) if media else None
        if body:
            anchor = [n for r in body_rows for n in r]
        else:
            label = placeholder_for(sub, media, list_node.width)
            if label is None:
                continue                 # 只有名字行（没正文也没附件）→ 跳过
            body, anchor = label, list(media)
        cx = _anchor_center(anchor, body_rows[0] if body_rows else texts)
        if cx is None:
            continue
        if abs(cx - mid) <= 0.08 * width:
            continue                     # 居中 = 系统提示（入群/撤回/公告）
        who = "me" if cx > mid else "her"
        if name is None and prev_who is not None and (cx > mid) == (prev_who == "me"):
            who, name = prev_who, prev_name      # 发言人没变，QQ 不重复显示昵称
        elif who == "me":
            name = None                          # 自己那条上的昵称/头衔不当“对方名字”用
        turns.append(Turn(who=who, text=body, name=name))
        if anchors is not None:
            anchors.append(media_source)
        prev_who, prev_name = who, name
    return turns


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
                pane_left: int = 0, list_node: Node | None = None,
                anchors: list | None = None) -> list[Turn]:
    """气泡节点 → Turn 列表（按 DOM 顺序 = 时间顺序）。三条路径，从准到糙：

      1. 类名路径（气泡/己方标记存在）——最准，其他 QQ 构建/以后可能又能用；
      2. 消息容器路径（本机实测可用：名字/徽标/正文分层清楚，非文字消息给占位）；
      3. 按 y 聚行的几何路径（最后手，容易把界面元素当消息）。

    anchors 非 None 时，按 turn 顺序填「这条消息对应的媒体节点」（没媒体就是 None）——
    上层用它去截图，以后交给多模态模型（Clef-Omni）。
    """
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
            if anchors is not None:
                anchors.append(None)
        return turns
    if list_node is not None:
        turns = turns_from_containers(nodes, list_node, sel, anchors=anchors)
        if turns:
            return turns
        rows_turns = build_turns_from_rows(nodes, list_node, sel)
        if anchors is not None:
            anchors.extend([None] * len(rows_turns))
        return rows_turns
    return []


def new_turns(seen: list[tuple[str, str, str | None]], turns: Sequence[Turn],
              similar, indices: list | None = None) -> list[Turn]:
    """增量：只留下没见过的（滚动/重绘不重复上报）。similar 传 app.textsim.similar。
    indices 非 None 时，按顺序填回「这条在 turns 里的下标」——上层用它去拿对应的媒体锚点。"""
    fresh = []
    for k, t in enumerate(turns):
        hit = any(w == t.who and similar(old, t.text) for w, old, _ in seen)
        if not hit:
            fresh.append(t)
            seen.append((t.who, t.text, t.name))
            if indices is not None:
                indices.append(k)
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

    # ── 群聊：消息容器分层（照真实 dump：d13 时间戳 / d14 昵称 / d15 徽标x2 / d16 正文）────
    LIST = (742, 281, 2000, 976)
    gn = [n(0, -1, 0, "PaneControl", PANEL),
          n(1, 0, 1, "GroupControl", (520, 260, 833, 1165)),
          n(2, 0, 1, "GroupControl", (742, 217, 2225, 1232)),
          n(3, 2, 2, "GroupControl", LIST, aid="ml-root")]

    def add_container(cid, specs, geo=True, media=()):
        """specs: (depth, x0, y, x1, text)；media: (x0, y0, x1, y1, name) 的图片控件。"""
        ci = len(gn)
        gn.append(n(ci, 3, 3, "GroupControl", (800, 255, 1980, 1290) if geo else (0, 0, 0, 0), aid=cid))
        for d, x0, y, x1, t in specs:
            rect = (x0, y, x1, y + 22) if geo else (0, 0, 0, 0)
            gn.append(n(len(gn), ci, d, "TextControl", rect, text=t))
        for x0, y0, x1, y1, nm in media:
            rect = (x0, y0, x1, y1) if geo else (0, 0, 0, 0)
            gn.append(n(len(gn), ci, 4, "ImageControl", rect, text=nm))

    # 对方：时间戳 + 昵称(d14) + 两个徽标(d15) + 正文(d16，下一行)
    add_container("7000000000000000001", [(13, 817, 240, 880, "12:34"),
                                         (14, 817, 264, 889, "昵称六个字"),
                                         (15, 898, 264, 913, "LV"),
                                         (15, 913, 264, 935, "99"),
                                         (16, 830, 299, 988, "四个字符")])
    # 同一个人接着说：QQ 不重复显示昵称 → 继承上一条的发言人
    add_container("7000000000000000002", [(16, 830, 340, 988, "接着说的一句")])
    # 自己发的：**真机实测自己那条也带昵称+徽标**（d14 是 28 字的昵称），正文靠右
    # → who 靠几何判 = me，而且不能把自己的昵称当成“对方名字”
    add_container("7000000000000000003", [(14, 1611, 400, 1925, "我自己的昵称头衔很长"),
                                         (15, 1573, 401, 1588, "LV"),
                                         (15, 1587, 401, 1602, "99"),
                                         (16, 1773, 435, 1913, "我自己发的")])
    # 自己连着发（无名字行）→ 仍然是 me
    add_container("7000000000000000004", [(16, 1773, 475, 1913, "又一条")])
    # 系统提示：居中 → 丢掉
    add_container("7000000000000000005", [(16, 1200, 520, 1550, "系统提示一条")])
    # 只有名字行没正文（图片/表情包那种）→ 丢掉
    add_container("7000000000000000006", [(14, 817, 560, 848, "两字"),
                                         (15, 857, 560, 872, "LV"),
                                         (15, 871, 560, 887, "9")])
    # 虚拟列表里没布局的（全无几何）→ 整条跳过，不猜
    add_container("7000000000000000007", [(14, 817, 600, 848, "两字"),
                                         (15, 857, 600, 872, "LV"),
                                         (16, 830, 635, 988, "看不见")], geo=False)

    # 非文字消息（图片/表情包/语音）→ 给占位，而不是直接消失
    # 图片：名字行 + 大图（靠左 = 对方）
    add_container("7000000000000000008", [(14, 817, 700, 848, "对方昵称"),
                                         (15, 857, 700, 872, "LV")],
                  media=[(830, 720, 1235, 1000, "")])
    # 表情包：名字行 + 小而方的图（靠右 = 我）
    add_container("7000000000000000009", [(14, 1573, 1010, 1925, "我的昵称"),
                                         (15, 1611, 1011, 1626, "LV")],
                  media=[(1737, 1030, 1925, 1218, "")])
    # 语音：附件节点自己带“语音”这个名字 → 关键词优先
    add_container("7000000000000000010", [(14, 817, 1250, 848, "对方昵称"),
                                         (15, 857, 1250, 872, "LV")],
                  media=[(830, 1270, 880, 1320, "语音")])

    pane_left, _ = pane_bounds(gn, PANEL)
    lst = find_message_list(gn, pane_left)
    assert lst is not None and lst.aid == "ml-root"
    turns = build_turns(gn, pane_left=pane_left, list_node=lst)
    assert [(t.who, t.name, t.text) for t in turns] == [
        ("her", "昵称六个字", "四个字符"),
        ("her", "昵称六个字", "接着说的一句"),      # 继承发言人
        ("me", None, "我自己发的"),                  # 带昵称行也归我（靠右）
        ("me", None, "又一条"),                      # 连着发 → 仍然是我
        ("her", "对方昵称", "[图片]"),                # 非文字 → 占位（方向按图算）
        ("me", None, "[表情包]"),                     # 小而方的图 → 表情包
        ("her", "对方昵称", "[语音]"),                # 附件自带“语音”名字
    ], turns
    # 媒体锚点：只有图片/表情包/语音这三条带 media，其余是 None（给多模态预留）
    anchors: list = []
    build_turns(gn, pane_left=pane_left, list_node=lst, anchors=anchors)
    assert [a is not None for a in anchors] == [False, False, False, False, True, True, True], anchors

    # ── 会话名：优先用消息列表外层 WindowControl 的 Name（实测它就是会话标题）────
    name_gn = [n(0, -1, 0, "PaneControl", PANEL),
               n(1, 0, 1, "GroupControl", (520, 260, 833, 1165)),
               n(2, 0, 1, "GroupControl", (742, 217, 2225, 1232)),
               n(3, 2, 2, "WindowControl", (742, 281, 2000, 976), text="高速网盘"),
               n(4, 3, 3, "GroupControl", LIST, aid="ml-root"),
               n(5, 2, 2, "TextControl", (1031, 237, 1084, 263), text="(423)")]   # 成员数
    pnl, _ = pane_bounds(name_gn, PANEL)
    l2 = find_message_list(name_gn, pnl)
    assert find_session(name_gn, pnl, list_node=l2, panel_right=PANEL[2]) == "高速网盘", \
        find_session(name_gn, pnl, list_node=l2, panel_right=PANEL[2])

    # 没有 WindowControl 时：头部行里只有成员数 → 不能被当成名字（之前真 bug：正则把它删成空串）
    no_wrap = [n(0, -1, 0, "PaneControl", PANEL),
               n(1, 0, 1, "GroupControl", (520, 260, 833, 1165)),
               n(2, 0, 1, "GroupControl", (742, 217, 2225, 1232)),
               n(3, 2, 2, "GroupControl", LIST, aid="ml-root"),
               n(4, 2, 2, "TextControl", (1031, 237, 1084, 263), text="(423)"),
               n(5, 2, 2, "TextControl", (900, 205, 1060, 231), text="另一个群名")]
    pnl2, _ = pane_bounds(no_wrap, PANEL)
    l3 = find_message_list(no_wrap, pnl2)
    got = find_session(no_wrap, pnl2, list_node=l3, panel_right=PANEL[2])
    assert got == "另一个群名", got

    # ── 区域标签不是会话名（实测：ml-root 外面那层 WindowControl 的 Name 就是「消息列表」）──
    # 这种树以前会拿「消息列表」当 key：不同会话撞成一个，切会话就不重置了。
    region = [n(0, -1, 0, "PaneControl", PANEL),
              n(1, 0, 1, "GroupControl", (520, 260, 833, 1165)),
              n(2, 0, 1, "WindowControl", (430, 281, 743, 1234), text="会话列表"),
              n(3, 0, 1, "GroupControl", (742, 217, 2225, 1232)),
              n(4, 3, 2, "WindowControl", (742, 281, 2000, 976), text="消息列表"),
              n(5, 4, 3, "GroupControl", LIST, aid="ml-root"),
              n(6, 3, 2, "WindowControl", (2025, 166, 2230, 216), text="窗口控制区域"),
              n(7, 3, 2, "WindowControl", (2002, 569, 2223, 1239), text="群成员列表"),
              n(8, 3, 2, "TextControl", (971, 237, 1024, 263), text="(521)")]
    pnl3, _ = pane_bounds(region, PANEL)
    l4 = find_message_list(region, pnl3)
    got = find_session(region, pnl3, list_node=l4, panel_right=2223)
    assert got == "群 521 人", got          # 头部只剩成员数：退回群指纹（有右栏时还会带成员名）
    assert got != "消息列表" and got != "会话列表"
    assert _is_region_label("消息列表") and not _is_region_label("高速网盘")
    assert find_session_row(region, pnl3, list_node=l4, panel_right=2223)[0].text.strip() == "(521)", \
        "区域标签应该被跳过，落到头部行"

    # 群指纹：成员数 + 前两个成员名（同一群里稳定，又能把不同的群分开）
    fp = region + [n(9, 7, 3, "TextControl", (2015, 529, 2090, 555), text="群聊成员 521"),
                   n(10, 7, 3, "TextControl", (2050, 579, 2140, 605), text="传输数据小助手"),
                   n(11, 7, 3, "TextControl", (2180, 581, 2223, 607), text="群主"),
                   n(12, 7, 3, "TextControl", (2050, 619, 2110, 645), text="菜菜子"),
                   n(13, 7, 3, "TextControl", (2167, 621, 2223, 647), text="管理员")]
    pnl4, _ = pane_bounds(fp, PANEL)
    l5 = find_message_list(fp, pnl4)
    assert group_fingerprint(fp, l5, 2223) == "群 521 人 · 传输数据小助手、菜菜子", \
        group_fingerprint(fp, l5, 2223)
    # 右栏顶部是「群公告 + 公告正文」：实测被当成了成员名（真 bug），不能进指纹
    ann = fp + [n(14, 7, 3, "TextControl", (2015, 304, 2065, 330), text="群公告"),
                n(15, 7, 3, "TextControl", (2015, 348, 2223, 400),
                  text="兑换码：93TJY3M6，先到先得")]
    pnl6, _ = pane_bounds(ann, PANEL)
    l7 = find_message_list(ann, pnl6)
    assert group_fingerprint(ann, l7, 2223) == "群 521 人 · 传输数据小助手、菜菜子", \
        group_fingerprint(ann, l7, 2223)
    assert find_session(fp, pnl4, list_node=l5, panel_right=2223) == "群 521 人 · 传输数据小助手、菜菜子"
    # 群里换了个成员数 → key 会变（少见）；但至少不会是空的、也不会跟别人撞
    assert group_fingerprint(fp, None, 2223) == ""          # 没有消息列表就什么都读不出
    assert group_fingerprint(no_wrap, l3, PANEL[2]) == "群 423 人", "只有成员数时也应给出指纹"
    # 单聊那种连成员数都没有的：给空串，调用方宁可不换 key（见 uia_qqnt 的「读不到会话名就不读消息」）
    bare_tree = [n(0, -1, 0, "PaneControl", PANEL),
                 n(1, 0, 1, "GroupControl", LIST, aid="ml-root"),
                 n(2, 0, 1, "WindowControl", (742, 281, 2000, 976), text="消息列表")]
    pnl5, _ = pane_bounds(bare_tree, PANEL)
    l6 = find_message_list(bare_tree, pnl5)
    assert group_fingerprint(bare_tree, l6, PANEL[2]) == "", "读不出群特征就别硬造"
    assert find_session(bare_tree, pnl5, list_node=l6, panel_right=PANEL[2]) == ""

    # 有名字的 WindowControl 依旧优先（这条不能被指纹抢走）
    assert find_session(name_gn, pnl, list_node=l2, panel_right=PANEL[2]) == "高速网盘"

    # 空树 / 后台状态
    assert build_turns([], pane_left=0) == []
    assert find_editor([], 0) is None
    assert find_message_list([], 0) is None
    print("uia_map ok（含 6 种尺寸/缩放/最大化用例）")
