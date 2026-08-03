---
name: interactive-task-car-sales
description: "Use when buying a car or booking test drives at dealerships."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hci, car, automotive, dealership, test-drive, car-sales, domain-skill]
    related_skills: [interactive-task-food, chinese-poi-search]
---

# Interactive Task: 汽车销售交互 (Domain Skill)

## Overview

Domain-specific specialization of the interactive-task skill framework. Pre-configured for 汽车看车、试驾、询价、购车场景。

Uses a **5-phase pipeline** to handle the full lifecycle:

1. **Phrase 1: 信息输入** - define what information to collect (for filtering, confirmation, and post-judgment)
2. **Phrase 2: 获取可交互对象** - 通过高德 POI 搜索 4S 店/汽车经销商
3. **Phrase 3: 信息输出** - structured JSON output with 4-module dialogue flow
4. **Phrase 4: 发起交互** - 复用 hermes-nexus 交互服务（LLM 扮演买车方打电话给 4S 店）
5. **Phrase 5: 交互信息整理输出** - compile results and output

The dialogue with the user follows a **4-module flow** within Phrase 3:

```
[模块一: 基础信息收集] ◀─────────────┐
  │ (获取: 品牌/车型/预算/用途/位置/能源类型)      │
  ▼                                   │
[模块二: 约束探测与消解] ◀─────────────┤
  │ (获取: 试驾/限牌/贷款/置换/提车时间)            │
  │ ├─> (内部循环: 冲突检测 -> 方案推荐)
  ▼                                   │
[模块三: 领域特色要求] ◀─────────────┤
  │ (获取: 保险/上牌/装饰/延保/赠品)               │
  ▼                                   │
[模块四: 全局校验与确认] ───(用户要求修改)─┘
  │ (确认无误)
  ▼
[结束会话]
```

**During any module, if constraints cannot be satisfied, end the session immediately.**

## When to Use

Triggers:
- 用户提到买车、看车、试驾、4S店、询价
- 用户提到品牌+预算+购车意向
- 用户要确认车型库存、优惠幅度、贷款方案
- 用户有特殊约束需求（限牌、置换、贷款、加急提车等）
- 用户需要增值服务（保险、上牌、装饰、延保等）

Don't use for:
- 纯汽车资讯/评测查询（不涉及外部交互）
- 二手车交易平台线上操作（C2C/C2B 线上交易，非线下 4S 店）
- 汽车维修/保养预约（那是 car-maintenance 领域）
- 租车/网约车（那是 car-rental 领域）

## Domain Definition

```yaml
domain: car-sales
description: "找4S店并确认车型可用性（库存、试驾、优惠、贷款、置换、约束消解、增值服务等）"
version: "1.0.0"

triggers:
  - "买车"
  - "看车"
  - "试驾"
  - "4S店"
  - "询价"
  - "经销商"
  - "帮我找xx品牌4S店"
  - "想看看xx车型"
  - "帮我预约试驾"
```

---

## Phrase 1: 信息输入 (Information Input)

### Purpose

Define what information needs to be collected from the user. Information serves **three distinct purposes**:

#### (1) 用于 Phrase 2 筛选可交互对象

信息用于 POI 搜索过滤，缩小 4S 店候选范围。

> 如：品牌、城市、地点/区县、新车/二手车 -- 这些字段直接映射到 `resolve_car_dealers()` 的查询参数。

#### (2) 用于与交互对象确认（联动 Phrase 3 信息输出模板）

信息用于 Phrase 4 外呼时向 4S 店确认的内容。

> 如：库存情况、试驾可用时段、实际优惠幅度、贷款方案、置换估价、提车周期 -- 这些字段不是搜索过滤条件，而是需要 4S 店直接回复的确认项。

#### (3) 用于后置判断

信息作为 Phrase 5 中与 4S 店沟通后的判断依据--对比用户原始需求与 4S 店实际回复，判定满足/不满足。

> 如：用户要求优惠≥2万 -> 4S 店回复优惠1.5万 -> 标记为 `unmet_constraint`。用户要求试驾 -> 4S 店确认可安排 -> 标记为满足。

### Mapping: 3 Purposes -> 4 Modules

| 用途 | 对应模块 | 信息项 |
|------|----------|--------|
| **(1) 筛选可交互对象** | 模块一: 基础信息收集 | 品牌、车型偏好、预算范围、用途、城市、地点/区县、能源类型、新车/二手车 |
| **(2) 与交互对象确认** | 模块二: 约束探测与消解 | 试驾需求、限牌城市、贷款需求、置换需求、提车时间、颜色偏好 |
| | 模块三: 领域特色要求 | 保险、上牌服务、装饰需求、延保、赠品需求 |
| **(3) 后置判断** | 模块四: 全局校验与确认 | 所有已收集信息的确认，与 Phrase 5 结果对比的基准 |

### Collection Strategy

- **Batch within module**: 同一模块内的字段一次性询问
- **Progress across modules**: 模块一 -> 模块二 -> 模块三 -> 模块四，逐步推进
- **Early exit**: 任一模块中约束无法满足，立即结束会话
- **Natural language**: 使用自然语言标签，不暴露内部字段名

---

## Phrase 2: 获取可交互对象 (Get Interactive Objects)

### Object Resolver

```yaml
object_resolver:
  tool: resolve_car_dealers
  description: "基于高德地图 POI 搜索汽车4S店/经销商。支持三种搜索模式：地点名周边搜索（推荐）、坐标周边搜索、城市区域搜索。"
  module: "interactive-task-car-sales/scripts/resolve_car_dealers"
  env_required: "AMAP_API_KEY"
  dependency: "chinese-poi-search/scripts/amap_poi_tool.py (底层搜索能力)"

  input_mapping:
    brand: "{{module_1_basic_info.brand}}"
    area: "{{module_1_basic_info.city}}"
    place_name: "{{module_1_basic_info.place_name}}"
    district: "{{module_1_basic_info.district}}"
    car_condition: "{{module_1_basic_info.car_condition}}"
    min_rating: "{{module_1_basic_info.min_rating}}"
    radius: 5000   # 4S店比餐厅分散，默认搜索半径5km

  output_schema:
    - name: object_id
      type: string
      description: "高德 POI ID"
    - name: name
      type: string
      description: "4S店/经销商名称"
    - name: address
      type: string
      description: "门店地址"
    - name: phone
      type: string
      description: "联系电话"
    - name: extra_info
      type: object
      description: "评分/商圈/营业时间/标签/图片/经纬度/搜索模式/品牌/车况等"

  on_empty: relax_criteria
  on_multi: confirm_with_user
```

### Resolver Implementation

Resolver 已实现，位于本 skill 目录：

```
脚本路径: interactive-task-car-sales/scripts/resolve_car_dealers.py
函数:     resolve_car_dealers(brand, area, district, place_name, location, radius, car_condition, min_rating, page, key)
环境变量: AMAP_API_KEY (高德开放平台 Web 服务 Key)
依赖:     chinese-poi-search/scripts/amap_poi_tool.py (import search_nearby, search_places, search_around)
```

Agent 调用方式 - 三种模式：

```python
import sys, os
SCRIPT_DIR = os.path.expanduser("~/.hermes/skills/productivity/interactive-task-car-sales/scripts")
sys.path.insert(0, SCRIPT_DIR)
from resolve_car_dealers import resolve_car_dealers

# 模式 1: 地点名周边搜索（推荐）
results = resolve_car_dealers(
    brand="比亚迪",
    place_name="望京",
    area="北京",
    car_condition="新车",
)

# 模式 2: 城市区域搜索
results = resolve_car_dealers(
    brand="丰田",
    area="上海",
    district="浦东",
    car_condition="新车",
)

# 模式 3: 坐标周边搜索
results = resolve_car_dealers(
    brand="宝马",
    location="121.4752,31.2297",
    radius=8000,
)
# 返回: [{object_id, name, address, phone, extra_info:{rating, business_area, opentime_today, tag, location, search_brand, car_condition, ...}}]
# 失败返回空数组 []，不抛异常
```

> 注意：`resolve_car_dealers` 内部调用 `chinese-poi-search` 的 `search_nearby` / `search_places` / `search_around`。需要 `AMAP_API_KEY` 环境变量已配置，且 `chinese-poi-search` skill 已安装。

CLI 调用方式：

```bash
# 地点名周边搜索
python3 ~/.hermes/skills/productivity/interactive-task-car-sales/scripts/resolve_car_dealers.py \
  nearby --brand "比亚迪" --place "望京" --city 北京 --condition 新车

# 城市区域搜索
python3 ~/.hermes/skills/productivity/interactive-task-car-sales/scripts/resolve_car_dealers.py \
  area --brand "丰田" --city 上海 --district 浦东

# 坐标周边搜索
python3 ~/.hermes/skills/productivity/interactive-task-car-sales/scripts/resolve_car_dealers.py \
  around --brand "宝马" --location "121.4752,31.2297" --radius 8000
```

### Resolver Not Registered

如果 `AMAP_API_KEY` 未配置或 `chinese-poi-search` skill 不可用：

1. 告诉用户："经销商搜索工具需要高德地图 API Key。请在 https://lbs.amap.com 注册并设置环境变量 AMAP_API_KEY。"
2. 输出已解析的查询参数，方便用户手动搜索。
3. 如果用户手动提供了 4S 店信息，验证并记录，继续 Phrase 3。
4. 如果用户没有提供，以 `status: "awaiting_resolver"` 继续。

---

## Phrase 3: 信息输出 (Information Output)

按 JSON 格式输出。**输出中必须包含 `sections` 字段（List）**。每个 section 必须包含：

1. **该 section 的任务描述**（`description` 字段）
2. **已收集的信息项**（`items` 数组，每项含 field/label/value/source）
3. **完成状态**（`status` 和 `criteria_met`）

### 3.1 Module Flow

信息收集按以下 4 个模块顺序推进。每个模块内的字段一次性询问，模块间用一句话过渡。

**关键规则：在任一模块中，如果约束无法满足，立即结束会话，不继续后续模块。**

#### 模块一: 基础信息收集

收集购车的基本信息，用于 Phrase 2 的 POI 筛选。

```yaml
module:
  name: basic_info
  label: 基础信息收集
  goal: "确认品牌、车型、预算、用途、位置及能源类型，为 4S 店搜索做准备"

  required_fields:
    - name: brand
      label: 品牌
      type: string
      description: "如比亚迪、丰田、宝马、奔驰。None=不限品牌"

    - name: city
      label: 城市
      type: string
      description: "如上海、北京、广州"

    - name: budget_range
      label: 预算范围
      type: string
      description: "如'15-20万'、'30万以内'。用于推荐车型和确认贷款需求"

    - name: car_condition
      label: 新车/二手车
      type: enum
      options: [new, used]
      description: "新车或二手车"
      default: new

  optional_fields:
    - name: model_preference
      label: 车型偏好
      type: string
      description: "如'SUV''轿车''MPV''轿跑'。具体车型名也可，如'汉EV''卡罗拉'"

    - name: energy_type
      label: 能源类型
      type: enum
      options: [any, ice, bev, phev, hev, reev]
      description: "不限/燃油/纯电/插混/油电混动/增程"
      default: any

    - name: primary_use
      label: 主要用途
      type: enum
      options: [commute, family, business, long_trip, first_car]
      description: "通勤/家用/商务/长途/首辆"
      default: commute

    - name: place_name
      label: 具体地点
      type: string
      description: "如'望京''陆家嘴'。提供后自动以该地点为中心周边5km搜索。优先于 district。"

    - name: district
      label: 区县
      type: string
      description: "如浦东、朝阳。当 place_name 未提供时使用。"

    - name: min_rating
      label: 最低评分
      type: number
      description: "0-5，如4.0"
      default: 4.0

  completion_criteria: "brand, city, budget_range, car_condition 已确认；place_name 或 district 至少填一个"
  dialogue_hint: "一次性询问：'您想看什么品牌的车？预算大概多少？新车还是二手？在哪个城市、哪个区域？' 如果用户说了具体地点，优先用 place_name 模式搜索（周边5km）。"
  search_mode:
    nearby: "place_name 有值 -> Phrase 2 调用 resolve_car_dealers(place_name=..., area=city, brand=..., ...)"
    area:   "仅有 city/district -> Phrase 2 调用 resolve_car_dealers(area=city, district=..., brand=..., ...)"
```

#### 模块二: 约束探测与消解

探测可能影响购车流程的约束条件，并通过**内部循环（冲突检测 -> 方案推荐）**消解冲突。

```yaml
module:
  name: constraint_resolution
  label: 约束探测与消解
  goal: "探测用户的试驾、限牌、贷款、置换等约束条件，检测冲突，推荐消解方案"

  required_fields:
    - name: need_test_drive
      label: 是否需要试驾
      type: boolean
      description: "是否需要安排试驾"
      default: true

    - name: test_drive_time
      label: 试驾时间偏好
      type: string
      description: "如'本周末''工作日下午'。need_test_drive=true 时询问"
      condition: "need_test_drive == true"

    - name: license_plate_restricted
      label: 是否限牌城市
      type: boolean
      description: "所在城市是否限牌（如上海、北京、广州、深圳、杭州等）"
      default: false

    - name: plate_type
      label: 牌照类型
      type: enum
      options: [already_have, need_new, need_transfer]
      description: "已有牌照/需新申请/需置换转移。license_plate_restricted=true 时询问"
      condition: "license_plate_restricted == true"

    - name: need_financing
      label: 是否需要贷款
      type: boolean
      description: "是否需要汽车贷款/分期"
      default: false

    - name: down_payment_budget
      label: 首付预算
      type: string
      description: "如'5万''30%'。need_financing=true 时询问"
      condition: "need_financing == true"

    - name: has_trade_in
      label: 是否有置换
      type: boolean
      description: "是否有旧车置换"
      default: false

    - name: trade_in_info
      label: 置换车辆信息
      type: string
      description: "如'2018款卡罗拉，8万公里'。has_trade_in=true 时询问"
      condition: "has_trade_in == true"

    - name: expected_delivery
      label: 期望提车时间
      type: string
      description: "如'1个月内''不急''年底前'"
      default: "不急"

    - name: color_preference
      label: 颜色偏好
      type: string
      description: "如'白色''黑色''红色''无所谓'"
      default: "无所谓"

  completion_criteria: "所有约束字段已确认（含默认值），冲突检测已完成"
  dialogue_hint: "分批询问。先问核心约束：'需要安排试驾吗？什么时间方便？所在城市限牌吗？' 然后深入：'需要贷款吗？有旧车置换吗？' 最后确认：'期望什么时候提车？颜色有偏好吗？'"
  optional: false

  conflict_resolution_loop:
    description: "对于探测到的约束，自动检测冲突并提供消解方案"
    rules:
      - conflict: "need_test_drive=true AND test_drive_time='工作日' AND car_condition='二手车'"
        hint: "二手车经销商通常工作日客流少，试驾可安排；但部分二手车商无固定试驾车"
        resolution: "告知用户：优先找有固定试驾车的二手车商，或调整为周末试驾"

      - conflict: "license_plate_restricted=true AND plate_type='need_new' AND energy_type='ice'"
        hint: "限牌城市燃油车牌照指标稀缺，可能需摇号/竞价，周期长"
        resolution: "建议：A) 考虑新能源车（绿牌免摇号） B) 接受摇号等待 C) 竞价拍牌（告知预估费用）"

      - conflict: "license_plate_restricted=true AND energy_type='bev' AND area='非限牌城市'"
        hint: "非限牌城市买纯电无牌照优势，但仍有补贴和免购置税"
        resolution: "提醒用户：非限牌城市新能源仍有购置税减免，但牌照优势不显著"

      - conflict: "need_financing=true AND has_trade_in=true AND trade_in_info='高里程老旧车'"
        hint: "高里程老旧车置换估价低，可能不足以覆盖首付"
        resolution: "提醒用户置换估价可能偏低，建议增加首付预算或选择低首付金融方案"

      - conflict: "expected_delivery='1个月内' AND energy_type='bev' AND brand='热门新能源品牌'"
        hint: "热门新能源车型常有等车周期（1-3个月），1个月内提车可能无法满足"
        resolution: "建议：A) 接受等车周期 B) 选有现车的车型/配置 C) 找其他经销商看是否有库存"

      - conflict: "budget_range='10万以内' AND need_financing=false AND has_trade_in=false"
        hint: "10万以内全款购车选择面较窄，部分热门车型可能超出预算"
        resolution: "提醒用户预算范围，建议考虑贷款扩大选择面，或调整车型预期"

      - conflict: "car_condition='二手车' AND need_test_drive=true AND expected_delivery='1周内'"
        hint: "二手车试驾+过户流程通常需要1-2周，1周内提车可能紧张"
        resolution: "建议放宽提车时间到2周，或选择已完成整备的二手车"

    loop_logic: |
      while (存在未消解的冲突):
        1. 列出冲突
        2. 推荐消解方案（2-3个选项）
        3. 等待用户选择
        4. 更新约束字段
        5. 重新检测冲突
      -> 所有冲突消解后，进入模块三
```

#### 模块三: 领域特色要求

确认是否需要增值服务，这些服务通常不影响经销商选择但需要在 Phrase 4 交互时确认。

```yaml
module:
  name: domain_specific
  label: 领域特色要求
  goal: "确认保险、上牌、装饰、延保、赠品等增值服务需求"

  required_fields:
    - name: need_insurance
      label: 是否需要保险
      type: boolean
      description: "是否需要4S店代办保险（交强险+商业险）"
      default: true

    - name: insurance_detail
      label: 保险需求说明
      type: string
      description: "如'三者100万''全险''只要交强险'。need_insurance=true 时询问"
      condition: "need_insurance == true"

    - name: need_registration
      label: 是否需要代办上牌
      type: boolean
      description: "4S店代办上牌服务"
      default: true

    - name: need_decoration
      label: 是否需要装饰
      type: boolean
      description: "如贴膜、脚垫、行车记录仪、隐形车衣等"
      default: false

    - name: decoration_detail
      label: 装饰需求说明
      type: string
      description: "如'贴膜+脚垫+记录仪''全车隐形车衣'"
      condition: "need_decoration == true"

    - name: need_extended_warranty
      label: 是否需要延保
      type: boolean
      description: "延长保修服务"
      default: false

    - name: gift_preference
      label: 赠品偏好
      type: string
      description: "如'保养套餐''油卡''不要赠品折现'"
      default: "保养套餐"

  completion_criteria: "所有增值服务字段已确认（含默认值/跳过）"
  dialogue_hint: "'还有几个服务想确认一下：需要4S店代办保险和上牌吗？需要装饰（贴膜、脚垫等）吗？需要延保吗？赠品方面有什么偏好？'"
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
        📋 **购车需求确认**

        **基础信息**
        - 品牌：[brand]
        - 车型：[model_preference or '不限']
        - 预算：[budget_range]
        - 能源：[energy_type_label]
        - 车况：[car_condition == 'new' ? '新车' : '二手车']
        - 用途：[primary_use_label]
        - 位置：[city] [place_name 或 district]

        **约束条件**
        - 试驾：[need_test_drive ? test_drive_time : '不需要']
        - 限牌：[license_plate_restricted ? '是（' + plate_type_label + '）' : '否']
        - 贷款：[need_financing ? '需要（首付' + down_payment_budget + '）' : '全款']
        - 置换：[has_trade_in ? trade_in_info : '无']
        - 提车：[expected_delivery]
        - 颜色：[color_preference]

        **增值服务**
        - 保险：[need_insurance ? insurance_detail : '不需要']
        - 上牌：[need_registration ? '代办' : '自行']
        - 装饰：[need_decoration ? decoration_detail : '不需要']
        - 延保：[need_extended_warranty ? '需要' : '不需要']
        - 赠品：[gift_preference]

    - step: user_confirm_or_modify
      description: "询问用户'以上信息是否正确？需要修改哪一项？'"
      actions:
        - if: "用户确认无误"
          then: "结束模块四，进入 Phrase 4"
        - if: "用户要求修改某项"
          then: "回到对应模块更新字段 -> 重新汇总 -> 再次确认"

    - step: final_lock
      description: "用户确认后，锁定所有字段，不可再修改"
      action: "将 status 设为 ready_to_dispatch"

  early_termination:
    description: "在模块一至模块三中，如果约束无法满足，立即结束会话"
    examples:
      - "用户要求限牌城市燃油车+1个月内提车+无牌照指标 -> 无法满足 -> 告知用户并结束"
      - "用户要求热门新能源车+1周内提车 -> 等车周期冲突无法消解 -> 告知用户并结束"
      - "用户要求10万以内买某豪华品牌新车 -> 预算不匹配 -> 告知用户并结束"
```

### 3.2 JSON Output Format

Phrase 3 结束时输出以下 JSON 结构。**必须包含 `sections` 字段**：

```json
{
  "task_id": "task_<YYYYMMDD>_<HHMMSS>",
  "task_type": "car-sales",
  "status": "ready_to_dispatch | partial | blocked | awaiting_resolver | constraint_failed",
  "timestamp": "<ISO 8601 with timezone>",
  "summary": "<one-line natural language summary>",
  "sections": [
    {
      "name": "basic_info",
      "label": "基础信息收集",
      "description": "确认品牌、车型、预算、用途、位置及能源类型，为 4S 店搜索做准备",
      "status": "completed",
      "items": [
        {"field": "brand", "label": "品牌", "value": "比亚迪", "source": "user_provided"},
        {"field": "model_preference", "label": "车型偏好", "value": "汉EV", "source": "user_provided"},
        {"field": "budget_range", "label": "预算范围", "value": "20-25万", "source": "user_provided"},
        {"field": "car_condition", "label": "新车/二手车", "value": "new", "source": "user_provided"},
        {"field": "energy_type", "label": "能源类型", "value": "bev", "source": "user_provided"},
        {"field": "primary_use", "label": "主要用途", "value": "commute", "source": "user_provided"},
        {"field": "city", "label": "城市", "value": "北京", "source": "user_provided"},
        {"field": "place_name", "label": "具体地点", "value": "望京", "source": "user_provided"},
        {"field": "min_rating", "label": "最低评分", "value": 4.0, "source": "default"}
      ],
      "completion_criteria": "brand, city, budget_range, car_condition 已确认；place_name 或 district 至少填一个",
      "criteria_met": true,
      "dialogue_summary": "用户想看比亚迪汉EV，预算20-25万，纯电通勤，北京望京附近"
    },
    {
      "name": "constraint_resolution",
      "label": "约束探测与消解",
      "description": "探测用户的试驾、限牌、贷款、置换等约束条件，检测冲突，推荐消解方案",
      "status": "completed",
      "items": [
        {"field": "need_test_drive", "label": "是否需要试驾", "value": true, "source": "user_provided"},
        {"field": "test_drive_time", "label": "试驾时间偏好", "value": "本周末", "source": "user_provided"},
        {"field": "license_plate_restricted", "label": "是否限牌城市", "value": true, "source": "user_provided"},
        {"field": "plate_type", "label": "牌照类型", "value": "need_new", "source": "user_provided"},
        {"field": "need_financing", "label": "是否需要贷款", "value": true, "source": "user_provided"},
        {"field": "down_payment_budget", "label": "首付预算", "value": "8万", "source": "user_provided"},
        {"field": "has_trade_in", "label": "是否有置换", "value": false, "source": "user_provided"},
        {"field": "expected_delivery", "label": "期望提车时间", "value": "1个月内", "source": "user_provided"},
        {"field": "color_preference", "label": "颜色偏好", "value": "白色", "source": "user_provided"}
      ],
      "completion_criteria": "所有约束字段已确认，冲突检测已完成",
      "criteria_met": true,
      "dialogue_summary": "用户需试驾（本周末），北京限牌需新申请指标（纯电绿牌免摇号），贷款首付8万，无置换，1个月内提车，白色。冲突检测：限牌+纯电无冲突。"
    },
    {
      "name": "domain_specific",
      "label": "领域特色要求",
      "description": "确认保险、上牌、装饰、延保、赠品等增值服务需求",
      "status": "completed",
      "items": [
        {"field": "need_insurance", "label": "是否需要保险", "value": true, "source": "user_provided"},
        {"field": "insurance_detail", "label": "保险需求说明", "value": "三者100万+车损", "source": "user_provided"},
        {"field": "need_registration", "label": "是否需要代办上牌", "value": true, "source": "user_provided"},
        {"field": "need_decoration", "label": "是否需要装饰", "value": true, "source": "user_provided"},
        {"field": "decoration_detail", "label": "装饰需求说明", "value": "贴膜+脚垫+记录仪", "source": "user_provided"},
        {"field": "need_extended_warranty", "label": "是否需要延保", "value": false, "source": "user_provided"},
        {"field": "gift_preference", "label": "赠品偏好", "value": "保养套餐", "source": "default"}
      ],
      "completion_criteria": "所有增值服务字段已确认",
      "criteria_met": true,
      "dialogue_summary": "用户需要保险（三者100万+车损）、代办上牌、装饰（贴膜+脚垫+记录仪）。不需要延保。赠品偏好保养套餐。"
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
      "object_id": "B0FFHXXXX",
      "name": "比亚迪海洋网（北京望京4S店）",
      "address": "北京市朝阳区望京xx路xx号",
      "phone": "010-87654321",
      "extra_info": {
        "rating": "4.2",
        "business_area": "望京",
        "opentime_today": "09:00-18:00",
        "tag": "汽车销售,比亚迪,4S店",
        "location": "116.4800,39.9900",
        "search_brand": "比亚迪",
        "car_condition": "新车",
        "search_mode": "nearby_by_name",
        "search_place": "望京"
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

复用 hermes-nexus 交互服务。详细 API 文档参考 `interactive-task-food` skill 的 Phrase 4。

### 领域定制

| 配置项 | 值 |
|--------|-----|
| LLM 角色 | 购车客户打电话给4S店销售顾问 |
| 交互对象描述 | 4S店销售顾问 |
| 渠道 | TerminalChannel（终端/mock 模式） |

### 调用方式

```bash
# 健康检查
curl -s http://localhost:8000/api/v1/health

# 发起对话（请求体 = Phrase 3 JSON 输出）
curl -s -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d @phrase3_output.json
```

请求体即 Phrase 3 的 JSON 输出，可选追加 mock 相关字段：

```json
{
  "task_id": "task_20260803_140000",
  "sections": [...],
  "interaction_objects": [...],
  "summary": "比亚迪汉EV，预算20-25万，北京望京，需试驾+贷款+保险上牌",
  "mock_mode": false,
  "mock_responses": null,
  "max_turns": 30
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `sections` | list | **必填**，Phrase 3 的 4 个模块输出 |
| `interaction_objects` | list | 候选4S店，第一家作为外呼对象注入 prompt |
| `summary` | string | 任务摘要 |
| `task_id` | string | 任务编号 |
| `mock_mode` | bool | true=使用预设脚本模拟4S店回复（测试用），默认 false |
| `mock_responses` | list[str] | mock_mode=true 时的预设回复 |
| `max_turns` | int | 最大对话轮数，默认 30 |

**响应体**：

```json
{
  "task_id": "task_20260803_140000",
  "messages": [
    {"role": "user", "content": "您好，XX汽车4S店，请问有什么可以帮您？"},
    {"role": "assistant", "content": "您好，我想咨询一下比亚迪汉EV..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "status": "completed"
}
```

- `role="assistant"`：LLM 扮演的购车客户（打电话方）
- `role="user"`：4S店销售顾问（接电话方），首条是固定开场白
- `status`：`completed` | `max_turns_reached` | `error`

### Agent 在 Phrase 4 中的操作步骤

1. **健康检查**：curl /api/v1/health 确认 hermes-nexus 服务已启动；若未启动，提示用户在独立终端运行 `cd ~/py_projects/hermes-nexus && conda activate hermes_nexus && python main.py`
2. **准备请求体**：将 Phrase 3 的 JSON 输出作为请求体，取 interaction_objects[0] 作为外呼对象
3. **发起对话**：POST /api/v1/chat，等待响应
4. **收集结果**：响应 messages 包含完整对话记录，status 标识结束状态
5. **传递给 Phrase 5**：将 messages 和用户原始约束（Phrase 3 sections）一起传给 Phrase 5 做后置判断

### 注意事项

- hermes-nexus 的 system_prompt 模板默认针对餐厅场景（"望京恒电公司行政人员打电话预定团建聚餐"）。汽车销售场景需修改 `src/build_prompt.py` 中的 `SYSTEM_PROMPT_TEMPLATE`，将角色改为购车客户，交互对象改为4S店销售顾问，确认内容改为库存/试驾/优惠/贷款/置换/保险等
- `build_prompt.py` 会从 `interaction_objects[0]` 提取4S店名称和信息注入 prompt，只外呼第一家
- 无 `LLM_API_KEY` 时使用 mock LLM，对话内容是预设的购车客户风格回复，可验证完整流程

---

## Phrase 5: 交互信息整理输出 (Interaction Info Organization & Output)

### Purpose

交互完成后，整理结果，输出确认信息。复用后置判断框架。

> 此阶段消费 Phrase 1 中定义的**用途(3)--后置判断**的信息：将 Phrase 3 模块四锁定的用户需求（基准值）与 Phrase 4 中4S店实际回复进行逐项对比，标记满足/不满足的约束，输出决策建议。

### Output Structure

```yaml
phrase_5_output:
  description: "获取满足用户要求的购车确认结果并输出"

  fields:
    - name: deal_confirmed
      label: 购车意向是否确认
      type: boolean
      description: "4S店是否确认车型可用/优惠方案/试驾安排"

    - name: dealership_name
      label: 4S店名称
      type: string

    - name: confirmed_details
      label: 已确认的详情
      type: object
      description: "包含库存情况、试驾安排、优惠方案、贷款方案、置换估价、保险报价、提车周期等 Phrase 4 交互中确认的所有细节"

    - name: unmet_constraints
      label: 未满足的约束
      type: array
      description: "4S店无法满足的约束列表，如['无现车需等车2个月', '优惠幅度低于预期']"

    - name: alternatives
      label: 备选方案
      type: array
      description: "如果当前4S店不满足约束，推荐的备选方案"

    - name: user_decision
      label: 用户最终决定
      type: enum
      options: [accept, decline, modify_constraints, try_alternative]
      description: "用户接受/拒绝/修改约束重试/尝试备选4S店"

  process:
    - step: collect_responses
      description: "收集 Phrase 4 中外呼的所有回复"
    - step: evaluate_constraints
      description: "逐项对比4S店回复与用户约束，标记满足/不满足"
    - step: present_result
      description: "用自然语言告知用户确认结果，突出显示不满足的约束"
    - step: handle_decision
      description: "根据用户决定：接受 -> 结束；拒绝 -> 结束；修改约束 -> 回到 Phrase 3 模块二；尝试备选 -> 用下一个 interaction_object 重新 Phrase 4"
```

### Result Summary Format

```
📞 **4S店确认结果**

🏪 **4S店**: [dealership_name]
📌 **地址**: [address]

✅ **已确认**:
  - 车型有现车（[颜色] [配置]）
  - 试驾安排：[test_drive_time]
  - 优惠方案：[discount_detail]
  - 贷款方案：[financing_detail]
  - 保险报价：[insurance_quote]
  - 提车周期：[delivery_estimate]

⚠️ **需要注意**:
  - [unmet_constraint_1]
  - [unmet_constraint_2]

📋 **下一步**:
  - 接受方案 -> 预约到店
  - 修改需求 -> 重新筛选
  - 尝试备选 -> [alternative_name]
```

---

## Domain Knowledge

### Common Defaults

```yaml
common_defaults:
  car_condition: new              # 默认新车
  energy_type: any                # 默认不限能源类型
  primary_use: commute            # 默认通勤
  min_rating: 4.0                 # 最低评分默认值
  need_test_drive: true           # 默认需要试驾
  license_plate_restricted: false # 默认非限牌城市
  need_financing: false           # 默认全款
  has_trade_in: false             # 默认无置换
  expected_delivery: "不急"       # 默认不急
  color_preference: "无所谓"      # 默认无颜色偏好
  need_insurance: true            # 默认需要保险
  need_registration: true         # 默认需要代办上牌
  need_decoration: false          # 默认不需要装饰
  need_extended_warranty: false   # 默认不需要延保
  gift_preference: "保养套餐"     # 默认赠品偏好
```

### Dialogue Patterns

```yaml
dialogue_patterns:
  # ---- 模块一: 基础信息 ----
  - trigger: "用户同时说了品牌+预算+地点"
    response: "好的，[brand]在[place_name]附近，预算[budget_range]。我来确认一下其他细节。"

  - trigger: "用户没说区域"
    response: "你在哪个城市？或者告诉我具体位置，我帮你找附近的4S店。"

  - trigger: "用户说了具体地点"
    response: "好的，我以[地点]为中心，周边5km范围内帮你搜4S店。"

  - trigger: "用户说随便看看"
    response: "那我根据你的预算推荐几个品牌，你选一个？有偏好的能源类型吗？燃油还是新能源？"

  - trigger: "用户提到新能源"
    response: "新能源的话，是纯电、插混还是增程？您有充电条件吗？"

  - trigger: "用户提到二手车"
    response: "二手车的话，有偏好的品牌和年份吗？预算多少？"

  # ---- 模块二: 约束探测 ----
  - trigger: "用户提到试驾"
    response: "好的，我帮您安排试驾。什么时间方便？周末还是工作日？"

  - trigger: "用户在限牌城市"
    response: "[city]是限牌城市。您已有牌照指标，还是需要新申请？新能源可以免摇号。"

  - trigger: "用户提到贷款"
    response: "了解，我帮您确认金融方案。首付预算大概多少？"

  - trigger: "用户提到置换"
    response: "好的，请告诉我旧车的品牌、车型、年份和里程，我帮您确认置换估价。"

  - trigger: "用户提到加急提车"
    response: "加急提车的话，我帮您确认哪些4S店有现车。热门车型可能需要等车，您能接受多久的等车周期？"

  - trigger: "约束冲突检测到"
    response: "⚠️ 注意：[冲突描述]。建议：A) [方案A] B) [方案B] C) [方案C]。你更倾向于哪个？"

  # ---- 模块三: 领域特色 ----
  - trigger: "用户提到保险"
    response: "好的，我帮您确认保险方案。需要三者险多少额度？还有其他险种需求吗？"

  - trigger: "用户提到装饰"
    response: "装饰的话，贴膜、脚垫、记录仪这些常用项目需要吗？还有其他需求吗？"

  - trigger: "用户提到赠品"
    response: "赠品方面，一般可以谈保养套餐、油卡、装饰等。您更偏向哪个？"

  # ---- 模块四: 全局校验 ----
  - trigger: "用户确认所有信息"
    response: "好的，信息已锁定。正在为您筛选4S店并进行外呼确认..."

  - trigger: "用户要求修改"
    response: "好的，你想修改哪一项？"
```

### Pitfalls

```yaml
pitfalls:
  # 基础
  - "部分4S店周末不营业或只接待预约客户，需确认营业时间"
  - "同一品牌在不同区域有多家4S店，价格和服务可能有差异"
  - "新能源4S店和传统燃油车4S店可能不在一起，部分品牌分网销售（如比亚迪王朝网/海洋网）"

  # 约束相关
  - "限牌城市政策各异：北京需摇号、上海需竞拍、广州可摇号+竞拍、杭州需摇号"
  - "新能源绿牌政策因城市而异，部分城市对插混/增程也有限制"
  - "热门新能源车型等车周期可能1-3个月，需提前告知用户"
  - "试驾需预约，热门时段（周末）可能排队，部分车型试驾车有限"
  - "贷款方案因人而异，最终利率和额度需4S店审核个人资质"
  - "置换估价需现场评估，电话只能给大致区间"
  - "二手车车况差异大，需确认是否事故车、泡水车"

  # 增值服务
  - "4S店保险报价可能高于外部保险公司，但理赔方便"
  - "上牌服务费各店不一，部分限牌城市上牌流程复杂"
  - "装饰项目利润高，可谈空间大，但质量参差不齐"
  - "延保条款需仔细阅读，覆盖范围和免赔条款各品牌不同"
  - "赠品可谈，但不要影响裸车价谈判"

  # 交互相关
  - "4S店电话可能由销售顾问接听，非客服，沟通风格不同"
  - "周末电话可能无人接听（销售在接待客户），建议工作日致电"
  - "优惠幅度随时变化，月底/季底/年底冲量时优惠更大"
  - "部分4S店要求到店才能给最终报价，电话只给指导价"

  # 全局
  - "用户可能在模块四修改后引入新的约束冲突，需重新做冲突检测"
  - "购车流程长，用户可能需要多次交互才能最终确认"
  - "不要假设用户了解限牌政策，必要时解释相关规则"
  - "新能源和燃油车的购置税政策不同，需准确告知"
```

---

## Business Knowledge Base

```yaml
business_knowledge_base:
  description: "业务知识库，用于存储汽车销售领域的业务规则、经验数据和最佳实践。默认为空，可根据实际使用不断积累。"
  status: empty
  rules: []
  examples: []
  notes: |
    可积累的内容类型：
    - 特定品牌的销售政策（如某品牌统一价/不允许议价）
    - 特定区域的4S店规律（如某区域某品牌4S店集中）
    - 限牌城市政策更新（如摇号中签率、竞拍均价）
    - 新能源补贴政策变化
    - 热门车型等车周期数据
    - 用户偏好积累（如用户常看某品牌、偏好某能源类型）
```

---

## Workflow Example

### 示例 1: 地点名周边搜索 + 试驾预约

用户: "帮我在北京望京附近找比亚迪4S店，想看汉EV，预算25万，想周末试驾"

1. **Phrase 1 意图分析**: Action=看车/试驾, Object=比亚迪4S店, Stated=北京/望京/汉EV/25万/周末试驾
2. **Phrase 3 模块一**:
   - brand=比亚迪, model_preference=汉EV, budget_range=25万, car_condition=new, energy_type=bev(汉EV是纯电), city=北京, place_name=望京
3. **Phrase 3 模块二**: 约束探测
   - 询问: "北京是限牌城市，您有牌照指标吗？需要贷款吗？有旧车置换吗？"
   - 用户: "指标有的，全款，没置换，月底前提车，白色"
   - 冲突检测: 限牌+纯电+已有指标 -> 无冲突（绿牌免摇号）
4. **Phrase 3 模块三**: 增值服务
   - 询问: "需要4S店代办保险和上牌吗？需要装饰吗？"
   - 用户: "保险自己买，上牌代办，贴膜脚垫要"
5. **Phrase 3 模块四**: 汇总确认 -> 用户确认
6. **Phrase 2**: 调用 `resolve_car_dealers(brand="比亚迪", place_name="望京", area="北京")`
7. **Phrase 4**: 外呼4S店确认库存+试驾
8. **Phrase 5**: 整理输出确认结果

### 示例 2: 约束冲突消解（限牌城市燃油车）

用户: "帮我在上海浦东找丰田4S店，看卡罗拉，15万，要燃油版"

1. **Phrase 3 模块一**: 基础信息收集
2. **Phrase 3 模块二**: 约束探测
   - license_plate_restricted=true, energy_type=ice -> **冲突检测触发！**
   - 输出: "⚠️ 上海限牌，燃油车需竞拍牌照指标（近期均价约9万元）。建议：A) 考虑插混/纯电（绿牌免竞拍） B) 接受竞拍费用 C) 已有牌照指标"
   - 用户选择 A -> 更新 energy_type=phev，model_preference=卡罗拉双擎
   - 重新检测 -> 无冲突
3. **Phrase 3 模块三**: 增值服务确认
4. **Phrase 3 模块四**: 汇总确认
5. **Phrase 2-5**: 正常流转

### 示例 3: 约束无法满足，提前终止

用户: "帮我在北京找某热门新能源车，1周内提车，指定颜色"

1. **Phrase 3 模块一**: 基础信息收集
2. **Phrase 3 模块二**: 约束探测
   - expected_delivery=1周内, energy_type=bev, brand=热门新能源品牌 -> **冲突检测触发！**
   - 输出: "⚠️ 该车型当前等车周期约2-3个月，1周内提车无法满足。建议：A) 接受等车周期 B) 选有现车的配置/颜色 C) 找其他经销商看库存"
   - 用户无法接受等车 -> **约束无法满足，会话终止**
3. 输出: `status: constraint_failed`，告知用户约束组合无法满足，建议放宽条件后重试

---

## Verification Checklist

- [ ] 意图已分析（Action + Object + Stated constraints）
- [ ] 模块一：品牌、城市、预算、车况已确认；place_name 或 district 至少填一个
- [ ] 搜索模式已确定：place_name（nearby模式）/ area+district（区域模式）
- [ ] 模块二：所有约束字段已确认（含默认值）
- [ ] 模块二：冲突检测已完成，所有冲突已消解
- [ ] 模块三：增值服务已确认或跳过
- [ ] 模块四：全局汇总已展示，用户已确认
- [ ] 约束失败时设置了 `status: constraint_failed`
- [ ] Phrase 2: resolve_car_dealers 已调用（或 AMAP_API_KEY 未配置时输出 awaiting_resolver）
- [ ] 至少一个4S店已筛选（或用户取消）
- [ ] Phrase 4: 已通过 hermes-nexus 调用 ChatSession 发起交互
- [ ] Phrase 5: 交互结果已整理，含满足/不满足约束标记
- [ ] Phrase 5: 用户最终决定已确认
- [ ] JSON 输出包含 `sections` 字段
- [ ] 业务知识库已检查（如有相关经验数据则应用）
