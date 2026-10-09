# -*- coding: utf-8 -*-
"""本地模型进程管理：替用户把 llama-server 拉起来，空闲了再关掉、把显存还回去。

两个服务：先跑 clef-flash（判断/排序）、再跑起草的文本模型，避免显存峰值：

    clef  —— clef-flash 决策模型，提供 POST /v1/systemone，给 core/jev_client._ask_local 用
    draft —— 普通文本模型，提供 POST /v1/chat/completions，给 core/llm._openai 用（起草三条候选）

clef-flash 是决策模型，只输出「每个选项的概率」，写不了字；所以起草必须换成第二个普通文本模型。

本模块不读 config.json（core/ 依赖方向要干净），配置由 app.settings 组好后传进来；
只有 config_from_settings() 和 __main__ 的 CLI 会碰 app.settings。全程只监听 127.0.0.1，不出网。
日志只在内存里留最后 200 行（不落盘），报错时给用户看。
"""
from __future__ import annotations

import atexit
import glob
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, fields

KINDS = ("clef", "draft")
_ALIAS = {"clef": "clef-flash", "draft": "local-draft"}
_NEEDLE = {"clef": "clef", "draft": "text"}  # 自动找模型时的关键词（draft 只要不是 clef 就行）
_HEALTH_TIMEOUT = 3
_FLAG_CACHE: dict[tuple[str, float], set[str]] = {}


@dataclass(frozen=True)
class Models:
    """本地模型配置。空路径 = 让 discover 去 models_dir 里找。"""

    llama_server: str = ""
    models_dir: str = ""
    clef_model: str = ""
    draft_model: str = ""
    clef_port: int = 8790
    draft_port: int = 8791
    gpu_layers: int = 999
    clef_ctx: int = 16384
    draft_ctx: int = 8192
    idle_seconds: int = 300
    idle_unload: bool = True
    autostart: bool = True


def config_from_settings() -> Models:
    """从 config.json 组一份配置。"""
    from app import settings

    return Models(
        llama_server=settings.local_llama_server(),
        models_dir=settings.local_models_dir(),
        clef_model=settings.local_clef_model(),
        draft_model=settings.local_draft_model(),
        clef_port=settings.local_clef_port(),
        draft_port=settings.local_draft_port(),
        gpu_layers=settings.local_gpu_layers(),
        clef_ctx=settings.local_clef_ctx(),
        draft_ctx=settings.local_draft_ctx(),
        idle_seconds=settings.local_idle_seconds(),
        idle_unload=settings.local_idle_unload(),
        autostart=settings.local_autostart(),
    )


def _scan_ggufs(root: str, depth: int = 2) -> list[str]:
    """在 root 下（最多 depth 层）找 *.gguf，按路径排序。"""
    root = os.path.expanduser(str(root).strip().strip('"'))
    if not os.path.isdir(root):
        return []
    out: list[str] = []
    base = os.path.abspath(root)
    for current, dirs, files in os.walk(base):
        level = current[len(base):].count(os.sep)
        if level >= depth:
            dirs[:] = []
        for name in files:
            if name.lower().endswith(".gguf"):
                out.append(os.path.join(current, name))
    return sorted(out)


def resolve_gguf(path: str, models_dir: str = "", kind: str = "draft") -> str:
    """把「文件 / 目录 / 空」统一成一个 .gguf 路径；找不到返回空串。

    - 文件：直接用它。
    - 目录：取里面排序第一的 .gguf（用户的 clef 目录就是这种）。
    - 空：在 models_dir 里找；clef 要文件名含 clef，draft 要文件名不含 clef（免得又抓到 clef）。
    """
    text = str(path or "").strip().strip('"')
    if text:
        expanded = os.path.expanduser(text)
        if os.path.isdir(expanded):
            ggufs = sorted(glob.glob(os.path.join(expanded, "*.gguf")))
            if ggufs:
                return ggufs[0]
        if os.path.isfile(expanded):
            return expanded
    if models_dir:
        found = _scan_ggufs(models_dir)
        if kind == "clef":
            hits = [g for g in found if "clef" in os.path.basename(g).lower()]
        else:
            hits = [g for g in found if "clef" not in os.path.basename(g).lower()]
        if hits:
            return hits[0]
    return ""


def find_llama_server(explicit: str = "") -> str:
    """llama-server.exe：先看用户填的，再 PATH，再 WinGet/Packages 和几个常见安装目录。"""
    text = str(explicit or "").strip().strip('"')
    if text:
        expanded = os.path.expanduser(text)
        if os.path.isfile(expanded):
            return expanded
    found = shutil.which("llama-server") or shutil.which("llama-server.exe")
    if found:
        return found
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links",
                     "llama-server.exe"),
        r"D:\AI_Tools\llama.cpp\llama-server.exe",
        r"C:\llama.cpp\llama-server.exe",
        os.path.join(os.environ.get("USERPROFILE", ""), "llama.cpp", "build", "bin",
                     "llama-server.exe"),
    ]
    packages = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
    if os.path.isdir(packages):
        candidates += sorted(glob.glob(os.path.join(packages, "*llamacpp*", "**", "llama-server.exe"),
                                       recursive=True))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return ""


def supported_flags(exe: str) -> set[str]:
    """llama-server --help 里出现的开关名；不同构建开关不一致，按有没得选来拼参数。"""
    try:
        mtime = os.path.getmtime(exe)
    except OSError:
        return set()
    cached = _FLAG_CACHE.get((exe, mtime))
    if cached is not None:
        return cached
    flags: set[str] = set()
    try:
        proc = subprocess.run([exe, "--help"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
        for token in (proc.stdout + "\n" + proc.stderr).split():
            token = token.strip(",;:")
            if token.startswith("-") and len(token) > 1 and not token.startswith("--<"):
                flags.add(token)
    except Exception:
        flags = set()
    if not flags:  # 探不出来就当老构建：只用最保险的那几个
        flags = {"-m", "--port", "--host", "-c", "-ngl"}
    _FLAG_CACHE[(exe, mtime)] = flags
    return flags


class LocalServerManager:
    """按需拉起 / 空闲卸载两个 llama-server。线程安全：ensure/stop 都过锁。"""

    def __init__(self, models: Models):
        self.models = models
        self._procs: dict[str, subprocess.Popen | None] = {k: None for k in KINDS}
        self._logs: dict[str, deque] = {k: deque(maxlen=200) for k in KINDS}
        self._error: dict[str, str] = {k: "" for k in KINDS}
        self._locks = {k: threading.Lock() for k in KINDS}
        self._last_use: dict[str, float] = {}
        self._exe = ""
        self._flags: set[str] = set()
        self._reaper: threading.Thread | None = None
        self._reaper_stop = threading.Event()

    # ---------- 地址 / 状态 ----------

    def base_url(self, kind: str) -> str:
        """clef 给根地址（拼 /v1/systemone），draft 给 /v1（OpenAI SDK 直接吃）。"""
        if kind == "clef":
            return f"http://127.0.0.1:{self.models.clef_port}"
        return f"http://127.0.0.1:{self.models.draft_port}/v1"

    def port(self, kind: str) -> int:
        return self.models.clef_port if kind == "clef" else self.models.draft_port

    def model_path(self, kind: str) -> str:
        raw = self.models.clef_model if kind == "clef" else self.models.draft_model
        return resolve_gguf(raw, self.models.models_dir, kind)

    def context(self, kind: str) -> int:
        return self.models.clef_ctx if kind == "clef" else self.models.draft_ctx

    def llama_server(self) -> str:
        if not self._exe:
            self._exe = find_llama_server(self.models.llama_server)
            if self._exe:
                self._flags = supported_flags(self._exe)
        return self._exe

    def running(self, kind: str) -> bool:
        proc = self._procs.get(kind)
        return proc is not None and proc.poll() is None

    def error(self, kind: str) -> str:
        return self._error.get(kind, "")

    def tail(self, kind: str, n: int = 40) -> str:
        return "\n".join(list(self._logs[kind])[-n:])

    # ---------- 启动 / 停止 ----------

    def ensure(self, kind: str, timeout: float = 300) -> str:
        """确保某个服务可用（必要的话拉起来，阻塞到 /health 就绪），返回它的 base_url。"""
        self.start_reaper()  # 幂等；配置换过一次后新 manager 也能自动卸载
        with self._locks[kind]:
            if self._healthy(kind):
                self._last_use[kind] = time.time()
                return self.base_url(kind)
            self._start_locked(kind, timeout)
            self._last_use[kind] = time.time()
        return self.base_url(kind)

    def touch(self, kind: str) -> None:
        """记一次「刚用过」，空闲卸载按它算。"""
        self._last_use[kind] = time.time()

    def _start_locked(self, kind: str, timeout: float) -> None:
        exe = self.llama_server()
        if not exe:
            raise RuntimeError(
                "找不到 llama-server.exe：把 llama.cpp 装好，或在设置里填它的完整路径")
        model = self.model_path(kind)
        if not model:
            which = "clef-flash" if kind == "clef" else "起草用的文本模型"
            tip = ("在设置页填 GGUF 路径，或填一个模型目录让它自己找"
                   if kind == "draft" else
                   "去 hf-mirror / ModelScope 下载 clef-flash GGUF，然后在设置页选它")
            raise RuntimeError(f"没找到{which}的 GGUF：{tip}")
        if not os.path.isfile(model):
            raise RuntimeError(f"模型文件不存在：{model}")
        if self._port_busy(self.port(kind)):
            # 端口上有东西但 /health 不通：可能是别的程序，也可能上游正在加载
            raise RuntimeError(
                f"端口 {self.port(kind)} 已被占用；要么等它加载完，要么在 config.json 里换端口")

        self._error[kind] = ""
        self._logs[kind].clear()
        args = self._build_args(kind, exe, model)
        self._logs[kind].append("$ " + " ".join(args))
        creation = 0
        if os.name == "nt":  # 别弹黑框
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=creation)
        except OSError as exc:
            raise RuntimeError(f"启动 llama-server 失败：{exc}") from None
        self._procs[kind] = proc
        threading.Thread(target=self._drain, args=(kind, proc), daemon=True).start()
        self._wait_ready(kind, proc, timeout)

    def _build_args(self, kind: str, exe: str, model: str) -> list[str]:
        f = self._flags
        ctx = self.context(kind)
        args = [exe, "-m", model, "--host", "127.0.0.1", "--port", str(self.port(kind)),
                "--alias", _ALIAS[kind], "-c", str(ctx),
                "-ngl", str(self.models.gpu_layers)]
        if kind == "clef":
            # clef 的决策实现把整个 state+questions 当成一个序列、一批算完（单序列限制），
            # 默认 -ub 512 会直接报 "input ... is too large to process"。批大小给到跟上下文一样大。
            batch, ubatch = str(ctx), str(ctx)
        else:
            batch, ubatch = "2048", "512"
        if any(x in f for x in ("-b", "--batch-size")):
            args += ["-b", batch]
        if any(x in f for x in ("-ub", "--ubatch-size")):
            args += ["-ub", ubatch]
        if "-np" in f:
            args += ["-np", "1"]              # clef 只吃单序列
        elif "--parallel" in f:
            args += ["--parallel", "1"]
        if "--no-webui" in f:
            args += ["--no-webui"]
        if "--log-verbosity" in f:            # 压低日志，别把请求内容写出去
            args += ["--log-verbosity", "1"]
        elif "--verbosity" in f:
            args += ["--verbosity", "1"]
        if kind == "draft" and "--jinja" in f:  # 用模型自带的 chat template
            args += ["--jinja"]
        return args

    def _drain(self, kind: str, proc: subprocess.Popen) -> None:
        """把子进程输出收进内存环形缓冲；顺便在失败时记一句原因。"""
        stream = proc.stdout
        if stream is None:
            return
        for line in stream:
            text = line.rstrip("\n")
            self._logs[kind].append(text)
            low = text.lower()
            if "error" in low or "unknown model architecture" in low or "failed" in low:
                self._error[kind] = text[:300]
        stream.close()

    def _wait_ready(self, kind: str, proc: subprocess.Popen, timeout: float) -> None:
        deadline = time.time() + max(30.0, timeout)
        while time.time() < deadline:
            if proc.poll() is not None:
                self._procs[kind] = None
                detail = self.tail(kind, 12)
                raise RuntimeError(
                    f"本地 {kind} 服务启动即退出（码 {proc.returncode}）：\n{detail}")
            if self._healthy(kind):
                return
            time.sleep(0.5)
        # 超时：真的把这个进程收掉再报错，别留个孤儿进程占着显存和端口
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        self._procs[kind] = None
        raise RuntimeError(f"本地 {kind} 服务 {timeout:.0f}s 内没就绪；看下面的日志：\n{self.tail(kind, 12)}")

    def _healthy(self, kind: str) -> bool:
        url = f"http://127.0.0.1:{self.port(kind)}/health"
        try:
            with urllib.request.urlopen(url, timeout=_HEALTH_TIMEOUT) as resp:
                body = resp.read().decode("utf-8", "replace")
            try:
                return str(json.loads(body).get("status", "")).lower() in ("ok", "no slot available")
            except ValueError:
                return True
        except urllib.error.HTTPError:
            return False
        except Exception:
            return False

    def _port_busy(self, port: int) -> bool:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    def stop(self, kind: str) -> None:
        with self._locks[kind]:
            proc = self._procs.get(kind)
            self._procs[kind] = None
            self._last_use.pop(kind, None)
            if proc is None or proc.poll() is not None:
                return
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

    def stop_all(self) -> None:
        for kind in KINDS:
            try:
                self.stop(kind)
            except Exception:
                pass

    # ---------- 空闲卸载 ----------

    def start_reaper(self) -> None:
        """起一个守护线程：空闲超过 idle_seconds 就 stop，把显存还回去。"""
        if self._reaper is not None or not self.models.idle_unload:
            return
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
        self._reaper.start()

    def _reap_loop(self) -> None:
        while not self._reaper_stop.wait(15):
            if not self.models.idle_unload:
                continue
            now = time.time()
            for kind in KINDS:
                last = self._last_use.get(kind)
                if self.running(kind) and last and now - last > self.models.idle_seconds:
                    self.stop(kind)

    def shutdown(self) -> None:
        self._reaper_stop.set()
        self.stop_all()

    def describe(self) -> str:
        """状态摘要，给设置页和 doctor 用。"""
        exe = self.llama_server() or "（未找到）"
        lines = [f"llama-server: {exe}"]
        for kind in KINDS:
            state = "运行中" if self.running(kind) else "未启动"
            if self._error.get(kind):
                state += f"（错误：{self._error[kind][:80]}）"
            lines.append(f"{kind}: {state}  {self.base_url(kind)}  模型: {self.model_path(kind) or '（未配置）'}")
        return "\n".join(lines)


_MANAGER: LocalServerManager | None = None
_MANAGER_KEY: tuple | None = None
_MANAGER_LOCK = threading.Lock()


def manager(models: Models | None = None) -> LocalServerManager:
    """进程内单例。配置变了就换一个（旧的先停掉，免得换了一半端口/模型）。
    会被 UI 线程和后台分析线程同时调到，加锁。"""
    global _MANAGER, _MANAGER_KEY
    models = models or config_from_settings()
    key = tuple(getattr(models, f.name) for f in fields(models))
    with _MANAGER_LOCK:
        if _MANAGER is None or _MANAGER_KEY != key:
            if _MANAGER is not None:
                _MANAGER.shutdown()
            _MANAGER, _MANAGER_KEY = LocalServerManager(models), key
        return _MANAGER


@atexit.register
def _cleanup() -> None:
    with _MANAGER_LOCK:
        if _MANAGER is not None:
            _MANAGER.shutdown()


def main(argv: list[str] | None = None) -> int:
    """CLI：python -m app.local_server [check|start [clef|draft]|stop|status]"""
    import sys

    # 中文 Windows 控制台默认 GBK，输出 UTF-8 一律炸
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else "status"
    mgr = manager()
    if action == "check":
        print(mgr.describe())
        exe = mgr.llama_server()
        if not exe:
            print("[FAIL] 没找到 llama-server.exe（PATH / WinGet / D:\\AI_Tools\\llama.cpp）")
            return 1
        print("[ok] 找到 llama-server")
        for kind in KINDS:
            path = mgr.model_path(kind)
            mark = "[ok]" if path and os.path.isfile(path) else "[FAIL]"
            print(f"{mark} {kind} 模型: {path or '（未配置）'}")
        print("提示：`python -m app.local_server start clef` 会真正拉起 clef，"
              "验证这个 llama.cpp 构建支不支持 /v1/systemone")
        return 0
    if action == "start":
        kinds = argv[1:] or list(KINDS)
        for kind in kinds:
            print(f"启动 {kind} …")
            mgr.ensure(kind)
            print(f"[ok] {kind} 就绪 {mgr.base_url(kind)}")
        print("llama-server 已拉起，Ctrl-C 退出（退出时会一并关掉）")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            mgr.stop_all()
        return 0
    if action == "start":
        kinds = argv[1:] or list(KINDS)
        for kind in kinds:
            print(f"启动 {kind} …")
            mgr.ensure(kind)
            print(f"✓ {kind} 就绪 {mgr.base_url(kind)}")
        return 0
    if action == "stop":
        mgr.stop_all()
        print("已停止")
        return 0
    if action == "status":
        print(mgr.describe())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
