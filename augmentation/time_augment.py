"""时间实体增强：基于 jionlp 的时间解析，在原文时间实体后追加可读的时间标注。

示例:
    我下周一可以去          -> 我下周一(2026-09-07)可以去
    我下周一7点到9点有空     -> 我下周一7点到9点(2026-09-07 07:00~09:00)有空
    七点到九点              -> 七点到九点(07:00~09:00)   # 日期等于今天时省略日期
"""
from __future__ import annotations

import contextlib
import io
import time as _time
from datetime import datetime, timedelta
from typing import List, Optional

from jionlp.algorithm.ner import extract_time


def _fmt_clock(s: str) -> str:
    h, m, _ = s.split(":")
    return f"{h}:{m}" if m != "00" else h.zfill(2) + ":00"


def _fmt_end_clock(s: str) -> str:
    """结束时刻渲染：XX:59:59 是闭区间到最后一秒的写法，进位到下一整点。"""
    if s[3:] == "59:59" and s[:2] != "23":
        return f"{int(s[:2]) + 1:02d}:00"
    return _fmt_clock(s)


def _same_day(a: str, b: str) -> bool:
    return a[:10] == b[:10]


def _whole_unit_date(start: str, end: str) -> Optional[str]:
    """起止恰好覆盖完整日/月/年时，返回对应粒度的简写；否则返回 None。"""
    if not (start.endswith("00:00:00") and end.endswith("23:59:59")):
        return None
    d0 = datetime.strptime(start[:10], "%Y-%m-%d")
    d1 = datetime.strptime(end[:10], "%Y-%m-%d")

    def _month_start(d: datetime) -> datetime:
        return d.replace(day=1)

    d0_ms = _month_start(d0) == d0
    d1_next = d1 + timedelta(days=1)
    d1_month_end = _month_start(d1_next) == d1_next

    if d0_ms and d1_month_end:  # 整月或整月区间（起点为月初、终点为月末）
        if d0.year == d1.year and (d0.month, d1.month) == (1, 12):
            return str(d0.year)  # 整年
        m0, m1 = d0.strftime("%Y-%m"), d1.strftime("%Y-%m")
        return m0 if m0 == m1 else f"{m0}~{m1}"
    if d0 == d1:  # 整天
        return d0.strftime("%Y-%m-%d")
    return None  # 跨天且非整月：由调用方按日期区间渲染


def _render(detail: dict, today: str) -> Optional[str]:
    """把 jionlp 解析结果渲染为括号内标注文本；无法渲染时返回 None。"""
    times = detail.get("time")
    if not (isinstance(times, list) and len(times) == 2
            and all(isinstance(t, str) for t in times)):  # time_delta 的 time 是 dict，跳过
        return None
    start, end = times

    # 整日/整月/整年 -> 按粒度简写日期
    whole = _whole_unit_date(start, end)
    if whole is not None:
        return whole
    if _same_day(start, end):
        day, today_omitted = start[:10], start[:10] == today
        # 整小时（如 15:00:00~15:59:59）-> 只标 15:00
        if start[14:] == "00:00" and end[14:] == "59:59" and start[11:13] == end[11:13]:
            span = start[11:16]
        else:
            span = f"{_fmt_clock(start[11:])}~{_fmt_end_clock(end[11:])}"
        return span if today_omitted else f"{day} {span}"
    if start.endswith("00:00:00") and end.endswith("23:59:59"):  # 跨完整天
        return f"{start[:10]}~{end[:10]}"
    # 跨天且带时刻：两侧日期都带上；结束为 23:59:59 时进位到次日 00:00
    if end[11:] == "23:59:59":
        end = (datetime.strptime(end[:10], "%Y-%m-%d")
               + timedelta(days=1)).strftime("%Y-%m-%d") + " 00:00:00"
    return f"{start[:10]} {_fmt_clock(start[11:])}~{end[:10]} {_fmt_end_clock(end[11:])}"


_RANGE_DELIMS = ("到", "至", "~", "～", "—", "-")


def _reanchor_end(text: str, start: str) -> Optional[str]:
    """修正省略式区间（如"下周一到周三"）结束早于开始的 jionlp 解析缺陷。

    把区间右段用开始时间做基准重新解析，取其所在日做新的结束日；失败返回 None。
    """
    idx = max((text.rfind(d) for d in _RANGE_DELIMS), default=-1)
    if idx <= 0 or idx >= len(text) - 1:
        return None
    tail = text[idx + 1:]
    anchor = _time.mktime(_time.strptime(start[:19], "%Y-%m-%d %H:%M:%S"))
    with contextlib.redirect_stdout(io.StringIO()):
        tail_ents = extract_time(tail, time_base=anchor)
    if not tail_ents:
        return None
    tail_time = tail_ents[0]["detail"].get("time")
    if not (isinstance(tail_time, list) and len(tail_time) == 2
            and all(isinstance(t, str) for t in tail_time)):
        return None
    new_end = tail_time[1]
    return new_end if new_end[:19] > start[:19] else None


def augment_time(
    text: str,
    time_base: Optional[float] = None,
) -> str:
    """返回在时间实体后追加了时间标注的文本；无时间实体时原样返回。

    Args:
        text: 待增强文本
        time_base: 相对时间（今天/下周等）的基准时间戳，默认当前时间
    """
    if time_base is None:
        time_base = _time.time()
    today = datetime.fromtimestamp(time_base).strftime("%Y-%m-%d")

    with contextlib.redirect_stdout(io.StringIO()):  # 屏蔽 jionlp 首次调用的公众号打印
        entities = extract_time(text, time_base=time_base)

    pieces: List[str] = []
    last = 0
    for ent in sorted(entities, key=lambda e: e["offset"][0]):
        s, e = ent["offset"]
        if s < last:  # 重叠实体跳过
            continue
        detail = ent.get("detail", {})
        times = detail.get("time")
        if (isinstance(times, list) and len(times) == 2
                and all(isinstance(t, str) for t in times) and times[1][:19] < times[0][:19]):
            fixed = _reanchor_end(ent["text"], times[0])  # 省略式区间修正
            if fixed is not None:
                detail = {**detail, "time": [times[0], fixed]}
        note = _render(detail, today)
        if note is None:
            continue
        pieces.append(text[last:s])
        pieces.append(f"{text[s:e]}({note})")
        last = e
    pieces.append(text[last:])
    return "".join(pieces)
