---
name: chinese-poi-search
description: "Search China businesses via Amap POI API."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [maps, poi, china, amap, gaode, restaurant, business-search, location]
    category: productivity
    requires_toolsets: [terminal]
    related_skills: [maps, interactive-task, interactive-task-food]
---

# Chinese POI Search (Amap)

Search and filter businesses/points-of-interest in mainland China using the
Amap (高德地图) POI API. Provides rating, per-person cost,
business hours, photos, and phone numbers -- data that OpenStreetMap/Overpass
often lacks for Chinese POIs.

**Critical**: search and around use the v3 endpoints (`/v3/place/text`,
`/v3/place/around`) with `extensions=all` because the v5 endpoints' `show_fields`
parameter does NOT return `rating`, `cost`, `tel`, or `business_area` for
personal (non-enterprise) API keys. The v3 endpoints return these fields via
`biz_ext`. See `references/amap-poi-api.md` for the v3/v5 comparison.

## When to Use

- User wants to find businesses in mainland China (restaurants, hotels, shops, cinemas, scenic spots)
- User needs rating / per-person cost / business hours for Chinese POIs
- The `maps` skill (OSM-based) returns thin results for a China location
- User asks about 大众点评 (Dianping) data but a compliant API is preferred
- `interactive-task` / `interactive-task-food` skill needs a resolver for restaurant finding (`resolve_restaurants` wrapper)

## When NOT to Use

- Non-China locations → use the `maps` skill (OSM/Nominatim/Overpass)
- You need user reviews / recommended dishes → no API available; Dianping scraping required (see pitfalls)
- Geocoding / routing / directions → use `maps` skill

## Prerequisites

1. **API Key**: Register at https://lbs.amap.com → Console → Application Management → Create app → Add Key → Select "Web服务" type
2. **Environment variable**: `export AMAP_API_KEY="your_key"`
3. **Python dependency**: `pip install requests`

Verify readiness:
```bash
echo $AMAP_API_KEY   # should print a 32-char hex string
python3 -c "import requests; print('ok')"
```

## Script

```
SCRIPT=~/.hermes/skills/productivity/chinese-poi-search/scripts/amap_poi_tool.py
```

Six commands: `search`, `around`, `nearby`, `detail`, `filter`, `geocode`.

### search — Keyword Search

```bash
# Search for hotpot restaurants in Shanghai
python3 $SCRIPT search "火锅" --city 上海 --city-limit

# Search by POI type code (050000 = food & dining)
python3 $SCRIPT search "" --city 北京 --types 050000 --city-limit

# Multiple keywords with | separator
python3 $SCRIPT search "火锅|烤肉" --city 上海 --city-limit
```

Returns: name, rating, cost, address, business_area, tel, opentime, photos, location.

### around — Nearby Search

```bash
# Coffee shops within 1km of a coordinate
python3 $SCRIPT around "121.4737,31.2304" --keywords "咖啡" --radius 1000

# All restaurants within 3km
python3 $SCRIPT around "121.4737,31.2304" --types 050000 --radius 3000
```

Location format: `longitude,latitude` (Amap uses GCJ-02, not WGS-84).

### nearby — Place Name + Nearby Search (recommended)

Auto geocodes a place name then searches around it. **This is the simplest flow**: just name a place, and the tool handles everything.

```bash
# Hotpot near Lujiazui, Shanghai (default 3km radius)
python3 $SCRIPT nearby "陆家嘴" --keywords "火锅" --city 上海

# Coffee within 1km of People's Square
python3 $SCRIPT nearby "人民广场" --keywords "咖啡" --radius 1000 --city 上海

# All restaurants near Beijing West Station
python3 $SCRIPT nearby "北京西站" --types 050000
```

**Note**: The `nearby` CLI subcommand does NOT support `--min-rating` / `--max-cost`.
Those flags exist only on the `filter` subcommand (which does client-side filtering).
For rating/cost filtering with the nearby workflow, use the Python API
`search_nearby(..., min_rating=4.5, max_cost=150)` instead, or pipe `nearby`
results through `filter`.

Workflow: place name → `geocode()` → coordinates → `search_around()` with radius. Default radius is **3000m (3km)**.

Returns: geocode info (coordinates, formatted address) + POI list.

**Tip**: always pass `--city` for more accurate geocoding. Without it, "人民广场" could match multiple cities.

### geocode — Address to Coordinates

```bash
python3 $SCRIPT geocode "陆家嘴" --city 上海
```

Returns: location (lng,lat), formatted_address, adcode, level, etc.

### detail — POI Detail by ID

```bash
python3 $SCRIPT detail B00178D6VU
```

### filter — Restaurant Filter (convenience wrapper)

Client-side filtering on top of `search` for rating/cost/district:

```bash
# Pudong hotpot, rating >= 4.5, per-person <= 150 yuan
python3 $SCRIPT filter "火锅" --city 上海 --min-rating 4.5 --max-cost 150 --district 浦东
```

## Python API

```python
import os, sys
SCRIPT_DIR = os.path.expanduser("~/.hermes/skills/productivity/chinese-poi-search/scripts")
sys.path.insert(0, SCRIPT_DIR)
from amap_poi_tool import search_places, search_around, geocode, search_nearby, get_place_detail, filter_restaurants

# Geocode — place name to coordinates
result = geocode("陆家嘴", city="上海")

# Nearby search — place name + keywords, auto geocode (simplest API)
result = search_nearby("陆家嘴", keywords="火锅", city="上海")
result = search_nearby("人民广场", keywords="咖啡", radius=1000, city="上海")

# Keyword search (by city)
result = search_places(keywords="火锅", city="上海", city_limit=True, types="050000")

# Around search (by coordinates)
result = search_around(location="121.4737,31.2304", keywords="咖啡", radius=1000)

# Detail
result = get_place_detail(poi_id="B00178D6VU")

# Filter (client-side: min_rating, max_cost, district)
result = filter_restaurants("火锅", city="上海", min_rating=4.5, max_cost=150, district="浦东")
```

### resolve_restaurants -- Resolver Wrapper

For `interactive-task` / `interactive-task-food` Phase 2 integration. Returns
standardized objects matching the resolver contract (`object_id`, `name`,
`address`, `phone`, `extra_info`). Never raises -- returns `[]` on failure.

```python
import os, sys
SCRIPT_DIR = os.path.expanduser("~/.hermes/skills/productivity/chinese-poi-search/scripts")
sys.path.insert(0, SCRIPT_DIR)
from amap_poi_tool import resolve_restaurants

# Returns [{object_id, name, address, phone, extra_info:{rating, cost, ...}}]
results = resolve_restaurants(
    cuisine="火锅",
    area="上海",
    min_rating=4.5,
    max_cost=150,
    district="浦东",
    party_size=4,
)
```

## Key Parameters

| Parameter   | Values | Notes |
|-------------|--------|-------|
| keywords    | Free text, `\|` for multiple | Max 80 chars |
| types       | 6-digit POI type code | `050000`=餐饮, `060000`=购物, `100000`=住宿. See `references/amap-poi-api.md` |
| city/region | City name, citycode, or adcode | adcode is most precise (district-level) |
| city_limit  | true/false | `true` = strict, `false` = nationwide with weighting |
| sortrule    | `weight` / `distance` | `weight` = comprehensive, `distance` = by proximity |
| extensions  | `all` / `base` | v3 param. `all` returns biz_ext (rating, cost, open_time, opentime2) + tel + business_area + tag. Script always uses `all`. |
| offset      | int | v3 param for page size (v5 uses `page_size`). Script handles this internally. |

## POI Type Codes (Common)

| Code   | Category |
|--------|----------|
| 050000 | 餐饮服务 |
| 050100 | 中餐厅 |
| 050200 | 外国餐厅 |
| 050300 | 快餐厅 |
| 050400 | 茶艺咖啡馆 |
| 060000 | 购物服务 |
| 070000 | 生活服务 |
| 071000 | 美容美发 |
| 080000 | 体育休闲服务 |
| 080600 | 电影 |
| 080900 | KTV |
| 100000 | 住宿服务 |
| 110200 | 风景名胜 |

Full table: https://lbs.amap.com/api/webservice/download

## Returned Fields

Fields are extracted from v3 `extensions=all` responses. The script's `_parse_poi`
handles both v3 (biz_ext nesting) and v5 (flat) formats, preferring v3 data.

| Field           | Source (v3) | Notes |
|-----------------|-------------|-------|
| name            | top-level   | Business name |
| address         | top-level   | Street address |
| location        | top-level   | `lng,lat` (GCJ-02) |
| tel             | top-level   | Phone |
| pname/cityname/adname | top-level | Province/city/district |
| business_area   | top-level   | Commercial district |
| tag             | top-level   | Specialty tags (food POIs, e.g. "川菜,粤菜") |
| keytag          | top-level   | Main search keyword (v3 only) |
| rating          | biz_ext     | 0-5 scale (food/hotel/scenic/cinema only) |
| cost            | biz_ext     | Per-person yuan (same categories) |
| opentime_today  | biz_ext.open_time | Today's hours |
| opentime_week   | biz_ext.opentime2 | Full week description |
| photos          | top-level   | Array of {title, url} |

## Pitfalls

1. **v3 vs v5 API for personal keys**: The v5 endpoints (`/v5/place/*`) advertise `show_fields` for rating/cost/tel/business_area, but these fields are silently empty for personal (non-enterprise) keys. The v3 endpoints (`/v3/place/text`, `/v3/place/around`) with `extensions=all` DO return these fields -- they are nested inside `biz_ext` (rating, cost, open_time, opentime2) and top-level (tel, business_area, tag). The script uses v3 for search/around. The v5 detail endpoint (`/v5/place/detail`) also lacks depth fields for personal keys, so the script does a two-step query: v5 detail gets name+location, then v3 around with that location+name retrieves the full depth fields.

2. **Coordinate system**: Amap uses GCJ-02, not WGS-84. If you get coordinates from GPS or OSM, they need conversion before using as `location` in `around` search. OSM coordinates fed directly to Amap will be ~50-500m off.

3. **rating/cost category restriction**: `rating` and `cost` fields are only returned for food, hotel, scenic spot, and cinema POIs. Other categories (shopping, life services) will not have these fields even with `extensions=all`.

4. **Client-side filter pagination**: The `filter` command does client-side filtering (API doesn't support server-side rating/cost filters). This means each page may return fewer than `page_size` results after filtering. For broad searches, fetch multiple pages.

5. **Max 200 results**: The API caps at 200 results per query (across all pages). For dense areas, narrow by district or type code.

6. **No reviews**: Amap has no user review text, no "recommended dishes", no star-rating distribution. That's Dianping's exclusive domain. If the user specifically needs review content, see the Dianping alternatives note below.

7. **Key quota**: Personal developer free tier has daily call limits (typically thousands). Check the console dashboard. Enterprise auth increases limits AND unlocks v5 depth fields.

8. **Dianping alternatives**: Dianping has no public API. Options if review content is critical:
   - MCP servers: `shawnq-msft/mcp-dianping` (Playwright + auth.json, 2 tools)
   - Multi-platform: `goesByhc/cn-scraper-mcp` (includes Dianping, CDP-based)
   - Paid API: `yuncaiji/API` (commercial scraping service)
   - All involve scraping (font anti-scraping, login state, IP risk control)

9. **Skill boundary**: This skill provides the `resolve_restaurants` wrapper for `interactive-task-food` Phase 2, but the resolver registration (input_mapping, output_schema, env_required) belongs in `interactive-task-food`'s SKILL.md, NOT in the general `interactive-task` skill. The general skill stays domain-agnostic.

10. **Python `~` expansion in sys.path**: Python's `sys.path.insert(0, "~/...")` does NOT expand `~` to the home directory - it creates a literal directory named `~`. Always use `os.path.expanduser("~/.hermes/skills/...")` before inserting into `sys.path`. The SKILL.md Python API examples above show the correct pattern.

11. **Cross-skill path references**: Domain skills that reference this skill's script (e.g. `interactive-task-food` SKILL.md) must use the full absolute path `~/.hermes/skills/productivity/chinese-poi-search/scripts/amap_poi_tool.py`, NOT relative paths like `"chinese-poi-search/scripts"`. Relative paths fail when the agent's cwd is not the skills directory (which is almost always the case). If you see a domain skill using relative paths to reach this script, flag it for correction.

12. **Resolver function name**: The actual function name is `resolve_restaurants`. If a referencing skill or template uses a different name (e.g. `search_nearby_restaurants` in `interactive-task/references/task-templates.md`), that is a naming mismatch - the template is for reference only and the real entry point is `resolve_restaurants`.

13. **execute_code env var not inherited**: The `execute_code` sandbox does NOT inherit shell environment variables. If `AMAP_API_KEY` is set in the terminal but not in the Python process, the script raises `ValueError: 缺少高德 API Key`. Fix: explicitly set `os.environ["AMAP_API_KEY"] = "..."` at the top of any execute_code script before importing `amap_poi_tool`, or pass `key=` as a function argument. The terminal CLI works fine because it inherits the shell env; only the execute_code path has this issue.

14. **opentime_week contains holiday closures**: The `opentime_week` field (from `biz_ext.opentime2`) sometimes embeds temporary holiday closure notices inline, e.g. `周一至周五 11:00-14:00,16:30-23:00；周六至周日 11:00-00:30 2026-02-14至2026-02-21 周一至周日 全天关闭`. When presenting hours to the user, prefer `opentime_today` for "are they open now" checks, and parse `opentime_week` carefully -- do not display the raw string without filtering for the current date's applicability.

15. **Late-night dining filtering**: When the user wants 夜宵 (late-night supper), the critical filter is `opentime_today` -- many restaurants close at 22:00-23:00. Always retrieve and present `opentime_today` for each candidate, and filter out places whose last order time is before the user's planned arrival. Amap does not provide a separate "last order" time; the closing time in `opentime_today` is the best proxy. For places open past midnight, `opentime_today` will show times like `17:00-03:00`.

16. **Compound place name geocoding ambiguity**: The `geocode()` / `nearby` function can resolve a compound place name to the WRONG location. Example: "望京恒电" geocoded to 海淀区温泉镇 (北京恒电创新科技有限公司, 30km off) instead of the intended 朝阳区望京恒电大厦. When the user says a compound name (area + building/company), always verify the geocoded `formatted_address` and `district` against the user's stated area. If the district doesn't match, use `search_places(keywords="<building name>", city=..., city_limit=True)` to find the correct POI, extract its `location` coordinate, then call `search_around(location=..., ...)` directly. This two-step lookup (POI search -> coordinate -> around search) is more reliable than `geocode()` for compound names that contain a company/building name matching multiple locations.

17. **`nearby` CLI does not support rating/cost filtering**: The `nearby` CLI subcommand does NOT accept `--min-rating` or `--max-cost` flags -- it will error with "unrecognized arguments". The Python API `search_nearby()` DOES support `min_rating` and `max_cost` as function parameters. For CLI-based workflows needing these filters, use the `filter` subcommand instead, or use `execute_code` to call the Python API. See pitfall #13 for the `execute_code` env-var workaround.

18. **Large group dining (团建/聚餐) search strategy**: For large parties (8+ people), the first page of `search_around` results is dominated by small eateries (food courts, fast food, desk meals in office buildings). To find restaurants suitable for group dining: (a) use cuisine-specific keywords like "烤鸭|北京菜|粤菜|淮扬菜" rather than generic "家常菜|中餐"; (b) expand radius to 5km to capture sit-down restaurants in the broader area; (c) filter by `min_rating >= 4.0` and `cost >= 60` (very low per-person cost indicates a food court / fast food stall, not a sit-down restaurant); (d) verify `opentime_today` covers the planned dining time. Present results with distance from the reference point, estimated total cost (per-person × party_size), and note whether the restaurant likely has private rooms (包间) -- Amap does not expose this field, so it must be confirmed by phone.

19. **execute_code uses base conda env, not project env**: The `execute_code` tool runs Python from the base conda env (`/Users/chengjian/miniforge3/bin/python`, currently Python 3.13), not project-specific conda envs. If a project (e.g. hermes-nexus) has dependencies only installed in its own env (`hermes_nexus`, Python 3.11), `execute_code` will fail with `ModuleNotFoundError`. Workaround: write a temp .py file and run it via `terminal` with the project's env python (e.g. `/Users/chengjian/miniforge3/envs/hermes_nexus/bin/python /tmp/script.py`). Avoid inline `-c` with complex f-strings (backslash escaping issues in shell quotes).

20. **High-end / fine-dining search strategy**: When the user asks for "高端" or "高端餐厅" (fine dining, celebration, anniversary, birthday), the default `nearby` search centered on a residential area name returns mostly casual chains and food-court eateries. Example: searching "牛排|西餐" near "太阳宫" (residential/commercial mix, Beijing) returned 萨莉亚 (¥54), 必胜客 (¥67), Wagas (¥80) — none are "高端". Two adjustments fix this: (a) use cuisine-specific keywords that signal fine dining — "牛排馆|扒房|steakhouse" rather than generic "西餐|牛排" — because Amap's `tag` field for upscale restaurants contains these terms; (b) center the search on the nearest major commercial hub (e.g. 三元桥, 亮马桥, 国贸) rather than the residential area the user named, because fine-dining restaurants cluster in commercial complexes (凤凰汇, 官舍, 置地广场) not residential malls. The residential area name is still useful as a `--city` hint for geocoding and for telling the user "about X km from [their area]". After getting results, filter by `cost >= 150` and `rating >= 4.5` to surface genuine fine-dining candidates. Present results sorted by cost descending (most premium first) when the user said "高端", or by rating when they said "性价比".

## Dianping vs Amap Quick Comparison

| Feature            | Dianping | Amap POI |
|--------------------|----------|----------|
| Public API         | No       | Yes      |
| Scraping required  | Yes      | No       |
| User reviews       | Yes      | No       |
| Rating             | Yes      | Yes (limited categories) |
| Per-person cost    | Yes      | Yes (limited categories) |
| Business hours     | Yes      | Yes      |
| Photos             | Yes      | Yes      |
| Stability          | Low      | High     |
| Cost               | Free (scraping) or paid | Free tier |

## Verification

```bash
# Should return JSON with pois array (requires valid AMAP_API_KEY)
export AMAP_API_KEY="your_key"
python3 ~/.hermes/skills/productivity/chinese-poi-search/scripts/amap_poi_tool.py search "肯德基" --city 上海 --city-limit
```

## References

- `references/amap-poi-api.md` - Full API endpoint reference, parameter tables, field availability, type codes, Dianping comparison
- `references/cross-skill-validation.md` - End-to-end test results, known issues in user-owned referencing skills, validation checklist for architecture reviews
- `references/late-night-filtering.md` - Recipe for filtering late-night/夜宵 candidates by opentime, including execute_code env-var workaround and holiday-closure parsing
- `references/compound-name-geocoding.md` - How to handle compound place names (e.g. "望京恒电") that geocode to the wrong location; POI-search-first fallback workflow
- `references/hermes-nexus-phrase4.md` - hermes-nexus mock interaction service (Phrase 4 of interactive-task-food): architecture, API, conda env, execute_code workaround, test results
