---
name: interactive-task-food
description: "Use when user wants to find a restaurant, check availability/queue/pet-policy, or make a dining reservation. Thin domain client for food-finding: collects requirements from the user, resolves candidate restaurants via POI search, delegates the counterpart conversation (negotiating with the restaurant) to a server-side dialogue template on hermes-nexus, polls the result, and reports the post-judgment outcome."
version: 3.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hci, food, restaurant, dining, reservation, domain-skill, counterpart]
    related_skills: [interactive-task-skill-generator, chinese-poi-search]
---

# Interactive Task: Food-Finding (Domain Skill)

## Overview

餐厅订位/确认领域的**代聊薄客户端**：对话话术全部在 hermes-nexus 服务端
（由 `references/template.json` 话术模版 `food_booking` 编译执行——与餐厅
对端逐项确认时段/座位/低消/特殊需求，无法满足时弹性协商），本 skill 只负责
三件事——向用户收集用餐需求、触发并轮询自主任务会话、对结果做后置判断并汇报。

```
用户："找餐厅/订位/帮我代聊订位"
  Phase 1 与用户多轮 → task_info（时间/人数/偏好/位置 + 约束 + 增值服务）
  Phase 2 POI 筛选候选餐厅（chinese-poi-search resolver）
  Phase 3 trigger_task.py ensure —— 模版对齐（探活/懒注册/hash 本地为准）
  Phase 4 trigger_task.py run   —— 触发任务（与餐厅对端多轮）+ 轮询终态
  Phase 5 后置判断 → 汇报（已确认项/未满足约束/备选方案/用户决定）
```

服务端约定：hermes-nexus 对话引擎（承载话术模版执行与任务会话）**默认已在
后台运行**（`http://127.0.0.1:8000`）；脚本每次调用前自动探活，环境异常时
自行处理——本 skill 不涉及、也不需要知道服务的部署位置。

## When to Use

**适用**：
- 用户说："找餐厅""吃什么""附近美食""订位""有没有xxx店""帮我订个餐厅""帮我预约"
- 用户给出菜系/地点/时间与用餐意向
- 用户要确认餐厅排队、宠物政策、包间低消、儿童设施、停车等
- 用户有特殊约束（孕妇、小孩、宠物、私密性）或增值服务需求（生日、车牌登记）

**不适用**：
- 纯外卖点餐（另一个领域）
- 自己做饭、食谱查询
- 纯评价/推荐查询，不涉及与餐厅交互确认

---

## Phase 1: 需求收集 (Collect Requirements from the User)

**与用户**多轮对话，产出本次任务的 `task_info`。分三组收集，组内一次问、
组间一句话过渡；关键缺失项先问再给默认值。

### 基础信息（用于 Phase 2 POI 筛选）

| 字段 | 必收 | 说明 | 示例/默认 |
|------|------|------|-----------|
| `dining_time` | ✓ | 预计到店时间 | "明天晚上7点" |
| `party_size` | ✓ | 用餐人数（含小孩） | 4 |
| `dietary_preference` | ✓ | 菜系偏好 + 饮食禁忌 | "火锅，不吃羊肉" |
| `city` | ✓ | 城市 | "北京" |
| `place_name` | 二选一 | 具体地点（优先，周边3km搜索） | "望京SOHO" |
| `district` | 二选一 | 区县 | "朝阳" |
| `min_rating` | 可选 | 最低评分 | 默认 4.0 |
| `max_cost` | 可选 | 最高人均 | 默认 200 |
| `meal_type` | 可选 | 餐别，可从时间推断 | lunch/dinner/late_night |

### 约束条件（服务端话术将向餐厅逐项确认）

| 字段 | 必收 | 说明 | 默认 |
|------|------|------|------|
| `has_pregnant` | ✓ | 有孕妇（无烟/安静/通风） | false |
| `has_children` / `children_age` | ✓ | 有小孩及年龄（儿童座椅/儿童餐） | false |
| `has_pet` | ✓ | 带宠物（宠物政策） | false |
| `need_private_room` | ✓ | 要包间 | false |
| `privacy_level` | 可选 | no_requirement/quiet_corner/private_room/fully_private | no_requirement |
| `max_queue_minutes` | ✓ | 排队容忍上限（分钟） | 30 |

**约束冲突速查**（探测到即向用户给 2-3 个消解选项，消解完再继续）：

| 冲突 | 提示与选项 |
|------|-----------|
| 宠物 + 包间 | 宠物友好店通常无包间：A 优先宠物（大厅/户外） B 优先包间（宠物另安排） C 扩大搜索 |
| 孕妇 + 火锅/烧烤 | 油烟重：提醒换菜系或确认通风 |
| 人数≥8 且不要包间 | 大桌难等：建议包间或确认大桌可用性 |
| 不排队 + 高峰时段 | 可能无解：建议错峰或放宽容忍度 |

### 增值服务（服务端话术按需向餐厅确认）

| 字段 | 说明 | 默认 |
|------|------|------|
| `need_parking` / `license_plate` | 停车 + 登记车牌 | false |
| `need_special_tableware` / `special_tableware_detail` | 特殊餐具（如儿童餐具2套） | false |
| `check_minimum_spend` | 需确认包间低消金额 | false |
| `is_birthday` / `birthday_detail` | 生日服务（蛋糕/布置/生日歌） | false |

产出 `task_info` JSON 形如：

```json
{
  "dining_time": "明天晚上7点", "party_size": 4,
  "dietary_preference": "火锅，不吃羊肉", "city": "北京", "place_name": "望京SOHO",
  "min_rating": 4.0, "max_cost": 200,
  "has_children": true, "children_age": "3岁", "has_pet": false,
  "need_private_room": true, "max_queue_minutes": 20,
  "need_parking": true, "license_plate": "京A12345",
  "need_special_tableware": true, "special_tableware_detail": "儿童餐具1套",
  "check_minimum_spend": true, "is_birthday": false
}
```

---

## Phase 2: 对象解析 (Resolve Candidate Restaurants)

用 `chinese-poi-search` 的 `resolve_restaurants`（已实现；失败返回空数组不抛异常）。

```python
import sys
sys.path.insert(0, "~/.hermes/skills/productivity/chinese-poi-search/scripts")  # 展开后用绝对路径
from amap_poi_tool import resolve_restaurants

# 地点名周边搜索（推荐）：用户说了"望京SOHO附近"
results = resolve_restaurants(cuisine="火锅", place_name="望京SOHO",
                              area="北京", min_rating=4.0, max_cost=200)
# 城市区域搜索：只有"上海浦东"
results = resolve_restaurants(cuisine="火锅", area="上海", district="浦东",
                              min_rating=4.5, max_cost=150, party_size=4)
# 返回 [{object_id, name, address, phone, extra_info:{rating, cost, opentime_today, tag, ...}}]
```

环境变量 `AMAP_API_KEY`（高德 Web 服务 Key）。

规则：
- **on_empty**：放宽条件重试一次（降评分/升人均/扩半径），仍空则告知用户并停
- **on_multi**：按评分排序取前 3，用一句话让用户挑；用户不挑默认第一家
- 选定餐厅的 `name/address/phone` 写入 `task_info.restaurant_name/address/phone`
  （服务端话术的会话信息块会带上，代聊知道在跟哪家店谈）
- **resolver 不可用**（无 Key / skill 缺失）：告知用户配置
  `AMAP_API_KEY`；用户手动给餐厅信息则验证记录后继续；否则以
  `awaiting_resolver` 状态停在这里

---

## Phase 3: 模版对齐 (Ensure Template Registered)

用自带脚本一次完成（探活、hash 对齐、懒注册）：

```bash
python scripts/trigger_task.py ensure --template references/template.json
```

行为（无需干预，失败才介入）：

1. `GET /api/v1/health` 探活，异常时脚本自行处理
2. `GET /api/v1/templates/food_booking` 查询：
   - 未注册 → `POST /api/v1/templates` 注册（懒注册，仅首次需要）
   - 已注册且 hash 与本地副本一致 → 直接复用
   - hash 不一致 → **本地为准**，覆盖重注册（元 skill 重新生成过新版）
   - 返回 `source: builtin` → 停止并报告冲突
3. 注册被拒（`400`）→ stderr 打印全量校验错误（带 JSON 路径），
   原样转述给用户并建议重新生成模版

---

## Phase 4: 触发任务 (Trigger Autonomous Task)

kickoff（首条喂给服务端话术模版的消息，**自然语言**，把核心需求说全）：

```
"你好，想订{dining_time}{party_size}位的位子。{需要包间/带3岁小孩要儿童座椅/
带宠物/京A12345需要停车登记/生日…按 task_info 实际有的说}。请问还有位子吗？"
```

触发并轮询到终态（stdout 输出结果 JSON）：

```bash
python scripts/trigger_task.py run \
    --template references/template.json \
    --kickoff "<上面组织的首条消息>" \
    --task-info-json '<Phase 1 的 task_info JSON>' \
    --counterpart-mode llm
```

| 参数 | 说明 |
|------|------|
| `--counterpart-mode llm` | 餐厅对端由 LLM 按模版 `counterpart_hint.role_prompt` 扮演（默认，真实感强） |
| `--counterpart-mode scripted --script-json '[...]'` | 对端按脚本逐轮回复（确定性测试，脚本见模版 `scripted_replies`） |
| `--max-turns N` / `--timeout-s S` | 轮次/时限上限（默认 20 / 600） |
| `--no-ensure` | 刚跑过 ensure 时跳过模版对齐 |

轮询期间：任务在服务端后台执行；用户想看进展时查询
`GET /api/v1/sessions/<session_id>/messages`（session_id 在触发输出的 stderr
日志里）。结果 JSON 关键字段：

```json
{
  "status": "done | failed",
  "finish_reason": "end_module | max_turns | timeout | counterpart_exhausted | error",
  "turn_count": 6,
  "result": {
    "end_module_code": "booking | negotiate",
    "filled_slots": {"queue_minutes": "0", "room_type": "大厅卡座",
                     "minimum_spend": "0", "children_facilities": "儿童座椅",
                     "parking_info": "可登记车牌", "final_confirmation": "confirmed"},
    "messages": [{"role": "user|assistant", "content": "…"}]
  }
}
```

`filled_slots` 是服务端 FSM 从对端回复里逐节点抽取的确认项——Phase 5 的
判断依据主要来自这里 + `messages` 原文。

---

## Phase 5: 后置判断与汇报 (Post-judgment & Report)

对 Phase 4 的 `filled_slots` + `messages` 做逐项判断，输出：

```json
{
  "reservation_confirmed": true,
  "restaurant_name": "海底捞火锅（望京SOHO店）",
  "confirmed_details": {"time": "明天18:45", "party_size": 4,
                        "room_type": "大厅卡座", "queue_minutes": 0,
                        "children_facilities": "儿童座椅", "parking": "已登记 京A12345"},
  "unmet_constraints": [],
  "alternatives": [],
  "user_decision": "accept",
  "summary": "一句话结论"
}
```

### 判定规则（本领域）

| 判定项 | 依据 |
|--------|------|
| `reservation_confirmed` | `finish_reason == "end_module"` 且 `final_confirmation == "confirmed"`（或 `negotiate` 收尾时对话原文含对端明确接受语）；`finish_reason` 为 max_turns/timeout 时一律 `false` 并如实说明中断原因 |
| 排队容忍 | `queue_minutes ≤ task_info.max_queue_minutes`；超出 → `unmet_constraints` 加"排队约N分钟（容忍M分钟）" |
| 包间需求 | `room_type == "包间"` 且 `minimum_spend ≤ max_cost`；低消超预算 → unmet 并给"大厅卡座"替代 |
| 宠物 | `has_pet` 时 `pet_policy ∈ {可入内, 仅户外}`；"不可" → unmet |
| 儿童 | `has_children` 时 `children_facilities ≠ 无` |
| 停车/生日 | `parking_info`、`birthday_service` 对应确认；对端没答或答"无" → unmet |

### 汇报格式

```
📞 **餐厅确认结果**

🏠 餐厅: [restaurant_name]（[phone]）
📌 地址: [address]

✅ 已确认: 时间/人数/座位/儿童座椅/停车登记/排队时长 逐项列出
⚠️ 需要注意: [unmet_constraints 逐条]
📋 下一步: 接受 → 完成；修改需求 → 回 Phase 1；尝试备选 → Phase 2 其余候选重跑 Phase 4
```

**关键承诺必须引用对话原文佐证**（对端说过的确认语），不凭空概括；
`user_decision` 等待用户选择后再落（accept / decline / modify_constraints /
try_alternative）。

---

## Domain Knowledge

### Common Defaults

```yaml
min_rating: 4.0
max_cost: 200
meal_type: dinner            # 未指定默认晚餐
privacy_level: no_requirement
max_queue_minutes: 30
has_pregnant/has_children/has_pet/need_private_room: false
need_parking/need_special_tableware/check_minimum_spend/is_birthday: false
```

### Dialogue Patterns（与用户的收集对话）

- 用户说了菜系+地点+时间 → "好的，[偏好]在[地点]附近，[时间][人数]位。我再确认几个细节。"
- 用户没说区域 → "你在哪个城市？或者告诉我具体位置，我帮你找附近的。"
- 用户说"随便" → "那我按评分推荐几家你选？有什么忌口吗？"
- 用户提到夜宵 → "夜宵营业时间是关键，我帮你确认哪些店还开着。"
- 用户提到带宠物 → "我筛选宠物友好的餐厅并确认政策。有体型/品种限制要注意吗？"
- 用户提到过敏 → "我会让餐厅确认能否规避[过敏原]，交叉接触也要避免吗？"
- 约束冲突 → "⚠️ [冲突描述]。建议：A)[方案A] B)[方案B]。你倾向哪个？"
- 用户确认全部信息 → "好的，正在跟餐厅确认，稍等片刻。"

### Pitfalls

- 部分餐厅午市不营业（尤其火锅/烧烤）；夜宵看 last order 不是关门时间
- 部分餐厅不接受预约只收现场排队；电话排队多数不支持
- 宠物政策**因门店而异**（同一连锁不同店不同），不能假设
- 排队时长随时段变化，节假日可能翻倍，汇报时注明时效
- 包间通常有低消，必须在对话中问到**具体金额**
- 孕妇友好是综合概念（无烟/通风/安静/菜品），不能只看单一维度
- 儿童友好 ≠ 有儿童座椅（还看环境安全/噪音/儿童餐）
- 特殊餐具部分餐厅不主动提供，要明确要求；生日服务需提前预约
- 过敏确认不能只看菜单，要和餐厅沟通交叉污染
- 不要假设用户记得之前说过的信息，也不要重复问已确认项

---

## Workflow Example

用户: "帮我在北京望京SOHO附近找火锅，4个人明天晚上7点，带一个3岁小孩，要包间，开车去"

1. **Phase 1**: dining_time=明天晚上7点, party_size=4, dietary_preference=火锅,
   city=北京, place_name=望京SOHO；约束：has_children=true(3岁)、
   need_private_room=true、max_queue_minutes 默认30；增值：need_parking=true、
   license_plate 待问；冲突检测：小孩+包间无冲突（标注找有儿童座椅的包间）
2. **Phase 2**: `resolve_restaurants(cuisine="火锅", place_name="望京SOHO",
   area="北京", min_rating=4.0, max_cost=200)` → 前3家让用户挑，
   选定后 `task_info.restaurant_name/phone/address` 写入
3. **Phase 3**: `trigger_task.py ensure` → `food_booking` 模版 hash 对齐完成
4. **Phase 4**: kickoff = "你好，想订明天晚上7点4位的位子，有3岁小朋友需要
   儿童座椅，想订包间，京A12345需要停车登记。请问还有位子吗？" →
   `trigger_task.py run` → 轮询至终态
5. **Phase 5**: `filled_slots` 里 room_type=大厅卡座（包间满，negotiate 模块
   协商后接受卡座、无低消）、children_facilities=儿童座椅、
   parking_info=已登记、final_confirmation=confirmed →
   `reservation_confirmed=true`，`unmet_constraints=["包间不可用（已接受大厅卡座替代）"]`
   → 按汇报格式输出，引用对端确认原文

---

## Verification Checklist

- [ ] Phase 1 task_info 必收字段齐全，冲突已消解
- [ ] Phase 2 已选定餐厅并写入 restaurant_name/phone/address（或 awaiting_resolver 明确告知）
- [ ] Phase 3 ensure 成功（或明确报告冲突/校验错误）
- [ ] Phase 4 拿到终态结果（非 running 中断）
- [ ] Phase 5 判定逐项对照上表，关键结论有对话原文佐证
- [ ] 用户已收到汇报并给出 user_decision
