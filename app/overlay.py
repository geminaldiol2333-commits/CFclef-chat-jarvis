# -*- coding: utf-8 -*-
"""浅色置顶回复助手：回复建议和独立设置页。发送始终由用户确认。"""
import threading
from datetime import datetime
from math import isfinite
from types import SimpleNamespace

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QPushButton, QSizeGrip, QSizePolicy,
    QStackedWidget, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CardWidget, CheckBox, ComboBox, EditableComboBox, FluentIcon as FIF,
    HyperlinkButton, IndeterminateProgressBar, LineEdit, PasswordLineEdit, PlainTextEdit,
    PrimaryPushButton, PushButton, ScrollArea, SpinBox, SwitchButton, Theme, TransparentToolButton,
    setCustomStyleSheet, setFont, setTheme, setThemeColor,
)

from app import settings
from app.version import VERSION
from core import jev_client, llm, providers
from core.questions import CHOICE_LABELS
from app.i18n import T, bind

_LOG_LINES = 300
_MUTED = "#68776f"
_GREEN = "#18794e"
_RELATIONSHIPS = [
    ("恋人", "romantic partners"), ("朋友", "friends"), ("同事", "colleagues"),
    ("家人", "family"), ("自定义", None),
]


def _choice(answers, name):
    return T(CHOICE_LABELS[name].get((answers.get(name) or {}).get("choice"), "暂未判断"))


def _origin_banner():
    """出处说明：fork 版不替上游导流，也不冒充上游。"""
    return HyperlinkButton(
        "CFclef-chat-jarvis · 本地 clef-flash 版（基于 jev-chat-windows 二次开发）",
        "https://github.com/geminaldiol2333-commits/CFclef-chat-jarvis")


class _FitCombo(ComboBox):
    """长名字不撑开窄布局。按钮上按当前宽度省略；条目仍是全文，findText 靠它。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def setText(self, text):
        self._full = text or ""
        QPushButton.setText(self, self._elide(self._full))
        if self._full and self.text() != self._full:
            self.setToolTip(self._full)

    def setPlaceholderText(self, text):
        index = self.currentIndex()
        super().setPlaceholderText(text)
        # Fluent ComboBox 会连第 0 项一起覆盖；重译占位文案时保留当前会话。
        if index >= 0:
            self.setCurrentIndex(index)
            self.setText(self.itemText(index))

    def minimumSizeHint(self):
        hint = QPushButton.minimumSizeHint(self)
        return QSize(48, hint.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        shown = self._elide(self._full)
        if shown != self.text():
            QPushButton.setText(self, shown)
        if self._full and shown != self._full:
            self.setToolTip(self._full)

    def _elide(self, text):
        # 右侧箭头大约 28px。还没排上版时先按一个窄宽度省略，避免最小宽度被整句名字撑开。
        avail = self.width() - 36 if self.width() > 64 else 120
        return self.fontMetrics().elidedText(text, Qt.ElideRight, max(24, avail))


def _label(text="", size=14, color=None, bold=False, parent=None):
    label = BodyLabel(text, parent)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    setFont(label, size, QFont.DemiBold if bold else QFont.Normal)
    if color:
        qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
        setCustomStyleSheet(label, qss, qss)
    return label


def _tlabel(source, *args, **kwargs):
    return bind(_label("", *args, **kwargs), source)


def _tool(icon, title, callback, parent=None):
    button = TransparentToolButton(icon, parent)
    button.setFixedSize(32, 32)
    bind(button, title, "setToolTip")
    bind(button, title, "setAccessibleName")
    button.clicked.connect(callback)
    return button


class _Surface(CardWidget):
    def __init__(self, parent=None, accent=False):
        self.accent = accent
        super().__init__(parent)
        self.setBorderRadius(12)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def _normalBackgroundColor(self):
        return QColor("#edf7f0" if self.accent else "#ffffff")

    def _hoverBackgroundColor(self):
        return self._normalBackgroundColor()

    def _pressedBackgroundColor(self):
        return self._normalBackgroundColor()


class _Fetched(QObject):
    """取模型列表的后台线程 → 主线程：哪一组（SimpleNamespace）、取回来的模型 id、失败原因（成功是空串）。
    Qt 不让跨线程碰控件，信号是跨线程唯一干净的路。"""
    done = Signal(object, list, str)


class _LocalResult(QObject):
    """本地模型后台操作（拉起/停止/检测）→ 主线程：一段可以直接显示的文本。"""
    done = Signal(str)


class _TitleBar(QWidget):
    """只有标题栏可拖动，选择正文或按按钮不会意外移动窗口。"""
    def __init__(self, parent):
        super().__init__(parent)
        self._drag = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.window().pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag = None
        super().mouseReleaseEvent(event)


class _MainWindow(QWidget):
    """窗口大小变了就叫 Overlay 重新排布；断点没跨过时 _relayout 自己不做事，这里不用防抖。"""
    def __init__(self, relayout):
        super().__init__()
        self._relayout = relayout

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout(event.size().width(), event.size().height())


class _ReplyCard(_Surface):
    def __init__(self, owner, index, recommended=False, number=1, score=None):
        super().__init__(accent=recommended)
        box = QVBoxLayout(self)
        self.box = box
        box.setSpacing(10)
        top = QHBoxLayout()
        title = lambda: T("推荐回复") if recommended else f"{T('备选')} {number}"
        top.addWidget(_tlabel(lambda: title() + (f" · {round(score * 100)}%" if score is not None else ""),
                             12, _GREEN if recommended else _MUTED, True))
        self.copyButton = _tool(FIF.COPY, "复制这条回复", lambda: owner._copy(index), self)
        self.copyButton.setFixedSize(24, 24)
        top.addWidget(self.copyButton)
        box.addLayout(top)
        self.text = _label(owner.cands[index], 15)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.addWidget(self.text)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.fillButton = bind((PrimaryPushButton if recommended else PushButton)("", self), "填入")
        bind(self.fillButton, lambda: T("填入 {reply}").format(reply=title()), "setAccessibleName")
        self.fillButton.clicked.connect(lambda: owner._fill(index))
        bottom.addWidget(self.fillButton)
        box.addLayout(bottom)
        self.set_compact(owner._compact)

    def set_available(self, enabled):
        self.fillButton.setEnabled(enabled)
        self.copyButton.setEnabled(enabled)

    def set_compact(self, compact):
        self.box.setContentsMargins(12, 8, 12, 8) if compact else self.box.setContentsMargins(16, 12, 16, 12)
        self.fillButton.setMinimumWidth(80 if compact else 100)


class _SpeakerCard(_Surface):
    """群聊里给某个发言人做的判断卡（群里不起草，所以没有「填入」按钮）。

    每个人一张：名字 + 建议 + 可能意图 + 紧张度。哪路先回来先上屏，没回来的先说「判断中」。
    """

    def __init__(self, owner, name, item=None, error=""):
        super().__init__()
        box = QVBoxLayout(self)
        self.box = box
        box.setSpacing(6)
        top = QHBoxLayout()
        top.addWidget(_label(name, 14, "#304c3c", True))
        top.addStretch(1)
        self.tension = _label("", 11)
        self.tension.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top.addWidget(self.tension)
        box.addLayout(top)
        if error:
            self.summary = _label(T("判断失败") + "：" + error, 12, "#b44832")
            self.tension.hide()
        else:
            answers = (item or {}).get("answers") or {}
            pending = not item
            self.summary = _label(
                T("正在判断…") if pending else T("建议：") + _choice(answers, "best_action"), 13)
            self.intent = _label("" if pending else
                                 T("可能意图 · ") + _choice(answers, "true_intent") +
                                 "\n" + T("可能需要 · ") + _choice(answers, "she_needs"), 12, _MUTED)
            box.addWidget(self.intent)
            score = (answers.get("danger_level") or {}).get("score")
            good = isinstance(score, (int, float)) and isfinite(score)
            self.tension.setText(f"{T('紧张度')} {score:.0f}/9" if good else T("紧张度待判断"))
            color = "#996819" if good and score >= 3 else _MUTED
            if good and score >= 6:
                color = "#b44832"
            qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
            setCustomStyleSheet(self.tension, qss, qss)
        box.addWidget(self.summary)
        self.set_compact(owner._compact)
        bind(self, T("在群里对每个人的判断，只给参考，不起草回复。"), "setToolTip")

    def set_compact(self, compact):
        self.box.setContentsMargins(12, 8, 12, 8) if compact else self.box.setContentsMargins(16, 12, 16, 12)


class Overlay:
    def __init__(self, on_fill, on_toggle_capture=None, on_target_change=None, result_of=None,
                 on_toggle_debug=None):
        """result_of(会话名) → 那个会话上次的结果或 None；切着看别的会话时用它把旧结果放回来。
        on_target_change(会话名, 人名) → 用户在群里挑了回复对象。
        on_toggle_debug(开不开) → 开关调试视图那个独立窗口。"""
        self.app = QApplication.instance() or QApplication([])
        setTheme(Theme.LIGHT)
        setThemeColor(_GREEN, save=False)
        self.on_fill = on_fill
        self.on_toggle_capture = on_toggle_capture
        self.on_target_change = on_target_change
        self.on_toggle_debug = on_toggle_debug
        self.result_of = result_of
        self.cands = []
        self.cards = []
        self.speakerCards = []          # 群聊按发言人的判断卡（和候选卡共用一个盒子，互相不会同时在）
        self._busy = False
        self._current = False
        self._compact = None  # 断点模式：None 保证 _relayout 第一次调用必定生效
        self._pageLayouts = []
        self._hintLabels = []
        self.feeds = {}  # {会话名: [(who, name, text, timestamp)]}，换语言只重画说话人标签
        self._feed_entries = []
        self.counts = {}  # {会话名: 消息条数}
        self.hers = {}  # {会话名: 对方最近一句}
        self.targets = {}  # {会话名: ([发言人], 当前回复对象)}
        self._chat = ""  # 微信当前开着的会话
        self._shown = ""  # 界面上正在看的会话（浏览时和上面不一样）
        self.win = _MainWindow(self._relayout)
        self.win.setObjectName("assistantWindow")
        self.win.setWindowTitle("CFclef-chat-jarvis")
        self.win.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.win.setStyleSheet(
            "QWidget#assistantWindow { background: #f5f7f6; border: 1px solid #dce3de; border-radius: 14px; }"
        )
        self.win.setMinimumWidth(320)
        self.win.setMaximumWidth(640)
        outer = QVBoxLayout(self.win)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        header = _TitleBar(self.win)
        title = QHBoxLayout(header)
        title.setContentsMargins(18, 12, 10, 10)
        title.setSpacing(8)
        name = _label("CFclef", 18, "#233c2f", True)
        name.setFixedWidth(62)
        name.setAttribute(Qt.WA_TransparentForMouseEvents)
        title.addWidget(name)
        self.subtitle = _label("chat-jarvis · 本地", 12, _MUTED)
        self.subtitle.setAttribute(Qt.WA_TransparentForMouseEvents)
        title.addWidget(self.subtitle, 1)
        self.captureSwitch = SwitchButton(header)
        bind(self.captureSwitch, "采集中", "setOnText")
        bind(self.captureSwitch, "已暂停", "setOffText")
        bind(self.captureSwitch, "开启或暂停采集", "setToolTip")
        bind(self.captureSwitch, "开启或暂停采集", "setAccessibleName")
        self.captureSwitch.setChecked(True)
        self.captureSwitch.checkedChanged.connect(self._capture_toggled)
        title.addWidget(self.captureSwitch)
        self.settingsButton = _tool(FIF.SETTING, "设置", self.open_settings, header)
        title.addWidget(self.settingsButton)
        title.addWidget(_tool(FIF.REMOVE, "最小化", self.win.showMinimized, header))
        title.addWidget(_tool(FIF.CLOSE, "关闭助手", self.win.close, header))
        outer.addWidget(header)
        self.updateBar = QWidget(self.win)
        update_row = QHBoxLayout(self.updateBar)
        update_row.setContentsMargins(18, 4, 8, 4)
        update_row.setSpacing(8)
        self.updateLabel = _label("", 12, _GREEN, True)
        update_row.addWidget(self.updateLabel, 1)
        self.updateLink = bind(HyperlinkButton("", "", self.updateBar), "去下载")
        self.updateLink.setFixedHeight(24)
        update_row.addWidget(self.updateLink)
        closeUpdate = TransparentToolButton(FIF.CLOSE, self.updateBar)
        closeUpdate.setFixedSize(20, 20)
        bind(closeUpdate, "关闭更新提示", "setToolTip")
        bind(closeUpdate, "关闭更新提示", "setAccessibleName")
        closeUpdate.clicked.connect(lambda: self.updateBar.hide())
        update_row.addWidget(closeUpdate)
        self.updateBar.setFixedHeight(32)
        self.updateBar.hide()
        outer.addWidget(self.updateBar)
        self.pages = QStackedWidget(self.win)
        outer.addWidget(self.pages, 1)
        self._build_home()
        self._build_settings()
        footer = QHBoxLayout()
        footer.setContentsMargins(20, 9, 8, 8)
        footer.addWidget(_tlabel(lambda: f"{T('仅填入输入框 · 发送由你确认')} · v{VERSION}", 11, _MUTED), 1)
        grip = QSizeGrip(self.win)
        grip.setFixedSize(16, 16)
        footer.addWidget(grip, 0, Qt.AlignBottom)
        outer.addLayout(footer)
        screen = self.app.primaryScreen().availableGeometry()
        self.win.setMinimumHeight(min(360, screen.height() - 32))
        self.win.resize(min(440, screen.width() - 32), min(820, screen.height() - 48))
        self.win.move(screen.right() - self.win.width() - 20, screen.top() + 24)
        self._relayout(self.win.width(), self.win.height())  # resizeEvent 补不到构造时这一次
        self.set_status("等待新消息" if settings.has_key() else "需要配置模型",
                        "idle" if settings.has_key() else "warning")
        self.win.show()

    def _scroll_page(self):
        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setAutoFillBackground(False)
        content = QWidget()
        content.setObjectName("pageContent")
        content.setStyleSheet("QWidget#pageContent { background: transparent; }")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 8, 20, 12)
        layout.setSpacing(14)
        scroll.setWidget(content)
        self.pages.addWidget(scroll)
        self._pageLayouts.append(layout)
        return scroll, layout

    def _relayout(self, w, h):
        """宽度跨过断点才重新摆布局（省事）；高度每次都重算，反正只是设个定高。"""
        compact = w < 400
        if compact != self._compact:
            self._compact = compact
            self._apply_compact(compact)
        self.feed.setFixedHeight(max(100, min(240, int(h * 0.25))))

    def _apply_compact(self, compact):
        """紧凑/常规两套间距和可见性；断点没变时不会被调用。"""
        self.subtitle.setVisible(not compact)
        bind(self.captureSwitch, lambda: "" if self._compact else T("采集中"), "setOnText")
        bind(self.captureSwitch, lambda: "" if self._compact else T("已暂停"), "setOffText")
        for label in self._hintLabels:
            label.setVisible(not compact)
        self.referenceNote.setVisible(bool(self.cands) and not compact)
        self._sync_model_fields()
        margins = (12, 8, 12, 12) if compact else (20, 8, 20, 12)
        for layout in self._pageLayouts:
            layout.setContentsMargins(*margins)
        for card in self.cards + self.speakerCards:
            card.set_compact(compact)

    def _build_home(self):
        self.home, body = self._scroll_page()
        heading = QHBoxLayout()
        self.headTitle = _tlabel("回复建议", 23, "#24382d", True)
        heading.addWidget(self.headTitle, 1)
        self.updated = _label("", 11, _MUTED)
        self.updated.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        heading.addWidget(self.updated)
        body.addLayout(heading)
        chat_row = QHBoxLayout()
        chat_row.setSpacing(8)
        prefix = _tlabel("当前会话", 12, _MUTED)
        prefix.setFixedWidth(56)
        chat_row.addWidget(prefix)
        self.chatBox = _FitCombo()
        bind(self.chatBox, "尚未识别到会话", "setPlaceholderText")
        bind(self.chatBox, "当前会话", "setAccessibleName")
        bind(self.chatBox, "聊天窗口切到哪个会话这里就跟到哪个；也可以自己选一个，只看它的记录和建议", "setToolTip")
        self.chatBox.currentIndexChanged.connect(self._on_chat_selected)
        chat_row.addWidget(self.chatBox, 1)
        self.chatFollow = _label("", 11, _MUTED)
        self.chatFollow.setFixedWidth(52)
        self.chatFollow.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        chat_row.addWidget(self.chatFollow)
        body.addLayout(chat_row)
        self.targetRow = QWidget()  # 只有开了「群聊指定回复对象」且这个会话是群聊才露出来
        target_row = QHBoxLayout(self.targetRow)
        target_row.setContentsMargins(0, 0, 0, 0)
        target_row.setSpacing(8)
        target_prefix = _tlabel("回复对象", 12, _MUTED)
        target_prefix.setFixedWidth(56)
        target_row.addWidget(target_prefix)
        self.targetBox = _FitCombo()
        bind(self.targetBox, "回复对象", "setAccessibleName")
        bind(self.targetBox, "三条候选都按这个人来写；不选就跟着最近说话的那位", "setToolTip")
        self.targetBox.currentIndexChanged.connect(self._on_target_selected)
        target_row.addWidget(self.targetBox, 1)
        self.atCheck = bind(CheckBox(""), "填入时带 @")
        self.atCheck.setChecked(True)
        bind(self.atCheck, "填入时在开头加「@名字 」。只是普通文字，不会变成真正的 @", "setToolTip")
        target_row.addWidget(self.atCheck)
        self.targetRow.hide()
        body.addWidget(self.targetRow)
        self.status = _label("", 12, _MUTED)
        body.addWidget(self.status)
        self.progress = IndeterminateProgressBar()
        self.progress.setFixedHeight(3)
        self.progress.hide()
        body.addWidget(self.progress)
        self.context = QWidget()
        context_box = QVBoxLayout(self.context)
        context_box.setContentsMargins(0, 0, 0, 0)
        context_box.setSpacing(5)
        context_box.addWidget(_tlabel("对方最近说", 11, _MUTED))
        self.latest = _label("", 14, "#42574a")
        self.latest.setTextInteractionFlags(Qt.TextSelectableByMouse)
        context_box.addWidget(self.latest)
        self.context.hide()
        body.addWidget(self.context)

        self.insight = _Surface()
        insight_box = QVBoxLayout(self.insight)
        insight_box.setContentsMargins(14, 12, 14, 12)
        insight_box.setSpacing(7)
        row = QHBoxLayout()
        self.insightTitle = _tlabel("对话参考", 12, _MUTED)
        row.addWidget(self.insightTitle, 1)
        self.tension = _label("", 11)
        self.tension.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.tension)
        insight_box.addLayout(row)
        self.summary = _label("", 14, "#304c3c", True)
        insight_box.addWidget(self.summary)
        self.intent = _label("", 12, _MUTED)
        insight_box.addWidget(self.intent)
        bind(self.insight, "根据当前聊天片段推测，可能理解有偏差。紧张度为 0–9 的参考评分。", "setToolTip")
        self.insight.hide()
        body.addWidget(self.insight)

        self.empty = _Surface()
        empty_box = QVBoxLayout(self.empty)
        empty_box.setContentsMargins(24, 36, 24, 36)
        empty_box.setSpacing(14)
        symbol = _label("…", 30, _GREEN, True)
        symbol.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(symbol)
        self.emptyTitle = _tlabel("等待对方的新消息", 17, "#304c3c", True)
        self.emptyTitle.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(self.emptyTitle)
        self.emptyHint = _tlabel("保持聊天窗口打开。\n收到新消息后，回复建议会出现在这里。", 13, _MUTED)
        self.emptyHint.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(self.emptyHint)
        self.setupButton = bind(PrimaryPushButton(""), "前往设置")
        self.setupButton.clicked.connect(self.open_settings)
        self.setupButton.setVisible(not settings.has_key())
        empty_box.addWidget(self.setupButton, 0, Qt.AlignHCenter)
        if not settings.has_key():
            bind(self.emptyTitle, "先设置，再开始", "setText")
            bind(self.emptyHint, "配置模型和关系背景，\n让建议更贴近你们的对话。", "setText")
        body.addWidget(self.empty)
        self.replyBox = QVBoxLayout()
        self.replyBox.setSpacing(10)
        body.addLayout(self.replyBox)
        self.referenceNote = _tlabel("AI 建议仅供参考，按你的语气调整后再发送。", 11, _MUTED)
        self.referenceNote.hide()
        body.addWidget(self.referenceNote)

        self.historyButton = bind(PushButton(FIF.HISTORY, ""), "聊天记录")
        self.historyButton.clicked.connect(self._toggle_history)
        bind(self.historyButton, "展开或收起聊天记录", "setAccessibleName")
        body.addWidget(self.historyButton)
        self.feed = PlainTextEdit()
        self.feed.setReadOnly(True)
        bind(self.feed, "识别到的聊天内容会显示在这里", "setPlaceholderText")
        self.feed.setMaximumBlockCount(_LOG_LINES)
        self.feed.setFixedHeight(160)
        self.feed.hide()
        body.addWidget(self.feed)
        self._history_title()
        body.addStretch(1)

    def _build_settings(self):
        self.settingsPage, body = self._scroll_page()
        heading = QHBoxLayout()
        heading.addWidget(_tool(FIF.RETURN, "返回回复建议", self._back_home))
        heading.addWidget(_tlabel("设置", 23, "#24382d", True), 1)
        body.addLayout(heading)
        body.addWidget(_origin_banner())

        body.addWidget(_tlabel("调整关系背景，配置判断和起草用的两个模型。", 13, _MUTED))
        preference = _Surface()
        box = QVBoxLayout(preference)
        box.setContentsMargins(16, 16, 16, 18)
        box.setSpacing(12)
        box.addWidget(_tlabel("回复偏好", 16, "#304c3c", True))
        relation_label = _tlabel("你们的关系", 13)
        box.addWidget(relation_label)
        self.relationshipBox = ComboBox()
        self.relationshipBox.setMinimumWidth(0)
        self.relationshipBox.addItems([T(name) for name, value in _RELATIONSHIPS])
        bind(self.relationshipBox, "你们的关系", "setAccessibleName")
        relation_label.setBuddy(self.relationshipBox)
        box.addWidget(self.relationshipBox)
        self.relEdit = LineEdit()
        bind(self.relEdit, "例如：刚认识的朋友，正在慢慢熟悉", "setPlaceholderText")
        bind(self.relEdit, "自定义关系背景", "setAccessibleName")
        box.addWidget(self.relEdit)
        self.relationshipBox.currentIndexChanged.connect(
            lambda index: self.relEdit.setVisible(_RELATIONSHIPS[index][1] is None)
        )
        box.addWidget(self._hint("帮助助手把握称呼、语气和回应分寸。"))
        style_label = _tlabel("说话风格（可选）", 13)
        box.addWidget(style_label)
        self.styleEdit = LineEdit()
        bind(self.styleEdit, "例如：话少、不用标点、偶尔用 doge、不说客套话", "setPlaceholderText")
        bind(self.styleEdit, "说话风格", "setAccessibleName")
        style_label.setBuddy(self.styleEdit)
        box.addWidget(self.styleEdit)
        box.addWidget(self._hint("候选本来就照着你最近发的消息模仿；这里可以再补一句你自己的口吻。"))
        context_label = _tlabel("参考上下文", 13)
        box.addWidget(context_label)
        self.contextBox = SpinBox()
        self.contextBox.setRange(3, 30)
        bind(self.contextBox, "参考的最近消息条数", "setAccessibleName")
        context_label.setBuddy(self.contextBox)
        box.addWidget(self.contextBox)
        box.addWidget(self._hint(
            "生成和判断时看最近这么多条消息。太少会丢上下文，太多会稀释重点，建议 6–12。"
        ))
        target_row = QHBoxLayout()
        target_row.addWidget(_tlabel("群聊指定回复对象", 13), 1)
        self.targetSwitch = SwitchButton()
        bind(self.targetSwitch, "开", "setOnText")
        bind(self.targetSwitch, "关", "setOffText")
        bind(self.targetSwitch, "群聊指定回复对象", "setAccessibleName")
        target_row.addWidget(self.targetSwitch)
        box.addLayout(target_row)
        box.addWidget(self._hint(
            "开了以后群聊里可以选回复给谁，候选会针对 TA 写，填入时可带 @。关了就正常回复。"
        ))
        group_row = QHBoxLayout()
        group_row.addWidget(_tlabel("群聊只给判断，不起草", 13), 1)
        self.groupOnlySwitch = SwitchButton()
        bind(self.groupOnlySwitch, "开", "setOnText")
        bind(self.groupOnlySwitch, "关", "setOffText")
        bind(self.groupOnlySwitch, "群聊只给判断，不起草", "setAccessibleName")
        group_row.addWidget(self.groupOnlySwitch)
        box.addLayout(group_row)
        box.addWidget(self._hint(
            "群里可能十几个人同时在聊三四件不相关的事，一条「下一句」没意义——开了就只显示判断摘要，"
            "不起草候选（也就不用加载起草模型）。只有开一个话题的小群才建议关掉。"))
        update_row = QHBoxLayout()
        update_row.addWidget(_tlabel("启动时检查更新", 13), 1)
        self.updateSwitch = SwitchButton()
        bind(self.updateSwitch, "开", "setOnText")
        bind(self.updateSwitch, "关", "setOffText")
        bind(self.updateSwitch, "启动时检查更新", "setAccessibleName")
        update_row.addWidget(self.updateSwitch)
        box.addLayout(update_row)
        box.addWidget(self._hint(
            "只向 GitHub 查最新版本号，不发送任何数据。国内访问 GitHub 慢的话关掉也行。"
        ))
        debug_row = QHBoxLayout()
        debug_row.addWidget(_tlabel("调试视图", 13), 1)
        self.debugSwitch = SwitchButton()
        bind(self.debugSwitch, "开", "setOnText")
        bind(self.debugSwitch, "关", "setOffText")
        bind(self.debugSwitch, "调试视图", "setAccessibleName")
        self.debugSwitch.checkedChanged.connect(self._debug_toggled)  # 这个开关立刻生效，不等「保存设置」
        debug_row.addWidget(self.debugSwitch)
        box.addLayout(debug_row)
        box.addWidget(self._hint(
            "另开一个窗口实时显示截到的画面和识别框：绿 = 我、蓝 = 对方、灰 = 过滤掉的灰字、"
            "红 = 当成图片丢掉、黄 = 小字丢掉。只在内存里画，不存图。"
        ))
        body.addWidget(preference)

        qq = _Surface()
        box = QVBoxLayout(qq)
        box.setContentsMargins(16, 16, 16, 18)
        box.setSpacing(12)
        box.addWidget(_tlabel("QQ NT（UIA 读取）", 16, "#304c3c", True))
        box.addWidget(self._hint(
            "QQ 只对前台窗口构建完整无障碍树（后台只有十来个节点），所以读消息时必须让 QQ 处于活动状态；"
            "解不出来时不会退回 OCR，会直接提示。"))
        qq_label = _tlabel("读取方式", 13)
        box.addWidget(qq_label)
        self.qqFocusBox = ComboBox()
        self.qqFocusBox.setMinimumWidth(0)
        self.qqFocusBox.addItems(["允许读取时短暂置前（读完把焦点还回去）",
                                  "只在 QQ 已在前台时读取（不抢焦点）"])
        bind(self.qqFocusBox, "QQ 读取方式", "setAccessibleName")
        qq_label.setBuddy(self.qqFocusBox)
        box.addWidget(self.qqFocusBox)
        box.addWidget(self._hint(
            "选第一种：QQ 在后台时也能读，但屏幕会闪一下、正在打字时可能被打断。"
            "选第二种：完全不打扰，但你切走就停更。"))
        body.addWidget(qq)

        models = _Surface()
        box = QVBoxLayout(models)
        box.setContentsMargins(16, 16, 16, 18)
        box.setSpacing(12)
        box.addWidget(_tlabel("模型", 16, "#304c3c", True))
        self._fetched = _Fetched()
        self._fetched.done.connect(self._models_fetched)
        self.jev = self._model_group(box, "判断 · Jev", "jev", providers.JEV_PROVIDERS)
        box.addWidget(self._hint(
            "判断意图、紧张度，并给三条候选排序。两家给的是同一个 Jev，必填。"
        ))
        self.draft = self._model_group(box, "起草 · 语言模型", "draft", providers.DRAFT_PROVIDERS)
        box.addWidget(self._hint(
            "写那三条候选。OpenAI / Anthropic / Gemini 三种接口都走各自官方 SDK。"
            "默认 DeepSeek 官网直连，国内最快。"
        ))
        think_row = QHBoxLayout()
        think_row.addWidget(_tlabel("起草时开启思考模式", 13), 1)
        self.thinkingSwitch = SwitchButton()
        bind(self.thinkingSwitch, "开", "setOnText")
        bind(self.thinkingSwitch, "关", "setOffText")
        bind(self.thinkingSwitch, "起草时开启思考模式", "setAccessibleName")
        think_row.addWidget(self.thinkingSwitch)
        box.addLayout(think_row)
        box.addWidget(self._hint(
            lambda: T("关：秒回，够用。开：模型先想再写，更斟酌但慢好几倍、贵一些。只有 {providers} 认这个开关。")
            .format(providers=" / ".join(providers.THINKING))
        ))
        body.addWidget(models)

        local = _Surface()
        box = QVBoxLayout(local)
        box.setContentsMargins(16, 16, 16, 18)
        box.setSpacing(12)
        box.addWidget(_tlabel("本地模型（离线运行）", 16, "#304c3c", True))
        box.addWidget(self._hint(
            "clef-flash 负责判断和排序，起草的文本模型负责写三条候选。两个都由本程序自己拉起 "
            "llama-server，空闲后自动卸载、把显存还回去。全程只连 127.0.0.1，不出网、不要 key。"))
        self.localServerEdit = self._path_row(box, "llama-server 路径", "file",
                                              "llama-server.exe 的完整路径；留空自动在 PATH / WinGet 里找")
        self.localDirEdit = self._path_row(box, "模型目录", "dir",
                                           "只填目录时自动在里面找 clef GGUF 和一个普通文本 GGUF")
        self.localClefEdit = self._path_row(box, "clef-flash GGUF", "file",
                                            "决策模型；填 .gguf 文件，或填放它的目录")
        self.localDraftEdit = self._path_row(box, "起草文本 GGUF", "file",
                                             "普通指令模型；不能填 clef-flash，它写不了字")
        auto_row = QHBoxLayout()
        auto_row.addWidget(_tlabel("启动时预热本地模型", 13), 1)
        self.localAutoSwitch = SwitchButton()
        bind(self.localAutoSwitch, "开", "setOnText")
        bind(self.localAutoSwitch, "关", "setOffText")
        bind(self.localAutoSwitch, "启动时预热本地模型", "setAccessibleName")
        auto_row.addWidget(self.localAutoSwitch)
        box.addLayout(auto_row)
        box.addWidget(self._hint("开着的话程序一启动就在后台把模型读进显存，第一条消息不用等加载。"))
        unload_row = QHBoxLayout()
        unload_row.addWidget(_tlabel("空闲后卸载模型", 13), 1)
        self.localUnloadSwitch = SwitchButton()
        bind(self.localUnloadSwitch, "开", "setOnText")
        bind(self.localUnloadSwitch, "关", "setOffText")
        bind(self.localUnloadSwitch, "空闲后卸载模型", "setAccessibleName")
        unload_row.addWidget(self.localUnloadSwitch)
        box.addLayout(unload_row)
        box.addWidget(self._hint(
            lambda: T("空闲 {sec} 秒后关掉 llama-server，把显存让给别的程序；下条消息再自动加载。")
            .format(sec=settings.local_idle_seconds())))
        self.localStatus = _label("", 12, _MUTED)
        self.localStatus.setWordWrap(True)
        box.addWidget(self.localStatus)
        local_actions = QHBoxLayout()
        self.localStartButton = bind(PushButton(""), "启动 / 检测")
        self.localStartButton.clicked.connect(self._local_start)
        local_actions.addWidget(self.localStartButton)
        self.localStopButton = bind(PushButton(""), "停止")
        self.localStopButton.clicked.connect(self._local_stop)
        local_actions.addWidget(self.localStopButton)
        local_actions.addStretch(1)
        box.addLayout(local_actions)
        body.addWidget(local)
        self._local = _LocalResult()
        self._local.done.connect(self._local_done)

        self.settingsFeedback = _label("", 13, _GREEN)
        self.settingsFeedback.hide()
        body.addWidget(self.settingsFeedback)
        actions = QHBoxLayout()
        back = bind(PushButton(""), "返回")
        back.clicked.connect(self._back_home)
        actions.addWidget(back)
        actions.addStretch(1)
        self.saveButton = bind(PrimaryPushButton(""), "保存设置")
        self.saveButton.clicked.connect(self._save)
        actions.addWidget(self.saveButton)
        body.addLayout(actions)
        body.addWidget(self._hint("保存后用于下一次生成的回复。"))
        body.addStretch(1)
        self._load_settings()

    def _hint(self, text):
        """设置页字段下面的灰字说明：记下来，紧凑模式一起隐藏。"""
        label = _tlabel(text, 12, _MUTED)
        self._hintLabels.append(label)
        return label

    # ---------- 本地模型设置卡片 ----------

    def _path_row(self, box, title, kind, hint):
        """一行「标题 + 输入框 + 浏览（文件/目录）」。"""
        label = _tlabel(title, 13)
        box.addWidget(label)
        row = QHBoxLayout()
        row.setSpacing(8)
        edit = LineEdit()
        bind(edit, hint, "setPlaceholderText")
        bind(edit, lambda: f"{T(title)} {T('路径')}", "setAccessibleName")
        row.addWidget(edit, 1)
        row.addWidget(_tool(FIF.FOLDER, f"浏览 {title}", lambda: self._browse(edit, kind)))
        box.addLayout(row)
        label.setBuddy(edit)
        box.addWidget(self._hint(hint))
        return edit

    def _browse(self, edit, kind):
        start = edit.text().strip()
        if kind == "dir":
            path = QFileDialog.getExistingDirectory(self.win, T("选择目录"), start)
        else:
            path, _ = QFileDialog.getOpenFileName(
                self.win, T("选择文件"), start,
                "GGUF (*.gguf);;llama-server (*.exe);;所有文件 (*)")
        if path:
            edit.setText(path)

    def _local_models(self):
        """把界面上的本地设置组一份 local_server.Models（端口/显存等高级项走 config.json）。"""
        from app import local_server

        return local_server.Models(
            llama_server=self.localServerEdit.text().strip(),
            models_dir=self.localDirEdit.text().strip(),
            clef_model=self.localClefEdit.text().strip(),
            draft_model=self.localDraftEdit.text().strip(),
            clef_port=settings.local_clef_port(),
            draft_port=settings.local_draft_port(),
            gpu_layers=settings.local_gpu_layers(),
            clef_ctx=settings.local_clef_ctx(),
            draft_ctx=settings.local_draft_ctx(),
            idle_seconds=settings.local_idle_seconds(),
            idle_unload=self.localUnloadSwitch.isChecked(),
            autostart=self.localAutoSwitch.isChecked(),
        )

    def _local_run(self, busy, work):
        """起/停/检测都在后台线程跑（读几 GB 权重要几十秒），结果经信号回主线程。"""
        from app import local_server

        self.localStatus.setText(busy)
        self.localStartButton.setEnabled(False)
        self.localStopButton.setEnabled(False)
        models = self._local_models()

        def runner():
            try:
                mgr = local_server.manager(models)
                mgr.start_reaper()
                text = work(mgr)
            except Exception as exc:
                text = f"{T('失败')}: {exc}"
            self._local.done.emit(str(text))

        threading.Thread(target=runner, daemon=True).start()

    def _local_done(self, text):
        self.localStartButton.setEnabled(True)
        self.localStopButton.setEnabled(True)
        self.localStatus.setText(text)

    def _local_start(self):
        wanted = []
        if providers.JEV_PROVIDERS[self._provider_of(self.jev)].local:
            wanted.append("clef")
        if providers.DRAFT_PROVIDERS[self._provider_of(self.draft)].local:
            wanted.append("draft")
        wanted = wanted or ["clef", "draft"]

        def work(mgr):
            for kind in wanted:
                mgr.ensure(kind)
            return mgr.describe()

        self._local_run(T("正在加载本地模型，第一次要读几 GB 权重，请稍等…"), work)

    def _local_stop(self):
        def work(mgr):
            mgr.stop_all()
            return T("已停止，显存已释放")

        self._local_run(T("正在停止…"), work)

    def _model_group(self, box, title, kind, table):
        """一组「来源 / 密钥 / 模型」控件，判断和起草各一份。table 是 core/providers.py 里那张表。"""
        group = SimpleNamespace(kind=kind, table=table, ids=list(table),
                                keyTitle="判断" if kind == "jev" else "起草",
                                stored_key=lambda k=kind: (settings.jev_key() if k == "jev"
                                                           else settings.llm_key()))
        heading = QHBoxLayout()
        heading.addWidget(_tlabel(title, 14, "#304c3c", True), 1)
        group.keyState = _label("", 12, _GREEN)
        group.keyState.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        heading.addWidget(group.keyState)
        box.addLayout(heading)
        source_label = _tlabel("来源", 13)
        box.addWidget(source_label)
        group.providerBox = ComboBox()
        group.providerBox.setMinimumWidth(0)  # 选项文字长短不一，别让它撑开设置页
        group.providerBox.addItems([table[i].name for i in group.ids])
        bind(group.providerBox, lambda: f"{T(title)} · {T('来源')}", "setAccessibleName")
        source_label.setBuddy(group.providerBox)
        box.addWidget(group.providerBox)
        if kind == "draft":  # 只有两个「自定义」来源要自己填地址，别的来源这一行藏着
            self.baseLabel = _label("Base URL", 13)
            box.addWidget(self.baseLabel)
            self.baseEdit = LineEdit()
            bind(self.baseEdit, "https://你的服务/v1", "setPlaceholderText")
            bind(self.baseEdit, "自定义来源 Base URL", "setAccessibleName")
            self.baseLabel.setBuddy(self.baseEdit)
            box.addWidget(self.baseEdit)
        key_label = _tlabel("密钥", 13)
        box.addWidget(key_label)
        group.keyEdit = PasswordLineEdit()
        bind(group.keyEdit, lambda: f"{T(title)} · {T('密钥')}", "setAccessibleName")
        key_label.setBuddy(group.keyEdit)
        group.keyEdit.returnPressed.connect(self._save)
        box.addWidget(group.keyEdit)
        group.keyLabel, group.keyEdit = key_label, group.keyEdit
        group.keyHint = self._hint(
            "OpenRouter 的 key 或 TypeSafe 的 key，看上面选的来源。" if kind == "jev"
            else "上面选哪家就填哪家的 key；换来源重填一次，只存这一把。")
        box.addWidget(group.keyHint)
        model_label = _tlabel("模型", 13)
        box.addWidget(model_label)
        row = QHBoxLayout()
        row.setSpacing(8)
        group.modelBox = EditableComboBox()  # 能选也能手打，接口新出的模型不用等我改代码
        group.modelBox.setMinimumWidth(0)
        bind(group.modelBox, lambda: f"{T(title)} · {T('模型')}", "setAccessibleName")
        model_label.setBuddy(group.modelBox)
        row.addWidget(group.modelBox, 1)
        group.fetchButton = bind(PushButton(""), "获取模型")
        bind(group.fetchButton, lambda: f"{T(title)} · {T('获取模型')}", "setAccessibleName")
        group.fetchButton.clicked.connect(lambda: self._fetch_models(group))
        row.addWidget(group.fetchButton)
        box.addLayout(row)
        group.status = _label("", 12, _MUTED)
        box.addWidget(group.status)
        group.providerBox.currentIndexChanged.connect(lambda _: self._provider_changed(group))
        return group

    @staticmethod
    def _provider_of(group):
        return group.ids[max(0, group.providerBox.currentIndex())]

    def _provider_changed(self, group):
        """换来源：模型框回到这家该有的值（存的就是这家才用存的，否则用它的默认），状态清掉。"""
        provider = self._provider_of(group)
        saved = settings.jev_provider() if group.kind == "jev" else settings.draft_provider()
        stored = settings.jev_model() if group.kind == "jev" else settings.draft_model()
        group.modelBox.clear()
        group.modelBox.setText(stored if provider == saved else group.table[provider].default)
        bind(group.status, "", "setText")
        self._sync_model_fields()

    def _sync_model_fields(self):
        """两组共用：密钥已配置/未配置、占位文案、自定义 Base URL 行的显隐，
        外加紧凑模式下把来源按钮上的文字省略——ComboBox 是 QPushButton，
        minimumSizeHint 按整段文字算，不会自动换行/省略，长名字会把设置页撑宽。"""
        for group in (self.jev, self.draft):
            provider = self._provider_of(group)
            spec = group.table[provider]
            name = spec.name
            local = bool(getattr(spec, "local", False))
            configured = bool(group.stored_key())
            bind(group.keyState, ("无需密钥" if local else
                                  ("已配置" if configured else "未配置")))
            for widget in (group.keyLabel, group.keyEdit, group.keyHint):
                widget.setVisible(not local)
            if not local:
                bind(group.keyEdit, "已配置，留空保留" if configured else
                     lambda name=name: T("输入 {name} API 密钥").format(name=name), "setPlaceholderText")
            if self._compact:
                name = group.providerBox.fontMetrics().elidedText(name, Qt.ElideRight, 180)
            group.providerBox.setText(name)
        custom = self._provider_of(self.draft) in providers.CUSTOM
        self.baseLabel.setVisible(custom)
        self.baseEdit.setVisible(custom)

    def _fetch_models(self, group):
        """「获取模型」：拿填的 key（没填就拿存的）去问接口，网络调用丢后台线程。
        选的是本地来源就跳过 key，直接问本地 llama-server（服务没起来就顺手拉起来）。"""
        provider = self._provider_of(group)
        spec = group.table[provider]
        local = bool(getattr(spec, "local", False))
        custom = group.kind == "draft" and provider in providers.CUSTOM
        base = self.baseEdit.text().strip() if custom else None
        key = group.keyEdit.text().strip() or group.stored_key()
        if not local and not key:
            bind(group.status, "先填密钥", "setText")
            return
        if custom and not base:
            bind(group.status, "先填 Base URL", "setText")
            return
        bind(group.status, "获取中…", "setText")
        group.fetchButton.setEnabled(False)
        threading.Thread(target=lambda: self._list_models(group, provider, key, base),
                         daemon=True).start()

    def _list_models(self, group, provider, key, base):
        """后台线程：判断走 jev_client，起草按协议走 llm；失败把原因一起送回主线程。
        本地来源先把 llama-server 拉起来，再问它的 /v1/models（llama-server 不校验 key）。"""
        try:
            spec = group.table[provider]
            if getattr(spec, "local", False):
                from app import local_server

                models = self._local_models_list(group.kind, provider)
            elif group.kind == "jev":
                models = jev_client.list_models(provider, key)
            else:
                models = llm.list_models(spec.protocol, base or spec.base, key, headers=spec.headers)
                if spec.keep:  # 目录里混了别的协议时，只留这条路打得通的
                    models = [m for m in models if spec.keep(m)]
            reason = "" if models else "这个来源没返回任何模型"
        except Exception as exc:  # 线程里漏异常会静默吞掉，按钮就永远停在禁用态
            models, reason = [], str(exc)[:120]
        self._fetched.done.emit(group, models, reason)

    @staticmethod
    def _local_models_list(kind, provider):
        """本地来源：确保 llama-server 在跑，再列它 /v1/models 里的 id。"""
        from app import local_server

        mgr = local_server.manager()
        code = "clef" if kind == "jev" else "draft"
        base = mgr.ensure(code)
        spec = providers.JEV_PROVIDERS[provider] if kind == "jev" else providers.DRAFT_PROVIDERS[provider]
        try:
            ids = llm.list_models(spec.protocol, base, "", timeout=10)
        except Exception:
            ids = []
        return ids or [spec.default]

    def _models_fetched(self, group, models, reason):
        """回到主线程：填进下拉框，原来选中的还在列表里就留着。"""
        group.fetchButton.setEnabled(True)
        if not models:
            bind(group.status, reason or "获取失败，检查密钥、网络或 Base URL")
            return
        current = group.modelBox.text().strip()
        group.modelBox.clear()
        group.modelBox.addItems(models)
        if current in models:
            group.modelBox.setCurrentIndex(models.index(current))
        else:
            group.modelBox.setText(current)  # 手打的没在列表里也不清掉
        bind(group.status, lambda: T("共 {count} 个").format(count=len(models)))

    def _set_group(self, group, provider, model):
        """把存下来的来源和模型放回一组控件里；填充不算用户操作，别触发换来源的重置。"""
        group.providerBox.blockSignals(True)
        group.providerBox.setCurrentIndex(group.ids.index(provider))
        group.providerBox.blockSignals(False)
        group.keyEdit.clear()
        group.modelBox.clear()
        group.modelBox.setText(model)
        bind(group.status, "", "setText")

    def _load_settings(self):
        relationship = settings.relationship()
        index = next((i for i, (_, value) in enumerate(_RELATIONSHIPS) if value == relationship),
                     len(_RELATIONSHIPS) - 1)
        self.relationshipBox.setCurrentIndex(index)
        self.relEdit.setText(relationship if _RELATIONSHIPS[index][1] is None else "")
        self.relEdit.setVisible(_RELATIONSHIPS[index][1] is None)
        self.styleEdit.setText(settings.style())
        self.contextBox.setValue(settings.context())
        self.targetSwitch.setChecked(settings.reply_target())
        self._set_group(self.jev, settings.jev_provider(), settings.jev_model())
        self._set_group(self.draft, settings.draft_provider(), settings.draft_model())
        self.baseEdit.setText(settings.draft_base_url())
        self.thinkingSwitch.setChecked(settings.thinking())
        self.updateSwitch.setChecked(settings.check_update())
        self.set_debug_switch(settings.debug_view())  # 屏蔽信号地拨，别在加载时开关一遍窗口
        self.localServerEdit.setText(settings.local_llama_server())
        self.localDirEdit.setText(settings.local_models_dir())
        self.localClefEdit.setText(settings.local_clef_model())
        self.localDraftEdit.setText(settings.local_draft_model())
        self.localAutoSwitch.setChecked(settings.local_autostart())
        self.localUnloadSwitch.setChecked(settings.local_idle_unload())
        self.qqFocusBox.setCurrentIndex(0 if settings.qq_focus_policy() == "steal" else 1)
        self.groupOnlySwitch.setChecked(settings.group_judge_only())
        self._sync_model_fields()  # 上面屏蔽了信号，这里补一次
        self.settingsFeedback.hide()

    def _save(self):
        relationship = _RELATIONSHIPS[self.relationshipBox.currentIndex()][1]
        relationship = relationship or self.relEdit.text().strip()
        jev_provider = self._provider_of(self.jev)
        draft_provider = self._provider_of(self.draft)
        base = self.baseEdit.text().strip()
        if not relationship:
            self._settings_feedback("请填写关系背景，或选择一个已有选项。", error=True)
            self.relEdit.setFocus()
            return
        if draft_provider in providers.CUSTOM and not base:
            self._settings_feedback("自定义来源要填 Base URL。", error=True)
            self.baseEdit.setFocus()
            return
        for group, provider in ((self.jev, jev_provider), (self.draft, draft_provider)):
            name = group.table[provider].name
            if not getattr(group.table[provider], "local", False) \
                    and not group.keyEdit.text().strip() and not group.stored_key():
                self._settings_feedback(lambda group=group:
                                        T("请先填写 {name} 的 API 密钥。").format(name=T(group.keyTitle)), error=True)
                group.keyEdit.setFocus()
                return
            if not group.modelBox.text().strip():
                self._settings_feedback(lambda: T("{name} 请先获取并选择一个模型。").format(name=name), error=True)
                group.modelBox.setFocus()
                return
        jev_local = providers.JEV_PROVIDERS[jev_provider].local
        draft_local = providers.DRAFT_PROVIDERS[draft_provider].local
        if (jev_local or draft_local) and not (self.localDirEdit.text().strip()
                                              or self.localClefEdit.text().strip()):
            self._settings_feedback("本地模式要至少填一个模型目录，或直接指定 clef-flash GGUF。", error=True)
            self.localClefEdit.setFocus()
            return
        try:
            settings.save(relationship, self.contextBox.value(),
                          jev_provider_text=jev_provider,
                          jev_key_text=self.jev.keyEdit.text().strip() or None,
                          jev_model_text=self.jev.modelBox.text().strip(),
                          draft_provider_text=draft_provider,
                          llm_key_text=self.draft.keyEdit.text().strip() or None,
                          draft_model_text=self.draft.modelBox.text().strip(),
                          draft_base_url_text=base,
                          reply_target_on=self.targetSwitch.isChecked(),
                          style_text=self.styleEdit.text().strip(),
                          thinking_on=self.thinkingSwitch.isChecked(),
                          check_update_on=self.updateSwitch.isChecked(),
                          local_llama_server_text=self.localServerEdit.text().strip(),
                          local_models_dir_text=self.localDirEdit.text().strip(),
                          local_clef_model_text=self.localClefEdit.text().strip(),
                          local_draft_model_text=self.localDraftEdit.text().strip(),
                          local_autostart_on=self.localAutoSwitch.isChecked(),
                          local_idle_unload_on=self.localUnloadSwitch.isChecked(),
                          qq_focus_policy_text=("steal" if self.qqFocusBox.currentIndex() == 0
                                                else "foreground"),
                          group_judge_only_on=self.groupOnlySwitch.isChecked())
        except Exception:
            self._settings_feedback("保存失败，请检查配置文件是否可写后重试。", error=True)
            return
        self._load_settings()
        self._render_targets()  # 开关刚改过，回到首页时这一行该显该藏得重算一次
        self._settings_feedback("设置已保存，将用于下一次回复。")
        self.setupButton.hide()
        if not self.cands and not self._busy:
            self._empty_text()
            self.set_status("设置已就绪，等待新消息", "idle")

    def _debug_toggled(self, on):
        """调试视图独立于「保存设置」：拨一下就开窗/收窗，顺手落盘，重启还在。"""
        settings.save(debug_view_on=on)
        if self.on_toggle_debug:
            self.on_toggle_debug(on)

    def set_debug_switch(self, on):
        """调试窗被用户直接关掉时把开关拨回去；屏蔽信号，免得又回调一圈。"""
        self.debugSwitch.blockSignals(True)
        self.debugSwitch.setChecked(on)
        self.debugSwitch.blockSignals(False)

    def _settings_feedback(self, text, error=False):
        color = "#b44832" if error else _GREEN
        qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
        setCustomStyleSheet(self.settingsFeedback, qss, qss)
        bind(self.settingsFeedback, text)
        self.settingsFeedback.show()

    def open_settings(self):
        if self.pages.currentWidget() != self.settingsPage:
            self._load_settings()
        self.pages.setCurrentWidget(self.settingsPage)
        self.settingsButton.setEnabled(False)
        self.relationshipBox.setFocus()  # 把焦点从首页控件上拿走，落到设置页第一个字段
        self.settingsPage.verticalScrollBar().setValue(0)

    def _back_home(self):
        self.jev.keyEdit.clear()
        self.draft.keyEdit.clear()
        self.pages.setCurrentWidget(self.home)
        self.settingsButton.setEnabled(True)

    def _fill(self, index):
        if self._busy or not self._current or index >= len(self.cands):
            return
        try:
            self.on_fill(self.cands[index])
        except Exception as e:
            # 状态栏保持友好文案；真实原因和压缩堆栈进聊天记录，认得出是哪一步炸的
            import traceback
            self.set_status("未能填入，请确认聊天窗口可用后重试，或复制回复。", "error")
            self.log(f"[填入失败] {type(e).__name__}: {e}")
            self.log(f"[填入失败堆栈] {' '.join(traceback.format_exc().split())[:300]}")
            return
        self.set_status("已尝试填入，请确认内容后发送。", "success")

    def _copy(self, index):
        if self._busy or not self._current or index >= len(self.cands):
            return
        self.app.clipboard().setText(self.cands[index])
        self.set_status("回复已复制，可粘贴并修改。", "success")

    def _capture_toggled(self, on):
        """用户自己拨的开关：界面先改，再通知父进程去开/停采集。"""
        self._capture_text(on)
        if self.on_toggle_capture:
            self.on_toggle_capture(on)

    def set_update(self, latest, url):
        """main.py 后台线程查到比当前新的版本才会调这个。只显示版本号和 Release 链接，别的什么都没有。"""
        bind(self.updateLabel, lambda: T("有新版本 v{version}").format(version=latest))
        self.updateLink.setUrl(url)
        self.updateBar.show()

    def set_capture(self, on, reason=""):
        """父进程回报的状态：只改界面，不回调（不然和父进程来回打架）。reason 为空用默认说明。"""
        self.captureSwitch.blockSignals(True)
        self.captureSwitch.setChecked(on)
        self.captureSwitch.blockSignals(False)
        self._capture_text(on, reason)

    def _capture_text(self, on, reason=""):
        """开关状态对应的状态行和空态文案。已有的候选不受影响，暂停了照样能填入/复制。"""
        configured = settings.has_key()
        if not on:
            self.set_status(reason or "采集已暂停，聊天内容不再读取", "warning")
        elif configured:
            self.set_status("等待新消息", "idle")
        else:
            self.set_status("请先在设置中配置模型", "warning")
        if self._busy or self.cands:  # 正在生成或已有候选时，空态卡片本来就看不见
            return
        if not on:
            bind(self.emptyTitle, "采集已暂停", "setText")
            bind(self.emptyHint, "聊天内容暂时不再读取。\n打开标题栏的开关，继续接收新消息。", "setText")
            self.setupButton.setVisible(not configured)
        else:
            self._empty_text()

    def set_busy(self, busy):
        self._busy = busy
        self.progress.setVisible(busy)
        if busy:
            self.invalidate_replies()
            self.progress.start()
            self.set_status("正在根据新消息整理回复…", "busy")
            if not self.cands:
                bind(self.emptyTitle, "正在想一句合适的回复", "setText")
                bind(self.emptyHint, "正在结合上下文生成建议，稍等一下。", "setText")
                self.setupButton.hide()
        else:
            self.progress.stop()
            if not self.cands:
                self._empty_text()
        for card in self.cards:
            card.set_available(self._current and not busy)

    def _empty_text(self):
        """空态卡片的默认文案，配好没配好两套说法。"""
        configured = settings.has_key()
        bind(self.emptyTitle, "等待对方的新消息" if configured else "先设置，再开始")
        bind(self.emptyHint, "保持聊天窗口打开。\n收到新消息后，回复建议会出现在这里。"
                            if configured else "配置模型和关系背景，\n让建议更贴近你们的对话。")
        self.setupButton.setVisible(not configured)

    def invalidate_replies(self):
        self._current = False
        if self.cands:
            bind(self.updated, "上次建议", "setText")
        for card in self.cards:
            card.set_available(False)
    def set_status(self, text, kind="idle"):
        colors = {"idle": _MUTED, "busy": _GREEN, "success": _GREEN,
                  "warning": "#93611d", "error": "#b44832"}
        markers = {"idle": "●", "busy": "●", "success": "✓", "warning": "!", "error": "!"}
        qss = f"BodyLabel {{ color: {colors.get(kind, _MUTED)}; background: transparent; }}"
        setCustomStyleSheet(self.status, qss, qss)
        bind(self.status, lambda: f"{markers.get(kind, '●')}  {text() if callable(text) else T(text)}")
        if kind == "error" and self._busy:
            self.set_busy(False)
        if kind == "error" and not self.cands:
            bind(self.emptyTitle, "暂时没有可用的回复", "setText")
            bind(self.emptyHint, "请按上方提示处理。收到新的对方消息后会再次尝试。", "setText")
            self.setupButton.setVisible(not settings.has_key())

    def _toggle_history(self):
        self.feed.setVisible(self.feed.isHidden())
        self._history_title()

    def _history_title(self):
        action = "展开聊天记录" if self.feed.isHidden() else "收起聊天记录"
        count = self.counts.get(self._shown, 0)
        bind(self.historyButton, lambda: T(action) + (f" · {count}" if count else ""))

    @staticmethod
    def _log_text(entry):
        if isinstance(entry, str):  # 诊断信息保持原文
            return entry
        who, name, text, timestamp = entry
        speaker = (name or T("对方")) if who == "her" else T("我")
        return f"{timestamp}  {speaker}\n{text}\n"

    def _refresh_history_text(self):
        bar = self.feed.verticalScrollBar()
        position = bar.value()
        follow = position >= bar.maximum() - 4
        self.feed.setPlainText("\n".join(self._log_text(entry) for entry in self._feed_entries))
        bar.setValue(bar.maximum() if follow else position)

    def log(self, line):
        """采集状态行：只进正在看的那个会话，不按会话存。"""
        bar = self.feed.verticalScrollBar()
        follow = self.feed.isHidden() or bar.value() >= bar.maximum() - 4
        self._feed_entries.append(line)
        del self._feed_entries[:-_LOG_LINES]
        self.feed.appendPlainText(self._log_text(line))
        if follow:
            bar.setValue(bar.maximum())

    def log_message(self, who, text, name="", timestamp=None, chat=None):
        """按会话存一份；只有正在看的那个会往显示区里写。"""
        chat = chat or self._shown
        timestamp = timestamp or datetime.now().strftime("%H:%M")
        self.counts[chat] = self.counts.get(chat, 0) + 1
        lines = self.feeds.setdefault(chat, [])
        lines.append((who, name, text, timestamp))
        del lines[:-_LOG_LINES]
        if who == "her":
            self.hers[chat] = text
        self._add_chat(chat)
        if chat != self._shown:
            return
        self.log(lines[-1])
        if who == "her":
            self._show_latest(text)
        self._history_title()

    def _show_latest(self, text):
        self.latest.setText(text if len(text) <= 120 else text[:120] + "…")
        self.latest.setToolTip(text)
        self.context.show()

    def current_chat(self):
        """界面上正在看的会话（不一定是微信当前开着的那个）。"""
        return self._shown

    def set_chat(self, title):
        """微信切到了哪个会话：登记进下拉框并自动跟过去，不触发用户选择的回调。"""
        if not title:
            return
        browsing = self._shown != self._chat  # 正看着的就是它、但之前是「浏览中」：也得重画，把填入放开
        self._chat = title
        self._add_chat(title)
        if title != self._shown or browsing:
            self.chatBox.blockSignals(True)
            self.chatBox.setCurrentIndex(self.chatBox.findText(title))
            self.chatBox.blockSignals(False)
            self._switch_to(title)
        self._follow_text()

    def _add_chat(self, title):
        """新会话自动进下拉框；addItem 添第一条时会自己选中，别让它触发切换。"""
        if not title or self.chatBox.findText(title) >= 0:
            return
        self.chatBox.blockSignals(True)
        self.chatBox.addItem(title)
        self.chatBox.blockSignals(False)

    def _on_chat_selected(self, index):
        """用户自己挑了一个会话：只换看的内容，微信那边不动。"""
        title = self.chatBox.itemText(index)
        if title and title != self._shown:
            self._switch_to(title)

    def _switch_to(self, title):
        """换正在看的会话：记录、对方最近说、条数、上次的建议一起换过去。"""
        self._shown = title
        self._feed_entries = list(self.feeds.get(title, []))
        self._refresh_history_text()
        self.feed.verticalScrollBar().setValue(self.feed.verticalScrollBar().maximum())
        her = self.hers.get(title)
        if her:
            self._show_latest(her)
        else:
            self.context.hide()
        self._history_title()
        self._follow_text()
        self._render_targets()
        self.show_cached(self.result_of(title) if self.result_of else None)

    def set_targets(self, chat, senders, current):
        """某个会话的发言人名单（最近的在前）和当前回复对象；正看着它才重画。"""
        self.targets[chat] = (list(senders), current)
        if chat == self._shown:
            self._render_targets()

    def _render_targets(self):
        """开关关着、或这个会话没有发言人（单聊），这一行就不出现。
        重填下拉框时屏蔽信号，别把自己的填充当成用户挑的。"""
        senders, current = self.targets.get(self._shown, ([], None))
        visible = bool(senders) and settings.reply_target()
        self.targetRow.setVisible(visible)
        if not visible:
            return
        self.targetBox.blockSignals(True)
        self.targetBox.clear()
        self.targetBox.addItems(senders)
        self.targetBox.setCurrentIndex(senders.index(current) if current in senders else 0)
        self.targetBox.blockSignals(False)

    def _on_target_selected(self, index):
        """用户挑了回复对象。浏览别的会话时改的就是那个会话的对象——记录、候选也都按会话走，口径一致。"""
        name = self.targetBox.itemText(index)
        if not name:
            return
        senders, _ = self.targets.get(self._shown, ([], None))
        self.targets[self._shown] = (senders, name)
        self.set_status(lambda: T("按「{name}」重新生成…").format(name=name), "busy")
        if self.on_target_change:
            self.on_target_change(self._shown, name)

    def at_prefix_enabled(self):
        """填入时要不要带「@名字 」前缀（只记在界面上，不落盘）。"""
        return self.atCheck.isChecked()

    def _follow_text(self):
        bind(self.chatFollow, ("跟随" if self._shown == self._chat else "浏览中") if self._chat else "")

    def show_cached(self, result):
        """把某个会话上次的结果放回界面；没有就回到空态。浏览别的会话时只给看不给填——
        微信当前开着的不是它，填进去就串会话了。"""
        if result:
            self.show(result)
        else:
            self.cands = []
            self._clear_cards()
            self.insight.hide()
            self.referenceNote.hide()
            self.empty.show()
            bind(self.headTitle, T("回复建议"), "setText")   # 群聊判断看过的，回单聊要改回来
            bind(self.updated, "", "setText")
            self._empty_text()
        if self._shown != self._chat:
            self.invalidate_replies()
            self.set_status(lambda: T("正在浏览「{name}」，只看不填；切回这个会话才能用。").format(name=self._shown))

    def show(self, result):
        """按推荐顺序展示，按钮始终绑定 candidates 的原始索引。

        result["mode"] == "judge_only" 时没有候选（群聊只判断）：只展示判断摘要，
        把空态卡藏起来，并说明为什么不起草。
        result["mode"] == "group" 时是按发言人的并发判断：一人一张卡，没有候选。"""
        if result.get("mode") == "group":
            rows = result.get("speakers") or []
            bind(self.headTitle, T("群聊判断"), "setText")
            self.set_busy(False)
            self.show_group([r["name"] for r in rows], {r["name"]: r for r in rows})
            return
        bind(self.headTitle, T("回复建议"), "setText")
        judge_only = result.get("mode") == "judge_only"
        self.cands = result["candidates"]
        self.set_busy(False)
        self._current = bool(self.cands)
        self._clear_cards()
        best = result.get("best_index", 0)
        if best not in range(len(self.cands)):
            best = 0
        raw_scores = result.get("scores") or []
        scores = [raw_scores[i] if i < len(raw_scores) else None for i in range(len(self.cands))]
        if not any(scores):  # 全 0/None（旧结果或接口未返回）就不展示百分比
            scores = [None] * len(self.cands)
        # 按概率降序排，推荐位（API 给的 choice）强制第一，同分按原索引
        order = sorted(range(len(self.cands)), key=lambda i: (i != best, -(scores[i] or 0), i))
        for position, index in enumerate(order):
            card = _ReplyCard(self, index, recommended=index == best, number=position, score=scores[index])
            self.replyBox.addWidget(card)
            self.cards.append(card)
        reply_to = result.get("reply_to")
        bind(self.insightTitle, lambda: T("对话参考 · 回复给 {name}").format(name=reply_to)
             if reply_to else T("对话参考"))
        answers = result.get("answers") or {}
        bind(self.summary, lambda: T("建议：") + _choice(answers, "best_action"))
        bind(self.intent, lambda: T("可能意图 · ") + _choice(answers, "true_intent") +
                                 "\n" + T("可能需要 · ") + _choice(answers, "she_needs"))
        score = (answers.get("danger_level") or {}).get("score")
        valid_score = isinstance(score, (int, float)) and isfinite(score) and 0 <= score <= 9
        bind(self.tension, lambda: f"{T('紧张度')} {score:.0f}/9" if valid_score else T("紧张度待判断"))
        color = "#996819" if valid_score and score >= 3 else _MUTED
        if valid_score and score >= 6:
            color = "#b44832"
        qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
        setCustomStyleSheet(self.tension, qss, qss)
        self.empty.setVisible(not self.cands and not judge_only)
        self.insight.setVisible(bool(self.cands) or judge_only)
        self.referenceNote.setVisible(bool(self.cands) and not self._compact)
        updated_at = datetime.now().strftime("%H:%M")
        bind(self.updated, lambda: updated_at + " " + T("更新"))
        if self.cands:
            self.set_status("建议已更新，选一句适合你的回复", "success")
        elif judge_only:
            self.set_status(lambda: T("群聊只给判断参考：群里可能同时在聊几件不相关的事，"
                                      "一条候选回复没有意义，所以不起草。"), "idle")
        else:
            self.set_status("未生成可用回复，请等待下一条新消息。", "error")

    def _clear_cards(self):
        """清掉候选卡和群聊判断卡（两者共用 replyBox，不会同时存在）。"""
        for card in self.cards + self.speakerCards:
            self.replyBox.removeWidget(card)
            card.hide()
            card.deleteLater()
        self.cards = []
        self.speakerCards = []

    def show_group(self, names, results=None):
        """群聊：一人一张判断卡。names = 这一轮要判断的人（最近说话的排前），results = 已回来的。

        哪路先回就先把它那张填上（其余还是「正在判断…」），所以这个函数会被反复调；
        每次重建：一共就几张卡，重建比精确更新好写也好读。
        """
        results = results or {}
        self.cands = []
        self._clear_cards()
        for name in names:
            item = results.get(name)
            card = _SpeakerCard(self, name, item=item,
                                error=(item or {}).get("error", "") if item else "")
            self.replyBox.addWidget(card)
            self.speakerCards.append(card)
        bind(self.headTitle, T("群聊判断"), "setText")
        self.insight.hide()                 # 整体判断那块对群聊不适用，每张卡自带判断
        self.referenceNote.setVisible(bool(names) and not self._compact)
        self.empty.setVisible(not names)
        self._current = False               # 群里没有「填入」这回事
        bind(self.updated, lambda: datetime.now().strftime("%H:%M") + " " + T("更新"))
        if names:
            done = sum(1 for n in names if results.get(n))
            self.set_status(lambda: T("群聊按发言人分别判断（不起草）：").format()
                            + f"{done}/{len(names)}" + T(" 已回"),
                            "success" if done == len(names) else "busy")
        else:
            self.set_status("群里还没有带名字的消息", "idle")

    def after(self, ms, fn):
        QTimer.singleShot(ms, fn)

    def run(self):
        self.app.exec()
