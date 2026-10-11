# -*- coding: utf-8 -*-
"""离线判定：一份 UIA dump 够不够驱动 QQ —— 不需要 QQ、不需要窗口、不联网。

读 probe/probe_qq_uia.py 写出来的 JSON（只含类名/几何/文本长度，不含消息内容），
按三条能力轴给结论：

    editor    有没有「消息输入框」可用（填候选要用它的矩形）
    bubbles   有没有「气泡」节点可用（读消息要用）
    self      有没有「这条是我发的」的可靠标记（分己方/对方要用）

每条轴给出 strong（类名/ID 直接命中）/ weak（只能靠几何推断）/ none。
三条轴都不是 none = UIA 能用；有 none 就说明**这份 dump 不足以驱动**（多半是没开会话或没在前台），
请把 QQ 切到前台、点开会话重新 dump——本项目只走 UIA，不退回 OCR/像素。

用法
    python probe/qq_uia_verdict.py qq_tree.json     # 分析真实 dump
    python probe/qq_uia_verdict.py --self-test      # 用合成数据自测（无头，可 CI）
"""
from __future__ import annotations

import argparse
import json
import sys

# 社区（treers2/qq-operations）公开的类名；能不能用取决于 QQ 版本
SEL_EDITOR = ("qq-msg-editor",)
SEL_BUBBLE = ("message-container", "msg-content-container")
SEL_SELF = ("container--self",)
RIGHT_PANE_MIN_W = 300


def _keys(n: dict) -> str:
    return f"{n.get('cls', '')} {n.get('id', '')}".lower()


def _rect(n: dict):
    r = n.get("rect") or [0, 0, 0, 0]
    return tuple(int(v) for v in r)


def _has_geo(n: dict) -> bool:
    l, t, r, b = _rect(n)
    return (r - l) > 0 and (b - t) > 0


def _area(n: dict) -> int:
    l, t, r, b = _rect(n)
    return max(0, r - l) * max(0, b - t)


def find_panes(nodes: list[dict], panel_rect) -> tuple[int, list[tuple[int, int, int, int]]]:
    """会话列表/聊天区的分界（物理像素）。优先用「窄高容器」，退而用面板宽度的经验比例。"""
    pl, pt, pr, pb = panel_rect
    tall = [r for n in nodes if n.get("type") == "GroupControl" and _has_geo(n)
            for r in [_rect(n)] if (r[3] - r[1]) > 0.6 * (pb - pt) and (r[2] - r[0]) < 600]
    if tall:
        # 会话列表 = 这些窄高条里最宽的那个（最左侧更窄的那条是竖排图标栏）
        left = max(tall, key=lambda r: r[2] - r[0])
        return left[2] + 1, tall
    return pl + int((pr - pl) * 0.22), tall


def analyze(dump: dict) -> dict:
    nodes = dump.get("nodes") or []
    panel_rect = None
    for n in nodes:
        if n.get("d") == 0 and _has_geo(n):
            panel_rect = _rect(n)
            break
    if panel_rect is None:
        wins = dump.get("windows") or [{}]
        panel_rect = tuple(wins[0].get("rect") or (0, 0, 0, 0)) if wins else (0, 0, 0, 0)
    pane_left, panes = find_panes(nodes, panel_rect)

    out = {"nodes": len(nodes), "panel_rect": list(panel_rect), "pane_left": pane_left,
           "panes": [list(p) for p in panes], "editor": "none", "editor_rect": None,
           "bubbles": "none", "bubble_count": 0, "self": "none", "self_count": 0,
           "pane_text_nodes": 0, "pane_text_with_geometry": 0,
           "x_clusters": None, "notes": []}

    if len(nodes) < 40 and not any(
            n.get("type") == "TextControl" and n.get("text_len", 0) > 0 and _has_geo(n)
            for n in nodes):
        out["notes"].append(f"树只有 {len(nodes)} 个节点、也没有带几何的文本——"
                            "QQ 在后台时就是这样，这个 dump 不能用来判断"
                            "（请把 QQ 切到前台、点开会话再 dump）")

    # editor：类名命中最强；其次右半聊天区里的 EditControl
    for n in nodes:
        if any(k in _keys(n) for k in SEL_EDITOR):
            out["editor"], out["editor_rect"] = "strong", list(_rect(n))
            break
    if out["editor"] == "none":
        right_edits = [n for n in nodes if n.get("type") == "EditControl"
                       and _has_geo(n) and _rect(n)[0] >= pane_left]
        if right_edits:
            best = max(right_edits, key=_area)
            out["editor"], out["editor_rect"] = "weak", list(_rect(best))
            out["notes"].append("输入框靠「右半区里的 EditControl」推断，建议实机确认点击点")

    # bubbles：类名命中
    bubble_nodes = [n for n in nodes if any(k in _keys(n) for k in SEL_BUBBLE)]
    if bubble_nodes:
        out["bubbles"], out["bubble_count"] = "strong", len(bubble_nodes)
    else:
        # 退而求其次：聊天区里有几何、有文字的 TextControl
        pane_texts = [n for n in nodes if n.get("type") == "TextControl"
                      and n.get("text_len", 0) > 0 and _rect(n)[0] >= pane_left]
        out["pane_text_nodes"] = len(pane_texts)
        out["pane_text_with_geometry"] = sum(1 for n in pane_texts if _has_geo(n))
        if out["pane_text_with_geometry"] >= 4:
            out["bubbles"] = "weak"
            out["notes"].append("没有气泡类名，但有带几何的文本节点；可以按 y 排序读消息")

    # self（己方标记）：类名命中，或者左右分簇
    self_nodes = [n for n in nodes if any(k in _keys(n) for k in SEL_SELF)]
    if self_nodes:
        out["self"], out["self_count"] = "strong", len(self_nodes)
    else:
        xs = sorted((_rect(n)[0] + _rect(n)[2]) // 2
                    for n in nodes
                    if n.get("type") == "TextControl" and n.get("text_len", 0) > 0
                    and _has_geo(n) and _rect(n)[0] >= pane_left)
        if len(xs) >= 6:
            mid = (pane_left + panel_rect[2]) / 2
            left = sum(1 for x in xs if x < mid)
            right = len(xs) - left
            out["x_clusters"] = {"left": left, "right": right, "mid_x": round(mid)}
            if left >= 2 and right >= 2:
                out["self"] = "weak"
                out["notes"].append(
                    f"没有己方类名，但文本横向分成两簇（左 {left} / 右 {right}）——"
                    "可以用「靠右=我发的」判己方")
            else:
                out["notes"].append(
                    f"文本只有一簇（左 {left} / 右 {right}），无法靠左右分己方/对方")

    axes = [out[k] for k in ("editor", "bubbles", "self")]
    if all(a == "strong" for a in axes):
        out["verdict"] = "uia-usable"          # 三条轴都有类名/ID 直接命中
    elif any(a == "none" for a in axes):
        out["verdict"] = "uia-unusable"        # 缺一不可，缺了就只能走像素
    else:
        out["verdict"] = "uia-partial"         # 全都能拿到，但靠几何推断
    return out


def report(res: dict) -> None:
    print(f"节点数: {res['nodes']}  面板: {res['panel_rect']}  聊天区左边界: {res['pane_left']}")
    if res["panes"]:
        print(f"窄高容器: {res['panes'][:4]}")
    print(f"  editor  : {res['editor']:6s} {res['editor_rect'] or ''}")
    print(f"  bubbles : {res['bubbles']:6s} count={res['bubble_count']} "
          f"(聊天区文本节点 {res['pane_text_nodes']}，其中有几何 {res['pane_text_with_geometry']})")
    print(f"  self    : {res['self']:6s} count={res['self_count']} clusters={res['x_clusters']}")
    for n in res["notes"]:
        print("  ·", n)
    print(f"\n结论: {res['verdict']}")
    if res["verdict"] == "uia-usable":
        print("→ UIA 可以驱动 QQ：三条轴都有 strong，按采集器接口写 uia_qqnt 即可")
    elif res["verdict"] == "uia-partial":
        print("→ 三条轴都能拿到，但气泡/己方靠几何推断（没有类名）；"
              "能用，但要先确认「靠右=我发的」在这个主题/布局下成立")
    else:
        print("→ 这份 dump 不足以驱动 UIA（至少一条轴拿不到）。")
        print("   最可能的原因：dump 的时候没点开会话、或 QQ 不在前台（QQ 只对前台窗口建树）。")
        print("   请点开一个会话、让 QQ 保持前台，重新跑 probe_qq_uia.py --dump。")


# ---------------------------------------------------------------- self-test

def _win(l=450, t=210, r=2110, b=1170):
    return {"windows": [{"hwnd": 1, "title_is_QQ": True, "rect": [l, t, r, b],
                         "is_foreground": True, "is_iconic": False}]}


def _n(i, p, d, typ, rect, cls="", aid="", text_len=0, offscreen=False):
    return {"i": i, "p": p, "d": d, "type": typ, "cls": cls, "id": aid,
            "rect": list(rect), "text_len": text_len, "offscreen": offscreen}


def _fixture_classnames():
    nodes = [
        _n(0, -1, 0, "PaneControl", (450, 210, 2110, 1170)),
        _n(1, 0, 1, "GroupControl", (520, 260, 833, 1165)),
        _n(2, 0, 1, "GroupControl", (833, 260, 2105, 1165)),
        _n(3, 2, 2, "EditControl", (900, 1000, 2000, 1080), cls="qq-msg-editor"),
        _n(4, 2, 2, "TextControl", (900, 300, 1200, 330), cls="chat-header__contact-name", text_len=3),
    ]
    for k in range(4):
        nodes.append(_n(5 + k, 2, 2, "GroupControl", (900, 400 + k * 60, 1300, 450 + k * 60),
                        cls="message-container container--self"))
    for k in range(4):
        nodes.append(_n(9 + k, 2, 2, "GroupControl", (1400, 700 + k * 60, 1900, 750 + k * 60),
                        cls="message-container"))
    return dict(_win(), **{"nodes": nodes})


def _fixture_geometry():
    nodes = [
        _n(0, -1, 0, "PaneControl", (450, 210, 2110, 1170)),
        _n(1, 0, 1, "GroupControl", (520, 260, 833, 1165)),
        _n(2, 0, 1, "GroupControl", (833, 260, 2105, 1165)),
        _n(3, 2, 2, "EditControl", (900, 1000, 2000, 1080)),          # 右半区的输入框
    ]
    for k in range(4):   # 对方：靠左
        nodes.append(_n(4 + k, 2, 2, "TextControl", (900, 400 + k * 70, 1150, 430 + k * 70), text_len=6))
    for k in range(4):   # 自己：靠右
        nodes.append(_n(8 + k, 2, 2, "TextControl", (1600, 750 + k * 70, 2000, 780 + k * 70), text_len=5))
    return dict(_win(), **{"nodes": nodes})


def _fixture_background():
    return dict(_win(), **{"nodes": [_n(i, 0 if i else -1, 1, "GroupControl", (450, 210, 2110, 1170))
                                      for i in range(9)]})


def self_test() -> int:
    a = analyze(_fixture_classnames())
    assert a["editor"] == "strong" and a["bubbles"] == "strong" and a["self"] == "strong", a
    assert a["verdict"] == "uia-usable", a
    b = analyze(_fixture_geometry())
    assert b["editor"] == "weak" and b["bubbles"] == "weak" and b["self"] == "weak", b
    assert b["verdict"] == "uia-partial", b
    assert b["x_clusters"]["left"] == 4 and b["x_clusters"]["right"] == 4, b
    c = analyze(_fixture_background())
    assert c["verdict"] == "uia-unusable" and c["editor"] == "none", c
    assert any("后台" in n for n in c["notes"]), c
    print("qq_uia_verdict ok（三份合成 dump：类名世界=usable / 几何世界=partial / 后台=unusable）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dump", nargs="?", help="probe_qq_uia.py 写出的 JSON")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.dump:
        ap.print_help()
        return 2
    with open(args.dump, encoding="utf-8") as f:
        dump = json.load(f)
    report(analyze(dump))
    return 0


if __name__ == "__main__":
    sys.exit(main())
