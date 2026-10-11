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
3. **UIA 路线在本机的 QQ 版本上走不通（实测否定）。** 社区（`treers2/qq-operations`）公开了
   QQ NT 的 UIA 控件类名（`qq-msg-editor` / `chat-header__contact-name` / `container--self`），
   听起来是最优解；但我在 QQ **9.9.31 和 9.9.36** 两个已安装版本上都验证过：
   那些类名**根本不存在**，聊天内容也不在无障碍树里（实测数据见下文「UIA 实测否定」）。
   所以 QQ 的可行路线是**像素路线**（本项目的强项），UIA 留着看 QQ 以后会不会重新暴露。
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

## UIA 实测否定（2026-10，QQ NT 9.9.31 / 9.9.36）

`qq-operations` / `QQSafeChat` 那套「读 UIA 类名」的路线在这两个版本上**不可用**。实测证据：

| 检查项 | 实测结果 |
| --- | --- |
| 不使用 `--force-renderer-accessibility` | 整棵树只有 **9 个节点**（QQ 只给前台窗口建树；且这 9 个里没有任何消息） |
| 加上 `--force-renderer-accessibility` 启动 | 节点涨到 **389 个**（说明参数有效、树建起来了） |
| `ClassName` | **389 个里 389 个是空字符串**（只有窗口级的 `Chrome_RenderWidgetHostHWND`） |
| `AutomationId` | 387 个为空，只有 `app` / `loading` / `drag-area` 几个 |
| `qq-msg-editor` / `chat-header__contact-name` / `container--self` / `message-container` | **全部找不到**（两个版本都找不到） |
| 聊天区里的节点 | 只有 20 个有几何信息，其中 `TextControl` 仅 4 个 |
| 有文本的 `TextControl` | 120 个，但 `BoundingRectangle` 全是 `(0,0,0,0)` → 无法排序、无法按左右对齐判己方/对方 |
| `EditControl`（唯一一个） | rect `(567,280,771,305)`，在**左侧会话列表**里 → 那是搜索框，**消息输入框根本没暴露** |
| `TextPattern` | 整个面板能取到 **1192 个字符**，但 `GetVisibleRanges()` 返回 `E_NOTIMPL`；按行迭代只能拿到前 2 行且带几何 |
| 窗口子控件 | 存在 `Intermediate D3D Window` → 聊天区是**GPU 合成**的，不在无障碍树里 |
| 两个版本的 `application.asar` 里搜类名 | `qq-msg-editor` 等字符串**一个都没有** |

结论：**QQ 9.9.36 的聊天内容是自绘 / D3D 合成，UIA 拿不到「谁说的、说了什么、输入框在哪」**。
`QQSafeChat` README 里那条「只能选中整个窗口（PaneControl）」的排错项，说的就是这个现象。
`qq-operations` 记录的类名应该来自另一个 QQ 构建/时期，留作参考——如果哪天 QQ 重新暴露，
按下面的选择器和坑位接进来即可（接口已经留好，见 [ARCHITECTURE.md](ARCHITECTURE.md) 的采集器）。

### 供参考：文档记录的类名（本机版本实测不存在）

| 用途 | 类名 / 条件 |
| --- | --- |
| 输入框 | `qq-msg-editor` |
| 头部联系人名（收件人） | `chat-header__contact-name` |
| 气泡容器 | 正则 `^(message-container\|msg-content-container)` |
| **己方气泡** | 容器类名匹配 `container--self` |
| 发送按钮 | 名称 `发送`（发文件时确认浮层是 `发送(1)`） |
| 主面板识别 | 标题为 `QQ` **且**里面真的有搜索框（Edit 控件）的那个窗口 |
| 界面模式 | 名称 `切换为经典模式` → 效率模式；`切换为效率模式` → 经典模式 |

必须知道的三个坑（如果哪天要用 UIA）：

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

## 落地建议（已根据实测修正）

**首选像素路线**——UIA 拿不到东西，而像素本来就是本项目的强项。采集器接口（P0）已经落地：

- `app/collectors/`（`base.py` 接口 + `ocr_wechat.py` 微信实现）、`app/worker.py` 现在只当壳，
  `ChatApp.collector` 字段决定用哪个引擎（`ocr` / `pixel` / `uia`）。
- 填入已经支持「采集器直接给输入框矩形」（`app/fill.py::fill_rect` + `main.py` 的 `input` 事件），
  不用再按消息区分隔线推。
- 所以剩下的只是写 `app/collectors/pixel_qqnt.py`：

1. **窗口**：进程 `qq.exe` + 标题恰好 `QQ`（实测：只有一个可见顶层窗，默认效率模式）。
2. **面板左边界**：会话列表背景是**平色**、聊天区是壁纸 → 按列算「平色占比」找分界；
   也可以用 UIA 容器矩形的实测值（会话列表 `520..833`、聊天区 `832..2105`，物理像素 @125%）做交叉验证。
3. **输入框**：聊天区底部那块浅色圆角矩形（实测输入区在窗高 62% 以下），
   找到后把屏幕坐标用 `emit("input", rect)` 发出去。
4. **会话名**：聊天区顶部那条（面板顶 ~ 消息区顶）交 `app/ocr.py::read_title` 识别。
5. **谁说的**：用高蓝低红的规则（见上文真值表），颜色写进 `config.json` 以便换主题。
6. 对着 `probe/probe_qq_pane.py` 的输出调参；调好后 `python tools/verify_local.py` 跑全链路。

**UIA 保留但暂不用**：类名、双模式识别、前台重建（`ShowWindow(9)` + `SetForegroundWindow` + 等 800ms）
都记在上面了。哪天 QQ 重新把聊天暴露到无障碍树里，加一个 `app/collectors/uia_qqnt.py` 就能接上，
`worker.py` / `main.py` 一行不用改——这正是 P0 分层的意义。

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

## 参考资料

- 详细架构设计（采集器抽象、图像判断、模型档案与驻留计划、Clef-Omni 就绪清单）：
  [ARCHITECTURE.md](ARCHITECTURE.md)

## 注意事项

- **QQ 必须打开、不能最小化**；UIA 路线还要求**前台**（后台只有约 9 个节点）。
- **帧是物理像素**，`chat_area(header_h=60)` 的 `header_h` 按 100% DPI 给的，125% 缩放要按比例放大。
- **不要走 LiteLoaderQQNT 插件和 NapCat/LLOneBot/Lagrange 那条路**：注入或独立协议栈，
  违反本项目「不 hook、不注入、不碰对方进程/数据库」的硬约束。
- **不要用 QQ 自带截图/OCR 的 Mojo IPC**（`QQImpl` 那套）：依赖 QQ 私有实现，版本一变就废，
  边界也暧昧。
- QQ 大版本更新后窗口结构、类名、配色、DPI 行为都可能变；适配器要能容错，认不出时明确报错，
  而不是把整个窗口当消息区。
