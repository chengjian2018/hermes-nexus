---
name: interactive-task-food
description: "Use when user wants to find a restaurant, check availability, or make a dining reservation. Domain skill for food-finding with 5-phase pipeline: information input (3-purpose categorization), object resolution, structured output (4-module dialogue flow with mandatory sections field), interaction initiation (direct tool calls in Phrase 4), and result compilation."
version: 2.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hci, food, restaurant, dining, reservation, domain-skill]
---

# Interactive Task: Food-Finding (Domain Skill)

## Overview

Domain-specific specialization of the general `interactive-task` skill. Pre-configured for restaurant finding, availability checking, and dining reservations. This skill implements the 5-phase pipeline defined in `skill生成需求.md`.

Uses a **5-phase pipeline** to handle the full lifecycle:

1. **Phrase 1: 信息输入** — define what information to collect (for filtering, confirmation, and post-judgment)
2. **Phrase 2: 获取可交互对象** — find restaurants via POI search (delegates to `chinese-poi-search`)
3. **Phrase 3: 信息输出** — structured JSON output with 4-module dialogue flow
4. **Phrase 4: 发起交互** - 直接调用工具（语音外呼、企微推送等）
5. **Phrase 5: 交互信息整理输出** — compile reservation results and output

The dialogue with the user follows a **4-module flow** within Phrase 3:
```
[模块一: 基础信息收集] ◀─────────────┐
  │ (获取: 时间/人数/饮食禁忌等)                │
  ▼                                   │
[模块二: 约束探测与消解] ◀─────────────┤
  │ (获取: 孕妇/宠物/小孩/私密性/排队时间等)           │
  │ ├─> (内部循环: 冲突检测 -> 方案推荐) 
  ▼                                   │
[模块三: 增值服务关联] ◀─────────────┤
  │ (获取: 车牌/餐具/低消/生日定制化等)                │
  ▼                                   │
[模块四: 全局校验与确认] ───(用户要求修改)─┘
  │ (确认无误)
  ▼
[结束会话]
```

**During any module, if constraints cannot be satisfied, end the session immediately.**

## When to Use

Triggers:
- 用户提到找餐厅、吃什么、附近美食、订位
- 用户提到菜系 + 地点 + 用餐意向
- 用户要确认餐厅排队、宠物政策、营业时间等
- 用户有特殊约束需求（孕妇、小孩、包间、私密性等）
- 用户需要增值服务（停车、生日定制、低消确认等）

Don't use for:
- 纯外卖点餐（那是另一个领域）
- 自己做饭、食谱查询
- 纯评价/推荐查询（不涉及外部交互）

## Domain Definition

```yaml
domain: food-finding
description: "找餐厅并确认可用性（排队、宠物政策、营业时间、约束消解、增值服务等）"
version: "2.1.0"

triggers:
  - "找餐厅"
  - "吃什么"
  - "附近美食"
  - "订位"
  - "有没有xxx店"
  - "帮我找个地方吃饭"
  - "帮我订个餐厅"
  - "帮我预约"
```

---

## Phrase 1: 信息输入 (Information Input)

### Purpose

Define what information needs to be collected from the user. Per the interactive-task requirements spec, information serves **three distinct purposes**:

#### (1) 用于 Phrase 2 筛选可交互对象

信息用于 POI 搜索过滤，缩小餐厅候选范围。

> 如：位置、菜系、口味、人均消费、评分 —— 这些字段直接映射到 `resolve_restaurants()` 的查询参数。

#### (2) 用于与交互对象确认（联动 Phrase 3 信息输出模板）

信息用于 Phrase 4 外呼/企微推送时向餐厅确认的内容。这些信息构成 Phrase 4 交互时向餐厅确认的来源。

> 如：排队确认、宠物政策、过敏忌口、包间需求、停车登记 -- 这些字段不是搜索过滤条件，而是需要餐厅直接回复的确认项。

#### (3) 用于后置判断

信息作为 Phrase 5 中与交互对象沟通后的判断依据——对比用户原始需求与餐厅实际回复，判定满足/不满足。

> 如：用户要求排队≤15分钟 → 餐厅回复排队约30分钟 → 标记为 `unmet_constraint`。用户要求宠物友好 → 餐厅确认宠物可入内 → 标记为满足。

### Mapping: 3 Purposes → 4 Modules

以下展示 3 种信息用途如何分布到 Phrase 3 的 4 个对话模块中：

| 用途 | 对应模块 | 信息项 |
|------|----------|--------|
| **(1) 筛选可交互对象** | 模块一: 基础信息收集 | 用餐时间、人数、饮食偏好/禁忌、城市、地点/区县、评分、人均消费 |
| **(2) 与交互对象确认** | 模块二: 约束探测与消解 | 孕妇、宠物、小孩、私密性、排队容忍度 |
| | 模块三: 增值服务关联 | 停车车牌、特殊餐具、低消确认、生日定制 |
| **(3) 后置判断** | 模块四: 全局校验与确认 | 所有已收集信息的确认，与 Phrase 5 结果对比的基准 |

### Collection Strategy

- **Batch within module**: 同一模块内的字段一次性询问
- **Progress across modules**: 模块一 → 模块二 → 模块三 → 模块四，逐步推进
- **Early exit**: 任一模块中约束无法满足，立即结束会话
- **Natural language**: 使用自然语言标签，不暴露内部字段名

---

## Phrase 2: 获取可交互对象 (Get Interactive Objects)

Reference the `chinese-poi-search` skill for POI-based restaurant search.

### Object Resolver

```yaml
object_resolver:
  tool: resolve_restaurants
  description: "基于高德地图 POI 搜索。支持三种搜索模式：地点名周边搜索（推荐）、坐标周边搜索、城市区域搜索。"
  module: "chinese-poi-search/scripts/amap_poi_tool"
  env_required: "AMAP_API_KEY"
  input_mapping:
    cuisine: "{{module_1_basic_info.dietary_preference}}"
    area: "{{module_1_basic_info.city}}"
    place_name: "{{module_1_basic_info.place_name}}"
    district: "{{module_1_basic_info.district}}"
    min_rating: "{{module_1_basic_info.min_rating}}"
    max_cost: "{{module_1_basic_info.max_cost}}"
    party_size: "{{module_1_basic_info.party_size}}"
    radius: 3000   # place_name 和 location 模式的默认搜索半径(米)

  output_schema:
    - name: object_id
      type: string
      description: "高德 POI ID"
    - name: name
      type: string
      description: "餐厅名称"
    - name: address
      type: string
      description: "餐厅地址"
    - name: phone
      type: string
      description: "联系电话"
    - name: extra_info
      type: object
      description: "评分/人均/商圈/营业时间/特色标签/图片/经纬度/搜索模式/geocode信息等"

  on_empty: relax_criteria
  on_multi: confirm_with_user
```

### Resolver Implementation

Resolver 已实现，位于 `chinese-poi-search` skill：

```
脚本路径: chinese-poi-search/scripts/amap_poi_tool.py
函数:     resolve_restaurants(cuisine, area, district, place_name, min_rating, max_cost, party_size, key)
环境变量: AMAP_API_KEY (高德开放平台 Web 服务 Key)
依赖:     pip install requests
```

Agent 调用方式 — 三种模式：

```python
import sys; sys.path.insert(0, "chinese-poi-search/scripts")
from amap_poi_tool import resolve_restaurants

# 模式 1: 地点名周边搜索（推荐）—— 用户说"左家庄附近"
results = resolve_restaurants(
    cuisine="螺蛳粉",
    place_name="左家庄南里",
    area="北京",          # 城市提示，提高 geocode 准确率
    min_rating=4.0,
    max_cost=200,
)

# 模式 2: 城市区域搜索 —— 用户说"上海浦东"
results = resolve_restaurants(
    cuisine="火锅",
    area="上海",
    district="浦东",
    min_rating=4.5,
    max_cost=150,
    party_size=4,
)

# 模式 3: 坐标周边搜索 —— 已知经纬度
results = resolve_restaurants(
    cuisine="咖啡",
    location="121.4752,31.2297",
    radius=1000,
)
# 返回: [{object_id, name, address, phone, extra_info:{rating, cost, business_area, opentime_today, tag, location, photos, search_mode, geocode, ...}}]
# 失败返回空数组 []，不抛异常
```

CLI 调用方式：

```bash
# 地点名周边搜索（最简单）
AMAP_API_KEY=*** python3 chinese-poi-search/scripts/amap_poi_tool.py \
  nearby "左家庄" --keywords "螺蛳粉" --city 北京

# 城市区域搜索
AMAP_API_KEY=*** python3 chinese-poi-search/scripts/amap_poi_tool.py \
  filter "火锅" --city 上海 --min-rating 4.5 --max-cost 150 --district 浦东
```

### Resolver Not Registered

如果 `AMAP_API_KEY` 未配置或 `chinese-poi-search` skill 不可用：

1. 告诉用户："餐厅筛选工具需要高德地图 API Key。请在 https://lbs.amap.com 注册并设置环境变量 AMAP_API_KEY。"
2. 输出已解析的查询参数，方便用户手动搜索。
3. 如果用户手动提供了餐厅信息，验证并记录，继续 Phrase 3。
4. 如果用户没有提供，以 `status: "awaiting_resolver"` 继续。

---

## Phrase 3: 信息输出 (Information Output)

按 JSON 格式输出。**输出中必须包含 `sections` 字段（List）**——这是与通用 `interactive-task` skill 的 `collected_info` 字段对应的领域特化字段。每个 section 必须包含：

1. **该 section 的任务描述**（`description` 字段，说明此模块要完成什么目标）
2. **已收集的信息项**（`items` 数组，每项含 field/label/value/source）
3. **完成状态**（`status` 和 `criteria_met`，标记此模块是否通过校验）

> 注：`sections` 字段的数组顺序即对话推进顺序（模块一 → 模块二 → 模块三 → 模块四），不可打乱。

### 3.1 Module Flow

信息收集按以下 4 个模块顺序推进。每个模块内的字段一次性询问，模块间用一句话过渡。

**关键规则：在任一模块中，如果约束无法满足（如无宠物友好餐厅、无包间等），立即结束会话，不继续后续模块。**

#### 模块一: 基础信息收集

收集用餐的基本信息，用于 Phrase 2 的 POI 筛选。

```yaml
module:
  name: basic_info
  label: 基础信息收集
  goal: "确认用餐时间、人数、饮食偏好及位置，为 POI 搜索做准备"
  
  required_fields:
    - name: dining_time
      label: 用餐时间
      type: datetime
      description: "预计到店时间，如'今天晚上7点''明天中午12点'"
    
    - name: party_size
      label: 用餐人数
      type: integer
      description: "几位用餐，含小孩"
    
    - name: dietary_preference
      label: 饮食偏好/禁忌
      type: string
      description: "菜系偏好（如火锅、日料、粤菜）以及饮食禁忌（如不吃辣、清真、素食、过敏原等）"
    
    - name: city
      label: 城市
      type: string
      description: "如上海、北京、广州"
    
    - name: place_name
      label: 具体地点
      type: string
      description: "如'左家庄''陆家嘴''望京SOHO'。提供后自动以该地点为中心周边3km搜索。优先于 district。"
    
    - name: district
      label: 区县
      type: string
      description: "如浦东、朝阳。当 place_name 未提供时使用。"

  optional_fields:
    - name: meal_type
      label: 餐别
      type: enum
      options: [lunch, dinner, late_night]
      description: "午餐/晚餐/夜宵，可从 dining_time 推断"
    
    - name: min_rating
      label: 最低评分
      type: number
      description: "0-5，如4.5"
      default: 4.0
    
    - name: max_cost
      label: 最高人均
      type: integer
      description: "人均消费上限，如150"
      default: 200

  completion_criteria: "dining_time, party_size, dietary_preference, city 已确认；place_name 或 district 至少填一个"
  dialogue_hint: "一次性询问：'请问几位用餐？什么时间？有什么饮食偏好吗？在哪个城市、哪个区域？' 如果用户说了具体地点，优先用 place_name 模式搜索（周边3km）。"
  search_mode:
    nearby: "place_name 有值 → Phrase 2 调用 resolve_restaurants(place_name=..., area=city, ...)"
    area:   "仅有 city/district → Phrase 2 调用 resolve_restaurants(area=city, district=..., ...)"
```

#### 模块二: 约束探测与消解

探测可能影响用餐体验的约束条件，并通过**内部循环（冲突检测 → 方案推荐）**消解冲突。

```yaml
module:
  name: constraint_resolution
  label: 约束探测与消解
  goal: "探测用户的特殊约束条件，检测冲突，推荐消解方案"
  
  required_fields:
    - name: has_pregnant
      label: 是否有孕妇
      type: boolean
      description: "孕妇可能需要无烟区、安静座位、特定饮食"
      default: false
    
    - name: has_children
      label: 是否有小孩
      type: boolean
      description: "小孩需要儿童座椅、儿童餐、安全环境"
      default: false
    
    - name: children_age
      label: 小孩年龄
      type: string
      description: "如'3岁''5岁和7岁'，has_children=true 时必填"
      condition: "has_children == true"
    
    - name: has_pet
      label: 是否带宠物
      type: boolean
      description: "需要宠物友好餐厅"
      default: false
    
    - name: need_private_room
      label: 是否需要包间
      type: boolean
      description: "包间/私密用餐空间"
      default: false
    
    - name: privacy_level
      label: 私密性要求
      type: enum
      options: [no_requirement, quiet_corner, private_room, fully_private]
      description: "私密性等级：无要求 / 安静角落 / 包间 / 完全私密空间"
      default: no_requirement
    
    - name: max_queue_minutes
      label: 最大排队容忍时间
      type: integer
      description: "可接受的最长排队时间（分钟），0表示不接受排队"
      default: 30

  completion_criteria: "所有约束字段已确认（含默认值），冲突检测已完成"
  dialogue_hint: "分批询问。先问基础约束：'用餐有什么特殊需求吗？比如有孕妇、小孩、需要带宠物、或者想要包间？' 然后根据回答深入。最后确认排队容忍度。"
  optional: false

  # 内部循环: 冲突检测 → 方案推荐
  conflict_resolution_loop:
    description: "对于探测到的约束，自动检测冲突并提供消解方案"
    rules:
      - conflict: "has_pet=true AND need_private_room=true"
        hint: "宠物友好餐厅通常没有包间，部分宠物友好餐厅可能只开放户外区域"
        resolution: "告知用户此冲突，提供选择：A) 优先宠物友好，接受大厅/户外 B) 优先包间，宠物另做安排 C) 扩大搜索范围找同时满足的"
      
      - conflict: "has_children=true AND need_private_room=true"
        hint: "包间适合带小孩，但需确认是否有儿童座椅和儿童餐"
        resolution: "标注此需求，Phrase 2 筛选时优先找有包间且支持儿童的餐厅"
      
      - conflict: "has_pregnant=true AND 餐厅类型=火锅/烧烤"
        hint: "火锅/烧烤类餐厅油烟较重，孕妇可能不适"
        resolution: "主动提醒用户，建议考虑其他菜系或确认餐厅通风情况"
      
      - conflict: "party_size >= 8 AND need_private_room=false"
        hint: "8人以上大桌可能需要包间或提前预订"
        resolution: "建议用户考虑包间，或确认大桌可用性"
      
      - conflict: "max_queue_minutes=0 AND 热门时段"
        hint: "不接受排队 + 用餐高峰时段，可能无可用餐厅"
        resolution: "建议调整时间避开高峰，或放宽排队容忍度"
    
    loop_logic: |
      while (存在未消解的冲突):
        1. 列出冲突
        2. 推荐消解方案（2-3个选项）
        3. 等待用户选择
        4. 更新约束字段
        5. 重新检测冲突
      → 所有冲突消解后，进入模块三
```

#### 模块三: 增值服务关联

确认是否需要增值服务，这些服务通常不影响餐厅选择但需要在 Phrase 4 交互时确认。

```yaml
module:
  name: value_added_services
  label: 增值服务关联
  goal: "确认停车、特殊餐具、低消、生日定制等增值服务需求"
  
  required_fields:
    - name: need_parking
      label: 是否需要停车
      type: boolean
      description: "是否需要停车位/代客泊车"
      default: false
    
    - name: license_plate
      label: 车牌号
      type: string
      description: "用于停车登记或免停申请，need_parking=true 时询问"
      condition: "need_parking == true"
    
    - name: need_special_tableware
      label: 是否需要特殊餐具
      type: boolean
      description: "如儿童餐具、老人餐具、特殊材质要求"
      default: false
    
    - name: special_tableware_detail
      label: 特殊餐具说明
      type: string
      description: "如'儿童餐具2套''不锈钢餐具'"
      condition: "need_special_tableware == true"
    
    - name: check_minimum_spend
      label: 是否需要确认低消
      type: boolean
      description: "包间或特定时段可能有最低消费"
      default: false
    
    - name: is_birthday
      label: 是否是生日用餐
      type: boolean
      description: "是否需要生日庆祝服务"
      default: false
    
    - name: birthday_detail
      label: 生日定制需求
      type: string
      description: "如'需要蛋糕''需要生日布置''需要生日歌'"
      condition: "is_birthday == true"

  completion_criteria: "所有增值服务字段已确认（含默认值/跳过）"
  dialogue_hint: "'还有几个增值服务想确认一下：需要停车吗？需要特殊餐具（比如儿童餐具）吗？需要确认包间低消吗？如果是生日用餐，我们也可以安排~' 根据上下文可适当精简，如无小孩跳过餐具询问。"
  optional: true
```

#### 模块四: 全局校验与确认

汇总所有已收集信息，展示给用户确认。用户可要求修改任何字段，修改后回到对应模块重新收集。

```yaml
module:
  name: global_validation
  label: 全局校验与确认
  goal: "汇总所有信息，用户确认或修改，最终锁定"

  steps:
    - step: summary_display
      description: "以自然语言汇总三个模块的收集结果"
      format: |
        📋 **用餐需求确认**

        **基础信息**
        - 时间：[dining_time]（[meal_type]）
        - 人数：[party_size]人
        - 偏好：[dietary_preference]
        - 位置：[city] [place_name 或 district]
        - 预算：评分≥[min_rating] | 人均≤[max_cost]元

        **约束条件**
        - 孕妇：[has_pregnant ? '是' : '否']
        - 小孩：[has_children ? children_age : '无']
        - 宠物：[has_pet ? '是' : '否']
        - 私密性：[privacy_level_label]
        - 排队容忍：[max_queue_minutes]分钟

        **增值服务**
        - 停车：[need_parking ? '需要（' + license_plate + '）' : '不需要']
        - 特殊餐具：[need_special_tableware ? special_tableware_detail : '不需要']
        - 低消确认：[check_minimum_spend ? '需要' : '不需要']
        - 生日：[is_birthday ? birthday_detail : '不是']

    - step: user_confirm_or_modify
      description: "询问用户'以上信息是否正确？需要修改哪一项？'"
      actions:
        - if: "用户确认无误"
          then: "结束模块四，进入 Phrase 4"
        - if: "用户要求修改某项"
          then: "回到对应模块更新字段 → 重新汇总 → 再次确认"
    
    - step: final_lock
      description: "用户确认后，锁定所有字段，不可再修改"
      action: "将 status 设为 ready_to_dispatch"

  early_termination:
    description: "在模块一至模块三中，如果约束无法满足，立即结束会话"
    examples:
      - "用户要求宠物友好 + 包间 + 评分≥4.8 + 人均≤100 → 无匹配结果 → 告知用户并结束"
      - "用户要求孕妇友好 + 火锅 + 无排队 → 冲突无法消解 → 告知用户并结束"
      - "用户要求深夜11点用餐 + 不接受排队 → 营业时间冲突 → 告知用户并结束"
```

### 3.2 JSON Output Format

Phrase 3 结束时输出以下 JSON 结构。**必须包含 `sections` 字段**：

```json
{
  "task_id": "task_<YYYYMMDD>_<HHMMSS>",
  "task_type": "food-finding",
  "status": "ready_to_dispatch | partial | blocked | awaiting_resolver | constraint_failed",
  "timestamp": "<ISO 8601 with timezone>",
  "summary": "<one-line natural language summary>",
  "sections": [
    {
      "name": "basic_info",
      "label": "基础信息收集",
      "description": "确认用餐时间、人数、饮食偏好及位置，为 POI 搜索做准备",
      "status": "completed",
      "items": [
        {"field": "dining_time", "label": "用餐时间", "value": "2026-08-03T19:00:00+08:00", "source": "user_provided"},
        {"field": "party_size", "label": "用餐人数", "value": 4, "source": "user_provided"},
        {"field": "dietary_preference", "label": "饮食偏好/禁忌", "value": "火锅，不吃羊肉", "source": "user_provided"},
        {"field": "city", "label": "城市", "value": "北京", "source": "user_provided"},
        {"field": "place_name", "label": "具体地点", "value": "望京SOHO", "source": "user_provided"},
        {"field": "min_rating", "label": "最低评分", "value": 4.0, "source": "default"},
        {"field": "max_cost", "label": "最高人均", "value": 200, "source": "default"}
      ],
      "completion_criteria": "dining_time, party_size, dietary_preference, city 已确认；place_name 或 district 至少填一个",
      "criteria_met": true,
      "dialogue_summary": "用户确认周六晚上7点、4人、火锅不吃羊肉、北京望京SOHO附近"
    },
    {
      "name": "constraint_resolution",
      "label": "约束探测与消解",
      "description": "探测用户的特殊约束条件，检测冲突，推荐消解方案",
      "status": "completed",
      "items": [
        {"field": "has_pregnant", "label": "是否有孕妇", "value": false, "source": "user_provided"},
        {"field": "has_children", "label": "是否有小孩", "value": true, "source": "user_provided"},
        {"field": "children_age", "label": "小孩年龄", "value": "3岁", "source": "user_provided"},
        {"field": "has_pet", "label": "是否带宠物", "value": false, "source": "user_provided"},
        {"field": "need_private_room", "label": "是否需要包间", "value": true, "source": "user_provided"},
        {"field": "privacy_level", "label": "私密性要求", "value": "private_room", "source": "user_provided"},
        {"field": "max_queue_minutes", "label": "最大排队容忍时间", "value": 20, "source": "user_provided"}
      ],
      "completion_criteria": "所有约束字段已确认，冲突检测已完成",
      "criteria_met": true,
      "dialogue_summary": "用户带3岁小孩，需要包间，排队不超过20分钟。冲突检测：小孩+包间无冲突，已标注优先找有儿童座椅的包间。"
    },
    {
      "name": "value_added_services",
      "label": "增值服务关联",
      "description": "确认停车、特殊餐具、低消、生日定制等增值服务需求",
      "status": "completed",
      "items": [
        {"field": "need_parking", "label": "是否需要停车", "value": true, "source": "user_provided"},
        {"field": "license_plate", "label": "车牌号", "value": "京A12345", "source": "user_provided"},
        {"field": "need_special_tableware", "label": "是否需要特殊餐具", "value": true, "source": "user_provided"},
        {"field": "special_tableware_detail", "label": "特殊餐具说明", "value": "儿童餐具1套", "source": "user_provided"},
        {"field": "check_minimum_spend", "label": "是否需要确认低消", "value": true, "source": "user_provided"},
        {"field": "is_birthday", "label": "是否是生日用餐", "value": false, "source": "user_provided"}
      ],
      "completion_criteria": "所有增值服务字段已确认",
      "criteria_met": true,
      "dialogue_summary": "用户需要停车（京A12345）、儿童餐具1套、确认包间低消。非生日用餐。"
    },
    {
      "name": "global_validation",
      "label": "全局校验与确认",
      "description": "汇总所有信息，用户确认或修改",
      "status": "completed",
      "items": [],
      "completion_criteria": "用户确认所有信息无误",
      "criteria_met": true,
      "dialogue_summary": "用户确认所有信息无误，锁定进入 Phrase 4。"
    }
  ],
  "interaction_objects": [
    {
      "object_id": "B0FFGXXX",
      "name": "海底捞火锅（望京SOHO店）",
      "address": "北京市朝阳区望京SOHO塔1 B1",
      "phone": "010-12345678",
      "extra_info": {
        "rating": 4.5,
        "cost": 150,
        "business_area": "望京",
        "opentime_today": "11:00-23:00",
        "tag": "火锅,有包间,儿童座椅,停车位",
        "location": "116.4800,39.9900"
      }
    }
  ]
}
```

### 3.3 status values

| status | meaning |
|--------|---------|
| ready_to_dispatch | 所有模块完成，用户已确认 |
| partial | 可选模块未完成，但必选模块已完成 |
| blocked | 一个或多个必选模块无法完成 |
| awaiting_resolver | Phrase 2 工具未注册；输出有效但无对象 |
| constraint_failed | 约束无法满足，会话已终止 |

---

## Phrase 4: 发起交互 (Initiate Interaction)

调用交互服务，让 LLM 扮演用户方（如公司行政）打电话给餐厅，通过多轮对话确认预订细节。

> 此阶段消费 Phrase 1 中定义的**用途(2)--与交互对象确认**的信息：所有 Phrase 3 模块二/模块三中收集的约束条件和增值服务字段，经 system_prompt 注入 LLM 后，在对话中自然地向餐厅确认。

### 交互服务：hermes-nexus

本地 mock 服务，项目地址 `~/py_projects/hermes-nexus`，conda 环境 `hermes_nexus`（Python 3.11）。

**架构**：

```
Phrase 3 JSON (sections + interaction_objects)
         │
         ▼
  ┌─────────────────┐
  │ build_prompt.py  │  sections → system_prompt
  │ (角色: 行政人员   │  LLM 扮演用户打电话给餐厅
  │  打电话给餐厅)    │  候选餐厅信息注入 prompt
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │   chat.py        │  ChatSession 编排多轮对话
  │ (LLM ↔ Channel)  │  检测 [CONVERSATION_COMPLETE] 标记
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │  channel.py      │  TerminalChannel 通信渠道
  │ (终端/mock 模式)  │  支持真实终端输入和 mock 预设脚本
  └─────────────────┘
           │
           ▼
  返回 {task_id, messages, status}
  messages = [{role, content}, ...]  (不含 system prompt)
```

**核心组件**：

| 文件 | 作用 |
|------|------|
| `main.py` | FastAPI 服务，POST /api/v1/chat 接口 |
| `src/build_prompt.py` | 将 sections 格式化为 system_prompt，LLM 扮演行政人员打电话订座 |
| `src/chat.py` | ChatSession 编排：构建 prompt → 打开渠道 → 多轮 LLM 对话 → 检测结束标记 → 返回消息 |
| `src/channel.py` | TerminalChannel 通信渠道，支持真实终端和 mock 模式 |
| `run_demo.py` | 体验脚本，支持交互/mock/API 三种模式 |

### LLM 配置

hermes-nexus 使用 OpenAI 兼容 SDK，默认指向 DeepSeek：

```bash
# 环境变量（均为可选，无 LLM_API_KEY 时自动 fallback 到 mock LLM）
LLM_API_KEY=sk-xxx          # LLM API 密钥
LLM_BASE_URL=https://api.deepseek.com  # API 地址（默认 DeepSeek）
LLM_MODEL=deepseek-v4-flash # 模型名称

# 无 API key 时：ChatSession.use_mock_llm == True
# mock LLM 模拟行政人员打电话的对话风格，可用于测试完整流程
```

### 调用方式：HTTP API

hermes-nexus 服务由用户在独立终端启动，Agent 通过 HTTP API 调用。

**前置检查**：

```bash
# 健康检查，确认服务已启动
curl -s http://localhost:8000/api/v1/health
# -> {"status":"ok","service":"hermes-nexus","version":"0.1.0"}
```

若健康检查失败，提示用户在独立终端启动服务：

```bash
cd ~/py_projects/hermes-nexus && conda activate hermes_nexus && python main.py
```

**发起对话**：

```bash
curl -s -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d @phrase3_output.json
```

请求体即 Phrase 3 的 JSON 输出，可选追加 mock 相关字段：

```json
{
  "task_id": "task_20260802_214438",
  "sections": [...],
  "interaction_objects": [...],
  "summary": "...",
  "mock_mode": false,
  "mock_responses": null,
  "max_turns": 30
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `sections` | list | **必填**，Phrase 3 的 4 个模块输出 |
| `interaction_objects` | list | 候选餐厅，第一家作为外呼对象注入 prompt |
| `summary` | string | 任务摘要 |
| `task_id` | string | 任务编号 |
| `mock_mode` | bool | true=使用预设脚本模拟餐厅方回复（测试用），默认 false |
| `mock_responses` | list[str] | mock_mode=true 时的预设回复 |
| `max_turns` | int | 最大对话轮数，默认 30 |

**响应体**：

```json
{
  "task_id": "task_20260802_214438",
  "messages": [
    {"role": "user", "content": "您好，餐馆预定，请问您有什么需要？"},
    {"role": "assistant", "content": "喂～你好，请问是美锦酒家吗？..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "status": "completed"
}
```

- `role="assistant"`：LLM 扮演的行政人员（打电话方）
- `role="user"`：餐厅工作人员（接电话方），首条是固定开场白
- `status`：`completed` | `max_turns_reached` | `error`

**辅助接口**：

```bash
# 预览 system_prompt（不发起对话）
curl -s "http://localhost:8000/api/v1/prompt/preview?summary=测试&task_id=t1"
```

### 对话结束机制

LLM 在 system_prompt 中被指示：当所有关键信息确认完毕、预定成功后，在回复末尾加 `[CONVERSATION_COMPLETE]` 标记。ChatSession 检测到该标记后：
1. 从回复中移除标记
2. 发送最终回复到 channel
3. 关闭渠道
4. status 设为 "completed"

其他结束情况：
- 用户输入 `/exit` `/done` `/quit` → status="completed"
- 达到 max_turns → status="max_turns_reached"
- 异常 → status="error"

### Agent 在 Phrase 4 中的操作步骤

1. **健康检查**：curl /api/v1/health 确认 hermes-nexus 服务已启动；若未启动，提示用户在独立终端运行 `cd ~/py_projects/hermes-nexus && conda activate hermes_nexus && python main.py`
2. **准备请求体**：将 Phrase 3 的 JSON 输出作为请求体，取 interaction_objects[0] 作为外呼对象
3. **发起对话**：POST /api/v1/chat，等待响应
4. **收集结果**：响应 messages 包含完整对话记录，status 标识结束状态
5. **传递给 Phrase 5**：将 messages 和用户原始约束（Phrase 3 sections）一起传给 Phrase 5 做后置判断

### 运行环境

```bash
# conda 环境
conda activate hermes_nexus
# 或直接使用绝对路径
/Users/chengjian/miniforge3/envs/hermes_nexus/bin/python

# 依赖（已安装在 hermes_nexus 环境中）
# fastapi, uvicorn, pydantic, openai, pytest, pytest-asyncio
```

### 注意事项

- hermes-nexus 是 **mock 服务**：LLM 对话模拟打电话场景，不是真实外呼。真实场景需接入语音外呼或企微推送渠道
- `mock_mode` 参数控制的是 **channel**（通信渠道），不是 LLM。mock_mode=True 使用预设脚本模拟餐厅方回复，用于自动化测试；mock_mode=False 需要真实终端输入
- 无 `LLM_API_KEY` 时使用 mock LLM，对话内容是预设的行政人员风格回复，可验证完整流程
- system_prompt 中角色固定为"望京恒电公司行政人员打电话预定团建聚餐"，如需通用化需修改 `src/build_prompt.py` 中的 `SYSTEM_PROMPT_TEMPLATE`
- `build_prompt.py` 会从 `interaction_objects[0]` 提取餐厅名称和信息注入 prompt，只外呼第一家

---

## Phrase 5: 交互信息整理输出 (Interaction Info Organization & Output)

### Purpose

在 Phrase 4 交互完成后，整理交互结果，输出符合用户要求的预订确认信息。

> 此阶段消费 Phrase 1 中定义的**用途(3)——后置判断**的信息：将 Phrase 3 模块四锁定的用户需求（基准值）与 Phrase 4 餐厅实际回复进行逐项对比，标记满足/不满足的约束，输出决策建议。

### Output Structure

```yaml
phrase_5_output:
  description: "获取满足用户要求的预订结果并输出"
  
  fields:
    - name: reservation_confirmed
      label: 预订是否确认
      type: boolean
      description: "餐厅是否接受预订/确认可用"
    
    - name: restaurant_name
      label: 餐厅名称
      type: string
    
    - name: confirmed_details
      label: 已确认的详情
      type: object
      description: "包含排队情况、包间、儿童设施、停车、低消等 Phrase 4 交互中确认的所有细节"
    
    - name: unmet_constraints
      label: 未满足的约束
      type: array
      description: "餐厅无法满足的约束列表，如['宠物不可入内', '无包间']"
    
    - name: alternatives
      label: 备选方案
      type: array
      description: "如果当前餐厅不满足约束，推荐的备选方案"
    
    - name: user_decision
      label: 用户最终决定
      type: enum
      options: [accept, decline, modify_constraints, try_alternative]
      description: "用户接受/拒绝/修改约束重试/尝试备选"

  process:
    - step: collect_responses
      description: "收集 Phrase 4 中外呼/企微的所有回复"
    - step: evaluate_constraints
      description: "逐项对比餐厅回复与用户约束，标记满足/不满足"
    - step: present_result
      description: "用自然语言告知用户预订结果，突出显示不满足的约束"
    - step: handle_decision
      description: "根据用户决定：接受 → 结束；拒绝 → 结束；修改约束 → 回到 Phrase 3 模块二；尝试备选 → 用下一个 interaction_object 重新 Phrase 4"
```

### Result Summary Format

```
📞 **餐厅确认结果**

🏠 **餐厅**: [restaurant_name]
📌 **地址**: [address]

✅ **已确认**:
  - 包间可用（[低消详情]）
  - 儿童座椅已预留
  - 停车位已登记（[license_plate]）
  - 排队约 [N] 分钟

⚠️ **需要注意**:
  - [unmet_constraint_1]
  - [unmet_constraint_2]

📋 **下一步**:
  - 接受预订 → 完成
  - 修改需求 → 重新筛选
  - 尝试备选 → [alternative_name]
```

---

## Domain Knowledge

### Common Defaults

```yaml
common_defaults:
  min_rating: 4.0            # 最低评分默认值
  max_cost: 200              # 最高人均默认值
  meal_type: dinner          # 未指定时默认晚餐
  privacy_level: no_requirement  # 默认无特殊私密性要求
  max_queue_minutes: 30      # 默认排队容忍30分钟
  has_pregnant: false
  has_children: false
  has_pet: false
  need_private_room: false
  need_parking: false
  need_special_tableware: false
  check_minimum_spend: false
  is_birthday: false
```

### Dialogue Patterns

```yaml
dialogue_patterns:
  # ---- 模块一: 基础信息 ----
  - trigger: "用户同时说了菜系+地点+时间"
    response: "好的，[dietary_preference]在[place_name]附近，[dining_time]，[party_size]位。我来确认一下其他细节。"

  - trigger: "用户没说区域"
    response: "你在哪个城市？或者告诉我具体位置，我帮你找附近的。"

  - trigger: "用户说了具体地点（如'左家庄''望京'）"
    response: "好的，我以[地点]为中心，周边3km范围内帮你搜。"

  - trigger: "用户同时说了城市和地点"
    response: "了解，在[城市][地点]附近帮你搜。"

  - trigger: "用户说随便"
    response: "那我根据你的位置推荐几家评分不错的，你选一个？有什么饮食禁忌吗？"

  - trigger: "用户提到夜宵"
    response: "夜宵的话营业时间是关键，我帮你确认一下哪些店还开着。"

  # ---- 模块二: 约束探测 ----
  - trigger: "用户提到带宠物"
    response: "好的，我帮你筛选宠物友好的餐厅，并确认宠物政策。有大小限制或品种限制需要注意吗？"

  - trigger: "用户提到人数较多（>=8人）"
    response: "8人以上的话，我建议优先找有包间的餐厅。需要我帮你确认包间可用性吗？"

  - trigger: "用户提到过敏"
    response: "了解，我会让餐厅确认是否能规避[过敏原]。请问过敏严重程度如何？交叉接触也需要避免吗？"

  - trigger: "用户提到孕妇"
    response: "好的，我会优先筛选环境舒适、通风好、无烟区的餐厅，并让餐厅确认是否有适合孕妇的座位和菜品。"

  - trigger: "用户提到带小孩"
    response: "了解，我会确认餐厅是否有儿童座椅、儿童餐，以及整体环境是否适合[小孩年龄]的小孩。"

  - trigger: "约束冲突检测到"
    response: "⚠️ 注意：[冲突描述]。建议：A) [方案A] B) [方案B] C) [方案C]。你更倾向于哪个？"

  # ---- 模块三: 增值服务 ----
  - trigger: "用户提到生日"
    response: "生日快乐！🎂 需要我帮您确认餐厅是否提供生日布置、蛋糕或其他庆祝服务吗？"

  - trigger: "用户提到开车"
    response: "好的，我帮您确认餐厅的停车情况。需要登记车牌吗？"

  - trigger: "用户提到包间+低消"
    response: "包间通常有最低消费，我帮您确认具体金额。"

  # ---- 模块四: 全局校验 ----
  - trigger: "用户确认所有信息"
    response: "好的，信息已锁定。正在为您筛选餐厅并进行外呼确认..."

  - trigger: "用户要求修改"
    response: "好的，你想修改哪一项？"
```

### Pitfalls

```yaml
pitfalls:
  # 基础
  - "部分餐厅午市不营业（尤其火锅、烧烤类），需确认营业时间"
  - "部分餐厅不接受预约，只接受现场排队"
  - "深夜用餐需确认 last order 时间，不是关门时间"
  
  # 约束相关
  - "大桌（8人以上）可能需要提前预订，现场等位时间长"
  - "宠物政策可能因门店而异（同一连锁不同店政策不同），不能假设"
  - "排队时间会随时段变化，午高峰/晚高峰差异大"
  - "节假日排队时间可能翻倍，需提前预告用户"
  - "孕妇友好是一个综合概念：无烟区、通风、安静、菜品选择，不能只看单一维度"
  - "儿童友好不等于有儿童座椅，还包括环境安全、噪音水平、儿童餐选择"
  
  # 增值服务
  - "部分餐厅有最低消费，特别是包间，需在 Phrase 4 中明确确认金额"
  - "停车位数量有限，高峰期可能无位，需提醒用户备选停车方案"
  - "生日定制服务需提前预约，当天到店可能无法安排"
  - "特殊餐具（如儿童餐具）部分餐厅不主动提供，需明确要求"
  
  # 交互相关
  - "过敏确认不能只看菜单，需直接和餐厅沟通交叉污染问题"
  - "部分餐厅只接受现场取号，不支持电话排队"
  - "部分餐厅电话可能无人接听（尤其午/晚高峰时间），需考虑外呼时间"
  - "企微推送消息应简洁，避免在聊天中输出完整 JSON"
  
  # 全局
  - "用户可能在模块四修改后引入新的约束冲突，需重新做冲突检测"
  - "不要假设用户记得之前说过的所有信息，但也不要重复询问已确认的信息"
```

---

## Business Knowledge Base

```yaml
business_knowledge_base:
  description: "业务知识库，用于存储特定领域的业务规则、经验数据和最佳实践。默认为空，可根据实际使用不断积累。"
  status: empty
  rules: []
  examples: []
  notes: |
    可积累的内容类型：
    - 特定餐厅的经验数据（如某餐厅常年排队30分钟以上）
    - 特定区域的餐饮规律（如望京周五晚高峰排队严重）
    - 特定菜系的约束规律（如火锅店通常无包间或包间很少）
    - 季节性规律（如夏季露天餐厅受欢迎、冬季火锅排队更长）
    - 用户偏好积累（如用户常去某区域、偏好某菜系）
```

---

## Workflow Example

### 示例 1: 地点名周边搜索 + 约束消解

用户: "帮我在北京左家庄附近找螺蛳粉，3个人，明天中午"

1. **Phrase 1 意图分析**: Action=find, Object=螺蛳粉, Stated=北京/左家庄/3人/明天中午
2. **Phrase 3 模块一**: 
   - dining_time=明天中午, party_size=3, dietary_preference=螺蛳粉, city=北京, place_name=左家庄 ✓
   - meal_type=lunch (推断), 评分/人均用默认值确认
3. **Phrase 3 模块二**: 约束探测
   - 询问: "有孕妇、小孩、宠物或包间需求吗？排队能等多久？"
   - 用户: "都没有，排队不超过15分钟吧"
   - 无冲突，进入模块三
4. **Phrase 3 模块三**: 增值服务
   - 询问: "需要停车、特殊餐具或确认低消吗？"
   - 用户: "不用"
   - 跳过，进入模块四
5. **Phrase 3 模块四**: 汇总确认 → 用户确认
6. **Phrase 2**: 调用 `resolve_restaurants(place_name="左家庄", cuisine="螺蛳粉", area="北京")`
7. **Phrase 4**: 外呼确认 + 企微推送
8. **Phrase 5**: 整理输出预订结果

### 示例 2: 约束冲突消解

用户: "帮我在上海浦东找火锅店，4个人带一只小狗，要包间，今天晚上7点"

1. **Phrase 3 模块一**: 基础信息收集 ✓
2. **Phrase 3 模块二**: 约束探测
   - has_pet=true, need_private_room=true → **冲突检测触发！**
   - 输出: "⚠️ 宠物友好餐厅通常没有包间，这是一个冲突。建议：A) 优先宠物友好，接受大厅 B) 优先包间，宠物另做安排 C) 扩大搜索范围"
   - 用户选择 A → 更新 need_private_room=false
   - 重新检测 → 无冲突
3. **Phrase 3 模块三**: 增值服务确认
4. **Phrase 3 模块四**: 汇总确认
5. **Phrase 2-5**: 正常流转

### 示例 3: 约束无法满足，提前终止

用户: "帮我找个人均50以下、评分4.8以上、有包间、宠物友好、排队不超过5分钟的火锅店，今晚7点，北京三里屯"

1. **Phrase 3 模块一**: 基础信息收集 ✓
2. **Phrase 3 模块二**: 约束探测 → 全部已填写
   - 冲突检测: pet+包间冲突、评分+人均冲突(高评分低人均罕见)、热门区域+低排队冲突
   - 用户无法妥协 → **约束无法满足，会话终止**
3. 输出: `status: constraint_failed`，告知用户约束组合无法满足，建议放宽条件后重试

---

## Verification Checklist

- [ ] 意图已分析（Action + Object + Stated constraints）
- [ ] 模块一：用餐时间、人数、饮食偏好、城市/地点已确认
- [ ] 搜索模式已确定：place_name（nearby模式）/ area+district（区域模式）
- [ ] 模块二：所有约束字段已确认（含默认值）
- [ ] 模块二：冲突检测已完成，所有冲突已消解
- [ ] 模块三：增值服务已确认或跳过
- [ ] 模块四：全局汇总已展示，用户已确认
- [ ] 约束失败时设置了 `status: constraint_failed`
- [ ] Phrase 2: resolve_restaurants 已调用（或 AMAP_API_KEY 未配置时输出 awaiting_resolver）
- [ ] 至少一个餐厅已筛选（或用户取消）
- [ ] Phrase 4: 已通过 hermes-nexus 调用 ChatSession 发起交互（直接 Python 调用或 HTTP API）
- [ ] Phrase 5: 交互结果已整理，含满足/不满足约束标记
- [ ] Phrase 5: 用户最终决定已确认
- [ ] JSON 输出包含 `sections` 字段
- [ ] 业务知识库已检查（如有相关经验数据则应用）
