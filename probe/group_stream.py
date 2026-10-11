# -*- coding: utf-8 -*-
"""无头集成测试：群聊「按发言人并发判断」的整条接线（不起真模型、不开窗口）。

只测接线，不测模型：把 main.py 的队列、悬浮窗、本地模型管理器换成假的，真跑一遍
    collector 的 \"lines\" → drain() → start_analyze() → analyze_bg() 线程 → analyze_group()
    → results 队列 → tick() → ov.show_group()
用假的 analyze 当模型（睡 0.25s），于是能验证：
  1. 一上来就把 3 个人的卡先占位上屏（正在判断…），不是等全部跑完才显示；
  2. 每路回来就立刻重画一次（show_group 被调多次，且每次已回的数量递增）；
  3. 并发真的发生（同时最多几路 > 1）；
  4. 群聊不会去加载起草模型（ensure(\"draft\") 一次都没调）。

    python probe/group_stream.py
"""
from __future__ import annotations

import sys
import threading
import time

sys.path.insert(0, ".")

import core.engine as eng                                              # noqa: E402
import main                                                            # noqa: E402
from core.engine import analyze_group as real_group                     # noqa: E402

NAMES = ["王五", "张三", "李四"]
# 注意线上格式和历史的格式不一样：采集器发的是 (who, name, text)，main 存历史时翻成
# (who, text, name)。这里要仿真的是**队列里那一份**。
WIRE = [("her", "张三", "甲一"), ("her", "李四", "乙一"), ("me", None, "嗯"),
        ("her", "张三", "甲二"), ("her", "王五", "丙一")]


class FakeOverlay:
    """记下每个调用；current_chat 一直是同一个群，所以结果会直接上屏。"""

    def __init__(self):
        self.calls: list[tuple] = []
        self.chat = "白金搬砖小分队"

    def current_chat(self):
        return self.chat

    def show_group(self, names, results=None):
        self.calls.append(("show_group", tuple(names), tuple(sorted((results or {}).keys()))))

    def __getattr__(self, name):
        def rec(*a, **k):
            self.calls.append((name, a, tuple(sorted(k))))
        return rec


class FakeManager:
    def __init__(self):
        self.ensured: list[str] = []

    def ensure(self, kind):
        self.ensured.append(kind)
        return f"http://127.0.0.1/9/{kind}"

    def start_reaper(self):
        pass


def main_test() -> int:
    import app.local_server as ls

    fake_ov, fake_mgr = FakeOverlay(), FakeManager()
    main.ov = fake_ov
    main.results = __import__("queue").Queue()
    main.chats.clear()
    ls.manager = lambda: fake_mgr                        # 别真去起 llama-server

    inflight, peak, lock = [0], [0], threading.Lock()
    seen = []

    def fake_analyze(msgs, rel, **k):
        with lock:
            inflight[0] += 1
            peak[0] = max(peak[0], inflight[0])
        seen.append((msgs[-1][2], k.get("reply_to")))
        time.sleep(0.25)                                 # 装成一次本地判断
        with lock:
            inflight[0] -= 1
        return {"answers": {"danger_level": {"score": 2}, "best_action": {"choice": "acknowledge"}},
                "usage": {}, "mode": "judge_only", "candidates": []}

    main.analyze = fake_analyze
    eng.analyze = fake_analyze          # analyze_group 内部调的是 core.engine 的那个名字

    # 采集器说：这个群来了新消息。分析真起线程（假模型睡 0.25s），主线程像 UI 一样
    # 每 50ms pump 一次 tick()，于是能看到「一路回来 → 重画一次」的流式过程。
    main.q = __import__("queue").Queue()
    main.q.put(("lines", "白金搬砖小分队", WIRE, (0, 0, 1000, 800)))
    main.tick()
    deadline = time.time() + 20
    while time.time() < deadline and main.state["busy"]:
        main.tick()
        time.sleep(0.05)
    main.tick()
    assert not main.state["busy"], "分析一直没结束"

    shows = [c for c in fake_ov.calls if c[0] == "show_group"]
    assert shows, f"没有上屏调用：{fake_ov.calls[:6]}"
    assert shows[0][1] == tuple(NAMES), shows[0]          # 先占位：三个人都在
    assert shows[0][2] == (), shows[0]                    # 且一个都还没回
    counts = [len(c[2]) for c in shows]
    assert counts == sorted(counts), f"已回数量应单调递增：{counts}"
    assert counts[-1] == len(NAMES), f"最后应三路都回：{counts}"
    assert peak[0] >= 2, f"没有并发（峰值 {peak[0]}）"
    assert sorted(seen) == sorted([(n, n) for n in NAMES]), seen   # 每路各看自己那句
    assert fake_mgr.ensured == ["clef"], f"群聊不该加载起草模型：{fake_mgr.ensured}"
    got = main.chat_of("白金搬砖小分队")["result"]
    assert got and got["mode"] == "group" and len(got["speakers"]) == 3, got
    print(f"群聊流式接线 ok：上屏 {len(shows)} 次，已回数量 {counts}，并发峰值 {peak[0]}，"
          f"加载 {fake_mgr.ensured}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_test())
