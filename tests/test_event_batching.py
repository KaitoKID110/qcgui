# -*- coding: utf-8 -*-
r"""回归测试：日志必须合批发，不能让"一行一个 SSE 事件"把浏览器拖死。

背景（真实数据）
----------------
一个 13 原子的几何优化任务，ORCA 输出 ``C_C_C_CO_opt.out`` 有
**296,389 行**。旧实现里每行都会：
  服务端 publish 一个 SSE 事件 →
  前端 new span + appendChild + ``scrollTop = scrollHeight``（强制重排）。
29.6 万次 DOM 插入 + 29.6 万次重排 → 浏览器标签页卡死、内存暴涨。
用户报的"算大分子时浏览器占用内存过多、电脑严重卡死"就是它。

修复后：
  服务端按 ~120ms 时间窗把日志合成一批（一条消息带 ``lines`` 数组）；
  前端按批插入、同类行合并成一个 span，自动滚动每次刷新只做一次。

这条测试钉住**服务端**这一半：事件数必须远小于行数，且一行不丢。
"""
import time

from qcgui.web.app import EventBus


def _drain(q) -> list[dict]:
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except Exception:
            return items


def test_log_lines_are_coalesced_into_few_events():
    """296k 行日志不能变成 296k 个事件 —— 应当压到千级以内。"""
    bus = EventBus()
    q = bus.subscribe()
    n_lines = 296_389
    for i in range(n_lines):
        bus.publish("job1", {"event": "log", "line": f"scf iter {i}"})
    time.sleep(0.4)
    bus.flush()
    events = _drain(q)

    log_events = [e for e in events if e.get("event") == "log"]
    total_lines = sum(len(e.get("lines") or []) for e in log_events)
    assert total_lines == n_lines, f"日志丢了：发出 {n_lines}，收到 {total_lines}"
    assert len(log_events) < 2000, (
        f"事件数没压下来：{len(log_events)} 个（{n_lines} 行）；"
        "应当合批成千级以内"
    )
    print(f"    {n_lines} 行 → {len(log_events)} 个事件（降幅 {n_lines // len(log_events)} 倍），一行不丢")


def test_non_log_events_are_not_delayed():
    """running / finished 这类事件不能被合批拖住，且顺序在日志之后。"""
    bus = EventBus()
    q = bus.subscribe()
    bus.publish("j", {"event": "log", "line": "a"})
    bus.publish("j", {"event": "log", "line": "b"})
    bus.publish("j", {"event": "finished", "status": "done"})
    events = _drain(q)
    kinds = [e.get("event") for e in events]
    assert "finished" in kinds, f"finished 事件没发出来：{kinds}"
    # 日志必须在 finished 之前被冲掉（否则界面会先看到"完成"再补日志）
    assert kinds.index("finished") == len(kinds) - 1, f"finished 不在最后：{kinds}"
    print(f"    非日志事件立即送达且顺序正确：{kinds}")


def test_very_long_line_is_truncated():
    """ORCA 偶尔打超长行：单行必须截断，否则一条就能吃掉大量内存。"""
    bus = EventBus()
    q = bus.subscribe()
    huge = "x" * 200_000
    bus.publish("j", {"event": "log", "line": huge})
    time.sleep(0.3)
    bus.flush()
    events = _drain(q)
    lines = [ln for e in events for ln in (e.get("lines") or [])]
    assert lines, "长行日志丢了"
    assert len(lines[0]) <= EventBus._MAX_LINE_CHARS + 32, (
        f"超长行没截断，长度 {len(lines[0])}"
    )
    print(f"    超长行被截断到 {len(lines[0])} 字符")


def test_unsubscribe_stops_delivery():
    """退订后不再收事件（避免泄漏的队列越积越大）。"""
    bus = EventBus()
    q = bus.subscribe()
    bus.publish("j", {"event": "log", "line": "a"})
    bus.unsubscribe(q)
    bus.flush()
    assert _drain(q) == [], "退订后仍收到事件"
    print("    退订后不再投递")
