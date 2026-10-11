# -*- coding: utf-8 -*-
"""QQ NT 采集器：读 UIA 无障碍树拿消息，不走 OCR、不比像素。

为什么需要「置前」：QQ 只对**前台**窗口构建完整 UIA 树（实测后台只有 ~9 个节点）。所以读树前
按设置里的策略处理：

    steal       （默认）把 QQ 置前 + 等树重建 → 读完立刻把焦点还给原来的窗口。屏幕会闪一下。
    foreground  完全不抢焦点；QQ 不在前台就跳过这一拍，并提示一句。

选择器（输入框 `qq-msg-editor`、头部联系人 `chat-header__contact-name`、气泡
`message-container*`、己方 `container--self`）来自社区实测，可在 config.json 覆盖。
认不出东西时只报一句可诊断的 status，绝不静默读错。

纯逻辑（认输入框/气泡/己方/顺序/去重）在 app/collectors/uia_map.py，那边不依赖 uiautomation，
可以用合成树离线自测；这里只管抓树和发事件。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import time

from app.collectors.base import BaseCollector, Capabilities
from app.collectors.uia_map import (Node, build_turns, detect_mode, find_editor,
                                    find_message_list, find_session, new_turns, pane_bounds,
                                    sel_from)
from app.textsim import similar

u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
u32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
u32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
u32.SetForegroundWindow.argtypes = [w.HWND]

TREE_BUDGET = 80000      # 节点上限，防止异常布局把子进程拖死
TREE_DEPTH = 60
TINY_TREE = 40           # 少于这么多节点 = 多半是「后台窗口只有 9 个节点」那种状态
REVIVE_WAIT = 0.8        # 等 Chromium 把无障碍树建出来（社区实测 800ms）
READ_INTERVAL = 1.2      # 两次读树的最小间隔（秒）：读一次要几百 ms，不能每 50ms 跑一遍
USER_ACTIVE_GRACE = 1.5  # 人在打字/动鼠标的这段时间内不去抢焦点（秒）


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def _click_box(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """把输入区矩形收一下再交给父进程点：容器可能把下面的工具栏一起包住（实测高度 259），
    所以永远取**上 60%**，保证中心点落在打字区而不是工具栏按钮上（与缩放/最大化无关）。"""
    l, t, r, b = rect
    return (l, t, r, t + max(20, int((b - t) * 0.6)))


class UiaQqntCollector(BaseCollector):
    key = "qqnt"
    capabilities = Capabilities(provides_text=True, provides_media=True,
                                can_insert=True, needs_foreground=True)

    def __init__(self, hwnd, emit, debug_on=None, app=None, *,
                 selectors=None, focus_policy="steal",
                 read_interval=READ_INTERVAL, user_grace=USER_ACTIVE_GRACE):
        super().__init__(hwnd, emit, debug_on)
        self.sel = selectors or sel_from(None)
        self.focus_policy = focus_policy or "steal"
        self.read_interval = float(read_interval)
        self.user_grace = float(user_grace)
        self.seen: list[tuple[str, str, str | None]] = []
        self.title = ""
        self.last_area = None
        self.last_input = None
        self.last_mode = ""
        self._auto = None
        self._noted: set[str] = set()
        self._prev_foreground = None
        self._last_read = 0.0

    @staticmethod
    def _user_idle_seconds() -> float:
        """距离上一次键鼠输入多久了（秒）。拿不到就当成用户在忙（保守 → 不抢焦点）。"""
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        try:
            if not u32.GetLastInputInfo(ctypes.byref(info)):
                return 0.0
            now = k32.GetTickCount()
            return max(0.0, (now - info.dwTime) / 1000.0)
        except Exception:
            return 0.0

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        try:
            import uiautomation as auto
        except Exception as exc:  # 依赖缺失要说人话，别让用户看 ImportError
            raise RuntimeError(
                "QQ 采集需要 uiautomation：pip install uiautomation"
                f"（{type(exc).__name__}）") from None
        self._auto = auto
        auto.SetGlobalSearchTimeout(3)
        self.emit("resumed")

    def stop(self) -> None:
        self.emit("paused")

    def alive(self) -> bool:
        return bool(u32.IsWindow(self.hwnd))

    # ---------------------------------------------------------------- 读树

    def _note_once(self, key: str, text: str) -> None:
        if key not in self._noted:
            self._noted.add(key)
            self.emit("status", text)

    def _window_origin(self) -> tuple[int, int]:
        r = w.RECT()
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(self.hwnd, 9, ctypes.byref(r),
                                                      ctypes.sizeof(r)) != 0:
            u32.GetWindowRect(self.hwnd, ctypes.byref(r))
        return r.left, r.top

    def _is_foreground(self) -> bool:
        return u32.GetForegroundWindow() == self.hwnd

    def _bring_forward(self) -> bool:
        """恢复 + 置前，返回是否动了手。抄 qq-operations 的 Revive-QQPanel。"""
        moved = False
        if u32.IsIconic(self.hwnd):
            u32.ShowWindow(self.hwnd, 9)   # SW_RESTORE
            moved = True
        if not self._is_foreground():
            self._prev_foreground = u32.GetForegroundWindow()
            fg_tid = u32.GetWindowThreadProcessId(self._prev_foreground, None)
            our_tid = k32.GetCurrentThreadId()
            u32.AttachThreadInput(our_tid, fg_tid, True)
            u32.SetForegroundWindow(self.hwnd)
            u32.AttachThreadInput(our_tid, fg_tid, False)
            moved = True
        if moved:
            time.sleep(REVIVE_WAIT)        # 等 Chromium 建树
        return moved

    def _restore_foreground(self) -> None:
        prev = self._prev_foreground
        self._prev_foreground = None
        if not prev or prev == self.hwnd or not u32.IsWindow(prev):
            return
        fg = u32.GetForegroundWindow()
        if fg != self.hwnd:      # 人已经把焦点切走了，别抢回来
            return
        try:
            fg_tid = u32.GetWindowThreadProcessId(fg, None)
            our_tid = k32.GetCurrentThreadId()
            u32.AttachThreadInput(our_tid, fg_tid, True)
            u32.SetForegroundWindow(prev)
            u32.AttachThreadInput(our_tid, fg_tid, False)
        except Exception:
            pass

    def _read_tree(self) -> list[Node]:
        auto = self._auto
        root = auto.ControlFromHandle(self.hwnd)
        nodes: list[Node] = []

        def walk(ctrl, depth: int, parent: int) -> None:
            if depth > TREE_DEPTH or len(nodes) >= TREE_BUDGET:
                return
            try:
                children = ctrl.GetChildren()
            except Exception:
                return
            for c in children:
                if len(nodes) >= TREE_BUDGET:
                    return
                idx = len(nodes)
                try:
                    r = c.BoundingRectangle
                    nodes.append(Node(index=idx, parent=parent, depth=depth,
                                      type=c.ControlTypeName or "",
                                      cls=c.ClassName or "",
                                      aid=getattr(c, "AutomationId", "") or "",
                                      rect=(r.left, r.top, r.right, r.bottom),
                                      text=c.Name or "", text_len=len(c.Name or "")))
                except Exception:
                    continue
                walk(c, depth + 1, idx)

        walk(root, 0, -1)
        return nodes

    # ---------------------------------------------------------------- 每拍

    def poll(self, emit) -> None:
        # 0. 限速：读一棵树要几百毫秒（+ 等建树 0.8s），不能跟着 worker 的 50ms 循环跑。
        #    注意：worker 是每拍调一次 poll，这里自己管节流。
        now = time.monotonic()
        if now - self._last_read < self.read_interval:
            return

        # 1. 前台策略
        if not self._is_foreground():
            if self.focus_policy == "foreground":
                self._last_read = now
                self._note_once("bg", "QQ 不在前台——想让它读消息，切到 QQ 窗口，"
                                      "或在设置里把「QQ 读取方式」改成允许短暂置前。")
                return
            # 抢焦点会打断打字：人刚动过键鼠就先不抢，下一拍再说。
            if self._user_idle_seconds() < self.user_grace:
                return
            self._bring_forward()
        self._last_read = now

        try:
            nodes = self._read_tree()
        finally:
            if self.focus_policy == "steal":
                self._restore_foreground()

        if not nodes:
            self._note_once(
                "tiny",
                "读不到任何控件——QQ 只在窗口处于活动状态时才构建完整无障碍树；"
                "若一直这样，请用 --force-renderer-accessibility 启动 QQ 再试。")
            return

        mode = detect_mode(nodes)
        if mode != self.last_mode:
            self.last_mode = mode

        panel = next((n for n in nodes if n.depth == 0 and n.has_geo), None)
        panel_rect = panel.rect if panel else (0, 0, 0, 0)
        pane_left, _ = pane_bounds(nodes, panel_rect)
        panel_right = panel_rect[2] or 0

        # 消息列表根（实测 id=ml-root）；类名世界中就是气泡的公共祖先
        list_node = find_message_list(nodes, pane_left)

        # 「只有十来个节点而且什么都认不出」= 就是后台窗口那棵空树（实测 9 个节点）
        has_bubbles = any(any(x in n.key for x in self.sel.bubble) for n in nodes)
        editor = find_editor(nodes, pane_left, self.sel, list_node=list_node,
                             panel_right=panel_right)
        if len(nodes) < TINY_TREE and not has_bubbles and editor is None and list_node is None:
            self._note_once(
                "tiny",
                f"只读到 {len(nodes)} 个控件、且认不出任何气泡或输入框——"
                "QQ 只在窗口处于活动状态时才构建完整无障碍树（实测后台只有 9 个节点）；"
                "若一直这样，请用 --force-renderer-accessibility 启动 QQ 再试。")
            return

        # 2. 会话名 / 聊天区
        name = find_session(nodes, pane_left, self.sel, list_node=list_node,
                            panel_right=panel_right)
        if name and name != self.title:
            self.title = name
            self.seen.clear()          # 换会话：去重状态跟着换（和 OCR 采集器一致）
            emit("chat", name)
        ox, oy = self._window_origin()
        # area：父进程只拿它当「能不能填」的护栏（QQ 靠 input 事件定位），
        # 所以直接用消息列表的矩形；不能拿全聊天区节点的并集——消息列表是虚拟列表，
        # 里面的 y 会是负数（实测到 -2522），并集会变成一坨垃圾值。
        pane = [n for n in nodes if n.has_geo and n.rect[0] >= pane_left - 8]
        box = list_node.rect if list_node is not None and list_node.has_geo else None
        if box is None and pane:
            box = (min(n.rect[0] for n in pane), min(n.rect[1] for n in pane),
                   max(n.rect[2] for n in pane), max(n.rect[3] for n in pane))
        if box is not None:
            area = (box[0] - ox, box[1] - oy, box[2] - ox, box[3] - oy)
            if area != self.last_area:
                self.last_area = area
                emit("area", area)

        # 3. 输入框（屏幕坐标，父进程直接点它）
        if editor is not None and editor.has_geo:
            rect = _click_box(tuple(editor.rect))
            if rect != self.last_input:
                self.last_input = rect
                emit("input", rect)
        else:
            self._note_once("no-editor",
                            "没找到消息输入框（qq-msg-editor）——选候选能看不能填；"
                            "可在 config.json 里用 qq_selectors.editor 覆盖类名。")

        # 4. 消息
        turns = build_turns(nodes, self.sel, pane_left=pane_left, list_node=list_node)
        if not turns:
            self._note_once("no-bubbles",
                            "没读到气泡（message-container / msg-content-container）——"
                            "可能这个 QQ 版本类名不同，可用 probe\\probe_qq_uia.py 抓一份树来对。")
            return
        fresh = new_turns(self.seen, turns, similar)
        if fresh and self.title:
            emit("lines", self.title, [(t.who, t.name, t.text) for t in fresh],
                 self.last_area or (0, 0, 0, 0))


if __name__ == "__main__":
    # 自测：**不开任何窗口、也不需要装 uiautomation**——把 uiautomation 换成假模块，
    # 重心逻辑（抓树 → 事件、去重、会话切换、认不出时的提示）都能离线跑。
    # 跑法：python -m app.collectors.uia_qqnt
    import sys
    import types
    from app.collectors.uia_map import Selectors

    class Ctrl:
        def __init__(self, typ, rect=(0, 0, 0, 0), cls="", aid="", name="", kids=()):
            self.ControlTypeName, self.ClassName, self.AutomationId, self.Name = typ, cls, aid, name
            self._r, self._kids = rect, list(kids)

        @property
        def BoundingRectangle(self):
            l, t, r, b = self._r
            return types.SimpleNamespace(left=l, top=t, right=r, bottom=b)

        def GetChildren(self):
            return list(self._kids)

    def tree(session, messages=(("in", "message-container", "在吗"),
                                ("out", "message-container container--self", "在的"))):
        bubble_group = []
        for who, cls, text in messages:
            bubble_group.append(Ctrl("GroupControl", (900, 400, 1300, 450), cls=cls, name=text))
        return Ctrl("PaneControl", (450, 210, 2110, 1170), kids=[
            Ctrl("GroupControl", (520, 260, 833, 1165)),
            Ctrl("GroupControl", (833, 260, 2105, 1165), kids=[
                Ctrl("TextControl", (900, 280, 1200, 310), cls="chat-header__contact-name",
                     name=session),
                *bubble_group,
                Ctrl("EditControl", (900, 1000, 2000, 1080), cls="qq-msg-editor"),
            ]),
        ])

    fake = types.ModuleType("uiautomation")
    fake.SetGlobalSearchTimeout = lambda *a, **k: None
    state = {"root": tree("甲方")}
    fake.ControlFromHandle = lambda hwnd: state["root"]
    sys.modules["uiautomation"] = fake

    events: list = []
    emit = lambda kind, *p: events.append((kind,) + tuple(p))
    c = UiaQqntCollector(1234, emit, selectors=Selectors(), focus_policy="steal",
                         read_interval=0)
    c._is_foreground = lambda: True          # 不碰真实焦点
    c._window_origin = lambda: (0, 0)
    c.start()
    c.poll(emit)
    kinds = [e[0] for e in events]
    assert kinds == ["resumed", "chat", "area", "input", "lines"], kinds
    assert events[1] == ("chat", "甲方"), events[1]
    assert events[3] == ("input", (900, 1000, 2000, 1048)), events[3]   # 上 60%（点中心落在打字区）
    _l, _t, _r, _b = events[3][1]
    assert _t >= 1000 and _b <= 1080, events[3]                          # 仍在原框之内
    assert events[4][2] == [("her", None, "在吗"), ("me", None, "在的")], events[4]

    # 同一棵树再跑一拍：不该重复上报
    n = len(events)
    c.poll(emit)
    assert [e[0] for e in events[n:]] == [], events[n:]

    # 新消息 → 只报新的
    state["root"] = tree("甲方", (("in", "message-container", "在吗"),
                                  ("out", "message-container container--self", "在的"),
                                  ("in", "message-container", "明天几点")))
    n = len(events)
    c.poll(emit)
    assert events[n][0] == "lines" and events[n][2] == [("her", None, "明天几点")], events[n:]

    # 换会话：发 chat、清去重状态，新会话的消息算新的
    state["root"] = tree("乙方", (("in", "message-container", "你好"),))
    n = len(events)
    c.poll(emit)
    assert [e[0] for e in events[n:]] == ["chat", "lines"], events[n:]
    assert events[n + 1][1] == "乙方"

    # 后台那种小树 → 提示要用 --force-renderer-accessibility
    state["root"] = Ctrl("PaneControl", (450, 210, 2110, 1170), kids=[Ctrl("GroupControl")])
    events.clear()
    c2 = UiaQqntCollector(1234, emit, selectors=Selectors(), focus_policy="steal",
                          read_interval=0)
    c2._is_foreground = lambda: True
    c2._window_origin = lambda: (0, 0)
    c2.start()
    c2.poll(emit)
    assert any(e[0] == "status" and "--force-renderer-accessibility" in e[1] for e in events), events

    # 有树但没有类名 → 提示类名对不上
    state["root"] = tree("甲方", ())
    events.clear()
    c3 = UiaQqntCollector(1234, emit, selectors=Selectors(), read_interval=0)
    c3._is_foreground = lambda: True
    c3._window_origin = lambda: (0, 0)
    c3.start()
    c3.poll(emit)
    assert any(e[0] == "status" and "气泡" in e[1] for e in events), events

    # focus_policy=foreground：QQ 不在前台就跳过，且不抢焦点
    events.clear()
    c4 = UiaQqntCollector(1234, emit, selectors=Selectors(), focus_policy="foreground",
                          read_interval=0)
    c4._is_foreground = lambda: False
    c4._bring_forward = lambda: (_ for _ in ()).throw(AssertionError("不该抢焦点"))
    c4.poll(emit)
    assert any(e[0] == "status" and "不在前台" in e[1] for e in events), events

    # 限速：read_interval 内第二次 poll 不重读（否则 worker 的 50ms 循环会把 CPU 和焦点吞光）
    state["root"] = tree("甲方", (("in", "message-container", "在吗"),))
    ev2: list = []
    em2 = lambda kind, *p: ev2.append((kind,) + tuple(p))
    c5 = UiaQqntCollector(1234, em2, selectors=Selectors(), read_interval=5.0)
    c5._is_foreground = lambda: True
    c5._window_origin = lambda: (0, 0)
    c5.start(); ev2.clear()
    c5.poll(em2)
    first = [e[0] for e in ev2]
    c5.poll(em2)                                   # 立刻再拍：应被节流拦住
    assert first == ["chat", "area", "input", "lines"], first
    assert [e[0] for e in ev2] == first, ev2

    # 抢焦点策略：人刚动过键鼠 → 不抢（不打断打字）；空闲下来 → 才抢
    stolen: list = []
    c6 = UiaQqntCollector(1234, lambda *a: None, selectors=Selectors(), focus_policy="steal",
                          read_interval=0, user_grace=1.5)
    c6._is_foreground = lambda: False
    c6._bring_forward = lambda: (stolen.append(1), True)[1]
    c6._read_tree = lambda: []
    c6._user_idle_seconds = lambda: 0.3            # 人正在打字
    c6.poll(lambda *a: None)
    assert not stolen, "人在打字时不该抢焦点"
    c6._user_idle_seconds = lambda: 3.0            # 人离开了一会儿
    c6.poll(lambda *a: None)
    assert stolen, "空闲时应该按 A 策略短暂置前"
    print("uia_qqnt ok")
