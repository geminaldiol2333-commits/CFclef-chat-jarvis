# -*- coding: utf-8 -*-
"""离线判定：一份 UIA dump 够不够驱动 QQ —— 不需要 QQ、不需要窗口、不联网。

判定用的是**和采集器同一套**定位代码（app/collectors/uia_map.py），所以这里说能用，
运行时就是能用；不存在「工具说没问题、跑起来认不出」的两套逻辑。

三条能力轴：

    editor    消息输入区（填候选用它的矩形）
    bubbles   消息列表（读消息）
    self      己方/对方的区分

每条轴：strong = 社区那套类名/ID 直接命中；weak = 靠结构 id（如 `ml-root`）+ 几何定位；
none = 拿不到。三轴全 strong → uia-usable；三轴都不是 none → uia-partial（靠几何，能用）；
任一轴 none → uia-unusable（这份 dump 不足以驱动，多半是没开会话或 QQ 不在前台）。

用法
    python probe/qq_uia_verdict.py qq_tree.json     # 分析 dump
    python probe/qq_uia_verdict.py --self-test      # 合成数据自测（无头，可 CI）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.collectors.uia_map import (PANE_TOL, Node, _text_rows, build_turns,  # noqa: E402
                                    find_editor, find_first, find_message_list,
                                    find_session, find_session_node, pane_bounds,
                                    SEL_DEFAULT, Selectors)


def nodes_from_dump(dump: dict) -> list[Node]:
    return [Node(index=n.get("i", k), parent=n.get("p", -1), depth=n.get("d", 0),
                 type=n.get("type", ""), cls=n.get("cls", ""), aid=n.get("id", ""),
                 rect=tuple(n.get("rect") or (0, 0, 0, 0)),
                 text=n.get("text", ""), text_len=n.get("text_len", 0))
            for k, n in enumerate(dump.get("nodes") or [])]


def analyze(dump: dict) -> dict:
    nodes = nodes_from_dump(dump)
    panel = next((n for n in nodes if n.depth == 0 and n.has_geo), None)
    if panel is None:
        wins = dump.get("windows") or [{}]
        rect = tuple(wins[0].get("rect") or (0, 0, 0, 0)) if wins else (0, 0, 0, 0)
        panel = Node(0, -1, 0, "PaneControl", rect=rect)

    pane_left, _ = pane_bounds(nodes, panel.rect)
    lst = find_message_list(nodes, pane_left)
    editor = find_editor(nodes, pane_left, list_node=lst, panel_right=panel.rect[2])
    session_node = find_session_node(nodes, pane_left, list_node=lst, panel_right=panel.rect[2])
    session = session_node.text.strip() if session_node is not None else ""

    rows = _text_rows(nodes, lst.rect) if lst is not None and lst.has_geo else []
    mid = ((lst.rect[0] + lst.rect[2]) / 2) if lst is not None else 0
    left = right = 0
    for row in rows:
        cx = (min(n.rect[0] for n in row) + max(n.rect[2] for n in row)) / 2
        right += 1 if cx > mid else 0
        left += 1 if cx <= mid else 0

    out = {
        "nodes": len(nodes), "panel_rect": list(panel.rect), "pane_left": pane_left,
        "message_list": list(lst.rect) if lst is not None else None,
        "editor_rect": list(editor.rect) if editor is not None else None,
        "session_found": session_node is not None, "rows": len(rows),
        "x_clusters": {"left": left, "right": right, "mid_x": round(mid)} if rows else None,
        "notes": [],
    }

    out["editor"] = ("strong" if find_first(nodes, tuple(SEL_DEFAULT.editor)
                                            + tuple(SEL_DEFAULT.composer)) is not None
                     else ("weak" if editor is not None else "none"))
    out["bubbles"] = ("strong" if any(any(x in n.key for x in SEL_DEFAULT.bubble) for n in nodes)
                      else ("weak" if (lst is not None and len(rows) >= 3) else "none"))
    out["self"] = ("strong" if any(any(x in n.key for x in SEL_DEFAULT.self_mark) for n in nodes)
                   else ("weak" if (left >= 1 and right >= 1) else "none"))

    if len(nodes) < 40 and not any(n.has_text and n.has_geo for n in nodes):
        out["notes"].append(
            f"树只有 {len(nodes)} 个节点、也没带几何的文本——QQ 只在窗口处于活动状态时才构建"
            "完整无障碍树（后台实测 9 个节点）。这份 dump 不能用来判定："
            "请点开会话、让 QQ 保持前台，再跑 probe_qq_uia.py。")
    if out["editor"] == "weak":
        out["notes"].append("输入区靠结构+几何定位（该 QQ 版本没暴露 qq-msg-editor），已实测可定位")
    if out["bubbles"] == "weak":
        out["notes"].append("没有气泡类名，按消息列表里的文本行读；发言人名/时间戳也会被当行，"
                           "需要实测校对（config.json 可覆盖选择器）")
    if out["self"] == "weak":
        out["notes"].append(f"没有 container--self，靠左右对齐判己方（当前视图 左 {left} / 右 {right}）")
    if out["self"] == "none" and rows:
        out["notes"].append("文本只有一簇，这份视图里分不出己方/对方——换到双方都有消息的位置再 dump 一次")

    axes = [out[k] for k in ("editor", "bubbles", "self")]
    out["verdict"] = ("uia-usable" if all(a == "strong" for a in axes)
                      else ("uia-unusable" if any(a == "none" for a in axes) else "uia-partial"))
    return out


def report(res: dict) -> None:
    print(f"节点数: {res['nodes']}  面板: {res['panel_rect']}  聊天区左边界: {res['pane_left']}")
    print(f"  message list : {res['message_list']}   (文本行 {res['rows']} 行)")
    print(f"  editor       : {res['editor']:6s} {res['editor_rect'] or ''}")
    print(f"  bubbles      : {res['bubbles']:6s}")
    print(f"  self         : {res['self']:6s} clusters={res['x_clusters']}")
    print(f"  会话名定位   : {'成功' if res['session_found'] else '失败'}")
    for n in res["notes"]:
        print("  ·", n)
    print(f"\n结论: {res['verdict']}")
    if res["verdict"] == "uia-usable":
        print("→ 社区那套类名都在，采集器走类名路径")
    elif res["verdict"] == "uia-partial":
        print("→ 类名没了，但 ml-root/输入区/左右对齐都在，采集器走结构+几何路径（已实现）")
    else:
        print("→ 这份 dump 不足以驱动（也不是没有 UIA 路线可退——本项目只用 UIA）。")
        print("   最可能是 dump 时没点开会话、或 QQ 不在前台；请重新 dump。")


# ---------------------------------------------------------------- self-test

def _win(l=450, t=210, r=2110, b=1170):
    return [{"hwnd": 1, "title_is_QQ": True, "rect": [l, t, r, b], "is_foreground": True}]


def _n(i, p, d, typ, rect, cls="", aid="", text_len=0, text=""):
    return {"i": i, "p": p, "d": d, "type": typ, "cls": cls, "id": aid,
            "rect": list(rect), "text_len": text_len, "text": text}


def _fixture_classnames():
    nodes = [_n(0, -1, 0, "PaneControl", (450, 210, 2110, 1170)),
             _n(1, 0, 1, "GroupControl", (520, 260, 833, 1165)),
             _n(2, 0, 1, "GroupControl", (833, 260, 2105, 1165)),
             _n(3, 2, 2, "EditControl", (900, 1000, 2000, 1080), cls="qq-msg-editor"),
             _n(4, 2, 2, "TextControl", (900, 300, 1200, 330), cls="chat-header__contact-name",
                text_len=3)]
    for k in range(4):
        nodes.append(_n(5 + k, 2, 2, "GroupControl", (900, 400 + k * 60, 1300, 450 + k * 60),
                        cls="message-container container--self", text_len=4))
    for k in range(4):
        nodes.append(_n(9 + k, 2, 2, "GroupControl", (1400, 700 + k * 60, 1900, 750 + k * 60),
                        cls="message-container", text_len=5))
    return {"windows": _win(), "nodes": nodes}


def _fixture_real():
    """照本机真实 dump（QQ 9.9.36）画的：无类名、有 ml-root、整宽输入区、左右两簇文本。"""
    nodes = [_n(0, -1, 0, "PaneControl", (450, 210, 2110, 1170)),
             _n(1, 0, 1, "GroupControl", (520, 260, 833, 1165)),
             _n(2, 0, 1, "GroupControl", (832, 260, 2105, 1165)),
             _n(3, 2, 2, "TextControl", (855, 281, 1094, 307), text_len=12),      # 会话名
             _n(4, 2, 2, "GroupControl", (832, 325, 1880, 907), aid="ml-root"),
             _n(5, 2, 2, "GroupControl", (832, 956, 1880, 1095)),                 # 输入区
             _n(6, 2, 2, "GroupControl", (832, 1095, 1880, 1165))]                # 工具栏
    for k in range(6):   # 对方：靠左
        nodes.append(_n(10 + k, 4, 3, "TextControl", (920, 380 + k * 40, 1300, 405 + k * 40),
                        text_len=8))
    for k in range(3):   # 我：靠右
        nodes.append(_n(20 + k, 4, 3, "TextControl", (1600, 600 + k * 40, 1840, 625 + k * 40),
                        text_len=6))
    nodes.append(_n(30, 5, 3, "TextControl", (852, 1000, 1100, 1022), text_len=9))  # 草稿
    return {"windows": _win(), "nodes": nodes}


def _fixture_background():
    return {"windows": _win(),
            "nodes": [_n(i, 0 if i else -1, 1, "GroupControl", (450, 210, 2110, 1170))
                      for i in range(9)]}


def self_test() -> int:
    a = analyze(_fixture_classnames())
    assert (a["editor"], a["bubbles"], a["self"]) == ("strong", "strong", "strong"), a
    assert a["verdict"] == "uia-usable", a

    b = analyze(_fixture_real())
    assert (b["editor"], b["bubbles"], b["self"]) == ("weak", "weak", "weak"), b
    assert b["verdict"] == "uia-partial", b
    assert b["message_list"] == [832, 325, 1880, 907], b
    assert b["x_clusters"]["left"] == 6 and b["x_clusters"]["right"] == 3, b
    assert b["editor_rect"] and b["editor_rect"][3] <= 1095, b   # 不是工具栏

    c = analyze(_fixture_background())
    assert c["verdict"] == "uia-unusable", c
    assert any("后台" in n for n in c["notes"]), c
    print("qq_uia_verdict ok（类名世界=usable / 真实结构=partial / 后台=unusable）")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
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
        report(analyze(json.load(f)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
