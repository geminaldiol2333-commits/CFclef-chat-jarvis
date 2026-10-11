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
import os
import time

from app.collectors.base import BaseCollector, Capabilities
from app.collectors.uia_map import (Node, build_turns, detect_mode, find_editor,
                                    find_message_list, find_session, new_turns, pane_bounds,
                                    sel_from)
# 注：不去重不用 app.textsim.similar（那是给 OCR 的模糊匹配），见下面 _exact()

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
WHEEL_TICKS = 3          # 回填历史时每次滚多少格
SCROLL_SETTLE = 0.35     # 滚动后等多久让列表渲染完（虚拟列表要现渲）


def _exact(a: str, b: str) -> bool:
    """UIA 的文本是精确的，去重必须用全等——不能用 OCR 那套模糊匹配。

    `app.textsim.similar` 是给 OCR 用的（同一段像素挪个位置会认成不同字，所以要允许 75% 相似）。
    拿它去重 QQ 的消息会把**不同消息吃掉**：实测两条仅差一两字的短消息（“好的明天见”/“好的后天见”）
    相似度超过 0.75，后一条就再也报不上来了。"""
    return a == b


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
                 read_interval=READ_INTERVAL, user_grace=USER_ACTIVE_GRACE,
                 capture_media=False, media_max_side=1024,
                 backfill=0, max_scrolls=8):
        super().__init__(hwnd, emit, debug_on)
        self.sel = selectors or sel_from(None)
        self.focus_policy = focus_policy or "steal"
        self.read_interval = float(read_interval)
        self.user_grace = float(user_grace)
        self.capture_media = bool(capture_media)   # 要不要把图片/表情包的像素截下来（给多模态判断）
        self.media_max_side = int(media_max_side)
        self.backfill = int(backfill)              # 切会话后自动补齐多少条历史（0 = 不补）
        self.max_scrolls = int(max_scrolls)
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

    # ---------------------------------------------------------------- 读图 + 回填历史

    def _scan(self):
        """读一棵树并定位：返回 (nodes, pane_left, pane_right, list_node)。"""
        nodes = self._read_tree()
        tops = [n.rect for n in nodes if n.depth == 0 and n.has_geo]
        panel = ((min(r[0] for r in tops), min(r[1] for r in tops),
                  max(r[2] for r in tops), max(r[3] for r in tops)) if tops else (0, 0, 0, 0))
        pane_left, pane_right = pane_bounds(nodes, panel)
        return nodes, pane_left, pane_right, find_message_list(nodes, pane_left)

    def _emit_turns(self, nodes, emit, pane_left, list_node) -> int:
        """从一棵树里取新消息发 lines，返回这次发了几条（去重靠 self.seen）。

        list_node 允许为 None：类名那条路（气泡 + container--self）不需要消息列表。"""
        anchors: list = []
        turns = build_turns(nodes, self.sel, pane_left=pane_left, list_node=list_node,
                            anchors=anchors)
        if not turns:
            return 0
        idx: list = []
        fresh = new_turns(self.seen, turns, _exact, idx)
        if not fresh or not self.title:
            return 0
        payload = []
        for j, t in enumerate(fresh):
            media = None
            a = anchors[idx[j]] if idx[j] < len(anchors) else None
            if self.capture_media and a is not None:
                media = self._grab(a)
            payload.append((t.who, t.name, t.text, media) if media
                           else (t.who, t.name, t.text))
        emit("lines", self.title, payload, self.last_area or (0, 0, 0, 0))
        return len(fresh)

    def _read_and_emit(self, emit) -> int:
        nodes, pane_left, _, list_node = self._scan()
        return self._emit_turns(nodes, emit, pane_left, list_node)

    def _backfill(self, emit) -> int:
        """切会话后自动往上翻，把历史消息补齐（默认 30 条），再滚回底部。

        为什么必须翻：QQ 的消息列表是**虚拟列表**，没渲染出来的项几何全是 0，
        只读当前视图往往只有几条（实测刚切过去只有 4 条），判断的上下文太薄。
        翻法：先滚到顶（有上限，并且不再变多就停），再逐格往下滚——
        每下一步读一遍并发消息，这样消息天然是**按时间顺序**补齐的，
        最后正好回到最新处（不会把用户的视图留在半截历史里）。
        只读不发。人在动键鼠时不去动他的滚动位置。
        """
        auto = self._auto
        if auto is None or self.backfill <= 0:
            return self._read_and_emit(emit)          # 不回填：至少把当前视图读了
        if self._user_idle_seconds() < self.user_grace:
            return self._read_and_emit(emit)          # 人在操作：不动滚动，只读当前视图
        nodes, pane_left, _, list_node = self._scan()
        if list_node is None or not list_node.has_geo:
            return self._read_and_emit(emit)
        sent = 0
        cx = (list_node.rect[0] + list_node.rect[2]) // 2
        cy = (list_node.rect[1] + list_node.rect[3]) // 2
        try:
            u32.SetCursorPos(cx, cy)          # 滚轮发给光标下的窗口
        except Exception:
            return sent
        ups = 0
        known: set[tuple[str, str]] = set()
        for _ in range(max(1, self.max_scrolls)):
            auto.WheelUp(wheelTimes=WHEEL_TICKS)
            time.sleep(SCROLL_SETTLE)
            ups += 1
            nodes, pane_left, _, list_node = self._scan()
            if list_node is None:
                break
            turns = build_turns(nodes, self.sel, pane_left=pane_left, list_node=list_node)
            now = {(t.who, t.text) for t in turns}
            # 到顶的判据是「这一滚没有带来任何新消息」，不是「可见条数不再变大」——
            # 可见条数本来就恒等于一屏（虚拟列表就渲这么多）。
            if not (now - known):
                break
            known |= now
            if len(known) >= self.backfill:
                break                         # 已经够多了
        # 往下走回到最新处，边走边发（于是顺序是从旧到新）
        for _ in range(ups):
            auto.WheelDown(wheelTimes=WHEEL_TICKS)
            time.sleep(SCROLL_SETTLE)
            sent += self._read_and_emit(emit)
        sent += self._read_and_emit(emit)     # 最后再读一次，确保最新那条也在
        return sent

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

    def _empty_reason(self, nodes) -> str:
        """聊天区里读不到东西时说清楚是哪种原因。

        先查 QQ 进程的命令行里到底有没有 --force-renderer-accessibility——这个最权威；
        查不到才退回「树里有没有 Web 内容的 id（ml-root / func-bar-*）」这个箱底办法。
        """
        flag = self._a11y_flag_on()
        if flag is False:
            return ("QQ 没带 --force-renderer-accessibility 启动，所以它的聊天内容不在无障碍树里。"
                    "关掉 QQ 后用 scripts\\restart-qq-with-a11y.bat 重新启动"
                    "（或手动：QQ.exe --force-renderer-accessibility），然后跑 probe\\probe_qq_uia.py 验证。")
        if flag is True:
            return ("QQ 的无障碍开关是开的，但聊天区里读不到消息列表/气泡——"
                    "可能没打开会话、会话还在加载，或者这个 QQ 版本结构变了。"
                    "先在 QQ 里点开一个会话；还不行就用 probe\\probe_qq_uia.py 抓一份树来对"
                    "（config.json 里可用 qq_selectors 覆盖）。")
        # 命令行拿不到：退回 id 判断
        web_ids = ("ml-root", "func-bar", "searchResultScrollView", "md-tips__")
        if not any(any(w in n.aid for w in web_ids) for n in nodes):
            return ("QQ 的聊天内容不在无障碍树里——它可能没有带 --force-renderer-accessibility 启动。"
                    "关掉 QQ 后用 scripts\\restart-qq-with-a11y.bat 重新启动。")
        return ("QQ 的聊天区里读不到消息列表/气泡——可能没打开会话或者结构变了。"
                "先点开一个会话；还不行就用 probe\\probe_qq_uia.py 抓一份树来对。")

    @staticmethod
    def _a11y_flag_on() -> bool | None:
        """QQ 进程的命令行里有没有 --force-renderer-accessibility。

        没这个开关时 Chromium 不把 Web 内容暴露给 UIA，我们什么都读不到。
        拿不到（没 PowerShell / 超时）返回 None，由调用方退回树启发式。
        """
        if os.name != "nt":
            return None
        import subprocess
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "(Get-CimInstance Win32_Process -Filter \"Name='QQ.exe'\").CommandLine"],
                capture_output=True, text=True, timeout=10, errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
        except Exception:
            return None
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if not lines:
            return None                      # QQ 没在跑 / 查不到，别乱下结论
        return any("--force-renderer-accessibility" in ln for ln in lines)

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

        # panel_rect 用「所有顶层容器的并集」，而不是单拿某一个：
        # 拿第一个可能只是会话列表条（panel_right 会算成 833，头部文字被筛掉）；
        # 拿最大的可能就是聊天区自己（pane_bounds 找不到更靠右的容器，退回比例切）。
        # 并集才是整个内容区，pane_bounds 的“左边缘最靠右的整高容器 = 聊天区”才成立。
        tops = [n.rect for n in nodes if n.depth == 0 and n.has_geo]
        if tops:
            panel_rect = (min(r[0] for r in tops), min(r[1] for r in tops),
                          max(r[2] for r in tops), max(r[3] for r in tops))
        else:
            panel_rect = (0, 0, 0, 0)
        pane_left, pane_right = pane_bounds(nodes, panel_rect)
        panel_right = pane_right or panel_rect[2]

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
        if not name:
            if list_node is None and not has_bubbles:
                # 连消息列表都没有：先说是「没开关」还是「没开会话」
                self._note_once("no-list", self._empty_reason(nodes))
                return
            # 会话名读不到时**不读消息**：否则切了会话但名字这一拍没读到，新会话的消息会被算到
            # 上一个会话的账上（历史/上下文全串了）。宁可这一拍什么都不发，下一拍（≥1.2s）就好了。
            self._note_once(
                "no-name",
                "这一拍读不到会话名（QQ 头部暂时不可读，切会话后常见）——先不读消息，"
                "以免把不同会话的消息混在一起。")
            return
        switched = name != self.title
        if switched:
            self.title = name
            self.seen.clear()          # 换会话：去重状态跟着换（和 OCR 采集器一致）
            emit("chat", name)
            emit("reset", name)        # 切会话要重置：让父进程把这个会话的历史清掉重读
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
        if list_node is None and not has_bubbles:
            # 树很大但聊天区里既没消息列表也没气泡。实测两种原因，处理方式完全不同：
            #   · QQ 没带 --force-renderer-accessibility 启动 → 聊天内容根本不在无障碍树里
            #     （实测：不带参数只有壳，没有 ml-root / func-bar-* 这些 id）
            #   · 没打开会话 / 会话还在加载
            self._note_once("no-list", self._empty_reason(nodes))
            return
        if not build_turns(nodes, self.sel, pane_left=pane_left, list_node=list_node):
            self._note_once("no-bubbles",
                            "没读到消息（message-container / ml-root 都没有）——"
                            "可能这个 QQ 版本结构变了，用 probe\\probe_qq_uia.py 抓一份树来对。")
            return
        if switched and self.backfill > 0:
            # 切会话：先自动把历史补齐（_backfill 内部按时间顺序发 lines），做完就返回
            self._backfill(emit)
            return
        self._emit_turns(nodes, emit, pane_left, list_node)

    def _grab(self, node) -> tuple[str, bytes] | None:
        """把媒体节点的屏幕矩形截下来 → (mime, JPEG 字节)。**只在内存里**，不落盘。

        长边超过 media_max_side 就缩小：给模型看图不需要原分辨率，
        而且图片 token 数跟像素直接相关（clef 整个 prompt 要塞进一个 batch）。
        """
        if not node.has_geo:
            return None
        try:
            import io

            from PIL import ImageGrab
        except Exception:
            return None
        left, top, right, bottom = node.rect
        w, h = right - left, bottom - top
        if w < 8 or h < 8:
            return None
        try:
            img = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        except Exception:
            return None
        scale = min(1.0, self.media_max_side / max(w, h))
        if scale < 1.0:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=85)
        return ("image/jpeg", buf.getvalue())


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
                Ctrl("GroupControl", (852, 916, 1218, 957), aid="func-bar-shortcuts-left"),
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
    assert kinds == ["resumed", "chat", "reset", "area", "input", "lines"], kinds
    assert events[1] == ("chat", "甲方"), events[1]
    assert events[4] == ("input", (900, 1000, 2000, 1048)), events[4]   # 上 60%（点中心落在打字区）
    _l, _t, _r, _b = events[4][1]
    assert _t >= 1000 and _b <= 1080, events[4]                          # 仍在原框之内
    assert events[5][2] == [("her", None, "在吗"), ("me", None, "在的")], events[5]

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
    assert [e[0] for e in events[n:]] == ["chat", "reset", "lines"], events[n:]
    assert events[n][1] == "乙方" and events[n + 1][1] == "乙方"   # chat 和 reset 都带新会话名

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

    # 头部读不到（但消息在）→ 不发 lines，避免把不同会话的消息混在一起
    state["root"] = Ctrl("PaneControl", (450, 210, 2110, 1170), kids=[
        Ctrl("GroupControl", (520, 260, 833, 1165)),
        Ctrl("GroupControl", (833, 260, 2105, 1165), kids=[
            Ctrl("GroupControl", (832, 325, 1880, 907), aid="ml-root", kids=[
                Ctrl("TextControl", (900, 400, 1300, 430), cls="", name="对方说的一句"),
            ]),
            Ctrl("GroupControl", (852, 916, 1218, 957), aid="func-bar-shortcuts-left"),
        ]),
    ])
    events.clear()
    c8 = UiaQqntCollector(1234, emit, selectors=Selectors(), read_interval=0)
    c8._is_foreground = lambda: True
    c8._window_origin = lambda: (0, 0)
    c8.start(); events.clear()
    c8.poll(emit)
    kinds8 = [e[0] for e in events]
    assert "lines" not in kinds8, kinds8
    assert any(e[0] == "status" and "读不到会话名" in e[1] for e in events), events

    # 有树但聊天区里没消息列表也没气泡 → 提示先点开会话 / 结构可能变了
    state["root"] = Ctrl("PaneControl", (450, 210, 2110, 1170), kids=[
        Ctrl("GroupControl", (520, 260, 833, 1165)),
        Ctrl("GroupControl", (833, 260, 2105, 1165), kids=[
            Ctrl("TextControl", (860, 280, 1100, 305), cls="", name="某个群名"),
        ] + [Ctrl("GroupControl", (520 + i, 400 + i, 700 + i, 430 + i)) for i in range(45)]),
    ])
    events.clear()
    c7 = UiaQqntCollector(1234, emit, selectors=Selectors(), read_interval=0)
    c7._a11y_flag_on = staticmethod(lambda: False)
    c7._is_foreground = lambda: True
    c7._window_origin = lambda: (0, 0)
    c7.start()
    c7.poll(emit)
    assert any(e[0] == "status" and "--force-renderer-accessibility" in e[1] for e in events), events

    # 有树也有 web id、但没有类名 → 提示结构可能变了
    state["root"] = tree("甲方", ())
    events.clear()
    c3 = UiaQqntCollector(1234, emit, selectors=Selectors(), read_interval=0)
    c3._a11y_flag_on = staticmethod(lambda: True)
    c3._is_foreground = lambda: True
    c3._window_origin = lambda: (0, 0)
    c3.start()
    c3.poll(emit)
    assert any(e[0] == "status" and "读不到消息列表/气泡" in e[1] for e in events), events

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
    assert first == ["chat", "reset", "area", "input", "lines"], first
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
    # 非文字消息：给占位；开了多模态就把图截下来一起发（Omni 预留）
    img_root = Ctrl("PaneControl", (450, 210, 2110, 1170), kids=[
        Ctrl("GroupControl", (520, 260, 833, 1165)),
        Ctrl("GroupControl", (832, 260, 2105, 1165), kids=[
            Ctrl("TextControl", (1031, 237, 1084, 263), cls="", name="群名"),
            Ctrl("GroupControl", (832, 325, 1880, 907), aid="ml-root", kids=[
                Ctrl("GroupControl", (800, 330, 1880, 620), aid="7000000000000000001", kids=[
                    Ctrl("TextControl", (817, 340, 848, 362), cls="", name="对方"),
                    # 徽标比昵称深一层（真机就是这样），所以包一层
                    Ctrl("GroupControl", (857, 340, 872, 362), kids=[
                        Ctrl("TextControl", (857, 340, 872, 362), cls="", name="LV"),
                    ]),
                    Ctrl("ImageControl", (830, 380, 1235, 620), cls="", name=""),
                ]),
            ]),
            Ctrl("GroupControl", (852, 916, 1218, 957), aid="func-bar-shortcuts-left"),
        ]),
    ])
    state["root"] = img_root
    ev3: list = []
    em3 = lambda kind, *p: ev3.append((kind,) + tuple(p))
    c9 = UiaQqntCollector(1234, em3, selectors=Selectors(), read_interval=0,
                          capture_media=False)
    c9._is_foreground = lambda: True
    c9._window_origin = lambda: (0, 0)
    c9.start(); ev3.clear(); c9.poll(em3)
    lines = [e for e in ev3 if e[0] == "lines"][0][2]
    assert lines == [("her", "对方", "[图片]")], lines        # 占位；没开多模态就只有 3 元组
    # 开了多模态：媒体会被截下来放进第 4 项
    c10 = UiaQqntCollector(1234, em3, selectors=Selectors(), read_interval=0,
                           capture_media=True)
    c10._is_foreground = lambda: True
    c10._window_origin = lambda: (0, 0)
    grabbed = []
    c10._grab = lambda node: (grabbed.append(node) or ("image/jpeg", b"X"))
    c10.start(); ev3.clear(); c10.poll(em3)
    lines = [e for e in ev3 if e[0] == "lines"][0][2]
    assert lines == [("her", "对方", "[图片]", ("image/jpeg", b"X"))], lines
    assert grabbed and grabbed[0].type == "ImageControl", grabbed

    # 切会话 → reset + 自动回填历史（虚拟列表 + 滚轮全部用假的）
    TOTAL, VIEW = 40, 5
    TXT = lambda i: f"msg{i}A内容{i}B"      # 造得彼此不像，免得被“相似即同一条”的去重吃掉
    vstate = {"lo": TOTAL - VIEW, "session": "群A"}
    def fake_window_tree():
        lo = max(0, min(vstate["lo"], TOTAL - VIEW))
        kids = [Ctrl("TextControl", (1031, 237, 1084, 263), cls="", name=vstate["session"]),
                Ctrl("GroupControl", (832, 325, 1880, 907), aid="ml-root", kids=[
                    Ctrl("GroupControl", (830, 340 + 40 * k, 1300, 380 + 40 * k),
                         cls="message-container", name=TXT(lo + k)) for k in range(VIEW)])]
        return Ctrl("PaneControl", (450, 210, 2110, 1170), kids=[
            Ctrl("GroupControl", (520, 260, 833, 1165)),
            Ctrl("GroupControl", (832, 260, 2105, 1165), kids=kids)])
    def as_nodes(root):
        """把 Ctrl 假树变成采集器真正会拿到的 Node 列表（_read_tree 的返回形状）。"""
        out: list = []
        def walk(c, d, p):
            for k in c.GetChildren():
                i = len(out)
                r = k.BoundingRectangle
                out.append(Node(i, p, d, k.ControlTypeName, k.ClassName, k.AutomationId,
                                (r.left, r.top, r.right, r.bottom), k.Name, len(k.Name)))
                walk(k, d + 1, i)
        walk(root, 0, -1)
        return out
    ev4: list = []
    em4 = lambda kind, *p: ev4.append((kind,) + tuple(p))
    c11 = UiaQqntCollector(1234, em4, selectors=Selectors(), read_interval=0,
                           backfill=30, max_scrolls=8)
    c11._is_foreground = lambda: True
    c11._window_origin = lambda: (0, 0)
    c11._user_idle_seconds = lambda: 9.0          # 假装人离开了一会儿，允许滚动
    c11._read_tree = lambda: as_nodes(fake_window_tree())
    fake.WheelUp = lambda wheelTimes=3: vstate.update(lo=max(0, vstate["lo"] - wheelTimes))
    fake.WheelDown = lambda wheelTimes=3: vstate.update(lo=min(TOTAL - VIEW, vstate["lo"] + wheelTimes))
    c11.start(); ev4.clear()
    c11.poll(em4)
    kinds = [e[0] for e in ev4]
    assert kinds[:2] == ["chat", "reset"], kinds        # 首次读就是“切到这个会话”
    assert vstate["lo"] == TOTAL - VIEW, vstate         # 回填完要回到最新处
    allt = [x[2] for e in ev4 if e[0] == "lines" for x in e[2]]
    assert allt and allt[-1] == TXT(TOTAL - 1), allt[-3:]      # 最后一条是最新的
    assert allt == sorted(allt, key=lambda t: int(t.split("A")[0][3:])), allt[:6]  # 从旧到新

    # 切到另一个群：必须 reset + 重新按时间顺序补齐历史
    ev4.clear()
    vstate["session"], vstate["lo"] = "群B", TOTAL - VIEW
    c11._last_read = 0.0
    c11.poll(em4)
    kinds = [e[0] for e in ev4]
    assert kinds[:2] == ["chat", "reset"], kinds
    texts = []
    for e in ev4:
        if e[0] == "lines":
            texts += [x[2] for x in e[2]]
    assert len(texts) >= 25, len(texts)                      # 补齐了一大批历史（上限内）
    num = lambda t: int(t.split("A")[0][3:])
    assert texts == sorted(texts, key=num), texts[:8]         # 按时间顺序
    assert texts[-1] == TXT(TOTAL - 1), texts[-3:]           # 最后回到最新那条
    assert vstate["lo"] == TOTAL - VIEW, vstate               # 视图回到最新处

    print("uia_qqnt ok")
