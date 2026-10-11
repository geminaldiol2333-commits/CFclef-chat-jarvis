# CFclef-chat-jarvis

聊天窗口旁挂的回复辅助，**全程本地运行**：本地 OCR 读屏 → 本地 **Cloudflare clef-flash** 判断意图/情绪 →
本地文本模型写 3 条候选回复 → 一键填入输入框。**发送永远手动，程序不替你按发送。**

不上传、不要 API key、不注册账号；聊天内容只在你自己的机器上、只走 `127.0.0.1`。

> 本仓库是 [jev-chat-windows](https://github.com/jev-chat/jev-chat-windows) 的二次开发（fork），
> 把它的云端判断内核换成了本地 clef-flash。项目关系与授权见
> [与上游项目的关系](#与上游项目的关系)，以及 [NOTICE](NOTICE)。

---

## 与上游项目的关系

上游链是这样的：

| 项目 | 说明 | 授权 |
| --- | --- | --- |
| [Finderchangchang/jev-chat-JARVIS](https://github.com/Finderchangchang/jev-chat-JARVIS) | Android 版，**判断内核与题目口径的源头**（那 7 道判断题就是它定的） | MIT |
| [jev-chat/jev-chat-windows](https://github.com/jev-chat/jev-chat-windows) | Windows 版，把采集换成窗口截图 + 离线 OCR；**本项目直接 fork 自它** | MIT |
| **本仓库 CFclef-chat-jarvis** | 把上面两个的「云端 Jev 判断」换成**本地 clef-flash**，起草也换成本地文本模型 | **Apache-2.0**（上游部分仍为 MIT） |

保留的东西：OCR 读屏、气泡识别、会话跟踪、群聊发言人、悬浮窗交互、三段式流程（判断 → 起草 → 排序）、
隐私不落盘的约定。改掉的东西只有一个核心：**模型调用从云端换到本地**，外加重命名与授权。

上游的 MIT 授权原文完整保留在 [LICENSES/MIT-upstream.txt](LICENSES/MIT-upstream.txt)。
「Jev」「jev-chat」「Jev 聊天助手」和 chatjevs.com 归上游作者所有，本项目不拿它们暗示上游出品或背书，
只作出处说明。

## 这个版本改了什么

| | 上游 jev-chat-windows | 本仓库 CFclef-chat-jarvis |
| --- | --- | --- |
| 判断 + 排序 | OpenRouter / TypeSafe 云端 Jev API | **本地 clef-flash**（llama.cpp `POST /v1/systemone`） |
| 起草 3 条候选 | DeepSeek 官网 / OpenRouter 等云端 | **本地文本模型**（llama.cpp OpenAI 兼容接口） |
| API key | 两把，必填 | **本地模式零 key**；远程来源保留为可选 |
| 联网 | 每次分析都出网 | 本地模式下**不出网**（更新检查默认关） |
| 启动模型 | 不用管 | 程序**自动拉起 llama-server**，空闲后自动卸载释放显存 |
| 授权 | MIT（发布包含 GPLv3 组件） | **Apache-2.0**（与 clef-flash 同协议） |

## 为什么需要两个模型

**clef-flash 是决策模型，不是聊天模型。** Cloudflare 在 2026 年 10 月开源（Apache-2.0），
9B、以 Qwen3.5-9B 为底座。它吃一份「state + 一组带类型的题目」，**一次前向就把每个选项的概率吐出来，
本身不写文字**。这恰好就是这套程序判断那一步的原生形态——上游那 7 道题本来就是一串
`noul / choice / score`，直接丢给 clef-flash 即可，不需要再解析自然语言。

但它写不了那 3 条候选回复。所以起草那一步必须另配一个普通文本模型：

```
截图 → 本地 OCR
      → clef-flash：判断意图 / 需要 / 建议动作 / 紧张度（一次 /v1/systemone）
      → 本地文本模型：顺着判断写 3 条候选（/v1/chat/completions）
      → clef-flash：给三条候选排序（再一次 /v1/systemone）
      → 悬浮窗 → 点「填入」→ 你自己看一眼、改一改、按发送
```

两个模型都由本程序用 llama.cpp 的 `llama-server` 拉起来，各占一个端口，**只监听 127.0.0.1**。

> ⚠️ **别下错模型。** 有一批 clef-flash 的 GGUF 只量化了「语言塔」（`general.architecture = qwen35`，
> 没有 `decision.*` 张量），那种打 `/v1/systemone` 会返回 `501 This model is not a decision model`。
> 判断用的必须是 `ggml-org/Clef-Flash-GGUF` 里的决策版（下面给了下载地址）。

## 隐私优势

这是本项目相对上游最大的差别。本地模式下：

- **零出网。** OCR 在本地、判断在本地、起草在本地，全程没有任何一个字节离开你的电脑。
  上游每分析一次要把最近 N 条对话发给 OpenRouter / DeepSeek；本版本默认一次都不发。
- **零 API key。** 不用注册任何服务，设置里也没有必填的密钥框（远程来源是可选功能，默认不选）。
- **零账号、零遥测、零自建服务器。** 项目没有服务器，也不收集任何数据。
- **模型只连本机。** llama-server 绑在 `127.0.0.1:8790`（clef）和 `127.0.0.1:8791`（起草），
  局域网里别的机器也访问不到。
- **截图只在内存里。** 捕获帧是 numpy 数组，不落盘、不进日志；主程序里没有 `.save()`。
- **聊天内容不进日志。** llama-server 的日志压到只报错误，且只在内存里留最后 200 行供排错，
  不写文件。
- **更新检查默认关闭。** 从上游的「默认开」改成默认关，做到开箱即用全程零联网。
  想用可以在设置里打开：那时只向本仓库的 GitHub Releases 发一个 GET，只带 UA 和版本号，
  不夹带任何聊天内容。
- **硬约束照旧**：只读你自己屏幕上、你本来就有权看的窗口；不 hook、不注入、不读对方数据库、
  不解密、绝不自动发送、不碰转账红包。

发出去的内容（仅当你在设置里**主动切到远程来源**时）才等于上游那套：最近 N 条对话文本 + 关系设置 +
你最近 12 条短消息（当口吻样本）+ 你填的说话风格。默认不选远程就永远不发。

## 设备要求

| 项目 | 最低 | 推荐 |
| --- | --- | --- |
| 系统 | Windows 10 1903+ / Windows 11 | 同上 |
| 显卡 | NVIDIA，**12 GB 显存**（clef 用 Q4_K_M 量化 ≈ 6.5 GB） | **16–24 GB 显存**（clef Q8_0 ≈ 9.7 GB + 起草模型） |
| 内存 | 16 GB | 32 GB |
| 硬盘 | ~15 GB 可用 | ~25 GB（两个模型 + llama.cpp） |
| 其它 | 聊天窗口开着、不要最小化 | 同上 |

说明：

- **判断 + 起草两个模型会同时驻留显存**（先判断、再起草），所以显存要按「clef + 起草模型 + KV cache」
  一起算。用 clef Q8_0 + 语言塔 Q8 ≈ **20 GB**（把 `local_clef_ctx` / `local_draft_ctx` 调小可以再省）；
  clef Q4_K_M（6.5 GB）+ 一个 9B Q4 起草模型 ≈ 12–13 GB。
- 参考配置：**RTX PRO 4000 Blackwell 24 GB**，clef Q8_0 + 9B 起草模型 Q4，3840 token 上下文
  跑得很宽松；16 GB 卡建议 clef 用 Q4_K_M。
- **纯 CPU 也能跑，但很慢**（一帧判断可能几十秒），不推荐。
- 显存不够时，在 `config.json` 里把 `local_gpu_layers` 调小（部分层放内存），或给 clef 换更小的量化，
  或把 `local_clef_ctx` / `local_draft_ctx` 调小。
- 需要一个**支持 clef 的 llama.cpp 构建**（含 [PR #29831](https://github.com/ggml-org/llama.cpp/pull/29831)，
  实测 `b11515` 可用）。程序会检测并在报错里说清楚；`scripts/doctor.ps1` 也能查。

## 快速开始

### 方式一：下载即用（推荐给普通用户）

1. 在 [Releases](https://github.com/geminaldiol2333-commits/CFclef-chat-jarvis/releases) 下载
   `cfclef-chat-jarvis-vX.Y.Z.zip`，解压到一个固定目录（整个文件夹一起，exe 要用旁边那堆文件）。
2. 双击 `cfclef-chat-jarvis.exe`。
3. 首次启动会弹设置页 → 拉到底部「**本地模型**」卡片，填三样东西（见下），保存。
4. 打开聊天窗口、点开一个会话 → 对方来消息 → 悬浮窗给出判断 + 3 条候选 → 点「填入」。

> exe 没签名，SmartScreen 会拦一下：「更多信息」→「仍要运行」。

### 方式二：源码运行（给开发者和想自己打包的人）

```bat
git clone https://github.com/geminaldiol2333-commits/CFclef-chat-jarvis.git
cd CFclef-chat-jarvis
pip install -r requirements.txt
python main.py
```

Python 建议 **3.11**（`rapidocr-onnxruntime` 1.4.x 不支持 3.13+）。自测：

```bat
python tools\verify_local.py      :: 真的拉起两个模型跑一次完整分析
scripts\doctor.bat                :: 环境检查（也提供 scripts\doctor.ps1，不用 Python）
python -m core.providers          :: 各模块自带 __main__ 自测
```

## 准备 llama.cpp

clef-flash 需要较新的 llama.cpp。装法二选一：

**A. WinGet（最简单）**

```bat
winget install llama.cpp
```

**B. 手动下载 Windows CUDA 构建**（国内 GitHub 直连慢，可用代理前缀，例如 `gh-proxy.com`）

```
https://gh-proxy.com/https://github.com/ggml-org/llama.cpp/releases/download/b11515/llama-b11515-bin-win-cuda-13.4-x64.zip
```

解压后把目录里的 `llama-server.exe` 路径填进设置页的「llama-server 路径」。
CUDA 版还需要 `cudart64_*.dll` / `cublas64_*.dll` / `cublasLt64_*.dll`；若压缩包里没有，
把它们和 `llama-server.exe` 放同一目录即可。留空路径时程序会自动在 PATH / WinGet / 常见目录里找。

## 下载模型

模型权重因体积原因**不在本仓库**。clef-flash 是 Apache-2.0，可自由下载、离线使用。
国内推荐 **hf-mirror** 或 **ModelScope**：

**判断用 · clef-flash 决策版（必须这个）**

| 来源 | 地址 |
| --- | --- |
| hf-mirror | `https://hf-mirror.com/ggml-org/Clef-Flash-GGUF` |
| ModelScope | `https://modelscope.cn/models/ggml-org/Clef-Flash-GGUF` |
| 源仓库 | `https://huggingface.co/ggml-org/Clef-Flash-GGUF`（需自备网络） |

选 `Clef-Flash-Q8_0.gguf`（9.66 GB，24 GB 显存推荐）或 `Clef-Flash-Q4_K_M.gguf`（6.49 GB，16 GB 显存推荐）。

**起草用 · 任意指令型文本 GGUF**（本项目不含任何模型）

任选一个 7B–14B 的**非推理型**指令模型即可。只想下一份的话，用 clef-flash 自己的
**语言塔**量化版就行（叫 `Cloudflare_clef-flash-Q8_0.gguf` 之类的），它认得 `enable_thinking`，
关掉思考后起草只要 ~1 秒，正好和 clef 共用同一个底座：

| 来源 | 地址 |
| --- | --- |
| hf-mirror | `https://hf-mirror.com/bartowski/Cloudflare_clef-flash-GGUF` |
| ModelScope | `https://modelscope.cn/models/bartowski/Cloudflare_clef-flash-GGUF` |

> ⚠️ **别用推理蒸馏（reasoning-distilled）模型当起草模型。** 那类模型的 chat template 会无条件先想一段，
> 400 token 的预算会被思考过程吃光，正文一个字都吐不出来。真要用，就在设置里打开「起草时开启思考模式」
> 把预算加到 4000。
>
> ⚠️ **起草模型也不能是 clef-flash 决策版**（它写不了字）。语言塔可以，决策版不行。

多线程下载（hf-mirror）。用官方 CLI：

```bat
set HF_ENDPOINT=https://hf-mirror.com
hf download ggml-org/Clef-Flash-GGUF Clef-Flash-Q8_0.gguf --local-dir D:\AI_Tools\models\Clef-Flash-GGUF
:: 老版 CLI 把 hf 换成 huggingface-cli
```

也可以直接从 hf-mirror / ModelScope 的网页下载，或用任意支持断点续传的下载器（IDM、Aria2 等）。
下完把 `.gguf` 路径填进设置页即可。

## 首次配置

设置页底部「**本地模型**」卡片里填：

| 字段 | 说明 |
| --- | --- |
| llama-server 路径 | 可留空，自动在 PATH / WinGet / 常见目录里找 |
| 模型目录 | 可选。只填目录时，程序自动在里面找 clef GGUF 和一个非 clef 的文本 GGUF |
| clef-flash GGUF | 判断用，**决策版**；可填 `.gguf` 文件，也可填放它的目录 |
| 起草文本 GGUF | 普通指令模型；不能是 clef-flash |
| 启动时预热本地模型 | 默认开。开了程序一启动就在后台把权重读进显存，第一条消息不用等 |
| 空闲后卸载模型 | 默认开。空闲一段时间后关掉 llama-server，把显存让给别的程序 |

填完点「**启动 / 检测**」：真的把两个模型拉起来并显示状态；出错会显示 llama-server 的日志
（最多 200 行，只在内存里）。之后设置页顶部「判断 · Jev」和「起草 · 语言模型」两组，来源默认就是
「本地」——本地来源不显示密钥框，也没有必填 key。

## 日常怎么用

- 聊天窗口开着、别最小化（被别的窗口盖住没事），把要聊的会话点开。
- 对方来一条消息 → 悬浮窗几秒后给判断摘要 + 三条候选（带 clef-flash 给的胜出概率）。
- 点「填入」→ 文字进输入框 → **你自己看一眼、改一改、按发送**。程序永远不碰发送。
- 你切到哪个会话，悬浮窗就跟到哪个；群聊会带发言人名。
- 暂时不想让它读聊天：标题栏开关拨到「已暂停」。

**怎么触发模型调用**：只有对方来新消息（或你在群里换了回复对象）才调一次。静默期零调用——
十分钟没人说话，llama-server 会被空闲卸载，显存归零。

## config.json 高级项

`config.json` 和 exe 放在一起（源码跑就在仓库根目录），`config.json` 不进仓库。
端口、显存层数、上下文这些在设置页 UI 里没露出来，需要改这里（改完重启生效）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `local_llama_server` | `""` | `llama-server.exe` 路径 |
| `local_models_dir` | `""` | 自动找模型的目录 |
| `local_clef_model` | `""` | clef 决策模型 `.gguf` 路径或所在目录 |
| `local_draft_model` | `""` | 起草文本模型 `.gguf` 路径或所在目录 |
| `local_clef_port` | `8790` | clef 端口（只绑 127.0.0.1） |
| `local_draft_port` | `8791` | 起草端口（只绑 127.0.0.1） |
| `local_gpu_layers` | `999` | `-ngl`，放显存的层数；显存不够就调小 |
| `local_clef_ctx` | `8192` | clef 上下文，同时也是它的批大小（clef 要求整个输入一批算完） |
| `local_draft_ctx` | `8192` | 起草模型上下文 |
| `local_autostart` | `true` | 启动时预热 |
| `local_idle_unload` | `true` | 空闲卸载 |
| `local_idle_seconds` | `300` | 空闲多少秒后卸载（30–86400） |
| `check_update` | `false` | 启动时查 GitHub 版本号（本 fork 默认关） |
| `local_clef_mmproj` | `""` | clef 的多模态投影器（mmproj）路径；填了判断模型才能看图 |
| `perceive_media` | 默认跟随 mmproj | 要不要把非文字消息（图片/表情包）的像素喂给判断模型 |
| `group_judge_only` | `true` | 群聊只给判断、不起草候选 |

## 支持的聊天软件

| 软件 | 采集方式 | 自己的气泡 | 窗口 |
| --- | --- | --- | --- |
| 微信 | RapidOCR（中英文模型）+ 像素定位 | 绿色 | 标题为「微信」的主窗口 |
| **QQ NT** | **UIA 无障碍树**（不 OCR、不比颜色） | `container--self` 类名 | 效率模式的主面板；经典模式下选真正在聊的那个窗口 |

采集器在 `app/collectors/`：微信是 `ocr_wechat.py`（截图 + OCR），QQ NT 是 `uia_qqnt.py`
（读无障碍树，纯逻辑在 `uia_map.py`，可离线自测）。加一个软件 = `app/chatapps.py` 加一条
+ 必要时写一个采集器；`worker.py` / `main.py` 不用改。

### QQ NT 怎么用

**单聊起草，群聊只判断。**

| | 行为 |
| --- | --- |
| 单聊 | 判断 → 起草 3 条候选 → 排序，点「填入」 |
| **群聊** | **只给判断摘要，不起草候选**，也不会去加载起草模型。群里可能十几个人同时在聊三四件不相关的事，一条「下一句」没有意义。设置页「群聊只给判断，不起草」可关（只有开一个话题的小群才建议关） |

非文字消息（图片 / 表情包 / 语音 / 视频 / 文件 / 红包…）在上下文里给**占位**（`[图片]` 这类）
而不是直接消失；配了 clef 的 **mmproj** 之后，这些占位还会把图一起喂给判断模型
（多模态判断，Clef-Omni 那种模型直接受益）。

1. **必须带 `--force-renderer-accessibility` 启动 QQ**（不带的话它的聊天内容根本不在无障碍树里，
   什么都读不到）。最省事：双击 `scripts\restart-qq-with-a11y.bat`——它会关掉 QQ 再带开关重新启动
   （**未发送的草稿会丢**）。手动等价于 `QQ.exe --force-renderer-accessibility`。
   采集器会查 QQ 进程命令行确认这一点，没开就直接提示你。
2. **QQ 必须处于活动状态**（或允许读取时短暂置前）。设置页「QQ NT（UIA 读取）」卡片里选：
   *允许读取时短暂置前（默认，人正在打字时不抢）* 或 *只在 QQ 已在前台时读取*。
3. **群聊能读，但只给判断**：群聊消息靠「消息容器分层 + 群等级/群头衔做锚点」读出来
   （昵称和正文都能对上，已实测），但**不起草候选回复**。细节和实测数据见
   [docs/QQNT.md](docs/QQNT.md)。
4. 认不出时只报可诊断的提示（例如“先点开一个会话”“QQ 没带无障碍开关启动”），
   **不会退回 OCR**，也不会静默读错。

离线自测（不装 uiautomation、不开窗口）：

```bat
python -m app.collectors.uia_map      :: 合成树的纯逻辑
python -m app.collectors.uia_qqnt     :: 假 uiautomation：事件流转/去重/换会话/提示
```

真机校准（只读：不启动、不点击、不截图、不含消息文本）：

```bat
python probe\probe_qq_uia.py --dump qq_tree.json
python probe\qq_uia_verdict.py qq_tree.json
```

## 故障排查

| 现象 | 原因 / 处理 |
| --- | --- |
| `501 This model is not a decision model` | clef 位置填了「语言塔」量化版，不是决策版。换成 `ggml-org/Clef-Flash-GGUF` 的 `Clef-Flash-Q8_0.gguf` / `Q4_K_M` |
| 起草候选是空的 / 只有一条 | 起草模型是推理蒸馏模型，思考过程把 max_tokens 吃光了。换成非推理型指令模型（或 clef 语言塔），或在设置里打开「起草时开启思考模式」 |
| `本地 llama-server 不支持 clef（/v1/systemone 404）` | llama.cpp 太旧。升级到含 PR #29831 的构建（实测 `b11515` 可用） |
| 找不到 llama-server.exe | 填「llama-server 路径」，或 `winget install llama.cpp`；跑 `scripts\doctor.ps1` 定位 |
| 端口 8790/8791 被占用 | 改 `config.json` 里的 `local_clef_port` / `local_draft_port` |
| 显存不够 / 报 CUDA OOM | 调小 `local_gpu_layers`；clef 换 Q4_K_M；或调小 `local_clef_ctx` / `local_draft_ctx` |
| 第一次分析特别慢 | 正常，首次要把几 GB 权重读进显存。开着「启动时预热」就不用在消息到来时等 |
| 没人说话后显存被释放了 | 「空闲后卸载」在起作用，下一条消息会自动重新加载 |
| SmartScreen 拦截 exe | exe 未签名：「更多信息」→「仍要运行」 |
| 候选质量不满意 | 换更好的起草文本模型（这是起草质量的主导因素）；clef 负责判断和排序，口径来自上游题目 |

## 已知限制

- 判断与起草两个模型同时驻留显存，显存占用按两者之和算；没有做「用完一个换另一个」的串行加载。
- 只适配微信；QQ NT 见 [docs/QQNT.md](docs/QQNT.md)（已调研，未实现）。

## 授权

- **本项目新增/修改的代码：Apache License 2.0**（见 [LICENSE](LICENSE)）。选它一部分原因就是
  和 clef-flash 同协议。
- **上游代码：MIT**，原文保留在 [LICENSES/MIT-upstream.txt](LICENSES/MIT-upstream.txt)。
  MIT 允许再许可，因此上游部分仍然是 MIT，本项目的改动部分按 Apache-2.0。
- **模型权重不在本仓库**，各自授权：clef-flash 是 Apache-2.0；你用的起草模型看它自己的协议。
- **发布包整体受 GPLv3 约束**：PyInstaller 打的包里含 GPLv3 的 PySide6-Fluent-Widgets
  （非商用免费，商用需另行购买授权）。细节见 [NOTICE](NOTICE)。

## 边界

这是个人自用工具：只读你自己屏幕上、你本来就有权查看的对话；不 hook、不注入、不读对方数据库或
进程内存、不调对方任何私有接口；绝不自动发送；不碰转账、红包、收款相关界面。
**AI 建议仅供参考，按你自己的语气调整后再发送。**
