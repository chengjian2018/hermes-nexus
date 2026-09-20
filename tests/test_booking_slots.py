"""booking_slots 纯函数单测——槽位算术的解析/容错边界（无 LLM、无框架依赖）。

覆盖：
1. 标注语法的正常解析（日期+时刻 / 跨天区间 / 仅时刻锚定当天）
2. 形状合法但日期/时刻不存在的幻觉标注（2026-09-31、25:00）→ 跳过不炸
3. available_slots 解析与排序
"""

from dialogue.booking_slots import (
    _build,
    extract_requested_time,
    parse_available_slots,
)

TODAY = "2026-09-20"


def test_annotation_date_plus_clock():
    slot = extract_requested_time("明天下午3点(2026-09-21 15:00)可以", TODAY)
    assert slot is not None
    start, end, display = slot
    assert start.hour == 15 and start.day == 21
    # 点时间：start == end
    assert start == end
    assert display == "2026-09-21 15:00"


def test_annotation_date_only_whole_day():
    slot = extract_requested_time("10月1号(2026-10-01)那天都行", TODAY)
    assert slot is not None
    start, end, _ = slot
    assert (start.hour, start.minute) == (0, 0)
    assert (end.hour, end.minute) == (23, 59)
    assert start.day == end.day == 1


def test_annotation_clock_only_anchors_today():
    slot = extract_requested_time("下午3点(15:00)吧", TODAY)
    assert slot is not None
    start, end, _ = slot
    assert start.strftime("%Y-%m-%d") == TODAY
    assert start.hour == 15


def test_annotation_spanning_days():
    slot = extract_requested_time(
        "9号到11号(2026-10-09~2026-10-11)之间", TODAY)
    assert slot is not None
    start, end, display = slot
    assert start.day == 9 and end.day == 11
    # date-only spanning: whole-day semantics, display keeps the start day
    assert display == "2026-10-09"


def test_no_annotation_returns_none():
    assert extract_requested_time("随便什么时候", TODAY) is None
    assert extract_requested_time("", TODAY) is None


def test_hallucinated_nonexistent_date_skipped():
    """形状合法但日期不存在（幻觉 9 月 31 日）：跳过该标注而非炸整轮。"""
    slot = extract_requested_time("就9月31号(2026-09-31 15:00)吧", TODAY)
    assert slot is None


def test_hallucinated_invalid_clock_skipped():
    assert extract_requested_time("25点(25:00)可以", TODAY) is None


def test_bad_annotation_falls_through_to_valid_one():
    """幻觉标注在前、合法标注在后：跳过坏的，取好的。"""
    slot = extract_requested_time(
        "9月31号(2026-09-31 15:00)不行的话，10月1号(2026-10-01 09:00)也行", TODAY)
    assert slot is not None
    start, _, _ = slot
    assert start.month == 10 and start.day == 1 and start.hour == 9


def test_build_nonexistent_date_returns_none():
    assert _build("2026-09-31", "15:00", None, None, TODAY) is None
    assert _build("2026-02-30", None, None, None, TODAY) is None


def test_parse_available_slots_sorted_and_tolerant():
    slots = parse_available_slots({
        "available_slots": [
            "2026-09-22 14:00-17:00",
            "2026-09-21 09:00-12:00",   # 应排到最前
            "garbage",                    # 跳过
            "2026-13-01 09:00-12:00",    # 非法月份跳过
        ]})
    assert [s[2] for s in slots] == [
        "2026-09-21 09:00-12:00", "2026-09-22 14:00-17:00"]
