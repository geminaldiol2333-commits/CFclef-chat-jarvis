# -*- coding: utf-8 -*-
"""端到端自检：把两个本地 llama-server 拉起来，跑一次真实的「判断 → 起草 → 排序」。

    python tools/verify_local.py

它做的就是你日常用这套程序的完整链路，只是对话是写死的样例、候选只打印不填入。
任何一步失败都会打印具体原因（找不到 llama-server / 模型架构不对 / 端口被占 / 权重没下载完 …）。
不需要 API key，也不联网。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main() -> int:
    try:
        from app import local_server, settings
        from core.engine import analyze
        from core.questions import CHOICE_LABELS
    except Exception as exc:
        print(f"[FAIL] 导入失败：{exc}")
        print("       先装依赖：pip install -r requirements.txt")
        return 1

    print("== 配置 ==")
    mgr = local_server.manager()
    print(mgr.describe())

    exe = mgr.llama_server()
    if not exe:
        print("\n[FAIL] 找不到 llama-server.exe。装 llama.cpp 或在设置页填它的完整路径。")
        return 1
    for kind in ("clef", "draft"):
        path = mgr.model_path(kind)
        if not path or not os.path.isfile(path):
            print(f"\n[FAIL] {kind} 的 GGUF 没配好：{path or '（空）'}")
            return 1

    print("\n== 启动本地模型（首次要读几 GB 权重）==")
    t0 = time.time()
    try:
        clef_base = mgr.ensure("clef", timeout=600)
        print(f"[ok] clef  {clef_base}  ({time.time() - t0:.1f}s)")
        t1 = time.time()
        draft_base = mgr.ensure("draft", timeout=600)
        print(f"[ok] draft {draft_base}  ({time.time() - t1:.1f}s)")
    except Exception as exc:
        print(f"[FAIL] 启动失败：{exc}")
        mgr.shutdown()
        return 1

    messages = [
        ("her", "你今天又忘了是吧"),
        ("me", "忘了什么"),
        ("her", "算了 你忙吧 我习惯了"),
    ]
    print("\n== 跑一次真实分析 ==")
    t0 = time.time()
    try:
        result = analyze(messages, settings.relationship(), context=settings.context(),
                         model=settings.draft_model() or None,
                         provider=settings.draft_provider(), base_url=draft_base,
                         style=settings.style(), thinking=settings.thinking(),
                         jev_provider=settings.jev_provider(),
                         jev_model=settings.jev_model() or None,
                         jev_base_url=clef_base)
    except Exception as exc:
        print(f"[FAIL] 分析失败：{exc}")
        mgr.shutdown()
        return 1
    print(f"[ok] 完成，用时 {time.time() - t0:.1f}s\n")

    answers = result.get("answers") or {}
    for name, labels in CHOICE_LABELS.items():
        choice = (answers.get(name) or {}).get("choice")
        if choice:
            print(f"  {name}: {choice}  ({labels.get(choice, '')})")
    score = (answers.get("danger_level") or {}).get("score")
    if score is not None:
        print(f"  danger_level: {score}/9")
    print()
    for i, cand in enumerate(result.get("candidates") or []):
        mark = "  <- 推荐" if i == result.get("best_index") else ""
        pct = (result.get("scores") or [0, 0, 0])[i]
        print(f"  [{i + 1}] ({pct * 100:.0f}%) {cand}{mark}")

    mgr.shutdown()
    print("\n[ok] 全链路通过：本地 clef-flash 判断 + 本地文本模型起草都正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
