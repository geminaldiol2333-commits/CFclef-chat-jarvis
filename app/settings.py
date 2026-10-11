# -*- coding: utf-8 -*-
"""设置持久化。key 硬约束（docs/KICKOFF.md #6）：只进环境变量，绝不落文件；其余设置落 config.json。

key 的持久化走 Windows 用户环境变量（注册表 HKCU\\Environment，跟 setx 写的是同一个地方）。
全程只有两把：判断 JEV_API_KEY、起草 LLM_API_KEY，跟选哪家来源无关。
读的时候先看进程环境，没有就直接读注册表——IDE 启动时把环境快照拿走了，之后再 Run 继承的还是旧环境，
只靠 os.environ 会「保存了下次打开还是没有」。"""
from __future__ import annotations

import ctypes
import json
import os
import sys  # 只为下面这一处：打包后 __file__ 指向临时解包目录，config.json 得放在 exe 旁边才存得住

from core.providers import CUSTOM, DRAFT_PROVIDERS, JEV_ENV, JEV_PROVIDERS, LEGACY, LLM_ENV

_ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
         else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG = os.path.join(_ROOT, "config.json")
_DEFAULT_RELATIONSHIP = "romantic partners"
_DEFAULT_CONTEXT = 10
# 默认全本地：clef-flash 判断/排序，本地文本模型起草，零 API key。想用远程在设置里换。
_DEFAULT_JEV = "clef"
_DEFAULT_DRAFT = "local"
_DEFAULT_CLEF_PORT = 8790
_DEFAULT_DRAFT_PORT = 8791
_DEFAULT_CLEF_CTX = 8192    # clef 的 -c/-b/-ub 都用它；参考编码器 max_length 默认 16384，8192 够用
_DEFAULT_DRAFT_CTX = 8192
_DEFAULT_GPU_LAYERS = 999   # 全放显卡；层数不够的机器可下调
_DEFAULT_IDLE_SECONDS = 300


def _read(name: str, default=None):
    """读 config.json 里的一个字段；每次都重新读文件，改设置不用重启进程。"""
    try:
        with open(_CONFIG, encoding="utf-8") as f:
            value = json.load(f).get(name)
    except (OSError, ValueError):
        return default
    return default if value is None else value


def _int(name: str, default: int, lo: int, hi: int) -> int:
    """整数配置：缺失/脏数据一律退默认值，再夹到 [lo, hi]。"""
    try:
        n = int(_read(name, default))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))

def relationship() -> str:
    return str(_read("relationship") or _DEFAULT_RELATIONSHIP)

def context() -> int:
    """参考上下文条数：起草和判断各看最近多少条消息。3~30，缺失/脏数据一律退默认值。"""
    try:
        n = int(_read("context", _DEFAULT_CONTEXT))
    except (TypeError, ValueError):
        return _DEFAULT_CONTEXT
    return max(3, min(30, n))

def style() -> str:
    """用户自己描述的说话风格（可选，自由文本），只喂给起草模型。默认空 = 只照着最近的消息模仿。"""
    return str(_read("style") or "")

def jev_provider() -> str:
    """判断模型走哪家：openrouter（默认）或 typesafe 直连。"""
    v = _read("jev_provider")
    return v if v in JEV_PROVIDERS else _DEFAULT_JEV

def jev_model() -> str:
    """判断模型 id；空 = 用该来源的默认模型。"""
    return str(_read("jev_model") or "") or JEV_PROVIDERS[jev_provider()].default

def draft_provider() -> str:
    """起草走哪家（见 core/providers.DRAFT_PROVIDERS）。老配置里的 openrouter/deepseek 照样认。"""
    v = _read("draft_provider")
    return v if v in DRAFT_PROVIDERS else _DEFAULT_DRAFT

def draft_provider_name() -> str:
    return DRAFT_PROVIDERS[draft_provider()].name

def draft_model() -> str:
    """起草模型 id；空 = 用该来源的默认模型（有的来源没有默认，那就得自己选）。"""
    return str(_read("draft_model") or "") or DRAFT_PROVIDERS[draft_provider()].default

def draft_base_url() -> str:
    """自定义来源的 Base URL；其余来源用表里的，这里返回空。"""
    return str(_read("draft_base_url") or "") if draft_provider() in CUSTOM else ""

def reply_target() -> bool:
    """群聊指定回复对象：开了才在界面上选回复给谁、才把对象喂给模型。默认关。"""
    return bool(_read("reply_target", False))

def thinking() -> bool:
    """起草时是否开思考模式：慢且贵，默认关。只有 DeepSeek / OpenRouter / Anthropic / Gemini 吃它。"""
    return bool(_read("thinking", False))

def check_update() -> bool:
    """启动时要不要去 GitHub 查一次最新版本号：本 fork 默认关，做到开箱即用全程零联网。
    开了也只带 UA 和当前版本号，不夹带任何聊天内容。"""
    return bool(_read("check_update", False))

def debug_view() -> bool:
    """调试视图：另开一个窗口实时画识别框。默认关，开了子进程才往队列里送帧。"""
    return bool(_read("debug_view", False))


# --- 本地模型（clef-flash 判断 + 文本模型起草，llama-server 由 app/local_server.py 拉起）---
def local_llama_server() -> str:
    """llama-server.exe 路径；空 = 自动找（PATH / WinGet / 常见安装目录）。"""
    return str(_read("local_llama_server") or "")

def local_models_dir() -> str:
    """模型目录；只填一个目录时，自动在里面找 clef GGUF 和一个非 clef 的文本 GGUF。"""
    return str(_read("local_models_dir") or "")

def local_clef_model() -> str:
    """clef-flash 的 .gguf 路径（可填目录，自动取里面唯一/第一个 gguf）。"""
    return str(_read("local_clef_model") or "")

def local_draft_model() -> str:
    """起草文本模型的 .gguf 路径（不能是 clef，它写不了字）。"""
    return str(_read("local_draft_model") or "")

def local_clef_port() -> int:
    return _int("local_clef_port", _DEFAULT_CLEF_PORT, 1024, 65535)

def local_draft_port() -> int:
    return _int("local_draft_port", _DEFAULT_DRAFT_PORT, 1024, 65535)

def local_gpu_layers() -> int:
    """-ngl：放显卡的层数。999 ≈ 全放；显存不够就在 config.json 里调小。"""
    return _int("local_gpu_layers", _DEFAULT_GPU_LAYERS, 0, 999)

def local_clef_ctx() -> int:
    return _int("local_clef_ctx", _DEFAULT_CLEF_CTX, 2048, 131072)

def local_draft_ctx() -> int:
    return _int("local_draft_ctx", _DEFAULT_DRAFT_CTX, 2048, 131072)

def local_autostart() -> bool:
    """启动应用时就把本地模型拉起来（后台预热）；默认开。"""
    return bool(_read("local_autostart", True))

def local_idle_unload() -> bool:
    """空闲多久后卸载模型、把显存还回去；默认开。"""
    return bool(_read("local_idle_unload", True))

def local_idle_seconds() -> int:
    return _int("local_idle_seconds", _DEFAULT_IDLE_SECONDS, 30, 86400)


# --- QQ NT（UIA 采集）---
def qq_focus_policy() -> str:
    """读 QQ 无障碍树时的前台策略。
    steal      （默认）读之前把 QQ 置前、读完把焦点还回去；屏幕会闪一下。
    foreground 完全不抢焦点；QQ 不在前台就跳过这一拍。
    QQ 只对前台窗口构建完整 UIA 树，所以这两个之外没有别的选择。"""
    v = str(_read("qq_focus_policy") or "steal").strip().lower()
    return v if v in ("steal", "foreground") else "steal"


def qq_selectors() -> dict:
    """覆盖 QQ 的 UIA 选择器（QQ 大版本改类名时才需要动）。
    config.json 写成："qq_selectors": {"editor": ["qq-msg-editor"], ...}"""
    v = _read("qq_selectors")
    return v if isinstance(v, dict) else {}


def qq_poll_seconds() -> float:
    """两次读 QQ 无障碍树的最小间隔（秒）。读一次要几百 ms + 等建树 0.8s，
    跟着采集循环的 50ms 跑会把 CPU 吃光。默认 1.2s。"""
    try:
        v = float(_read("qq_poll_seconds", 1.2))
    except (TypeError, ValueError):
        return 1.2
    return max(0.3, min(30.0, v))


def qq_user_grace_seconds() -> float:
    """人在打字/动鼠标的这段时间内不去抢 QQ 焦点（秒）。0 = 关掉这个保护。默认 1.5s。"""
    try:
        v = float(_read("qq_user_grace_seconds", 1.5))
    except (TypeError, ValueError):
        return 1.5
    return max(0.0, min(30.0, v))


def group_judge_only() -> bool:
    """群聊只做判断，不起草候选回复（默认开）。

    群里可能十几个人同时在聊三四件不相关的事，一条「下一句」没意义；
    关掉这个开关才会像单聊那样起草——知道自己群里只有一个话题的人可以关。"""
    return bool(_read("group_judge_only", True))

def _read_env(env_name: str) -> str:
    """进程环境优先；没有就读注册表并带进进程环境，之后 core/ 里按 os.environ 读就有了。"""
    v = os.environ.get(env_name, "").strip()
    if not v:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v = str(winreg.QueryValueEx(k, env_name)[0]).strip()
        except Exception:  # 非 Windows / 没这个值
            v = ""
        if v:
            os.environ[env_name] = v
    return v

def _get_key(env_name: str) -> str:
    """两把 key 之一。新名字空着就退回老版本按来源存的变量（下次保存会抄进新名字）。"""
    return _read_env(env_name) or _read_env(LEGACY[env_name])

def _set_key(env_name: str, value: str) -> None:
    """只写进程环境 + HKCU\\Environment，不写任何文件。"""
    os.environ[env_name] = value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, env_name, 0, winreg.REG_SZ, value)
    except Exception:
        pass  # 非 Windows（本机 Mac 开发）走不到，忽略


def _notify_env() -> None:
    """告诉别的进程环境变量变了。不能用 SendMessageTimeout 对 HWND_BROADCAST：
    它会逐个窗口等回复，超时 5 秒还按窗口数累加，保存按钮在界面线程上就卡死。
    SendNotifyMessage 把消息交出去就返回。"""
    try:
        fn = ctypes.windll.user32.SendNotifyMessageW
        fn.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p)
        fn.restype = ctypes.c_int
        fn(0xFFFF, 0x001A, 0, "Environment")  # HWND_BROADCAST, WM_SETTINGCHANGE
    except Exception:
        pass

def jev_key() -> str:
    """判断那把 key，两家来源共用。"""
    return _get_key(JEV_ENV)

def has_jev_key() -> bool:
    """本地 clef 不需要 key；远程来源才要看密钥。"""
    return JEV_PROVIDERS[jev_provider()].local or bool(jev_key())


def has_llm_key() -> bool:
    """本地起草模型不需要 key；远程来源才要看密钥。"""
    return DRAFT_PROVIDERS[draft_provider()].local or bool(llm_key())

def llm_key() -> str:
    """起草那把 key，所有语言模型来源共用。"""
    return _get_key(LLM_ENV)

has_key = has_jev_key  # 旧名字：界面上「配没配好」问的就是判断模型这把 key

def save(relationship_text: str | None = None, context_n: int | None = None, *,
         jev_provider_text: str | None = None, jev_key_text: str | None = None,
         jev_model_text: str | None = None, draft_provider_text: str | None = None,
         llm_key_text: str | None = None, draft_model_text: str | None = None,
         draft_base_url_text: str | None = None, reply_target_on: bool | None = None,
         style_text: str | None = None, thinking_on: bool | None = None,
         check_update_on: bool | None = None, debug_view_on: bool | None = None,
         local_llama_server_text: str | None = None,
         local_models_dir_text: str | None = None, local_clef_model_text: str | None = None,
         local_draft_model_text: str | None = None, local_autostart_on: bool | None = None,
         local_idle_unload_on: bool | None = None, local_idle_seconds_n: int | None = None,
         qq_focus_policy_text: str | None = None, group_judge_only_on: bool | None = None) -> None:
    """每个参数为空/None = 保留当前值。两把 key 写进程环境 + HKCU\\Environment，不写任何文件。"""
    jev = jev_provider_text if jev_provider_text in JEV_PROVIDERS else jev_provider()
    draft = draft_provider_text if draft_provider_text in DRAFT_PROVIDERS else draft_provider()
    # 没重填就把老变量里的值抄进新名字，迁移一次性做完（_get_key 已经退回读过老的了）
    wrote_key = False
    for env, typed in ((JEV_ENV, jev_key_text), (LLM_ENV, llm_key_text)):
        value = typed or ("" if _read_env(env) else _get_key(env))
        if value:
            _set_key(env, value)
            wrote_key = True
    if wrote_key:
        _notify_env()
    n = context() if context_n is None else max(3, min(30, int(context_n)))
    # 空串 = 清掉，None = 原样留着（读原始字段，别读补过默认值的那个）
    keep = lambda new, name: str(_read(name) or "") if new is None else str(new).strip()
    flag = lambda new, now: now() if new is None else bool(new)
    # 整个 dict 必须在 open(..., "w") **之前**拼好：open 一上来就把文件截断，
    # 之后再 _read() 读到的是空文件，None 那几项就不是「保留」而是被清空了。
    data = {
        # 关系为空 = 只改别的开关（调试视图那种单项保存），别把它写没了
        "relationship": relationship_text or relationship(), "context": n,
        "style": keep(style_text, "style"),
        "jev_provider": jev, "jev_model": keep(jev_model_text, "jev_model"),
        "draft_provider": draft, "draft_model": keep(draft_model_text, "draft_model"),
        "draft_base_url": keep(draft_base_url_text, "draft_base_url"),
        "reply_target": flag(reply_target_on, reply_target),
        "thinking": flag(thinking_on, thinking),
        "check_update": flag(check_update_on, check_update),
        "debug_view": flag(debug_view_on, debug_view),
        "local_llama_server": keep(local_llama_server_text, "local_llama_server"),
        "local_models_dir": keep(local_models_dir_text, "local_models_dir"),
        "local_clef_model": keep(local_clef_model_text, "local_clef_model"),
        "local_draft_model": keep(local_draft_model_text, "local_draft_model"),
        "local_autostart": flag(local_autostart_on, local_autostart),
        "local_idle_unload": flag(local_idle_unload_on, local_idle_unload),
        "local_idle_seconds": (local_idle_seconds() if local_idle_seconds_n is None
                               else max(30, min(86400, int(local_idle_seconds_n)))),
        "qq_focus_policy": (qq_focus_policy() if qq_focus_policy_text is None
                            else (qq_focus_policy_text if qq_focus_policy_text in ("steal", "foreground")
                                  else qq_focus_policy())),
        "group_judge_only": flag(group_judge_only_on, group_judge_only),
    }
    with open(_CONFIG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
