# Amap (高德地图) POI Search API Reference

Condensed reference for the Amap Web Services POI API. Useful when OSM/Overpass
coverage is thin (especially in mainland China) or when you need rating,
cost, business hours, and photos that OSM doesn't provide.

## v3 vs v5: Which to Use (CRITICAL)

Amap has two API versions. For personal (non-enterprise) keys, the version
choice is critical:

| Aspect | v3 (`/v3/place/*`) | v5 (`/v5/place/*`) |
|--------|-------------------|-------------------|
| Depth fields param | `extensions=all` | `show_fields=...` |
| rating | ✅ via `biz_ext.rating` | ❌ empty for personal keys |
| cost | ✅ via `biz_ext.cost` | ❌ empty for personal keys |
| tel | ✅ top-level | ❌ empty for personal keys |
| business_area | ✅ top-level | ❌ empty for personal keys |
| tag / keytag | ✅ top-level | ❌ not returned |
| opentime | ✅ `biz_ext.open_time` + `biz_ext.opentime2` | ❌ empty for personal keys |
| photos | ✅ top-level | ✅ top-level (works) |
| name/address/location | ✅ | ✅ |
| Pagination param | `offset` | `page_size` |
| City param name | `city` | `region` |

**Rule**: Use v3 with `extensions=all` for search and around. Use v5 detail
(`/v5/place/detail`) only to get name+location by ID, then do a v3 around
query with that location+name to retrieve depth fields.

Enterprise keys may unlock v5 `show_fields` for depth data, but this has not
been verified. If you have an enterprise key and v5 works, you can switch
the script to v5 endpoints.

## Key Setup

1. Register at https://lbs.amap.com
2. Console -> Application Management -> Create app -> Add Key
3. Select **Web服务** (Web Services) type
4. Set the key as env var: `export AMAP_API_KEY="your_key"`
5. Free tier: personal developer gets daily quota (thousands of calls).
   Enterprise auth increases limits AND may unlock v5 depth fields.

## Endpoints

### 1. Keyword Search (v3/place/text) -- USE THIS

```
GET https://restapi.amap.com/v3/place/text
```

| Param        | Required | Description |
|--------------|----------|-------------|
| key          | yes      | API key |
| keywords     | yes*     | Search terms, `\|` for multiple (max 80 chars) |
| types        | yes*     | POI type code (e.g. `050000` for food). keywords or types required. |
| city         | no       | City name, citycode, or adcode (v3 uses `city`, v5 uses `region`) |
| city_limit   | no       | `true` = strict limit to city; `false` = nationwide with weighting |
| sortrule     | no       | `weight` (default) or `distance` |
| page         | no       | Page number (starts at 1) |
| offset       | no       | Per page, v3 name (recommend <=25) |
| extensions   | no       | `all` = return biz_ext + tel + business_area + tag. `base` = minimal. |
| output       | no       | `json` (default) |

*One of `keywords` or `types` is required.

### 2. Around Search (v3/place/around) -- USE THIS

```
GET https://restapi.amap.com/v3/place/around
```

| Param        | Required | Description |
|--------------|----------|-------------|
| key          | yes      | API key |
| location     | yes      | Center point `longitude,latitude` (GCJ-02) |
| keywords     | no       | Search terms |
| types        | no       | POI type code |
| radius       | no       | Search radius in meters (0-50000, default 5000) |
| sortrule     | no       | `distance` (default) or `weight` |
| page         | no       | Page number |
| offset       | no       | Per page |
| extensions   | no       | `all` for depth fields |
| output       | no       | `json` |

### 3. Detail by ID (v5/place/detail) -- LIMITED FOR PERSONAL KEYS

```
GET https://restapi.amap.com/v5/place/detail
```

| Param        | Required | Description |
|--------------|----------|-------------|
| key          | yes      | API key |
| id           | yes      | POI ID from search results |
| show_fields  | no       | Extra fields (rating/cost/tel etc.) -- empty for personal keys |

**Workaround**: v5 detail returns name+location but NOT rating/cost/tel for
personal keys. The script does a two-step query:
1. v5 detail -> gets `name` and `location`
2. v3 around with `location` + `keywords=name` + `radius=50` + `extensions=all` -> gets full depth

## v3 biz_ext Structure (extensions=all)

v3 nests depth fields inside `biz_ext`:

```json
{
  "biz_ext": {
    "rating": "4.8",
    "cost": "120.00",
    "open_time": "11:00-07:00",
    "opentime2": "周一至周日 11:00-07:00",
    "meal_ordering": "0"
  },
  "tel": "021-63339190",
  "business_area": "北京路",
  "tag": "川味五花腊肉,双人餐,牛肉..."
}
```

Note: v3 sometimes returns `[]` (empty list) instead of `""` for string fields
when no data is available. The script's `_parse_poi` handles this.

## Return Fields (per POI, v3 extensions=all)

| Field           | Source | Notes |
|-----------------|--------|-------|
| id              | top-level | Unique POI ID |
| name            | top-level | Business name |
| type            | top-level | Category hierarchy string |
| typecode        | top-level | 6-digit code (2+2+2: major+mid+sub) |
| address         | top-level | Street address |
| location        | top-level | `longitude,latitude` (GCJ-02) |
| tel             | top-level | Phone number |
| pname/cityname/adname | top-level | Province/city/district |
| business_area   | top-level | Commercial district |
| tag             | top-level | Specialty tags (food POIs only) |
| keytag          | top-level | Main search keyword (v3 only) |
| rating          | biz_ext | Rating (food/hotel/scenic/cinema only) |
| cost            | biz_ext | Average per-person cost (same categories) |
| biz_ext.open_time | biz_ext | Today's hours |
| biz_ext.opentime2 | biz_ext | Weekly hours description |
| photos          | top-level | Array of {title, url} |

## Common POI Type Codes

| Code   | Category |
|--------|----------|
| 050000 | 餐饮服务 (Food & Dining) |
| 050100 | 中餐厅 (Chinese restaurant) |
| 050200 | 外国餐厅 (Foreign restaurant) |
| 050300 | 快餐厅 (Fast food) |
| 050400 | 茶艺咖啡馆 (Tea/Coffee) |
| 060000 | 购物服务 (Shopping) |
| 070000 | 生活服务 (Life services) |
| 071000 | 美容美发 (Beauty/Hair) |
| 080000 | 体育休闲服务 (Sports/Leisure) |
| 080600 | 电影 (Cinema) |
| 080900 | KTV |
| 100000 | 住宿服务 (Accommodation) |
| 110200 | 风景名胜 (Scenic spots) |

Full code table: https://lbs.amap.com/api/webservice/download

## Limitations

- Max 200 results per query (paginated)
- `rating` and `cost` only returned for food/hotel/scenic/cinema POIs
- No user reviews or review text (that's Dianping's domain)
- No "recommended dishes" like Dianping
- No star-rating distribution breakdown

## Dianping vs Amap

| Feature            | Dianping | Amap POI |
|--------------------|----------|----------|
| Public API         | No       | Yes      |
| Scraping required  | Yes (font anti-scraping, login) | No |
| User reviews       | Yes      | No       |
| Rating             | Yes      | Yes (limited categories) |
| Per-person cost    | Yes      | Yes (limited categories) |
| Business hours     | Yes      | Yes      |
| Photos             | Yes      | Yes      |
| Phone/address      | Yes      | Yes      |
| Stability          | Low (selectors break) | High (official API) |
| Cost               | Free (scraping) or paid API | Free tier available |

## Dianping Alternatives (when review content is critical)

Dianping has no public API. All alternatives involve scraping:

1. **shawnq-msft/mcp-dianping** (GitHub, 2 stars)
   - MCP server with 2 tools: category_rank, shop_detail
   - Playwright + auth.json for login state
   - Python/FastMCP

2. **goesByhc/cn-scraper-mcp** (GitHub, 14 stars)
   - Multi-platform MCP (Taobao, JD, XHS, Zhihu, Weibo, Bilibili, Douban, Dianping, ZSXQ)
   - CDP-based cookie harvesting via guided_login("dianping")
   - Public web page parsing, stability "depends on page structure"

3. **yuncaiji/API** (GitHub, 372 stars)
   - Commercial scraping API service
   - Pay per call, includes Dianping merchant data
   - Contact via WeChat for token

4. **Sniper970119/dianping_spider** (GitHub, 1267 stars)
   - Full-site crawler, solves font anti-scraping (non-OCR)
   - Python, self-hosted

All options face: font anti-scraping, IP risk control, login state requirements.
