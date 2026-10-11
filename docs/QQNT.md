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
| `qq_backfill_messages` | `30` | 切会话后自动往上翻、最多补齐多少条历史（0 = 只读当前可见的） |
| `qq_backfill_scrolls` | `8` | 回填时最多往上滚几格（每格 3 个滚轮刻度） |
| `group_per_speaker` | `true` | 群聊按发言人**分别**判断，一人一张卡（关掉就还像从前那样把整个群当一段判断） |
| `group_speaker_workers` | `3` | 按发言人判断时最多几路并发（1–8）。本地 clef 在服务端串行，调大只让「谁先回来先上屏」更顺 |
| `group_speaker_max` | `4` | 一次最多给几个人做判断（1–12），只挑最近说过话的 |
| `group_judge_only` | `true` | 群聊不起草候选回复（关掉且没开按发言人时，群聊会像单聊那样起草） |
| `local_clef_mmproj` | `""` | clef 的多模态投影器（mmproj）路径；填了 llama-server 会带 `--mmproj`，判断模型才能看图 |
| `perceive_media` | 默认跟随 mmproj | 要不要把非文字消息的像素喂给判断模型（不写就是：配了 mmproj 就开） |

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

## 9. 已知限制（全部实测）

**必须先带开关启动 QQ。** 不带 `--force-renderer-accessibility` 时，QQ 的聊天内容根本不在无障碍树里
（实测：树里只有 `app` / `drag-area` / `loading` 这几个壳 id，没有 `ml-root` / `func-bar-*`）。
采集器会**查 QQ 进程的命令行**确认这一点并直接告诉你该怎么做（比看树准）；
一键脚本：`scripts\restart-qq-with-a11y.bat`（会关掉 QQ 再带开关启，未发送的草稿会丢）。

**群聊：能读，且只做判断不起草。** 实测（群「高速网盘」，900 个节点）：

| | 情况 |
| --- | --- |
| 消息容器结构 | ✅ 每个数字 id 容器的子树是固定的五层：`d13 时间戳(可选) · d14 昵称 · d15 徽标(群等级) · d15 徽标(群头衔) · d16 正文`；名字行与正文行分开（实测 y 差 35px） |
| **己方/对方** | ✅ **只看几何：靠右 = 我**（实测自己发文字时：正文 x=1773..1913，列表中线 x=1371 → me）。
❗ **不能靠“有没有名字行”判**：真机发现**自己发的消息同样带昵称+徽标**（自己那条 d14 是 28 字的昵称），早期用名字行判会把自己的消息当对方的 |
| 发言人名 | ✅ 取名字行上**深度最小**的那串；两个徽标（深度更深、很短）自动忽略。名字只用在对方的消息上（自己的昵称不当名字用） |
| 连发不说话 | ✅ QQ 只在**发言人变化时**显示昵称，所以没名字行的容器继承上一条的发言人（几何对得上才继承） |
| **非文字消息** | ✅ **给占位**（不是直接消失）：`[图片]` `[表情包]` `[语音]` `[视频]` `[文件]` `[红包]` `[转账]` `[位置]` `[聊天记录]` `[链接]`…认不出具体类型就 `[非文字消息]`；归谁用几何判（实测自己发的图片靠右 → me） |
| **图片像素（给 Omni 预留）** | ✅ 每条占位消息都带一个「媒体锚点」（图片控件的实时矩形）。配了 mmproj + 开了 `perceive_media` 时，采集器会把这块矩形截下来（内存里 JPEG，长边 ≤1024）随消息传给判断模型当成 `/v1/systemone` 的 `files`；不配 mmproj 就只发占位文字 |
| 附件识别 | 优先看附件节点自带的名字（含“语音/视频/文件/红包…”就用对应占位）；没有名字时靠矩形分：小而方 → `[表情包]`，否则 `[图片]` |
| **起草候选** | ❌ **群聊不起草、不排序**：十几个人可能同时在聊几件不相关的事，一条「下一句」没有意义。默认改成**按发言人分别判断**（一人一张卡，见下一节） |
| 虚拟列表滚出去的消息 | ⚠️ 没渲染的项几何全是 0 → **整条跳过，不猜**。所以切会话后会**自动往上滚**把历史补回来（`qq_backfill_messages`），滚回来时按时间顺序发；人在动键鼠时不动他的滚动位置 |
| 会话名 | ✅ 用**消息列表外层那个 `WindowControl` 的 Name**（实测就是会话标题，群名 4 字 → 那层 Name 4 字）。不用几何、不受头部重绘影响 |
| 头部行（几何兜底） | ⚠️ 会时隐时现，**而且经常只有成员数 `(423)`**（早期 bug：去尾巴的正则把它删成空串）；现在优先用上面那条，并把纯成员数/单字符滤掉 |

根因还是那一条：**这个 QQ 版本不暴露 CSS 类名**（`message-container` / `container--self` 全无），
所以只能靠结构 id + 几何 + 分层；单聊和群聊都因此走的是“容器 + 分层”这套。
社区文档（`qq-operations`）那套类名应该来自另一个 QQ 构建/时期——真回到那一天，`build_turns`
的类名分支会优先生效。

群聊的判断口径见下一节；还没做的是：把「自己连着发图片/表情包」在面板里标注一下
（现在图片/表情包已经是 `[图片]`/`[表情包]` 占位，不再是直接跳过）。

## 10. 群聊：按发言人并发判断

**问题**：群里十几个人可能同时在聊三四件**正交**的事。一条「下一句说什么」是伪命题——
所以群聊只判断不起草。但「整个群判断一次」也不够：把三段不相关的对话糊成一段，
判断模型看到的是一个不存在的对话。

**做法**：按**发言人**拆成 N 路，一路一个判断，并发跑。

| | |
| --- | --- |
| 拆谁 | `core.questions.group_speakers()`：最近 `context` 条消息里出现过的人，去重、**最近说话的排前**，最多 `group_speaker_max`（默认 4）个 |
| 每路的上下文 | `core.questions.up_to_speaker()`：**把历史截到那个人说的最后一句话为止**。不截的话所有路都看到同一个结尾，按人拆就白拆了 |
| 每路怎么问 | `core.engine.analyze_group()` → 每路调 `analyze(..., reply_to=那个人, group_judge_only=True)`，于是 `state.chat.reply_to` 标明「这一路是给谁的」 |
| 并发 | `ThreadPoolExecutor(max_workers=group_speaker_workers)`；`on_result` 在池线程里回调，只往队列里塞（Qt 不能跨线程碰） |
| 上屏 | 先把 N 张「正在判断…」占位卡放上去，**哪一路回来就填哪一张**，底部状态写 `x/N 已回` |
| 一路挂了 | 只有那一张卡显示失败原因，其它路照常 |

**为什么不按话题聚类**：切话题是又一次模型调用，还可能切错；按发言人拆是机械的、不会错的近似。
真需要正交切分时，`analyze_group` 的 `base_urls` 已经把「多实例轮流分配」的位置留好了。

### 关于「并发」能省多少时间：`-np` 实测无效

同一个 clef 服务上并发打请求**不会变快**，因为我们给 clef 加了 `-np 1`——clef 的判断实现要把
整个 `state+questions` 当成**一批、单序列**算完（llama.cpp README：*the whole prompt is evaluated
in one batch*）。实测（`probe/clef_parallel.py`，Q8_0，16384 ctx）：

| 服务端 | 1 个请求 | 2 个并发 | 4 个并发 | 结论 |
| --- | --- | --- | --- | --- |
| `-np 1` | 0.89–1.02s | 2.28s | 3.91s | 排队 |
| `-np 2` | — | **2.28s**（和 `-np 1` 一模一样） | — | 无提升 |
| `-np 4` | 0.89s | — | **3.91s** | 串行 4 个 2.26s vs 并发 4 个 2.26s → **加速比 1.00x** |

启动日志确认服务端确实开了 4 个 slot（`n_slots = 4, n_ctx_slot = 4096`），`/slots` 也能看到
同时有 1~3 个 slot 处于 `is_processing`——但多 slot 的 token **合不进同一个 batch**（单序列限制），
所以总时长 ≈ 各路之和。**真并行只有一条路：多开 clef 实例**（每个约 9.7GB 权重），
那时把地址列表传给 `analyze_group(base_urls=[...])` 就会轮流分配。

所以按发言人并发的实际收益是：**每路各看自己那段（判断更准）+ 谁先回来先上屏（体感不等）**，
而不是总时长变短。

其它：

- **QQ 必须在前台**（或允许短暂置前）才能读；最小化时读不到。
- **QQ 以管理员运行时读不到**（Windows UIPI：普通权限进程读不了高完整性进程的 UIA）。
- QQ 大版本更新后结构可能变；认不出时采集器只报可诊断的 status，不会静默读错、
  也不会退回 OCR。
- QQ 自带截图 / Mojo IPC（`EEEEhex/QQImpl` ⭐383 那套）**不用**：依赖 QQ 私有实现，
  版本一变就废，也模糊了「不碰对方进程」的边界。
