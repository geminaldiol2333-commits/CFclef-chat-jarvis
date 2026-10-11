# QQ NT（Windows）适配：UIA

> **状态：已实现。** 采集器在 `app/collectors/uia_qqnt.py`，纯逻辑在 `app/collectors/uia_map.py`，
> 选择器与三条坑位写在下面。**本项目只走 UIA，不做像素/OCR 识别 QQ。**
>
> 最后更新：2026-10。QQ NT `9.9.36-53644` / `9.9.31-49738`。

## 1. 结论摘要

1. **上游和所有 fork 都没有 QQ NT 支持。** `jev-chat/jev-chat-windows` 全部 11 个近期有提交的 fork，
   `app/chatapps.py` 里一个 QQ 都没有；上游只维护微信这一套。Android 版
   （`Finderchangchang/jev-chat-JARVIS`）**支持 QQ**（读无障碍节点），所以判断口径和题目集是现成的。
2. **QQ NT 走 UIA 无障碍树**：输入框 `qq-msg-editor`、头部联系人 `chat-header__contact-name`、
   气泡 `message-container*` / `msg-content-container*`、**己方标记 `container--self`**。
   己方/对方直接读类名——不用比颜色、不用 OCR、不受主题和 DPI 影响。
3. **硬前提：QQ 只对前台窗口构建完整 UIA 树**（实测后台只有 9 个节点）。所以读消息前必须让 QQ
   处于活动状态。本项目给了两种策略（设置页可选，默认第一种）：
   - **允许读取时短暂置前**（默认）：置前 + 等 800ms 建树 → 读完把焦点还给原来的窗口。屏幕会闪一下。
   - **只在 QQ 已在前台时读取**：完全不抢焦点，但切走就停更。

## 2. 我第一轮测错过（记录一下，免得重犯）

第一轮我启动 QQ 后直接抓树，**没有点开任何会话**，右侧聊天区是空的，于是得出「类名不存在、
聊天内容不在无障碍树里」——这个结论**是错的，已作废**。当时的观测（389 个节点里 ClassName 全空、
唯一 `EditControl` 是左栏搜索框、文本节点没有几何信息）都属于「没开会话」这个前提下的必然结果。

仍然成立的两条：**后台只有 9 个节点**、**要 `--force-renderer-accessibility` 才能把树建全**
（无 GPU 设备尤其需要；`QQSafeChat` 记录的「只能选中整个窗口」就是同一个现象）。

## 3. 参考项目

| 项目 | 语言/许可 | 价值 |
| --- | --- | --- |
| [`treers2/qq-operations`](https://github.com/treers2/qq-operations) ⭐2 | PowerShell / MIT | **最重要的一份**。`scripts/qq_lib.ps1` 里有完整类名、双界面模式识别、前台重建（`Revive-QQPanel`）、收件人校验 |
| [`TheD0ubleC/QQSafeChat`](https://github.com/TheD0ubleC/QQSafeChat) ⭐156 | Python / MIT | UIA 绑定「消息列表 / 输入框 / 发送按钮」，带 `uia_debug.py` 拾取控件；零注入、不抓协议、不扫内存 |
| [`yinkaisheng/Python-UIAutomation-for-Windows`](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows) ⭐3585 | Python / Apache-2.0 | 本项目用的 UIA 封装；README 明确写了 Electron/Chrome 需要 `--force-renderer-accessibility` |

**不采用**：`NapCatQQ` ⭐10907 / `LuckyLilliaBot` ⭐3653 / `Lagrange.Core` ⭐2992 /
`LiteLoaderQQNT` ⭐7071 及其插件生态 / `NtQQHook` / `NTQQAntiRecall` —— 这些是协议侧或往 QQ 里注入，
违反本项目「不 hook、不注入、不碰对方进程/数据库」的硬约束。

## 4. 选择器与三条坑位

| 用途 | 选择器 | 本项目怎么用 |
| --- | --- | --- |
| 输入框 | `qq-msg-editor` | 取它的 `BoundingRectangle`（屏幕坐标）→ `emit("input", rect)`，填入时点它中心 |
| 头部联系人 | `chat-header__contact-name` | 当会话名（`emit("chat", name)`）；**不看窗口标题** |
| 气泡 | 正则 `^(message-container\|msg-content-container)` | 每个容器一个 Turn，文本取 `Name`，为空则拼子节点 |
| 己方 | `container--self` | 命中 = `who="me"`，否则 `"her"` |
| 发言人名（群聊） | 类名含 `nickname`/`sender`/`member-name`；没有类名时取紧贴气泡上方的短文本 | 排除头部联系人/输入框/气泡本身，并要求几何上紧邻 |

三条必须处理的坑：

1. **只对前台窗口建树**：读之前 `ShowWindow(9)` + `SetForegroundWindow` + **等 800ms** + 重试；
   读完把焦点还回去。代码在 `UiaQqntCollector._bring_forward/_restore_foreground`。
2. **双界面模式**：效率模式（聊天内嵌在主面板，标题 `QQ`）和经典模式（一个会话一个独立窗口，
   标题是联系人名）。窗口挑选交给 `app/chatapps.py`：`main_title=""` + `skip_titles=("QQ",)` ——
   经典模式跳掉主面板选真正在聊的窗口，效率模式只有一个窗口时兜底选它。
3. **多会话合并窗口标题只显示激活的那个**（`张三等2个会话`），特殊字符还会显示成 `?`
   → 会话名一律取 `chat-header__contact-name`。

## 5. 实现与验证

```
app/collectors/uia_map.py    纯逻辑：树 → Node → 会话名/输入框/气泡/己方/顺序/去重（不依赖 uiautomation）
app/collectors/uia_qqnt.py   活体采集器：抓树、前台策略、发事件（chat/area/input/lines/status）
app/chatapps.py              QQNT 条目：exes=("qq.exe",)、collector="uia"
app/settings.py              qq_focus_policy（steal|foreground）、qq_selectors（覆盖类名）
```

**无头自测（不需要 QQ、不需要 uiautomation、不开任何窗口）**：

```bat
python -m app.collectors.uia_map      :: 合成树：类名可用 / 只有几何 / 空树 / 去重 / 换会话
python -m app.collectors.uia_qqnt     :: 假 uiautomation 模块：事件流转、去重、换会话、两条提示、前台策略
```

**对着真实 QQ 校准（只读，不会点击/切换/截图、不含消息文本）**：

```bat
:: 先自己打开 QQ、点开一个会话、让 QQ 保持前台上
python probe\probe_qq_uia.py --dump qq_tree.json
python probe\qq_uia_verdict.py qq_tree.json      :: 三条轴：editor / bubbles / self 各是 strong/weak/none
```

`qq_uia_verdict.py` 判 `uia-unusable` 就说明**这份 dump**（多半是没开会话或没在前台）不能用来驱动；
判 `uia-usable`/`uia-partial` 就说明选择器能用。它自己也有合成数据自测：
`python probe\qq_uia_verdict.py --self-test`。

## 6. 配置

`config.json`（不进仓库）。改完重启生效：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `qq_focus_policy` | `steal` | `steal` = 读取时短暂置前、读完还焦点；`foreground` = 只在 QQ 前台时读 |
| `qq_selectors` | `{}` | 覆盖选择器（QQ 大版本改类名时才需要）：`{"editor": ["qq-msg-editor"], "header": [...], "bubble": [...], "self_mark": [...], "sender": [...]}` |

设置页「QQ NT（UIA 读取）」卡片里可以改 `qq_focus_policy`。

## 7. 已知限制

- **QQ 必须在前台**（或允许短暂置前）才能读；QQ 最小化时读不到。
- 群聊发言人名靠「紧贴气泡上方的短文本」推断，气泡列表被虚拟化时可能挂到上一个人头上；
  单聊不受影响（没有名字）。
- 群聊置顶公告、合并转发卡片这类非气泡节点走的是同一套类名，遇到读串了请在
  `config.json` 里用 `qq_selectors` 收紧类名。
- QQ 大版本更新后类名可能变。认不出时采集器只报**可诊断的 status**（提示用 `probe_qq_uia.py`
  抓树来对），不会静默读错、也不会退回 OCR。
- QQ 自带截图 / Mojo IPC（`EEEEhex/QQImpl` ⭐383 那套）**不用**：依赖 QQ 私有实现，
  版本一变就废，也模糊了「不碰对方进程」的边界。
