# QQ NT（Windows）适配：UIA

> **状态：已实现。** 采集器在 `app/collectors/uia_qqnt.py`，纯逻辑在 `app/collectors/uia_map.py`，
> 选择器与三条坑位写在下面。**本项目只走 UIA，不做像素/OCR 识别 QQ。**
>
> 最后更新：2026-10。QQ NT `9.9.36-53644` / `9.9.31-49738`。

## 1. 结论摘要

1. **上游和所有 fork 都没有 QQ NT 支持。** `jev-chat/jev-chat-windows` 全部 11 个近期有提交的 fork，
   `app/chatapps.py` 里一个 QQ 都没有；上游只维护微信这一套。Android 版
   （`Finderchangchang/jev-chat-JARVIS`）**支持 QQ**（读无障碍节点），所以判断口径和题目集是现成的。
2. **QQ NT 走 UIA 无障碍树**。社区文档给的 CSS 类名（`qq-msg-editor` / `container--self`）
   在本机 QQ 9.9.31/9.9.36 上**已经查不到了**，但它们留了两个可用的东西：
   - **结构 id还在**：`ml-root` 就是消息列表（实测子树 386 个节点 / 134 条文本），`func-bar-*` 是输入区工具栏；
   - **几何还在**：文本节点带真实矩形，输入区是消息列表下面那块**整宽**容器
     （实测 `(832,956,1880,1095)`，里面装着草稿文字）。

   所以采集器是**双路径**：类名在就用类名（更准），不在就用 `ml-root` + 几何，
  己方/对方靠**左右对齐**（自己发的靠右）。两条都是读无障碍树，**不 OCR、不比像素**。
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

## 4. 选择器：类名优先，ID + 几何兜底

实测（QQ 9.9.36）：无障碍树里 `ClassName` 几乎全是空字符串，`AutomationId` 只在少数元素上。
所以采集器按这个顺序找：

| 要什么 | 首选（社区类名） | 本机实际走的那条 |
| --- | --- | --- |
| 消息列表 | 气泡类名 `message-container*` 的公共祖先 | **`id=ml-root`**（实测 rect `(832,325,1880,907)`） |
| 输入区 | `qq-msg-editor` | 消息列表下方、聊天区内、**横跨整宽**的容器（优先取“里面含文字”那块）；再把高度砍到上 60% 再把矩形交给父进程，保证点到打字区而不是工具栏 |
| 会话名 | `chat-header__contact-name` | 消息列表上方 80px 内、最靠左的那条文本（实测 len=12；不会再抓到标题栏那个 1 字符按钮） |
| 消息 | 气泡容器 + `container--self` | `ml-root` 里按 y 聚行的文本；行中心在列表中线右侧 = 我发的 |
| 发言人名（群聊） | 类名含 `nickname`/`sender` | 类名路径下靠“紧贴气泡上方的短文本”；几何路径下拿不到（留空） |

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
app/collectors/uia_map.py    纯逻辑：树 → Node → 消息列表/输入区/会话名/消息行/左右对齐/去重
                             （不依赖 uiautomation，可完全离线自测）
app/collectors/uia_qqnt.py   活体采集器：抓树、前台策略、发事件（chat/area/input/lines/status）
app/chatapps.py              QQNT 条目：exes=("qq.exe",)、collector="uia"
app/settings.py              qq_focus_policy（steal|foreground）、qq_selectors（覆盖选择器）
```

**无头自测（不需要 QQ、不需要 uiautomation、不开任何窗口）**：

```bat
python -m app.collectors.uia_map      :: 合成树：类名世界 / 真实 ml-root 结构 / 空树 / 去重 / 换会话
                                      ::   + 6 种尺寸用例：125%、100%、最大化、最大化@150%、窄窗口、会话列表拖宽
python -m app.collectors.uia_qqnt     :: 假 uiautomation：事件流转、去重、换会话、两条提示、
                                      ::   前台策略（人在打字时不抢焦点）、读树限速
```

**实机多状态实测**（QQ 9.9.36，会话开着；采完把窗口位置/大小还原）：

| 窗口状态 | 窗口尺寸 | 会话名 | 输入区 | 读到消息 |
| --- | --- | --- | --- | --- |
| 普通（125% 缩放） | 1660x960 | len=12 ✓ | `(832,906,1880,1061)` | 7 条（1 我 / 6 对方） |
| **最大化** | 2578x1398 | len=12 ✓ | `(382,1116,2330,1271)` | 16 条（2 我 / 14 对方） |
| 拉伸到 1100x820 | 1100x820 | len=12 ✓ | `(500,726,990,809)` | 10 条（1 我 / 9 对方） |
| 拉伸到 1920x1080 | 1920x1080 | len=12 ✓ | `(502,936,1810,1091)` | 13 条（1 我 / 12 对方） |

会话名在四种状态下都是同一个（len=12）、输入区跟着窗口缩放、消息数随可见区域变化——
说明定位规则是相对的，没有写死像素。最大化时能看到 2 条「我发的」，也顺便验证了左右对齐判己方。

**聚焦策略（不能每拍都抢焦点）**：读一次树要几百 ms + 等建树 0.8s，所以

- 读树**限速**：两次之间至少 `qq_poll_seconds`（默认 1.2s）；
- `steal` 模式下用 `GetLastInputInfo` 看键鼠空闲时长：人刚动过（< `qq_user_grace_seconds`，默认 1.5s）
  **就不抢**，等下一拍——不会在你打字时把焦点抢走。

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
| `qq_focus_policy` | `steal` | `steal` = 读取时短暂置前、读完还焦点（人正在打字时不抢）；`foreground` = 只在 QQ 前台时读 |
| `qq_selectors` | `{}` | 覆盖选择器（QQ 大版本改类名时才需要）：`{"message_list": ["ml-root"], "editor": [...], "header": [...], "bubble": [...], "self_mark": [...], "sender": [...]}` |
| `qq_poll_seconds` | `1.2` | 两次读树的最小间隔（秒） |
| `qq_user_grace_seconds` | `1.5` | 人在打字/动鼠标的这段时间内不抢焦点（秒，0 = 关掉） |

设置页「QQ NT（UIA 读取）」卡片里可以改 `qq_focus_policy`。

## 8. 手册（uiautomation / Microsoft UIA）里需要注意的几条

- **Electron/Chrome 必须 `--force-renderer-accessibility`**：官方 README 明确写了「Chrome 和基于
  Electron 的应用需要加这个启动参数才支持 UIAutomation」，不加就只能看到最外层
  `PaneControl Chrome_WidgetWin_1`。本项目实测完全一致（不加：9 个节点；加了：900 左右）。
- **非管理员跑 uiautomation 会出 COMError**：官方 README 说 Win7+ 建议以管理员运行 Python，
  否则很多调用会失败。本项目不要求管理员：QQ 也是普通权限，同级完整性就能读；
  **但 QQ 若以管理员启动就会读不到**（这是 Windows 的 UIPI 限制，不是本项目的问题）。
- **布局变化后元素会失效，必须重新查找**：官方例子用 `Refind()`/`Exists()`。本采集器**每次 poll 都
  `ControlFromHandle(hwnd)` 重建根节点**，所以拉伸/最大化/切会话后拿到的都是新的坐标——
  实测四种窗口状态下都能定位（见第 5 节）。
- **`BoundingRectangle` 是物理像素**：所以进程做过 `SetProcessDPIAware()` 才与截图/点击坐标一致；
  采集子进程和父进程都调了。跨不同缩放比例的显示器拖动窗口时坐标会变，因为每拍重读，所以能跟上。

## 9. 已知限制

- **QQ 必须在前台**（或允许短暂置前）才能读；QQ 最小化时读不到。
- 群聊发言人名靠「紧贴气泡上方的短文本」推断，气泡列表被虚拟化时可能挂到上一个人头上；
  单聊不受影响（没有名字）。
- 群聊置顶公告、合并转发卡片这类非气泡节点走的是同一套类名，遇到读串了请在
  `config.json` 里用 `qq_selectors` 收紧类名。
- QQ 大版本更新后类名可能变。认不出时采集器只报**可诊断的 status**（提示用 `probe_qq_uia.py`
  抓树来对），不会静默读错、也不会退回 OCR。
- QQ 自带截图 / Mojo IPC（`EEEEhex/QQImpl` ⭐383 那套）**不用**：依赖 QQ 私有实现，
  版本一变就废，也模糊了「不碰对方进程」的边界。
