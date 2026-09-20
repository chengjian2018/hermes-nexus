# Compound Place Name Geocoding Ambiguity

## Problem

When a user provides a compound place name (e.g. "望京恒电" = area "望京" + building/company "恒电"),
the Amap `geocode()` API may resolve it to the WRONG location -- a different POI with a similar
name in a completely different district.

## Real Example (2026-08-03 session)

User said: "望京恒电附近的餐馆"

### Wrong path (geocode directly):

```
geocode("望京恒电", city="北京")
→ formatted_address: "北京市海淀区恒电(西门)"
→ district: 海淀区
→ location: 116.196778,40.039431
```

This resolved to **北京恒电创新科技有限公司** in 海淀区温泉镇 -- 30km away from the actual
望京恒电大厦 in 朝阳区.

The subsequent `search_around` with this wrong coordinate returned restaurants in 海淀温泉镇,
completely useless to the user.

### Correct path (POI search first):

```
search_places(keywords="恒电", city="北京", city_limit=True)
→ Found "望京恒电大厦" at 望京东路4号, 朝阳区, business_area: 望京
→ location: 116.487901,40.008555

search_around(location="116.487901,40.008555", keywords="...", radius=5000)
→ Correct results in 望京 area
```

## Root Cause

Amap geocoding matches the full string against all POI names and addresses. When a compound
name contains a common company/building name (like "恒电"), the geocoder may match a different
business that has "恒电" in its registered name, especially if the compound name isn't an
exact POI name in the geocoding database.

The geocoder returned `level: 兴趣点` (POI) and `matches: 2`, indicating multiple candidates
existed -- it picked the wrong one.

## Detection Signal

Always check the geocoded result's `district` and `formatted_address` against the user's
stated area. If the user says "望京" but the geocode returns "海淀区", that's a mismatch.

## Recommended Workflow for Compound Place Names

1. **Try `geocode(place_name, city=city)` first** -- it's the simplest path.
2. **Verify the result**: check `formatted_address`, `district`, and `level`.
   - If `district` matches user's expected area → proceed with `search_around`.
   - If `district` does NOT match → go to step 3.
3. **Fallback: POI keyword search**: `search_places(keywords="<building/company name>",
   city=city, city_limit=True)`.
   - Look through results for the POI whose `business_area` or `address` matches the user's
     expected area.
   - Extract that POI's `location` coordinate.
4. **Use `search_around(location=correct_coord, ...)` directly** with the verified coordinate.

## When This Matters

- Compound names: "望京恒电", "陆家嘴国泰", "张江高科" (area + company/building)
- Ambiguous place names: "人民广场" (exists in many cities), "万达广场" (many locations)
- Company-named locations: user says company name, not the building name

## Related Pitfalls

- SKILL.md pitfall #16: Compound place name geocoding ambiguity
- SKILL.md pitfall #2: Coordinate system (GCJ-02 vs WGS-84)
