# Cross-Skill Validation Findings

Validation results from a full architecture review of the three-skill stack:
`interactive-task` (general) -> `interactive-task-food` (domain) -> `chinese-poi-search` (tool).

## End-to-End Test Results (2026-08-02)

resolve_restaurants tested with live AMAP_API_KEY, all three modes passed:

| Mode | Input | Results | Status |
|------|-------|---------|--------|
| place_name nearby | 螺蛳粉 + 左家庄 + 北京 | 8 results | PASS |
| city+district | 火锅 + 上海 + 浦东 + rating>=4.5 | 3 results | PASS |
| coordinates | 咖啡 + 121.4752,31.2297 + 1km | 20 results | PASS |
| error case | 不存在的菜系 + 火星 | 0 results, no exception | PASS |

Returned fields verified: object_id, name, address, phone, extra_info (rating,
cost, opentime_today, opentime_week, tag, photos, location, search_mode, geocode).

## Known Issues in User-Owned Skills (cannot patch directly)

### interactive-task-food (user-owned)

1. **Relative path bug (P0)**: SKILL.md L182 uses:
   ```python
   sys.path.insert(0, "chinese-poi-search/scripts")
   ```
   This is a relative path that fails when cwd is not the skills directory.
   Should be:
   ```python
   import os, sys
   sys.path.insert(0, os.path.expanduser("~/.hermes/skills/chinese-poi-search/scripts"))
   ```
   Affected lines: L135, L173, L182, L218, L222.

2. **Phrase 4 tools not implemented (P0)**: voice_call and wechat_bot dispatch
   targets have payload schemas defined but no actual tool implementation exists.
   The pipeline ends at Phrase 3 output; Phrase 4-5 cannot execute.

3. **Phrase 4->5 result loop not implemented (P1)**: No InteractionResult format,
   no evaluate_constraints() function, no retry/timeout policy for failed calls.

### interactive-task (user-owned)

1. **Resolver tool name mismatch (P1)**: `references/task-templates.md` L69
   references `tool: search_nearby_restaurants` but the actual function in
   `chinese-poi-search/scripts/amap_poi_tool.py` is `resolve_restaurants`.
   The template is marked "reference only" but the mismatch causes confusion.

2. **3-phase vs 5-phrase terminology (P2)**: General skill uses "3-phase"
   (Discover -> Resolve -> Dispatch). Domain skill uses "5-phrase". The mapping
   is: Phrase 1+3 = Phase 1, Phrase 2 = Phase 2, Phrase 4+5 = Phase 3. This
   mapping is not documented in either skill.

## Validation Checklist for Future Reviews

When validating this skill stack, verify:

- [ ] resolve_restaurants runs in all 3 modes (place_name, location, area+district)
- [ ] resolve_restaurants returns [] on invalid input (no exception)
- [ ] Python import paths use os.path.expanduser(), not bare "~" or relative paths
- [ ] Resolver function name matches across all referencing skills
- [ ] AMAP_API_KEY is set and valid (32-char hex)
- [ ] v3 endpoints used for search/around (not v5 - depth fields empty for personal keys)
- [ ] Cross-skill path references use absolute paths (~/.hermes/skills/...)
