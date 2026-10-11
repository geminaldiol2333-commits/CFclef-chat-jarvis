# -*- coding: utf-8 -*-
"""采集子进程的壳：跑哪一拍（暂停/恢复）、把采集器吐出来的消息转发给父进程。

真实采集逻辑在 app/collectors/ 里（微信=截图+OCR，QQ NT=像素定位等）。
一次 OCR 250~800ms、UIA 读树也可能几百毫秒，放父进程的 Qt 主线程界面就僵了，所以都在这里跑。
只往队列里丢纯 tuple/str/bytes；帧和 COM 对象都留在子进程内部，绝不落盘。
"""
import ctypes
import time
import traceback

from app import chatapps, collectors


def _err(q):
    """异常压成一行发给父进程，子进程的 stderr 一般没人看得见。"""
    q.put(("status", " ".join(traceback.format_exc().split())[-200:]))


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:120] or type(exc).__name__


def run(q, hwnd, enabled, debug_on, app_key=None):
    """enabled 置位=采集，清掉=暂停。暂停时让采集器收掉采集（微信的 WGC 会话一起停掉，
    Windows 那圈黄色采集边框也跟着没了），恢复时重新开一个；去重状态留在采集器里，
    恢复后不会把屏幕上的旧消息再报一遍。
    debug_on 置位才往队列里送整帧（一帧 2~3MB），关着一点额外活都不干。"""
    ctypes.windll.user32.SetProcessDPIAware()
    app = chatapps.get(app_key)
    emit = lambda kind, *payload: q.put((kind,) + tuple(payload))

    try:
        collector = collectors.build(app, hwnd, emit, debug_on)
    except Exception as exc:
        q.put(("dead", "无法开始采集：" + _one_line(exc)))
        return

    started = False
    while True:
        if not enabled.is_set():
            if started:
                collector.stop()
                started = False
            enabled.wait()
            continue
        if not started:
            try:
                collector.start()
            except Exception as exc:
                q.put(("dead", "无法开始采集：" + _one_line(exc)))
                enabled.clear()  # 自己清掉，下一圈就去等着，别一秒重试几十次
                continue
            started = True
        if not collector.alive():
            break
        try:
            collector.poll(emit)
        except Exception:
            _err(q)  # 一帧出错不退出
        time.sleep(0.05)

    q.put(("dead", "采集停了（聊天窗口关了？）"))
    try:
        collector.wait()  # 采集底层若是异步的（WGC），这里把线程错误抛出来
    except Exception:
        _err(q)


if __name__ == "__main__":
    # 自测（不碰真实窗口）：用一个假采集器验证事件流转和失败处理。
    # 跑法：python -m app.worker
    import queue
    import threading
    from unittest.mock import patch

    class _Stub(collectors.BaseCollector):
        key = "stub"

        def __init__(self, hwnd, emit, debug_on=None):
            super().__init__(hwnd, emit, debug_on)
            self.n = 0

        def start(self):
            self.emit("resumed")

        def stop(self):
            self.emit("paused")

        def poll(self, emit):
            self.n += 1
            emit("area", (1, 2, 3, 4))
            emit("input", (10, 20, 30, 40))
            emit("chat", "会话A")
            emit("lines", "会话A", [("her", None, "你好")], (1, 2, 3, 4))

        def alive(self):
            return self.n < 3

    q, enabled, debug = queue.Queue(), threading.Event(), threading.Event()
    enabled.set()
    with patch.object(collectors, "build", lambda app, hwnd, emit, debug_on=None: _Stub(hwnd, emit, debug_on)):
        t = threading.Thread(target=run, args=(q, 0, enabled, debug, None), daemon=True)
        t.start()
        t.join(timeout=5)
    assert not t.is_alive(), "worker 没在 alive() 变 False 后退出"
    events = []
    while not q.empty():
        events.append(q.get())
    kinds = [e[0] for e in events]
    assert kinds[0] == "resumed" and kinds[-1] == "dead", kinds
    assert kinds.count("lines") == 3, kinds
    assert ("input", (10, 20, 30, 40)) in events, events
    assert events[-1] == ("dead", "采集停了（聊天窗口关了？）"), events[-1]

    class _Boom(_Stub):
        def start(self):
            raise RuntimeError("窗口没了")

    q2, en2 = queue.Queue(), threading.Event()
    en2.set()
    with patch.object(collectors, "build", lambda app, hwnd, emit, debug_on=None: _Boom(hwnd, emit, debug_on)):
        t2 = threading.Thread(target=run, args=(q2, 0, en2, debug, None), daemon=True)
        t2.start()
        time.sleep(0.5)
    assert not en2.is_set(), "启动失败后没清掉 enabled，会重试风暴"
    first = q2.get_nowait()
    assert first[0] == "dead" and "无法开始采集" in first[1], first
    en2.set()
    t2.join(timeout=3)
    print("worker ok")
