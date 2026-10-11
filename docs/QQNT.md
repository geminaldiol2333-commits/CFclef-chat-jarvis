# QQ NT（Windows）适配调研

> **状态：未实现。** 本文只记录调研、实测数据和可行路线，代码里还没有 QQ 适配器；
> `app/chatapps.py` 目前只有微信。等真要做的时候按这里走，不用重新摸一遍。
>
> 最后更新：2026-10，QQ NT `9.9.36-53644`。

## 结论摘要

1. **上游和所有 fork 都没有 QQ NT 支持。** 查过 `jev-chat/jev-chat-windows` 全部 11 个近期有提交的
   fork，`app/chatapps.py` 里一个 QQ 都没有；上游自己明确只维护微信这一套识别。
   Android 版（`Finderchangchang/jev-chat-JARVIS`）**支持 QQ**（读无障碍节点，9.3.50 实测），
   所以判断口径和题目集是现成的，缺的只有 Windows/QQ NT 这侧的采集。
2. **本项目的「截图 + OCR + 比颜色」路线在 QQ NT 上不好走**：`chat_area()` 靠「面板底色 =
   画面里最常见的颜色」定位消息区和输入框，而 QQ NT 的聊天区是壁纸/渐变，锚点直接失效
   （实测返回整个窗口，见下文）。
3. **好消息：QQ NT 有更好的路线——UIA 无障碍树，而且控件类名是固定的。**
   社区已经把输入框、头部联系人、气泡、**己方气泡标记**都摸出来了，
   `己方/对方` 直接读类名（`container--self`），不用比颜色、也不用 OCR。
   两个完整参考实现见 [`treers2/qq-operations`](https://github.com/treers2/qq-operations)（PowerShell，MIT）
   和 [`TheD0ubleC/QQSafeChat`](https://github.com/TheD0ubleC/QQSafeChat)（Python + `uiautomation`，MIT）。
   **这条路能完全绕开第 2 条的阻塞点，推荐优先考虑。**
4. 气泡颜色随主题变（本机那套是改过的），所以如果要走像素路线，颜色规则得可配置或按主题标定；
   走 UIA 路线则不需要关心主题。

## 参考项目（分类）

### A. 无障碍树 / UIA 路线 —— 推荐，跟本项目一样「不注入、不读内存」

| 项目 | 语言 / 许可 | 为什么重要 |
| --- | --- | --- |
| [`treers2/qq-operations`](https://github.com/treers2/qq-operations) ⭐2 | PowerShell / MIT | **最有价值的一份**。README 和 `scripts/qq_lib.ps1` 里把 QQ NT 的 UIA 类名、双界面模式识别、前置窗口重建、收件人校验全写出来了（见下文「UIA 蓝图」）。零第三方依赖，脚本可单独跑 |
| [`TheD0ubleC/QQSafeChat`](https://github.com/TheD0ubleC/QQSafeChat) ⭐156 | Python / MIT | UIA 绑定「消息列表 / 输入框 / 发送按钮」，并提供 `uia_debug.py` 拾取控件；README 记录了「只能选中整个窗口」的成因与解法（无 GPU 设备要用 `--force-renderer-accessibility` 启动 QQ）。同样是「不注入、不抓协议、不扫内存」 |
| [`yinkaisheng/Python-UIAutomation-for-Windows`](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows) ⭐3585 | Python / Apache-2.0 | Python 的 UIA 封装。README 明确写了 **Chrome / Electron 应用需要加启动参数 `--force-renderer-accessibility` 才支持 UIAutomation**——这是那条「无 GPU 才需要」说法的权威出处 |
| `pywinauto` | Python / BSD | 另一个 UIA 后端选择，`Appium` 式 API |

### B. 协议 / 插件注入路线 —— 生态最大，但**与本项目硬约束冲突，不采用**

这些能拿到结构化消息、发消息、收事件，工程量比 UIA 小得多，但都是**往 QQ 里注入/挂钩**或跑独立协议栈：

| 项目 | 说明 |
| --- | --- |
| [`NapNeko/NapCatQQ`](https://github.com/NapNeko/NapCatQQ) ⭐10907 | 目前最主流的 NTQQ 协议侧框架（把 NTQQ 变成 OneBot 机器人） |
| [`LLOneBot/LuckyLilliaBot`](https://github.com/LLOneBot/LuckyLilliaBot) ⭐3653 | 支持 OneBot 11 / Satori / Milky 协议 |
| [`LagrangeDev/Lagrange.Core`](https://github.com/LagrangeDev/Lagrange.Core) ⭐2992（另有 `LagrangeGo` ⭐230、`lagrange-python` ⭐135） | 纯协议实现，不依赖 QQ 客户端 |
| [`LiteLoaderQQNT/LiteLoaderQQNT`](https://github.com/LiteLoaderQQNT/LiteLoaderQQNT) ⭐7071（已归档） | QQNT 插件加载器；插件生态很大：`LiteLoaderQQNT-OneBotApi-JS`、`lite-tools` ⭐1331、`Anti-Recall` ⭐701、`QQNTim` ⭐311、`QQScreenshotSwitch` 等 |
| `ziweni/NtQQHook` ⭐71、`NapNeko/NTQQAntiRecall` ⭐300 | Electron QQ 的 Hook / 反撤回 |

> **为什么不用：** 本项目的硬约束是「不 hook、不注入、不读对方数据库、不碰对方进程内存」。
> 这条路线全部违反。UIA 和截图都不算注入，所以 A 路线可以，B 路线不行。
> 另外 B 路线本质是「机器人」而不是「读我自己屏幕上的对话给建议」，产品定位也不同。

### C. 数据库解密 / 导出路线 —— 只能读历史，不能实时

`sgaofen/murmur`、`DaizeDong/qq-history-recovery`、`ShintoKosei/qqnt-db-export`、`miniyu157/qq-dump`、
`shang555555/ChatArchive`、`saucer-man/qq_msg_decode`、`wujiangcai/qq`。
读的是本地加密数据库（要解密密钥），拿不到「当前屏幕上这条新消息」的实时性，也违反「不读数据库」约束。

### D. QQ 自带的截图 / OCR

- [`EEEEhex/QQImpl`](https://github.com/EEEEhex/QQImpl) ⭐383：把 QQ 的 **Mojo IPC** 和微信的
  `XPlugin`（WeChatOCR）扒出来单独调用。也就是说 QQ/微信客户端自带的 OCR 可以被外部调用。
- [`bakyrd/QQScreenshotSwitch`](https://github.com/bakyrd/QQScreenshotSwitch)：切换 QQ 新版/旧版截图工具
  （新版截图工具就在 QQ 进程内，JS + Electron 实现）。
- 相关文章：《逆向调用 QQ 截图 NT 与 WeChatOCR》。

> QQ 自带的 OCR 是中文识别质量最好的一档，但把 QQ 的内部 IPC 当 OCR 服务用，等于依赖 QQ 私有实现、
> 版本一变就废，而且和「不碰对方进程」的边界很暧昧。**不建议**，仅作记录。

### E. 其它「截图 + LLM」同类工具（非 QQ 专用）

`Frostyume/AutoSendMsg`（微信/QQ 定时发送）、`RyuuMeow/NeverThinkAutoReply`（PySide6 + EasyOCR 热键截图）、
`DreDabe/auto-reply`（VLM 直接看窗口）。思路可参考，但都没有解决 QQ NT 的定位问题。

## UIA 蓝图（来自 qq-operations 的实测选择器）

QQ NT 用 Chromium/Electron 渲染，但**控件完整暴露在 UIA 树上**，类名固定：

| 用途 | 类名 / 条件 |
| --- | --- |
| 输入框 | `qq-msg-editor` |
| 头部联系人名（收件人） | `chat-header__contact-name` |
| 气泡容器 | 正则 `^(message-container\|msg-content-container)` |
| **己方气泡** | 容器类名匹配 `container--self` —— **自己/对方直接读类名，不用比颜色** |
| 发送按钮 | 名称 `发送`（发文件时确认浮层是 `发送(1)`） |
| 主面板识别 | 标题为 `QQ` **且**里面真的有搜索框（Edit 控件）的那个窗口 |
| 界面模式 | 找名称匹配 `切换为经典模式` 的控件 → 当前是**效率模式**；`切换为效率模式` → 当前是**经典模式** |

必须知道的三个坑（都来自该项目的实测记录）：

1. **QQ 只对「前台」窗口构建完整 UIA 树。** Electron/Chromium 的行为：窗口在后台时树只有约 9 个节点，
   连搜索框都找不到。它的做法是 `Revive-QQPanel`：`ShowWindow(hwnd, 9)`（恢复）+ `SetForegroundWindow`
   + **等 800ms 让树重建** + 重试一次。自研脚本绕过这步就会莫名其妙找不到控件。
2. **双界面模式**：经典模式（每个会话一个独立聊天窗，标题是联系人名）和效率模式（聊天内嵌在主面板，
   靠 `chat-header__contact-name` 认人）要分别处理，得先探测当前是哪种。
3. **多会话合并窗口**：把多个会话拖到一起后，窗口标题只显示当前激活的那个（形如 `张三等2个会话`），
   所以**不能靠窗口标题判断在跟谁聊**；一律走「搜索 → 打开会话」再读 `chat-header__contact-name`。
   另外特殊字符（emoji、`㍿`）的会话名在标题里会显示成 `?`，只能靠头部联系人兜底。

GPU 与 UIA：`QQSafeChat` 记录了「只能选中整个窗口（PaneControl）」的现象，**一般出现在没有 GPU 的设备上**——
Chromium 在无 GPU 时默认不构建 Accessibility 树，解决办法是用 `--force-renderer-accessibility` 启动 QQ。
本机有独显，实测窗口树正常（`qq-operations` 也没要求加这个参数）。

## 附：像素路线的实测数据（备选方案）

环境：QQ NT `9.9.36-53644`，系统缩放 125%，主题/气泡是用户改过的（非默认）。
测量方式：只抓像素统计颜色，**不跑 OCR、不读文本、不落盘**。

| 项 | 实测值 |
| --- | --- |
| 顶层窗口类名 | `Chrome_WidgetWin_1` |
| 主窗口标题 | 恰好 `QQ`（只有一个可见顶层窗口，默认效率模式） |
| 窗口矩形 / 抓到的帧 | `1328x768` → 帧 `1660x960`（125% 缩放，帧是物理像素） |
| 自己的气泡底色 | ≈ `rgb(0,128,245)` = `#0080F5` |
| 聊天区背景 | ≈ `#D5E2F6`（浅蓝壁纸，**不是平底色**） |
| 对方气泡（该主题下） | 白 `#FFFFFF` / 粉 `rgb(253,217,218)` |

### 为什么 `chat_area()` 在 QQ NT 上失效

`app/capture.py::chat_area()` 的步骤：取右半边最常见颜色当面板底色 `bg` → 按列求「等于 bg」的占比，
第一根 > 0.3 的列当面板左边界 → 同理求上下边界和横向分隔线，45% 高度以下第一根分隔线当输入框顶。

QQ NT 聊天区是壁纸/渐变，**没有任何一种颜色占比高**，于是 `isbg` 掩码几乎全空、列占比永远到不了 0.3，
`np.argmax` 返回 0 → `x0 = 0`、`x1 = W`，分隔线也找不到 → `y_in = H`。实测输出正好是这个退化结果：

```
chat_area() -> (0, 60, 1660, 960)
```

后果：OCR 会把会话列表、搜索框、标题栏一起读进去；`fill()` 会点在窗口右下角而不是输入框。
**所以 QQ NT 不是 `chatapps.py` 加一行就能好的。**

如果要走像素路线，建议的判色规则（**高蓝低红**，比宽松的「蓝色主导」稳，因为背景 `#D5E2F6` 离蓝不远）：

```python
def _qq_me(bg):
    r, g, b = int(bg[0]), int(bg[1]), int(bg[2])
    return b > 180 and r < 120 and b > r + 120 and b >= g + 30
```

| 颜色 | 判定 | 说明 |
| --- | --- | --- |
| `#0080F5` `(0,128,245)` | ✅ me | 本机实测的自己的气泡 |
| `#12B7F5` `(18,183,245)` | ✅ me | 旧版 QQ 蓝 |
| `#4A9EFF` `(74,158,255)` | ✅ me | 常见 QQ 蓝 |
| `#D5E2F6` `(213,226,246)` | ❌ | 聊天区壁纸（`r` 太高） |
| `#FFFFFF` | ❌ | 对方气泡 / 面板 |
| `rgb(253,217,218)` | ❌ | 本机主题里的粉色气泡 |
| 微信绿 `(149,236,105)` | ❌ | 与微信规则互不误伤 |

## 两条路线对比

| | **UIA 类名法**（推荐） | 像素 + OCR（现有架构） |
| --- | --- | --- |
| 自己 / 对方 | 类名 `container--self`，**精确** | 比底色猜，主题一变就崩 |
| 消息文本 | UIA 直接给，**不用 OCR、不认错字** | 中文 OCR 有误读风险 |
| 发言人名（群聊） | UIA 节点里有 | 靠灰字+位置猜 |
| 抗 DPI / 缩放 / 窗口移动 | 强（坐标每步现取） | 差（125% 缩放就要重调参数） |
| 会话识别 | `chat-header__contact-name`，可靠 | 从头部 OCR 猜 |
| 前置条件 | 窗口必须在**前台**（否则树不完整，需 `ShowWindow`+`SetForegroundWindow`+等 800ms） | 窗口可见即可（可被遮挡） |
| 对现有代码的改动 | **大**：等于新增一条采集路径，`capture`/`ocr`/`who_said` 对 QQ 全部绕开（类似 Android 版那套） | 中：写 QQ 专用定位器 + 调参 |
| 脆弱点 | QQ 大版本更新后类名可能变 | 主题、壁纸、DPI、QQ 更新 |
| 合规 | ✅ 不注入、不读内存 | ✅ 只截图 |

## 落地建议

**首选 UIA 类名法。** 具体做法：

1. `app/chatapps.py` 里给 QQ 条目加个标记（如 `collect = "uia"`），只用来声明「这个 app 走 UIA」，不参与像素那条路。
2. 新写 `app/qq_uia.py`：用 `uiautomation`（或 `pywinauto`）按上表类名取节点，输出跟
   `app/ocr.py::Reader.read()` **一样的结构** `[(who, name, text)]`（`who ∈ me/her`）。
   这样 `main.py` 往上（会话跟踪、engine、悬浮窗、填入）一行都不用改。
3. 填入选候选仍可复用 `app/fill.py`（剪贴板 + Ctrl+V），但输入框改用 `qq-msg-editor` 的实时矩形，
   比按分隔线 +40px 猜稳得多。
4. 会话名用 `chat-header__contact-name`；多会话合并窗口标题不可信（见上文第 3 个坑）。
5. 依赖 `uiautomation` 只在 QQ 模式下 import，微信模式不受影响。

**次选像素路线**：写 QQ 专用定位器（竖向分界线找面板左边界、底部浅色圆角矩形找输入框顶），
配 `probe/probe_qq_pane.py` 对着实际主题调参，颜色规则做成 `config.json` 可配。

## 怎么验证 / 怎么调

仓库里留了两个探针（都只读像素、不打印文本、不落盘）：

```bat
:: 1) 确认窗口类名和标题（决定 main_title 怎么填）
python probe\probe_qq_windows.py

:: 2) 看 chat_area() 认出了什么、聊天区实际配色是多少（像素路线调参用）
python probe\probe_qq_pane.py
```

走 UIA 路线的话，直接用 `QQSafeChat` 的 `uia_debug.py` 或 `qq-operations` 的
`scripts/qq_recon.ps1` / `qq_diag.ps1` 拾取控件，比我们自己写拾取器省事。

## 注意事项

- **QQ 必须打开、不能最小化**；UIA 路线还要求**前台**（后台只有约 9 个节点）。
- **帧是物理像素**，`chat_area(header_h=60)` 的 `header_h` 按 100% DPI 给的，125% 缩放要按比例放大。
- **不要走 LiteLoaderQQNT 插件和 NapCat/LLOneBot/Lagrange 那条路**：注入或独立协议栈，
  违反本项目「不 hook、不注入、不碰对方进程/数据库」的硬约束。
- **不要用 QQ 自带截图/OCR 的 Mojo IPC**（`QQImpl` 那套）：依赖 QQ 私有实现，版本一变就废，
  边界也暧昧。
- QQ 大版本更新后窗口结构、类名、配色、DPI 行为都可能变；适配器要能容错，认不出时明确报错，
  而不是把整个窗口当消息区。
