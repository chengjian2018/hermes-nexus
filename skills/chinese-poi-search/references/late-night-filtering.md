# Late-Night / 夜宵 Restaurant Filtering

Session-derived recipe for filtering restaurant candidates by business hours
when the user wants late-night dining (夜宵). Covers the execute_code env-var
gotcha, opentime parsing, and holiday-closure handling.

## Problem

When searching for 夜宵, the user's planned arrival time (e.g. 21:30) is the
critical filter. Most POI search results will be restaurants that close at
22:00-23:00 and are unsuitable. Amap does not provide a server-side "open now"
or "open at time T" filter -- this must be done client-side.

## Solution: Two-Pass Approach

### Pass 1: Broad search via CLI (terminal)

Use the `nearby` command to get the full result set with opentime fields:

```bash
python3 ~/.hermes/skills/productivity/chinese-poi-search/scripts/amap_poi_tool.py \
  nearby "左家庄" --keywords "烧烤" --city 北京 --radius 3000
```

This returns formatted text output with name, rating, cost, opentime, phone.

### Pass 2: Filter and rank via execute_code

**CRITICAL: execute_code does NOT inherit shell env vars.** You MUST set
`AMAP_API_KEY` explicitly inside the Python script before importing
`amap_poi_tool`, or the script will raise `ValueError: 缺少高德 API Key`.

```python
import os, sys

# MUST set env var BEFORE importing the tool
os.environ["AMAP_API_KEY"] = "1efea7737d000b178f0db199bf3d4a8b"

SCRIPT_DIR = os.path.expanduser(
    "~/.hermes/skills/productivity/chinese-poi-search/scripts"
)
sys.path.insert(0, SCRIPT_DIR)
from amap_poi_tool import search_nearby

result = search_nearby("左家庄", keywords="烧烤", city="北京", radius=3000)

candidates = []
for poi in result.get("pois", []):
    rating_str = poi.get("rating", "")
    cost_str = poi.get("cost", "")
    try:
        rating = float(rating_str) if rating_str else 0
    except:
        rating = 0
    try:
        cost = float(cost_str) if cost_str else 0
    except:
        cost = 0

    if rating >= 4.3:  # threshold adjustable
        candidates.append({
            "name": poi.get("name", ""),
            "rating": rating,
            "cost": cost,
            "address": poi.get("address", ""),
            "phone": poi.get("tel", ""),
            "opentime_today": poi.get("opentime_today", ""),
            "opentime_week": poi.get("opentime_week", ""),
            "location": poi.get("location", ""),
        })

# Sort by rating desc, then cost asc
candidates.sort(key=lambda x: (-x["rating"], x["cost"]))

for c in candidates[:8]:
    print(f"--- {c['name']} ---")
    print(f"  评分: {c['rating']}  人均: ¥{c['cost']}")
    print(f"  今日营业: {c['opentime_today']}")
    print(f"  电话: {c['phone']}")
    print()
```

## opentime Field Parsing

### opentime_today (biz_ext.open_time)

Format: `HH:MM-HH:MM` or `HH:MM-HH:MM HH:MM-HH:MM` (split shift).

For late-night filtering, check if the closing time is after the user's
planned arrival. Times like `17:00-03:00` mean open until 3 AM next day.

Simple check: if closing hour <= 12, it's past midnight (e.g. `03:00` = 3 AM).
If closing hour > 12, it's same-day (e.g. `22:30` = 10:30 PM).

### opentime_week (biz_ext.opentime2)

**WARNING**: This field can contain holiday closure notices mixed into the
weekly schedule. Example:

```
周一至周五 11:00-14:00,16:30-23:00；周六至周日 11:00-00:30 2026-02-14至2026-02-21 周一至周日 全天关闭
```

The `2026-02-14至2026-02-21 周一至周日 全天关闭` segment is a temporary
holiday closure appended to the regular weekly hours. When displaying hours
to the user:

1. Prefer `opentime_today` for "are they open now" checks.
2. Do NOT display raw `opentime_week` without checking if a holiday closure
   segment applies to today's date.
3. If today falls within a holiday closure range, the restaurant may be
   closed even if `opentime_today` shows hours (the field may be stale).

## Presentation to User

When presenting late-night candidates, include a compact table:

```
店名                        评分  人均   营业至    电话
齐齐哈尔小炉匠炭火烤肉      4.7   ¥89   00:30    18510408222
后巷音乐串吧(左家庄店)      4.6   ¥67   03:00    (无电话)
```

Notes to add:
- Weekend hours may differ from weekday (check opentime_week).
- Flag restaurants with no phone number -- cannot call to confirm.
- Amap has no queue time or parking data -- these require a phone call.
