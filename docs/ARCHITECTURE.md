# 架构：采集器抽象 + 多模态判断 + Clef-Omni 就绪

> 状态：**P0 采集器分层 + QQ NT 的 UIA 采集器已落地**（`app/collectors/`：`base` / `ocr_wechat` /
> `uia_map` / `uia_qqnt`，加上 `worker.py` 壳、`ChatApp.collector`、`fill_rect`/`input` 事件）。
> 仍是设计的：config v2、`LoadPlan`、`files` 图像通道（P1/P3）。
> **QQ 只走 UIA，不做像素/OCR**（见 [QQNT.md](QQNT.md)）。
> 目标有两件事：**(1)** 用 UIA 支持 QQ NT；**(2)** 让 Clef-Omni（30B-A3B，多模态决策模型）
> 落地时只改配置、不改代码。

## 0. 先说结论

1. **Clef-Omni 是决策模型，不是聊天模型**——没有自由文本生成。所以它替换的是**判断/排序**那一步，
   起草仍然需要另一个文本模型。这一点跟现在的 clef-flash 一模一样，架构不变。
2. 它多出来的是**输入模态**：图像 / 音频 / 视频（文本和 JSON 本来就有）。llama.cpp 的
   `/v1/systemone` 已经支持图像输入（`files` / `images` 字段，base64 data URL），
   前提是加载了多模态投影器（`--mmproj`）。
3. **视觉能力可以现在就验证**：`ggml-org/Clef-Flash-GGUF` 自带
   `mmproj-Clef-Flash-Q8_0.gguf`（约 624 MB），b11515 已含「server: support vision input for Clef」
   （PR #29969）。所以「给判断模型看截图」这条路**不依赖 Clef-Omni**，今天就能做、能测。
4. **Clef-Omni 的 GGUF 还没发布**（`ggml-org/Clef-Omni-GGUF` 不存在，ModelScope 也没有；
   目前只有 safetensors 和 MLX/NVFP4 量化）。而且 b11515 里**没有 `qwen3_omni_moe` 架构支持**，
   将来还要升 llama.cpp。所以架构必须把「模型」做成**可配置档案**，不能写死。
5. **30B-A3B 装不下「判断 + 起草同时驻留」**（Q4_K_M 约 18–19 GB + mmproj 1–2 GB，
   再加一个 9B 起草模型就超 24 GB）。所以要引入**驻留计划（LoadPlan）**：
   优先「换小一点的起草模型」，其次才「串行换载」，并提供降级模式。
6. 顺带一个必须做的能力：`GET /models` 会返回 `architecture.output_modalities`
   （决策模型含 `decisions`）和 `input_modalities`（来自投影器）。**可以在不加载模型的情况下
   提前判断这份 GGUF 是不是决策模型、支不支持图像**——比等一个 501 好得多。

## 1. 现在的问题

现在的数据流把「怎么看到聊天」「怎么判」「怎么写」「怎么填」揉在一条链上：

```
app/worker.py（子进程）
  找窗口 app.capture.find_chat_hwnd
  WGC 抓帧 app.capture.Capture
  像素定位消息区/输入框 app.capture.chat_area
  OCR + 按底色分 me/her app.ocr.Reader / who_said
  会话名 app.ocr.read_title
      │  队列 [(who, name, text)]
      ▼
main.py  chats[会话] → deque              ← 会话状态
core.engine.analyze()                     ← 判断 → 起草 → 排序
app.overlay 悬浮窗
app.fill.fill(hwnd, area, text)           ← 按 chat_area 的分隔线 +40px 点输入框
```

三个痛点，正好对应这次要做的三件事：

| 痛点 | 后果 | 这次怎么解 |
| --- | --- | --- |
| 「怎么看到聊天」写死在 `worker.py` 里，默认就是像素 + OCR | QQ NT 走不通（`chat_area` 失效，见 `docs/QQNT.md`） | 抽出**采集器（Collector）**接口，OCR 和 UIA 是两种实现 |
| 判断请求只能带文本 | 表情包、图片消息、语音、谁先谁后这类信息全丢 | 采集结果带**可选媒体**，判断请求带 `files` |
| 模型是几个扁平配置项 | 换 30B 模型要改代码/改一堆键，显存也装不下 | **模型档案 + 驻留计划**，配置 schema v2 |

## 2. 目标分层

```
┌─ 采集层 Collector ────────────────────────────────────────────────┐
│  OcrCollector(微信)        UiaCollector(QQ NT)                      │
│  抓帧 → chat_area → OCR     读无障碍树：类名取节点 / container--self │
│  产出：Turn(who, name, text, media=[])                            │
│  能力：provides_text / provides_media / can_insert / session_key   │
└───────────────┬───────────────────────────────────────────────────┘
                │  Turn 列表（和现在 engine 要求的形状兼容）
┌───────────────▼─ 会话与感知层 ─────────────────────────────────────┐
│  ChatStore（现有 chats[会话] 逻辑）                                │
│  core.questions.build_state()       ← 不动                         │
│  app/perception.py（新）                                           │
│    · 决定要不要附截图（capability 协商）                            │
│    · 截帧 → 缩放 → JPEG → base64 data URL（**只在内存里**）          │
│    产出：{state, files?}                                           │
└───────────────┬───────────────────────────────────────────────────┘
                │
┌───────────────▼─ 决策层（判断 + 排序）───────────────────────────────┐
│  core.jev_client.ask(state, questions, files=?)                    │
│    · clef / clef-flash / clef-omni 都走本地 /v1/systemone            │
│    · 能力探测：GET /models → output_modalities 含 decisions？       │
│                 input_modalities 含 image？                        │
│    · 远程来源（OpenRouter/TypeSafe）保持可用，但不支持 files         │
└───────────────┬───────────────────────────────────────────────────┘
                │
┌───────────────▼─ 起草层（文本生成）────────────────────────────────┐
│  core.draft.draft_candidates()  ← 不动（本地文本模型 / 远程）        │
└───────────────┬───────────────────────────────────────────────────┘
                │
┌───────────────▼─ 动作层（填入）────────────────────────────────────┐
│  app/fill.py + InputTarget：微信=按 chat_area 推；QQ=qq-msg-editor 实时矩形│
│  永不自动发送                                                      │
└───────────────────────────────────────────────────────────────────┘
                │
┌───────────────▼─ 模型进程层 ───────────────────────────────────────┐
│  app/local_server.py：档案 → 命令行 → 拉起/健康检查/空闲卸载          │
│  app/model_profiles.py（新）：ModelProfile / LoadPlan / 显存预算      │
└───────────────────────────────────────────────────────────────────┘
```

**关键约束**：`Turn` 的形状和 `engine.analyze()` 的返回保持不变，所以 `main.py` 往上（会话跟踪、
悬浮窗、排序展示）**基本不用改**。新东西都加在两端（采集层、模型层）和中间一个薄薄的感知层。

## 3. 采集层：Collector 接口

### 3.1 数据形状

```python
# app/collectors/base.py
from dataclasses import dataclass, field
from typing import Protocol

@dataclass(frozen=True)
class Media:
    kind: str                 # "image"（将来 "audio" / "video"）
    data: bytes               # 编码后的字节（JPEG/PNG/WAV…），**只在内存里**
    mime: str = "image/jpeg"
    width: int = 0
    height: int = 0

@dataclass(frozen=True)
class Turn:
    who: str                  # "me" | "her"   ← 与 core.questions.build_state 一致
    text: str                 # UIA 直接给；OCR 给识别结果；纯视觉模式可以是 ""
    name: str | None = None   # 群聊发言人
    media: tuple[Media, ...] = ()   # 可选：这条消息相关的图（截图/图片消息）

@dataclass(frozen=True)
class Capabilities:
    provides_text: bool
    provides_media: bool
    can_insert: bool          # 支不支持把候选填进输入框
    needs_foreground: bool    # UIA(QQ) 为 True：读控件树前需要把目标窗口置前

class InputTarget(Protocol):
    """填入目标：把文本放进聊天软件的输入框，绝不发送。"""
    def insert(self, text: str) -> None: ...

class Collector(Protocol):
    key: str                                   # "wechat" / "qqnt"
    capabilities: Capabilities
    def start(self) -> None: ...
    def poll(self) -> list[Turn]: ...          # 增量：只返回这一拍新出现的
    def session(self) -> str: ...              # 当前会话名（会话切换的 key）
    def input_target(self) -> InputTarget | None: ...
    def stop(self) -> None: ...
```

### 3.2 两种实现

| | `OcrCollector`（微信，**已实现**） | `UiaCollector`（QQ NT，**已实现**） |
| --- | --- | --- |
| 找窗口 | `capture.find_chat_hwnd`（进程名 + 标题） | 同名函数；`main_title=""` + `skip_titles=("QQ",)` 兼顾效率/经典两种界面模式 |
| 会话名 | 头部 OCR（`ocr.read_title`） | `chat-header__contact-name` 的文本（不看窗口标题） |
| 消息 | WGC 抓帧 → `chat_area` → `Reader.read` → 按底色分 me/her | UIA 节点：气泡类名正则 + **`container--self` 判己方** |
| 输入框 | `chat_area` 的分隔线推 +40px | `qq-msg-editor` 的实时矩形 → `emit("input", rect)` |
| 前台要求 | 不需要（可被遮挡） | **需要前台**：`steal`（默认，读完还焦点）或 `foreground`（不抢焦点、切走停更） |

QQ NT 侧的实测选择器（来自 `treers2/qq-operations`，见 `docs/QQNT.md`）：

| 用途 | 选择器 |
| --- | --- |
| 输入框 | `qq-msg-editor` |
| 头部联系人 | `chat-header__contact-name` |
| 气泡 | 正则 `^(message-container\|msg-content-container)` |
| 己方气泡 | `container--self` |
| 发送按钮 | 名称 `发送`（我们**不点**它，只用来定位输入区） |
| 主面板 | 标题 `QQ` 且含 Edit（搜索框） |
| 界面模式 | 控件名 `切换为经典模式` → 效率模式；`切换为效率模式` → 经典模式 |

三个必须处理的坑（都实测过）：

1. **QQ 只对前台窗口构建完整 UIA 树**（后台约 9 个节点）。读取前要
   `ShowWindow(hwnd, 9)` + `SetForegroundWindow` + **等 800ms** + 重试一次。
   ⚠️ 这与本项目的「不抢焦点、安静地挂在旁边」理念冲突，**是本次设计最大的取舍**，见第 8 节。
2. **双界面模式**（经典＝独立窗口 / 效率＝内嵌）要先探测，两条路取会话名的方式不同。
3. **多会话合并窗口标题只显示激活的那个**（`张三等2个会话`）→ 会话名一律取
   `chat-header__contact-name`，不看窗口标题。

## 4. 感知层：把「要不要给模型看图」收在一处

新增 `app/perception.py`，只有两个职责：

```python
def encode_image(frame: np.ndarray, max_side: int = 1024, quality: int = 80) -> Media
    """内存里的帧 → 缩放 → JPEG → Media。**不落盘、不进日志。**"""

def build_request(messages, relationship, *, keep, reply_to,
                  collector_caps, judge_caps, mode: str) -> dict
    """产出 {state, files?}：
       - mode="text"：只有 state（现在的行为）
       - mode="vision"：state + 截图（判断模型看整块消息区，比逐条 OCR 更懂表情包/语气）
       - mode="auto"：judge 支持图像 且 collector 能给帧 → vision，否则 text
    """
```

**重要澄清（避免误解）**：视觉**不能**完全替代 OCR。因为起草那一步是**文本生成模型**，
它要读文字。所以：

| 配置 | 判断输入 | 起草输入 | OCR 还需要吗 |
| --- | --- | --- | --- |
| A. 现状 | 文本（OCR 结果） | 文本 | 需要 |
| **B. Clef-Omni 的甜点** | **文本 + 截图** | 文本 | 需要（给起草用） |
| C. 纯视觉判断 | 只有截图 | 文本 | 需要 |
| D. 全多模态 | 截图 | 截图（需要多模态**生成**模型，如 Qwen3-Omni-Instruct） | 不需要 |

所以 Clef-Omni 的价值是**B**：判断更准（看得见表情包、图片、语音气泡、谁先谁后），
而不是省掉 OCR。**但 QQ NT 走 UIA 之后，文字本来就是精确的（不用 OCR），
所以 QQ + 视觉判断是收益最大、瓶颈最少的组合。**

图片会占 token（模型卡：边长按 32 像素取整），而且 clef 系**整个 prompt 必须塞进 `--ubatch-size`**
（llama.cpp README 原文）。所以感知层要配合：`image_max_side`（默认 1024）、
clef 档案的 `ctx/ubatch` 要留够（默认 8192，必要时上调并 `--mtmd-batch-max-tokens` 调大）。

## 5. 决策层：请求带图 + 能力探测

### 5.1 请求

`core/jev_client._ask_local` 现在发 `{model, state, questions}`，只加一个可选字段：

```python
def ask(state, questions, *, timeout=20, provider="clef", model=None,
        base_url=None, files: list[Media] | None = None) -> dict:
    ...
    payload = {"model": model, "state": state, "questions": questions}
    if files:
        payload["files"] = [f"data:{m.mime};base64,{base64.b64encode(m.data).decode()}"
                            for m in files]
```

- 官方 schema：`files`（`images` 是别名），数组元素是 data URL；也支持把图放进 `state` 的
  chat messages 里（`image_url` part）。我们用 `files` 更直白。
- **错误语义要分开报**（README 明确列了）：`400` 请求非法 / `501` 不是决策模型 /
  `501` 模型不支持图像或没加载 mmproj。现在 `_ask_local` 只区分了 404 和 501，要细化成
  「这份 GGUF 不是决策模型」vs「你要看图但这个模型/投影器不支持」。

### 5.2 能力探测（新增，很值）

`GET /v1/models`（或 `/models`）返回：

```json
{"id": "...", "architecture": {"output_modalities": ["decisions"], "input_modalities": ["text","image"]}}
```

**本机实测**（b11515 + `Clef-Flash-Q8_0.gguf`，未加载 mmproj）：

```
GET /v1/models → id=clef-flash
  architecture: {"input_modalities": ["text"], "output_modalities": ["decisions"]}
```

`output_modalities` 含 `decisions` = 这份 GGUF 是决策模型；`input_modalities` 里没有 `image`
是因为没加 `--mmproj`——加投影器后应变成 `["text","image"]`（P1 阶段用这个当验收点）。

- **不用加载模型就能查**（未加载/卸载/休眠时也算得出来）。
- 用处：① 启动时先校验 clef GGUF 是不是决策模型，报错比 501 早得多；
  ② 决定感知层走 text 还是 vision；③ 设置页/doctor 直接显示「这份模型支持：文本/图像/音频/视频」。

### 5.3 排序

`build_rank_question()` 不变——Clef-Omni 一样吃 `choice` 题。

## 6. 模型层：档案 + 驻留计划（Clef-Omni 的关键）

### 6.1 ModelProfile

```python
@dataclass(frozen=True)
class ModelProfile:
    name: str                 # "clef-flash-q8" / "clef-omni-q4" / "qwen3.5-9b-q4"
    role: str                 # "judge" | "draft"
    path: str                 # .gguf；分片时指向 00001-of-0000N
    mmproj: str = ""          # 多模态投影器；空=不支持图像
    alias: str = ""
    port: int = 0
    ctx: int = 8192
    batch: int = 8192         # clef 系要求整个 prompt 一个 batch
    ubatch: int = 8192
    ngl: int = 999
    vram_gb: float = 0.0      # 估算值，用于驻留计划
    can_generate_text: bool = False   # 决策模型 False
```

**分片 GGUF**：30B 的量化多半是 `-00001-of-00005.gguf` 这种。`local_server.resolve_gguf` 现在
「目录里取排序第一个」碰巧能用，但要**显式**处理：识别 `of` 分片命名，永远取 `00001` 那片，
并把同目录其它分片视为同一模型（避免误判成「有多个模型」）。

### 6.2 LoadPlan

```python
@dataclass(frozen=True)
class LoadPlan:
    resident: tuple[str, ...]     # 同时驻留的档案名
    swaps: tuple[tuple[str, str], ...]   # (先卸, 再载)
    mode: str                     # "all" | "smaller_draft" | "swap" | "judge_only"
    warnings: tuple[str, ...]
```

决策顺序（`residency.policy = "auto"`）：

1. 用 `nvidia-smi` 查空闲显存（查不到就用配置里的 `vram_budget_gb`）。
2. 判断 + 起草**都装得下** → `all`（现在 clef-flash Q8 + 9B Q8 ≈ 19.2 GB，装得下）。
3. 装不下 → **先试着换小起草模型**（`draft` 角色可以有多个候选档案，按 `vram_gb` 从小到大）。
   例：clef-omni Q4(≈19 GB) + 起草 4B Q4(≈2.5 GB) ≈ 21.5 GB，24 GB 卡可行。
4. 还不行 → `swap`：串行换载。**注意代价**：一次分析要「判断 → 起草 → 排序」，judge 前后各一次，
   纯 swap 会重载 judge 两次（每次十几到几十秒），所以这个模式要**明确警告**，只在用户坚持时用。
5. 实在不行 → `judge_only`：只给判断摘要 + 三条候选由**远程**起草来源生成（或明确显示「只判断不起草」）。

> 这三档正好对应「显存不够」时的三种诚实回答：换小模型 / 忍着慢 / 砍功能。绝不假装能跑。

### 6.3 local_server 的改动

- `Models` 数据类升级为「档案集合」（或直接换成 `LoadPlan` 驱动）。
- `_build_args` 增加：`--mmproj <path>`（有就带）、`--mtmd-batch-max-tokens`（图像时）、
  以及把 `batch/ubatch` 从档案里取（现在写死 `= ctx`）。
- 新增 `probe_capabilities(port) -> Capabilities`：GET `/v1/models` 读
  `architecture.output_modalities` / `input_modalities`。
- `ensure(kind)` 变成 `ensure(role)`，并尊重 `LoadPlan`（需要换载就先停另一个）。

## 7. 配置 schema v2 + 迁移

```json
{
  "config_version": 2,
  "profiles": {
    "judge": ["clef-flash-q8", "clef-omni-q4"],
    "draft": ["qwen3.5-9b-q4", "qwen3-4b-q4"]
  },
  "models": {
    "clef-flash-q8": {"role": "judge", "path": "D:/.../Clef-Flash-Q8_0.gguf",
                      "mmproj": "D:/.../mmproj-Clef-Flash-Q8_0.gguf",
                      "port": 8790, "ctx": 8192, "batch": 8192, "ubatch": 8192, "vram_gb": 10.6},
    "clef-omni-q4":  {"role": "judge", "path": "", "mmproj": "", "port": 8790,
                      "ctx": 16384, "batch": 16384, "ubatch": 16384, "vram_gb": 21.0},
    "qwen3.5-9b-q4": {"role": "draft", "path": "D:/.../Jackrong...gguf", "port": 8791,
                      "ctx": 4096, "vram_gb": 6.0},
    "qwen3-4b-q4":   {"role": "draft", "path": "", "port": 8791, "ctx": 4096, "vram_gb": 2.6}
  },
  "perception": {"mode": "auto", "attach_image": "auto", "image_max_side": 1024, "image_quality": 80},
  "residency":  {"policy": "auto", "vram_budget_gb": 0, "idle_unload": true, "idle_seconds": 300},
  "collectors": {"wechat": {"engine": "ocr", "enabled": true},
                 "qqnt":   {"engine": "uia", "enabled": false}}
}
```

**迁移**（`settings.py` 里一次性做，读旧写新，旧键保留不删）：
`local_clef_model` → `models[profiles.judge[0]].path`；
`local_draft_model` → `models[profiles.draft[0]].path`；
`local_clef_port/draft_port/clef_ctx/draft_ctx/gpu_layers` → 对应档案字段；
`local_autostart/idle_unload/idle_seconds` → `residency`；
`local_llama_server`、`local_models_dir` 保持顶层。
迁移要**幂等**，并且迁移后第一次启动的行为和现在完全一样。

## 8. QQ 的前台取舍（已实现）

QQ 只对**前台**窗口构建完整 UIA 树（后台实测 9 个节点），所以读消息前必须让 QQ 处于活动状态。
两种策略都实现了，设置页可切（`app/settings.py::qq_focus_policy`）：

| 选项 | 做法 | 代价 |
| --- | --- | --- |
| **A. 读取时短暂置前（默认）** | `ShowWindow(9)` + `SetForegroundWindow` + 等 800ms 建树，读完把焦点还给原来的窗口 | 屏幕会闪一下、正在打字时可能被打断 |
| **B. 只在 QQ 已在前台时读** | 完全不抢焦点；QQ 在后面就跳过这一拍并提示一句 | 切走就停更 |

不选像素路线的理由：**UIA 拿到的文本是精确的**（不用 OCR、不怕主题变化、不怕 DPI），
而己方/对方有 `container--self` 明确标记，比按气泡颜色/左右对齐猜稳得多。

## 9. Clef-Omni 就绪清单（将来要改的东西 = 只有配置）

| 事项 | 现在 | Clef-Omni 到手后 |
| --- | --- | --- |
| 模型文件 | 不存在 GGUF | 放到本地，填 `models.clef-omni-q4.path` |
| 投影器 | — | 填 `mmproj`（多模态必需；没有它请求带图会 501） |
| llama.cpp | b11515 支持 clef + clef 图像输入，**不支持 `qwen3_omni_moe`** | 升级到含 omni 架构支持的构建 |
| 判断/排序 | clef-flash | 同一个 `/v1/systemone`，**代码不变** |
| 感知 | 文本（可选图像，能提前用 clef-flash + mmproj 验证） | 图像/音频/视频；填 `profiles.judge` 换档案 |
| 显存 | 19.2 GB（两个都驻留） | 约 21 GB（judge 一个），需 `LoadPlan` 换小起草模型 |
| 音频/视频 | 无来源 | 需要新的采集能力（语音消息），**本次不做**，只留 `Media.kind` |
| 上下文 | 8192 | 模型卡写 64k；按 `image_max_side` 和实际 prompt 估，别一上来就拉满 |

**本次架构必须先落地的三件事**（这样将来才真的只改配置）：
① 档案化的模型配置（含 `mmproj` 字段）；② `files` 请求通道 + 能力探测；
③ `LoadPlan` 的显存预算与换载逻辑。

## 10. 改造清单（文件级）

新增：

| 文件 | 内容 |
| --- | --- |
| `app/collectors/__init__.py` | 采集器注册表（按 `chatapps` 的 key 选实现） |
| `app/collectors/base.py` | `Turn` / `Media` / `Capabilities` / `Collector` / `InputTarget` |
| `app/collectors/ocr_wechat.py` | 把 `worker.py` 里现有的找窗口→抓帧→`chat_area`→`Reader` 逻辑搬进来 |
| `app/collectors/uia_map.py` | 纯逻辑：UIA 树 → 会话名/输入框/气泡/己方/顺序/去重（不依赖 uiautomation，可离线自测） |
| `app/collectors/uia_qqnt.py` | QQ NT 采集器：抓树、前台策略（steal/foreground）、发事件 |
| `app/textsim.py` | `similar()`（去重用的文本相似度），从 app/ocr.py 拆出来，让采集层不依赖 numpy/rapidocr |
| `app/perception.py` | 图像编码 + `text/vision/auto` 决策 + 组装 `{state, files}` |
| `app/model_profiles.py` | `ModelProfile` / `LoadPlan` / 显存探测 / 分片 GGUF 解析 / 能力探测封装 |
| `docs/ARCHITECTURE.md` | 本文 |

改动：

| 文件 | 改什么 |
| --- | --- |
| `app/chatapps.py` | `ChatApp` 加 `collector`（`"ocr"` / `"uia"`）与 QQ NT 条目 |
| `app/worker.py` | 采集部分改为「委托给采集器」，自己只管子进程与队列 |
| `core/jev_client.py` | `ask(..., files=)`；`_ask_local` 支持 `files`；细化 501 的两种含义 |
| `core/engine.py` | `analyze(..., files=None)` 透传；其余不动 |
| `app/local_server.py` | 档案驱动参数、`--mmproj`、`ensure(role)` + `LoadPlan`、能力探测 |
| `app/settings.py` | schema v2 + 幂等迁移 + 新 getter |
| `app/overlay.py` | 设置页：档案选择、`mmproj`、感知模式、驻留策略、能力显示；QQ 抢焦点开关与提示 |
| `app/fill.py` | 抽出 `InputTarget`；QQ 用 `qq-msg-editor` 实时矩形 |
| `main.py` | 按 `collectors` 配置选采集器；连接感知层 |
| `scripts/doctor.ps1` / `tools/verify_local.py` | 打印能力探测结果（decisions / image / audio / video）与驻留计划 |
| `requirements.txt` | `uiautomation`（仅在 QQ 模式 import；放可选段） |
| `docs/QQNT.md` | 指向本文，保留实测数据与探针 |

## 11. 分阶段实施

| 阶段 | 做什么 | 验收（可测） |
| --- | --- | --- |
| **P0 缝与地基** ✅ **已完成** | 采集器接口 + `OcrCollector`（微信逻辑原样搬家，行为零变化）；`ChatApp.collector` 引擎字段；`fill_rect` + `input` 事件（采集器可直接给输入框矩形） | 已验：`python -m app.worker` 用假采集器断言事件流转（resumed → area/input/chat/lines → dead）与启动失败不重试；微信路径代码未变 |
| **P1 图像判断** | `app/perception.py` + `files` 通道；下 `mmproj-Clef-Flash-Q8_0.gguf`（624 MB）；`--mmproj` 启动参数 | `tools/verify_local.py` 在「文本」和「文本+截图」两种模式下都跑通并对比 |
| **P2 QQ NT（UIA）** ✅ **已完成** | `app/collectors/uia_map.py`（纯逻辑）+ `uia_qqnt.py`（活体）、`ChatApp` QQNT 条目、`qq_focus_policy` / `qq_selectors` 设置、设置页卡片 | 已验：`python -m app.collectors.uia_map`（合成树）与 `python -m app.collectors.uia_qqnt`（假 uiautomation：事件流转/去重/换会话/两条提示/前台策略）全过；真机用 `probe/probe_qq_uia.py --dump` + `qq_uia_verdict.py` 校准 |
| **P3 Clef-Omni** | 档案换成 clef-omni + `mmproj`；`LoadPlan` 换小起草模型；升 llama.cpp | GGUF 发布后：`/v1/systemone` 判断正常、带图正常、显存不超；起草用 4B 模型也出得来三条 |

## 12. 风险与开放问题

1. **QQ 抢焦点**（第 8 节）——最需要你拍板的一条。默认 A 还是 B？
2. **UIA 类名会变**：QQ 大版本更新后 `qq-msg-editor` / `container--self` 可能改名。对策：类名进配置
   （`collectors.qqnt.selectors`），认不出时明确报错并在 doctor 里给出提示，绝不静默读错。
3. **`uiautomation` 依赖**：只在 QQ 模式 import；如果用户不装，微信模式不受影响（要写清报错信息）。
4. **Clef-Omni 的 GGUF 何时有**：不确定。所以 P1/P2 都不依赖它——P1 用 clef-flash + mmproj 就能验证
   整条视觉链路，P3 只等文件。
5. **图像 token 与 ubatch**：clef 系要求整个 prompt 一个 batch；截图会显著增加 token。
   要实测「1024 边长截图 + 7 道题」在 8192 ctx/ubatch 下够不够；不够就调 `image_max_side`
   或把档案的 ctx 提到 16384。
6. **30B-A3B 的首字延迟**：MoE 3B 激活，理论上很快，但权重 18+ GB 要读进显存，首次加载会慢；
   配合现有的「启动时预热 + 空闲卸载」策略即可。
7. **多模态判断的稳定性**：截图里包含昵称、头像等；判断变准的同时，也要确认模型不会因为
   UI 元素（比如「对方正在输入」）而误判——P1 阶段要用同一段对话做「文本 vs 文本+图」的 A/B 对比。
