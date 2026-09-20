"""Booking slot arithmetic — pure helpers behind the booking-time hard guard
(install_booking / repair_booking 两个外呼 pattern 共享；no LLM, no framework
imports; independently unit-testable).

迁移自 nexus-kit apps/install_booking_agent/slots.py（机制不变，日志前缀中性化）。

Data sources:
- ``available_slots`` in task_info: the installer's / technician's bookable
  visit windows, each "YYYY-MM-DD HH:MM-HH:MM" (e.g. "2026-09-10 09:00-12:00");
- the customer's spoken time: arrives time-augmented (the pattern-level
  query slot rewrites e.g. "明天下午3点" [tomorrow 3pm] ->
  "明天下午3点(2026-09-10 15:00)", see stages/query/time_aug.py), so
  extraction parses the parenthesized annotations of the rewritten query,
  not the raw utterance.
"""
from __future__ import annotations

import calendar
import json
import logging
import re
from datetime import datetime
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# (start, end, display text)
Slot = Tuple[datetime, datetime, str]

_DATE = r"\d{4}-\d{2}-\d{2}"
_CLOCK = r"\d{2}:\d{2}"

# Annotation grammar as produced by augmentation.time_augment._render:
#   (2026-09-10)                          whole day
#   (15:00) / (15:00~17:00)               today (date omitted)
#   (2026-09-10 15:00~17:00)              same-day range
#   (2026-09-10~2026-09-12)               spanning full days
#   (2026-09-10 15:00~2026-09-11 17:00)   spanning with clocks
_ANNOTATION_RE = re.compile(
    rf"\((?:(?P<d1>{_DATE})\s?)?(?P<c1>{_CLOCK})?"
    rf"(?:~(?:(?P<d2>{_DATE})\s?)?(?P<c2>{_CLOCK})?)?\)"
)

# Whole-unit annotations from _render's _whole_unit_date shorthand (these do
# NOT match _ANNOTATION_RE — the two grammars are deliberately disjoint):
#   (2026-09)  whole month          (2026)          whole year
#   (2026-09~2026-10) month range   (2026~2027)     year range
_WHOLE_UNIT_RE = re.compile(
    r"\((?P<y1>\d{4})(?:-(?P<m1>\d{2}))?"
    r"(?:~(?P<y2>\d{4})(?:-(?P<m2>\d{2}))?)?\)"
)


def _month_end(year: int, month: int) -> datetime:
    last_day = calendar.monthrange(year, month)[1]
    return datetime(year, month, last_day, 23, 59)


def _expand_whole_unit(y1, m1, y2, m2) -> Optional[Slot]:
    """Expand a whole month/year annotation into a (start, end, display)
    window: month → [month-start 00:00, month-end 23:59]; year →
    [Jan-1 00:00, Dec-31 23:59]. Returns None on non-existent months."""
    try:
        start = datetime(int(y1), int(m1 or 1), 1, 0, 0)
        end_year, end_month = (
            (int(y2), int(m2 or 12)) if y2 else (int(y1), int(m1 or 12)))
        end = _month_end(end_year, end_month)
    except ValueError:
        logger.warning(
            "[booking] 整月/整年标注非法（跳过）: %s%s%s%s", y1, m1, y2, m2)
        return None
    display = f"{y1}" + (f"-{m1}" if m1 else "")
    if y2:
        display += f"~{y2}" + (f"-{m2}" if m2 else "")
    return start, end, display


def parse_available_slots(task_info: dict,
                          now: Optional[datetime] = None) -> List[Slot]:
    """Parse task_info["available_slots"] ("YYYY-MM-DD HH:MM-HH:MM" strings)
    into (start, end, original) windows, start-sorted; malformed entries are
    skipped with a warning (a bad schedule never blocks the dialogue).

    ``now`` filters out windows that have already ended (``end <= now``) —
    an afternoon call must not recommend that morning's slot; None keeps
    every window (backward-compatible raw view).

    Value shape tolerance: the canonical form is a list of strings, but some
    entry paths (channel/webhook models) declare task_info as Dict[str, str]
    and deliver a JSON-encoded array or a separator-joined string — both are
    normalized here so the booking guard sees the real schedule either way.
    """
    raw_slots = (task_info or {}).get("available_slots") or []
    if isinstance(raw_slots, str):
        text = raw_slots.strip()
        if not text:
            raw_slots = []
        elif text.startswith("["):
            try:
                raw_slots = json.loads(text)
            except ValueError:
                logger.warning(
                    "[booking] available_slots JSON 数组解析失败: %.120s", text)
                raw_slots = []
        else:
            raw_slots = [s.strip() for s in re.split(r"[;；，,、\n]+", text) if s.strip()]
    if not isinstance(raw_slots, (list, tuple)):
        # Any other JSON shape (scalar int/bool etc.): `or []` does not
        # absorb truthy scalars, and iterating a dict yields keys, not
        # slots — uniformly degrade to an empty table as bad slots
        logger.warning(
            "[booking] available_slots 形态非法（按空档期处理）: %r",
            raw_slots)
        raw_slots = []
    slots: List[Slot] = []
    for raw in raw_slots:
        try:
            day, clocks = str(raw).split(" ", 1)
            c_start, c_end = clocks.split("-", 1)
            start = datetime.strptime(f"{day} {c_start}", "%Y-%m-%d %H:%M")
            end = datetime.strptime(f"{day} {c_end}", "%Y-%m-%d %H:%M")
        except ValueError:
            logger.warning("[booking] 可约时间格式非法（跳过）: %r", raw)
            continue
        slots.append((start, end, str(raw)))
    slots.sort(key=lambda s: s[0])
    if now is not None:
        expired = sum(1 for _s, e, _d in slots if e <= now)
        if expired:
            logger.info("[booking] 过滤已过期档期 %d 条（now=%s）", expired, now)
        slots = [s for s in slots if s[1] > now]
    return slots


def _build(d1, c1, d2, c2, today: str) -> Optional[Slot]:
    """Assemble an annotation's groups into a (start, end, display) window.

    Missing pieces follow the render grammar's semantics: no clock on a
    date-only annotation means the whole day; a lone clock anchors to
    ``today``; no end means point time (start == end) or whole day.

    Returns None when the shape is valid but the date/clock does not exist
    (e.g. a hallucinated "(2026-09-31 15:00)" — regex checks shape only);
    callers skip such annotations, matching parse_available_slots' tolerance
    for a bad schedule.
    """
    start_day = d1 or today
    start_clock = c1 or "00:00"
    if d2:
        end_day, end_clock = d2, (c2 or "23:59")
    elif c2:
        end_day, end_clock = start_day, c2
    elif c1:
        end_day, end_clock = start_day, c1  # point time
    else:
        end_day, end_clock = start_day, "23:59"  # whole day

    try:
        start = datetime.strptime(f"{start_day} {start_clock}", "%Y-%m-%d %H:%M")
        end = datetime.strptime(f"{end_day} {end_clock}", "%Y-%m-%d %H:%M")
    except ValueError:
        logger.warning(
            "[booking] 标注日期/时刻不存在（跳过该标注）: %s %s ~ %s %s",
            start_day, start_clock, end_day, end_clock)
        return None

    if c1 and start == end:  # point time
        display = f"{start_day} {start_clock}"
    elif not c1:  # whole day
        display = start_day
    elif end_day == start_day:
        display = f"{start_day} {start_clock}~{end_clock}"
    else:
        display = f"{start_day} {start_clock}~{end_day} {end_clock}"
    return start, end, display


def extract_requested_time(rewritten_query: str,
                           today: str) -> Optional[Slot]:
    """Extract the customer's requested time from the time-augmented query.

    jionlp may split one utterance into several entities ("10月1号(2026-10-01)
    下午3点(15:00)" [Oct 1 (2026-10-01), 3pm (15:00)]), so preference order: a
    date+clock annotation > a date-only
    combined with a following clock-only > date-only (whole day) > clock-only
    (today) > a whole month/year annotation ("这个月都行" -> "(2026-09)") as
    the coarse fallback. Returns None when the utterance carries no parsable
    time annotation.
    """
    found = []
    for m in _ANNOTATION_RE.finditer(rewritten_query or ""):
        d1, c1 = m.group("d1"), m.group("c1")
        d2, c2 = m.group("d2"), m.group("c2")
        if d1 or c1:
            found.append((d1, c1, d2, c2))

    for d1, c1, d2, c2 in found:
        if d1 and c1:
            slot = _build(d1, c1, d2, c2, today)
            if slot is not None:
                return slot
    for i, (d1, c1, _d2, _c2) in enumerate(found):
        if d1 and not c1:
            for dd1, cc1, _dd2, _cc2 in found[i + 1:]:
                if cc1 and not dd1:
                    slot = _build(d1, cc1, None, None, today)
                    if slot is not None:
                        return slot
    for d1, c1, d2, c2 in found:
        if d1:
            slot = _build(d1, None, d2, c2, today)
            if slot is not None:
                return slot
    # Clock-only annotations anchor to today; a broken date+clock annotation
    # must NOT degrade into "today at that clock" (the date was hallucinated)
    for d1, c1, d2, c2 in found:
        if not d1 and c1:
            slot = _build(None, c1, d2, c2, today)
            if slot is not None:
                return slot

    # Coarse fallback: whole month/year annotations ("这个月都行" ->
    # "(2026-09)"); only reached when no specific annotation parsed
    for w in _WHOLE_UNIT_RE.finditer(rewritten_query or ""):
        slot = _expand_whole_unit(
            w.group("y1"), w.group("m1"), w.group("y2"), w.group("m2"))
        if slot is not None:
            return slot
    return None


def match_slot(requested: Slot, slots: List[Slot]) -> Optional[str]:
    """Bookability against the schedule:

    - specific request (annotation carries a clock): the requested window
      must be fully contained in one available slot — a partial overlap
      exceeding a window stays unbookable (the installer cannot stay past
      the window; a point time counts when it falls inside the window);
    - coarse request (date-only / month / year annotation — display has no
      clock): a flexibility window; any available slot fully inside it
      satisfies the customer (the earliest wins, slots start-sorted).

    Returns the matched slot's original text, else None."""
    rs, re_, rdisp = requested
    coarse = ":" not in rdisp
    for ss, se, sdisp in slots:
        if ss <= rs and re_ <= se:
            return sdisp
        if coarse and rs <= ss and se <= re_:
            return sdisp
    return None


def suggest_slots(slots: List[Slot], limit: int = 2) -> str:
    """The first N bookable windows (already start-sorted), 、-joined."""
    return "、".join(sdisp for _, _, sdisp in slots[:limit])
