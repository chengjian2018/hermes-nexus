"""booking_slots 纯函数单测——槽位算术的解析/容错边界（无 LLM、无框架依赖）。

覆盖：
1. 标注语法的正常解析（日期+时刻 / 跨天区间 / 仅时刻锚定当天）
2. 形状合法但日期/时刻不存在的幻觉标注（2026-09-31、25:00）→ 跳过不炸
3. 整月/整年粒度标注（time_augment 的 _whole_unit_date 简写形态）
4. available_slots 解析、排序与过期过滤
"""

from datetime import datetime

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


# ---------------------------------------------------------------------------
# 整月/整年粒度标注（time_augment._whole_unit_date 渲染形态）
# ---------------------------------------------------------------------------

def test_whole_month_annotation():
    """“这个月都行” → (2026-09)：展开为整月窗口，不再解析失败。"""
    slot = extract_requested_time("这个月(2026-09)都可以", TODAY)
    assert slot is not None
    start, end, display = slot
    assert (start.year, start.month, start.day) == (2026, 9, 1)
    assert start.hour == 0
    assert (end.year, end.month, end.day) == (2026, 9, 30)
    assert end.hour == 23 and end.minute == 59
    assert display == "2026-09"


def test_whole_year_annotation():
    slot = extract_requested_time("今年(2026)内都行", TODAY)
    assert slot is not None
    start, end, display = slot
    assert (start.month, start.day) == (1, 1)
    assert (end.month, end.day) == (12, 31)
    assert display == "2026"


def test_whole_month_range_annotation():
    slot = extract_requested_time("九十月(2026-09~2026-10)都行", TODAY)
    assert slot is not None
    start, end, display = slot
    assert (start.year, start.month) == (2026, 9)
    assert (end.year, end.month, end.day) == (2026, 10, 31)
    assert display == "2026-09~2026-10"


def test_whole_year_range_annotation():
    slot = extract_requested_time("今明两年(2026~2027)", TODAY)
    assert slot is not None
    start, end, display = slot
    assert start.year == 2026 and end.year == 2027
    assert display == "2026~2027"


def test_specific_annotation_beats_whole_month():
    """具体日期标注优先于整月兜底。"""
    slot = extract_requested_time("这个月(2026-09)都行，最好10月1号(2026-10-01)", TODAY)
    assert slot is not None
    start, _, display = slot
    assert start.day == 1 and start.month == 10
    assert display == "2026-10-01"


def test_invalid_whole_month_skipped():
    """非法月份（2026-13）：跳过不炸。"""
    assert extract_requested_time("就(2026-13)吧", TODAY) is None


def test_whole_month_matches_slot_within_month():
    """整月窗口与档期的包含关系：月内任一档期命中 match_slot。"""
    from dialogue.booking_slots import match_slot
    slot = extract_requested_time("这个月(2026-09)都行", TODAY)
    slots = parse_available_slots({
        "available_slots": ["2026-09-25 09:00-12:00"]})
    assert match_slot(slot, slots) == "2026-09-25 09:00-12:00"


# ---------------------------------------------------------------------------
# 过期档期过滤
# ---------------------------------------------------------------------------

def test_parse_available_slots_filters_expired():
    """now 过滤已结束的档期：下午外呼不再推荐当天上午窗。"""
    now = datetime(2026, 9, 20, 14, 0)
    slots = parse_available_slots({
        "available_slots": [
            "2026-09-20 09:00-12:00",   # 已结束 → 过滤
            "2026-09-20 15:00-18:00",   # 进行中（end > now）→ 保留
            "2026-09-21 09:00-12:00",   # 未来 → 保留
        ]}, now=now)
    assert [s[2] for s in slots] == [
        "2026-09-20 15:00-18:00", "2026-09-21 09:00-12:00"]


def test_parse_available_slots_now_none_keeps_all():
    slots = parse_available_slots({
        "available_slots": ["2020-01-01 09:00-12:00"]}, now=None)
    assert len(slots) == 1
