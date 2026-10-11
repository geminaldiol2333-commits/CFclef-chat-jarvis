# -*- coding: utf-8 -*-
"""整条链的唯一入口：对话 → Jev 判断 → 带着判断起草 3 条 → Jev 排序 → 结构化结果。

平台无关。SSE 消费者、悬浮窗、命令行 demo 都只调 analyze()。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

try:
    from .draft import draft_candidates
    from .jev_client import JevError, ask
    from .questions import (JUDGE_QUESTIONS, build_rank_question, build_state, group_speakers,
                            guidance_text, up_to_speaker)
except ImportError:
    from draft import draft_candidates
    from jev_client import JevError, ask
    from questions import (JUDGE_QUESTIONS, build_rank_question, build_state, group_speakers,
                           guidance_text, up_to_speaker)

_REPLY_IDX = {"reply_a": 0, "reply_b": 1, "reply_c": 2}


def _add_usage(total: dict, one: dict | None) -> None:
    """两次 Jev 调用的 usage 相加（tokens、cost）；非数字的字段后来的盖掉前面的。"""
    for k, v in (one or {}).items():
        total[k] = total.get(k, 0) + v if isinstance(v, (int, float)) else v


def analyze_group(messages: list, relationship: str, *, context: int = 10, workers: int = 3,
                  on_result=None, base_urls: list[str] | None = None,
                  max_speakers: int = 4, **kw) -> dict:
    """群聊：**按发言人并发**做判断，不起草也不排序。

    为什么按人拆：群里十几个人可能在聊三四件正交的事，一条「下一句说什么」是伪命题；
    但「针对张三这句我该怎么接」是有意义的，而且拆开后每路都拿自己的尾句当上下文，
    不会互相污染（见 questions.up_to_speaker）。**不做话题聚类**：那是又一次模型调用，
    还可能分错，按发言人拆是机械的、不会错的近似。

    并发只是把 N 路请求同时发出去、谁先回谁先上屏——本地 clef 服务在服务端串行（实测 -np>1
    对 /v1/systemone 无效，加速比 1.00x），所以别指望总时长变短；真并行要 base_urls 给多份
    实例（将来多开 clef 时把地址列表传进来就会轮流分配到不同实例）。

    on_result(单路结果 dict) 在**池线程**里回调，只允许往队列里塞东西（不能碰 Qt/共享字典）。
    base_urls 给了就按顺序轮流给每路用（None 长度 == 1 时就是同一个地址）。
    返回 {mode: "group", speakers: [{name, answers, usage, error}...], ...}，顺序 = 最近说话的排前。
    """
    names = group_speakers(messages, keep=context, limit=max_speakers)
    if not names:                                   # 群里一条带名字的都没有：退回整段判断
        one = analyze(messages, relationship, context=context, group_judge_only=True, **kw)
        return {**one, "speakers": []}
    urls = [u for u in (base_urls or []) if u]
    usage: dict = {}

    def one_route(i: int, name: str) -> dict:
        route = dict(kw)
        if urls:
            route["jev_base_url"] = urls[i % len(urls)]
        try:
            got = analyze(up_to_speaker(messages, name), relationship, context=context,
                          reply_to=name, group_judge_only=True, **route)
            item = {"name": name, "answers": got.get("answers") or {},
                    "usage": got.get("usage") or {}, "error": ""}
        except Exception as exc:                    # 一路挂了不影响其他路
            item = {"name": name, "answers": {}, "usage": {},
                    "error": f"{type(exc).__name__}: {exc}"}
        if on_result is not None:
            on_result(item)
        return item

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(names))),
                            thread_name_prefix="clef-group") as pool:
        items = list(pool.map(lambda pair: one_route(*pair), list(enumerate(names))))
    for item in items:
        _add_usage(usage, item.get("usage"))
    return {"mode": "group", "speakers": items, "candidates": [], "best_index": 0,
            "best_reply": "", "scores": [], "answers": {}, "usage": usage}


def analyze(messages: list, relationship: str, model: str | None = None,
            timeout: float = 30, context: int = 10, provider: str = "local",
            base_url: str | None = None, reply_to: str | None = None, style: str = "",
            thinking: bool = False, jev_provider: str = "clef",
            jev_model: str | None = None, jev_base_url: str | None = None,
            group_judge_only: bool = False, files: list | None = None) -> dict:
    """messages: [(from, text)] from ∈ {her, me}，最新一条在最后；
    群聊里可以带第三项 name（说这句话的人），单聊不带。
    context: 起草和判断各看最近多少条消息（用户设置里的「参考上下文」）。
    provider: 起草走哪家（core.providers.DRAFT_PROVIDERS），base_url 只有自定义来源要传。
    jev_provider / jev_model: 判断和排序走哪家、哪个模型（core.providers.JEV_PROVIDERS）。
    jev_base_url: 判断走本地 clef-flash 时，llama-server 的地址（app/local_server.py 算出来）。
    reply_to: 群聊里指定回复给谁；None = 正常回复。
    style: 用户自己描述的说话风格，只影响起草。
    thinking: 起草时是否开思考模式，只影响起草，默认关。
    group_judge_only: 群聊只做判断，不起草也不排序（默认关，由调用方按设置传）。
        群聊里可能十几个人同时在聊几件不相关的事，一条「下一句」没有意义；
        这种时候连起草模型都不用加载。开启后返回的 candidates 是空的、mode="judge_only"。
    files: 给判断模型看的图，每项 (mime, bytes)。**只传给判断那一步**（排序不需要看图，
        免得白花 token），只有本地多模态 clef（配了 mmproj）支持。
    model / jev_model = None 用该来源的默认模型。

    返回 {candidates, best_index, best_reply, scores, answers, usage, reply_to}。
    scores 是每条候选的胜出概率（0~1），取自 best_reply.probabilities，取不到记 0.0。
    只有对方最新说话时才有意义调它——是不是该触发由调用方判断（看 latest_from）。

    三段式（issue #4）：先让 Jev 答 7 道判断题，把判断当小抄喂给起草，最后 Jev 只排序。
    判断那次挂了就退回老路：盲起草 + 判断和排序一次问完，行为跟以前一样。usage 是两次之和。
    """
    state = build_state(messages, relationship, keep=context, reply_to=reply_to)
    usage: dict = {}
    answers: dict = {}
    judged = False

    # 群聊只判断：不起草、不排序。群里多人多话题时「下一条说什么」是个伪命题，
    # 而且这样连起草模型都不必修/加载。
    if group_judge_only and (state.get("chat") or {}).get("is_group"):
        first = ask(state, dict(JUDGE_QUESTIONS), timeout=timeout,
                    provider=jev_provider, model=jev_model, base_url=jev_base_url,
                    files=files)
        answers = first.get("answers") or {}
        _add_usage(usage, first.get("usage"))
        return {
            "candidates": [], "best_index": 0, "best_reply": "", "scores": [],
            "answers": answers, "usage": usage, "reply_to": reply_to,
            "mode": "judge_only",
        }

    try:
        first = ask(state, dict(JUDGE_QUESTIONS), timeout=timeout,
                    provider=jev_provider, model=jev_model, base_url=jev_base_url,
                    files=files)
        answers = first.get("answers") or {}
        _add_usage(usage, first.get("usage"))
        judged = True
    except JevError:
        pass  # 退回盲起草 + 老的一次合问；错误不打日志（里面可能带请求内容）

    candidates = draft_candidates(messages, relationship, provider=provider, model=model,
                                  base_url=base_url, timeout=timeout, keep=context,
                                  reply_to=reply_to, style=style, thinking=thinking,
                                  guidance=guidance_text(answers) if judged else None)
    if not candidates:  # 注入过滤可以把起草结果全扔掉；接着取 [0] 会 IndexError
        raise JevError("起草结果没有可用候选回复")

    questions = {} if judged else dict(JUDGE_QUESTIONS)
    if len(candidates) >= 2:  # 起草只给了 1 条就没什么可排的，判断题照问
        questions.update(build_rank_question(candidates))
    if questions:
        try:
            second = ask(state, questions, timeout=timeout,
                         provider=jev_provider, model=jev_model, base_url=jev_base_url)
        except JevError:
            if not judged:  # 老路只有这一次调用，挂了就是挂了
                raise
            second = {}  # 判断还在，只是没排上序：下面按第一条推荐
        answers = {**answers, **(second.get("answers") or {})}
        _add_usage(usage, second.get("usage"))

    best_key = (answers.get("best_reply") or {}).get("choice")
    best_index = _REPLY_IDX.get(best_key, 0)  # 解析不出就退第一条
    if best_index >= len(candidates):
        best_index = 0

    probabilities = (answers.get("best_reply") or {}).get("probabilities") or {}
    scores = [0.0, 0.0, 0.0]
    for key, idx in _REPLY_IDX.items():
        try:
            scores[idx] = float(probabilities.get(key, 0.0))
        except (TypeError, ValueError):
            scores[idx] = 0.0  # 脏数据一律按 0 处理

    return {
        "candidates": candidates,
        "best_index": best_index,
        "best_reply": candidates[best_index],
        "scores": scores,
        "answers": answers,
        "usage": usage,
        "reply_to": reply_to,
        "mode": "draft",
    }


if __name__ == "__main__":
    # 候选被过滤光时要抛 JevError，不能在取第一条时 IndexError。
    # 另外：群聊 judge_only 模式不能碰起草函数。
    from unittest.mock import patch

    with patch("__main__.ask", return_value={"answers": {}, "usage": {}}), \
         patch("__main__.draft_candidates", return_value=[]):
        try:
            analyze([("her", "hello")], "friends")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "没有可用候选" in str(e)

    calls = []
    def _boom(*a, **k):
        calls.append(1)
        raise AssertionError("群聊只判断时不该调用起草")

    group = [("her", "甲说", "张三"), ("her", "乙说", "李四")]
    with patch("__main__.ask", return_value={"answers": {"danger_level": {"score": 2}}, "usage": {}}), \
         patch("__main__.draft_candidates", _boom):
        r = analyze(group, "friends", group_judge_only=True)
    assert calls == [], "起草被调用了"
    assert r["mode"] == "judge_only" and r["candidates"] == [] and r["scores"] == []
    assert r["answers"]["danger_level"]["score"] == 2
    # 单聊不受影响（还是老三段）
    with patch("__main__.ask", return_value={"answers": {}, "usage": {}}), \
         patch("__main__.draft_candidates", return_value=["a", "b", "c"]):
        r2 = analyze([("her", "hello")], "friends", group_judge_only=True)
    assert r2["mode"] == "draft" and r2["candidates"] == ["a", "b", "c"]

    # ---- 群聊按发言人并发 ----
    import threading
    import time

    try:
        from .questions import group_speakers, up_to_speaker
    except ImportError:
        from questions import group_speakers, up_to_speaker

    hist = [("her", "甲一", "张三"), ("her", "乙一", "李四"), ("me", "嗯"),
            ("her", "甲二", "张三"), ("her", "丙一", "王五")]
    assert group_speakers(hist, keep=10) == ["王五", "张三", "李四"]
    assert group_speakers(hist, keep=2) == ["王五", "张三"]           # 只看最近两条
    assert group_speakers(hist, keep=10, limit=2) == ["王五", "张三"]  # 人数上限
    assert up_to_speaker(hist, "张三") == hist[:4]                     # 截到他最后一句话
    assert len(up_to_speaker(hist, "李四")) == 2
    assert up_to_speaker(hist, "查无此人") == hist

    seen_routes, inflight, peak = [], [0], [0]
    lock = threading.Lock()

    def fake_analyze(msgs, rel, **k):
        with lock:
            inflight[0] += 1
            peak[0] = max(peak[0], inflight[0])
        seen_routes.append((msgs[-1][2], k.get("reply_to"), k.get("group_judge_only"),
                            k.get("jev_base_url")))
        time.sleep(0.2)                              # 装成一次模型调用
        with lock:
            inflight[0] -= 1
        return {"answers": {"danger_level": {"score": 1}}, "usage": {"cost": 0.1},
                "mode": "judge_only", "candidates": []}

    streamed = []
    with patch("__main__.analyze", fake_analyze):
        t0 = time.time()
        got = analyze_group(hist, "friends", context=10, workers=3, on_result=streamed.append)
        wall = time.time() - t0
    assert got["mode"] == "group" and got["candidates"] == []
    assert [s["name"] for s in got["speakers"]] == ["王五", "张三", "李四"]   # 最近说话的排前
    assert peak[0] >= 2, f"没有并发跑（峰值 {peak[0]}）"
    assert wall < 0.45, f"3 路 0.2s 的活跑了 {wall:.2f}s，像是串行"
    assert len(streamed) == 3, streamed
    # 每路各看自己那句（尾句属于自己，reply_to 也是自己），不然按人拆就白拆了
    assert sorted(seen_routes) == sorted([("王五", "王五", True, None),
                                         ("张三", "张三", True, None),
                                         ("李四", "李四", True, None)]), seen_routes
    assert all(s["answers"]["danger_level"]["score"] == 1 for s in got["speakers"])
    assert round(got["usage"]["cost"], 3) == 0.3                 # 三路 usage 相加

    # 多实例：给了 base_urls 就轮流分配（将来多开 clef 时靠这个真并行）
    seen_routes.clear()
    with patch("__main__.analyze", fake_analyze):
        analyze_group(hist, "friends", context=10, workers=3,
                      base_urls=["http://a", "http://b"])
    assert sorted(r[3] for r in seen_routes) == ["http://a", "http://a", "http://b"], seen_routes

    # 单路挂掉不连坐
    def boom_once(msgs, rel, **k):
        if k.get("reply_to") == "张三":
            raise RuntimeError("模型挂了")
        return {"answers": {}, "usage": {}}

    with patch("__main__.analyze", boom_once):
        got2 = analyze_group(hist, "friends", context=10, workers=3)
    errs = {s["name"]: s["error"] for s in got2["speakers"]}
    assert "模型挂了" in errs["张三"] and errs["李四"] == "" and errs["王五"] == "", errs
    print("engine ok")
