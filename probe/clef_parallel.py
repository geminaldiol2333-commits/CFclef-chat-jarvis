"""测 llama-server 的 -np（并行 slot）在 clef 上到底能不能用。

为什么单独测：clef 的决策实现把整个 state+questions 当成一批算完（llama.cpp README：
"the whole prompt is evaluated in one batch: it must fit in --ubatch-size"），所以
app/local_server.py 一直给 clef 加 -np 1。群聊要「按发言人并发判断」，就必须先回答：
同一个 clef 服务能不能同时吃两三个请求？
  能 → 零显存代价的真并发（也是 app/local_server.py 之后要开的开关）；
  不能 → 只能线程池排队 + 多张卡陆续贴出来。

用法：
    python probe/clef_parallel.py --np 1 --reqs 2      # 对照：串行基线
    python probe/clef_parallel.py --np 2 --reqs 2      # 两路并发
    python probe/clef_parallel.py --np 4 --reqs 4      # 四路并发

自己起服务、自己收尸（finally 里 kill），端口默认 8795，不碰 app 在用的 8790。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, ".")

from app import settings                                    # noqa: E402
from core.questions import JUDGE_QUESTIONS, build_state      # noqa: E402

PORT = 8795
ALIAS = "clef-flash"

# 四个不同的人/话题，尽量让每次判断都有活干
SPEAKERS = [
    ("张三", ["今晚那个盘还开吗", "我这边有 20G 要传"]),
    ("李四", ["服务器续费了吗", "下周一到期"]),
    ("王五", ["刚那个片子谁看过", "我觉得第二段可以"]),
    ("赵六", ["钥匙放前台了", "明天来拿"]),
]


def body(speaker: str, lines: list[str], i: int) -> bytes:
    msgs = [{"from": "her", "name": "老王", "text": "在的，说"},
            {"from": "her", "name": speaker, "text": lines[0]},
            {"from": "me", "text": "嗯"},
            {"from": "her", "name": speaker, "text": lines[1]}]
    state = build_state(msgs, "朋友", keep=10)
    return json.dumps({"model": ALIAS, "state": state, "questions": dict(JUDGE_QUESTIONS)},
                      ensure_ascii=False).encode("utf-8")


def fire(i: int, payload: bytes, out: list, t0: float) -> None:
    """一个请求一路线程；记下自己的起止，用来算「总墙钟」和「有没有真的重叠」。"""
    t_start = time.time() - t0
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/systemone", data=payload,
                                 headers={"Content-Type": "application/json"})
    rec = {"i": i, "start": round(t_start, 2)}
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            got = json.loads(r.read().decode("utf-8"))
        rec["answers"] = len(got.get("answers") or {})
        rec["ok"] = bool(got.get("answers"))
        rec["sample"] = {k: (v if not isinstance(v, dict) else list(v)[:1])
                         for k, v in list((got.get("answers") or {}).items())[:2]}
    except urllib.error.HTTPError as exc:
        rec["ok"], rec["err"] = False, f"HTTP {exc.code}: {exc.read().decode('utf-8')[:200]}"
    except Exception as exc:                                # noqa: BLE001
        rec["ok"], rec["err"] = False, f"{type(exc).__name__}: {exc}"
    rec["end"] = round(time.time() - t0, 2)
    rec["secs"] = round(rec["end"] - rec["start"], 2)
    out.append(rec)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--np", type=int, default=2, help="llama-server 的 --parallel")
    ap.add_argument("--reqs", type=int, default=2, help="同时打几个请求")
    ap.add_argument("--ctx", type=int, default=16384)
    ap.add_argument("--model", default=None, help="默认取 config.json 的 local_clef_model")
    ap.add_argument("--exe", default=None, help="默认取 config.json 的 local_llama_server")
    args = ap.parse_args()

    exe = args.exe or settings.local_llama_server()
    model = args.model or settings.local_clef_model()
    if not exe or not model:
        print("缺少 llama-server 或 clef 模型路径（看 config.json）")
        return 2
    cmd = [exe, "-m", model, "--host", "127.0.0.1", "--port", str(PORT), "--alias", ALIAS,
           "-c", str(args.ctx), "-b", str(args.ctx), "-ub", str(args.ctx),
           "-ngl", "999", "-np", str(args.np), "--no-webui", "--log-verbosity", "1"]
    print(f"$ {' '.join(cmd[:6])} ... -np {args.np} -c {args.ctx}")
    log: list[str] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)
    threading.Thread(target=lambda: [log.append(ln.rstrip()) for ln in proc.stdout],
                     daemon=True).start()
    try:
        t0 = time.time()
        while time.time() - t0 < 300:
            if proc.poll() is not None:
                print("服务自己退了：\n" + "\n".join(log[-25:]))
                return 1
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/v1/models", timeout=2).read()
                break
            except Exception:                               # noqa: BLE001
                time.sleep(1)
        else:
            print("等模型加载超时")
            return 1
        print(f"服务就绪（{time.time() - t0:.1f}s 加载）")

        out: list = []
        t0 = time.time()
        threads = [threading.Thread(target=fire, args=(i, body(*SPEAKERS[i % len(SPEAKERS)], i),
                                                        out, t0))
                   for i in range(args.reqs)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        wall = time.time() - t0

        out.sort(key=lambda r: r["i"])
        spans = [(r["start"], r["end"]) for r in out if "end" in r]
        overlap = max((min(b[1] for b in spans) - max(b[0] for b in spans)), 0)
        print()
        print(f"{'#':>3} {'起':>6} {'止':>6} {'耗时':>6}  {'答案数':>5}  结果")
        for r in out:
            print(f"{r['i']:>3} {r.get('start', -1):>6.2f} {r.get('end', -1):>6.2f} "
                  f"{r.get('secs', -1):>6.2f}  {r.get('answers', 0):>5}  "
                  f"{'ok' if r.get('ok') else r.get('err', '?')}")
        print(f"\n总墙钟 {wall:.2f}s 单请求最长 "
              f"{max((r.get('secs', 0) for r in out), default=0):.2f}s；"
              f"两者相近 = 是真并发，总墙钟≈各请求之和 = 服务端在排队")
        if len(spans) > 1:
            print(f"时间轴重叠 {overlap:.2f}s（0 = 没有真正重叠）")
        bad = [ln for ln in log if "error" in ln.lower() or "fail" in ln.lower()]
        if bad:
            print("\n服务端报错行：")
            print("\n".join(bad[-8:]))
        return 0 if all(r.get("ok") for r in out) else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except Exception:                                   # noqa: BLE001
            proc.kill()
        print("服务已停")


if __name__ == "__main__":
    raise SystemExit(main())
