---
name: interactive-task-skill-generator
description: "Use when user wants to generate a domain-specific interactive task skill (e.g. appointment booking, service scheduling, ticket purchasing). Runs a 5-phase heuristic pipeline to analyze a domain, elicit modules, and produce a ready-to-use SKILL.md with 4-module dialogue flow."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [skill-generator, hci, pipeline, domain-skill, heuristic]
    related_skills: [interactive-task-food, chinese-poi-search]
---

# Interactive Task Skill Generator

## Overview

A meta-skill that generates domain-specific interactive task skills. Given a domain description (e.g. "医院挂号", "家政预约", "车辆维修预约"), this skill walks through a heuristic 5-phase pipeline to produce a complete, ready-to-use SKILL.md file following the same architecture as `interactive-task-food`.

The generator itself uses a 5-phase pipeline, mirroring the target skill's structure:

```
[Phase G1: 领域分析] ─── 启发式分析目标领域，提取领域特征
        │
        ▼
[Phase G2: 对象解析策略] ─── 确定可交互对象的获取方式（已有skill/API/用户提供）
        │
        ▼
[Phase G3: 模块设计] ─── 启发式生成4个对话模块的字段和冲突规则
        │
        ▼
[Phase G4: Skill 生成] ─── 填充模板，生成完整 SKILL.md + 辅助文件
        │
        ▼
[Phase G5: 校验与交付] ─── 结构校验、一致性检查、交付
```

## When to Use

Triggers:
- 用户要求"生成一个XX领域的交互skill"
- 用户要求"为XX场景创建预订/预约/排队/挂号 skill"
- 用户要求"照着 interactive-task-food 的模式，做一个XX领域的 skill"
- 用户描述一个需要信息收集 + 对象筛选 + 外呼确认的交互流程

Don't use for:
- 修改已有 domain skill（直接 patch 即可）
- 纯信息查询类 skill（不涉及外呼/确认交互）
- 不涉及 4-module 对话流的简单 task skill

## Prerequisites

- 已安装 `interactive-task-food` skill（作为参考实现）
- 已安装 `chinese-poi-search` skill（若目标领域需要 POI 搜索）
- 了解 Hermes skill 编写规范（参考 `hermes-agent-skill-authoring`）

---

## Phase G1: 领域分析 (Domain Analysis)

### Goal

启发式分析目标领域，提取领域特征，为后续模块设计提供依据。

### Steps

1. **确认领域边界**

   向用户确认：
   - 领域名称（如"医院挂号"、"家政预约"、"车辆维修"）
   - 核心交互动作（如"挂号"、"预约"、"下单"）
   - 交互对象类型（如"医院科室"、"家政阿姨"、"汽修店"）

   输出 `domain_definition`：
   ```yaml
   domain: <domain-name>
   action: <core-action>
   object_type: <interactive-object-type>
   description: "<one-line description>"
   ```

2. **启发式领域特征提取**

   按 6 个维度分析领域。每个维度给出启发式问题，根据用户回答填充。

   | 维度 | 启发式问题 | 输出 |
   |------|-----------|------|
   | **时间敏感性** | 该领域的时间约束有多强？是否需要精确到分钟？是否有时段限制？ | time_sensitivity: high/medium/low + 约束描述 |
   | **对象可变性** | 交互对象（如餐厅/医院）的属性是否固定？评分/价格/排队等是否实时变化？ | object_volatility: high/medium/low + 说明 |
   | **约束复杂度** | 该领域常见的约束条件有哪些？是否存在已知冲突模式？ | constraint_complexity: high/medium/low + 典型约束列表 |
   | **增值服务** | 该领域有哪些常见的增值服务/附加选项？ | value_added_services: [list] |
   | **信息不对称** | 用户和交互对象之间哪些信息需要通过交互才能确认（不能提前知道）？ | asymmetry_fields: [list] |
   | **失败兜底** | 交互失败时的常见备选方案是什么？ | fallback_patterns: [list] |

   详见 `references/domain-heuristics.md`。

3. **信息用途分类**

   将提取到的信息项按 3 种用途分类（与目标 skill 的 Phase 1 对应）：

   | 用途 | 说明 | 示例 |
   |------|------|------|
   | **(1) 筛选可交互对象** | 用于 Phase 2 搜索过滤 | 位置、类型、评分、价格 |
   | **(2) 与交互对象确认** | 需要通过外呼/推送向对象确认 | 排队情况、政策确认、特殊需求 |
   | **(3) 后置判断** | 交互后对比用户需求与实际回复 | 所有约束项的满足/不满足标记 |

   输出 `info_purposes`：
   ```yaml
   info_purposes:
     filtering: [field1, field2, ...]       # 用途1
     confirmation: [field3, field4, ...]     # 用途2
     post_judgment: [field5, field6, ...]    # 用途3
   ```

4. **Completion Criteria**

   - [ ] domain_definition 已确认（domain, action, object_type, description）
   - [ ] 6 个领域特征维度已分析
   - [ ] 信息项已按 3 种用途分类
   - [ ] 用户已确认分析结果

---

## Phase G2: 对象解析策略 (Object Resolution Strategy)

### Goal

确定目标 skill 的 Phase 2 如何获取可交互对象。

### Steps

1. **检查已有 skill/API**

   按优先级检查：
   - (a) `chinese-poi-search` skill 是否适用？（餐饮、零售、服务等有实体门店的领域）
   - (b) 是否有其他已安装的 skill 可以提供对象搜索？
   - (c) 是否有公开 API 可以封装为 tool？
   - (d) 用户是否提供对象列表？

2. **确定 resolver 策略**

   ```yaml
   object_resolver:
     strategy: <poi_search | api | user_provided | hybrid>
     tool: <tool_name or null>
     skill_ref: <skill_name or null>
     api_endpoint: <url or null>
     input_mapping: {...}   # module_1 fields -> resolver params
     output_schema: [...]
     on_empty: <relax_criteria | ask_user | terminate>
     on_multi: <confirm_with_user | auto_select | rank_and_list>
   ```

3. **若需要新建 tool**

   如果目标领域需要新的搜索 API（非 POI），在此阶段规划 tool 脚本：
   - 脚本路径：`<domain-skill-name>/scripts/<tool_name>.py`
   - 函数签名：`resolve_<objects>(**params) -> list[dict]`
   - 输出 schema 与 POI resolver 保持一致结构

   > 注意：tool 脚本的实现不在本 pipeline 范围内。若需要，在生成的 skill 中标注 `status: awaiting_implementation`。

4. **Completion Criteria**

   - [ ] resolver 策略已确定
   - [ ] input_mapping 已映射（module_1 字段 -> resolver 参数）
   - [ ] output_schema 已定义
   - [ ] on_empty / on_multi 策略已设定

---

## Phase G3: 模块设计 (Module Design)

### Goal

启发式生成目标 skill 的 4 个对话模块。这是本 pipeline 的核心。

### Design Principles

1. **模块一（基础信息）承载用途(1)的字段** -- 用于筛选
2. **模块二（约束探测）承载用途(2)的字段** -- 需要确认的约束
3. **模块三（领域特色）承载领域特有的增值服务/特殊需求**
4. **模块四（全局校验）汇总确认** -- 承载用途(3)的基准

### Steps

1. **模块一: 基础信息收集**

   启发式问题：
   - 该领域的"时间"信息是什么？（用餐时间 -> 挂号时段 / 预约日期 / 维修时间）
   - 该领域的"人数/规模"信息是什么？（用餐人数 -> 就诊人数 / 服务面积 / 车辆数）
   - 该领域的"偏好/类型"信息是什么？（菜系 -> 科室 / 服务类型 / 维修类型）
   - 该领域的"位置"信息是什么？（城市+地点 -> 城市+医院 / 城市+门店）
   - 该领域的"质量/价格"筛选条件是什么？（评分+人均 -> 医院等级 / 服务评分 / 报价范围）

   为每个字段定义：
   ```yaml
   - name: <field_name>
     label: <中文标签>
     type: <datetime|integer|string|enum|number|boolean>
     description: "<说明>"
     required: true|false
     default: <default_value>
     condition: "<optional, 如 has_children == true>"
   ```

2. **模块二: 约束探测与消解**

   启发式问题：
   - 该领域有哪些**特殊人群约束**？（孕妇/小孩/宠物 -> 老人/残疾人/急症/过敏）
   - 该领域有哪些**环境/设施约束**？（包间/私密性 -> 无障碍/电梯/停车位）
   - 该领域有哪些**时效约束**？（排队容忍 -> 候诊容忍/上门时间窗口/取车时间）
   - 该领域有哪些**已知冲突模式**？

   冲突检测规则格式：
   ```yaml
   conflict_resolution_loop:
     rules:
       - conflict: "<condition A> AND <condition B>"
         hint: "<为什么冲突>"
         resolution: "<消解方案选项>"
   ```

   > 启发式冲突发现方法：将模块二的所有 boolean/enum 字段做两两组合，问自己"这两个同时为 true/某值时是否矛盾？"。

3. **模块三: 领域特色要求**

   启发式问题：
   - 该领域有哪些**配套服务**？（停车/餐具/低消 -> 代驾/陪诊/保修期）
   - 该领域有哪些**定制化需求**？（生日布置 -> 上门服务/特殊护理/加急处理）
   - 该领域有哪些**确认类需求**？（低消确认 -> 报销凭证/医保类型/保修条款）

   > 区分模块二和模块三的原则：模块二的字段影响对象选择（筛选层面），模块三的字段不影响选择但需要在交互时确认（服务层面）。

4. **模块四: 全局校验与确认**

   汇总格式模板：
   ```yaml
   summary_display:
     sections: [basic_info, constraint_resolution, domain_specific]
     format: "自然语言汇总，按模块分组展示"
   ```

   早终止条件：
   - 从 Phase G1 的 `fallback_patterns` 提取
   - 约束无法消解 -> `status: constraint_failed`

5. **信息用途映射表**

   生成 3-purposes -> 4-modules 的映射表（与 interactive-task-food 的格式一致）。

6. **Completion Criteria**

   - [ ] 4 个模块的 required_fields / optional_fields 已定义
   - [ ] 每个字段有 name/label/type/description
   - [ ] 模块二的冲突检测规则已生成（至少覆盖主要冲突）
   - [ ] 模块三的领域特色字段已提取
   - [ ] 信息用途映射表已完成
   - [ ] 早终止条件已定义
   - [ ] dialogue_hint 已为每个模块编写

---

## Phase G4: Skill 生成 (Skill Generation)

### Goal

将 G1-G3 的设计成果填充到模板中，生成完整的 SKILL.md。

### Steps

1. **加载模板**

   读取 `templates/domain-skill-template.md`。

2. **填充变量**

   以下变量需要从 G1-G3 的输出中填充：

   | 变量 | 来源 | 说明 |
   |------|------|------|
   | `{{DOMAIN_NAME}}` | G1 domain_definition | 如 hospital-appointment |
   | `{{DOMAIN_LABEL}}` | G1 domain_definition | 如 医院挂号 |
   | `{{DOMAIN_DESCRIPTION}}` | G1 domain_definition | 一句话描述 |
   | `{{TRIGGERS}}` | G1 启发式提取 | 触发词列表 |
   | `{{MODULE_1_FIELDS}}` | G3 step 1 | YAML 字段定义 |
   | `{{MODULE_2_FIELDS}}` | G3 step 2 | YAML 字段定义 |
   | `{{MODULE_2_CONFLICTS}}` | G3 step 2 | 冲突规则 |
   | `{{MODULE_3_FIELDS}}` | G3 step 3 | YAML 字段定义 |
   | `{{MODULE_4_FORMAT}}` | G3 step 4 | 汇总格式 |
   | `{{RESOLVER_CONFIG}}` | G2 | resolver YAML |
   | `{{INFO_PURPOSES_MAP}}` | G1 step 3 | 用途映射表 |
   | `{{DIALOGUE_PATTERNS}}` | G3 | 对话模式 |
   | `{{PITFALLS}}` | G1+G3 | 领域 pitfalls |
   | `{{DEFAULTS}}` | G3 | 默认值 |
   | `{{EARLY_TERMINATION}}` | G3 step 4 | 早终止条件 |
   | `{{BUSINESS_KB}}` | G1 | 业务知识库初始状态 |

3. **生成文件**

   将填充后的内容写入：
   ```
   ~/.hermes/skills/productivity/interactive-task-{{DOMAIN_NAME}}/SKILL.md
   ```

   > 使用 `skill_manage(action='create')` 创建。

4. **生成辅助文件（按需）**

   | 文件 | 条件 | 路径 |
   |------|------|------|
   | resolver 脚本 | G2 strategy=api 且需要新建 | `scripts/<tool_name>.py` |
   | 领域知识参考 | 领域复杂度高 | `references/domain-knowledge.md` |
   | 交互示例 | 用户要求 | `references/workflow-examples.md` |

5. **Completion Criteria**

   - [ ] SKILL.md 已生成，frontmatter 合法
   - [ ] 所有 `{{变量}}` 已填充（无残留占位符）
   - [ ] 4 个模块的 YAML 结构完整
   - [ ] JSON 输出格式包含 sections 字段
   - [ ] Phase 4 和 Phase 5 已按"复用"策略处理（引用 hermes-nexus）
   - [ ] 文件路径正确

---

## Phase G5: 校验与交付 (Validation & Delivery)

### Goal

校验生成的 skill 的结构完整性和一致性。

### Steps

1. **Frontmatter 校验**

   ```python
   import yaml, re, pathlib
   content = pathlib.Path(skill_path).read_text()
   assert content.startswith("---"), "Frontmatter must start with ---"
   m = re.search(r'\n---\s*\n', content[3:])
   fm = yaml.safe_load(content[3:m.start()+3])
   assert "name" in fm, "Missing name"
   assert "description" in fm, "Missing description"
   assert len(fm["description"]) <= 1024, "Description too long"
   assert len(content) <= 100_000, "Skill too large"
   ```

2. **结构一致性检查**

   - [ ] 5 个 Phase 都存在（Phase 1-5）
   - [ ] 4 个模块都存在（模块一-四）
   - [ ] 每个 section 有 name/label/description/status/items/criteria_met
   - [ ] 模块二的冲突规则引用的字段在模块二 fields 中定义
   - [ ] resolver input_mapping 引用的字段在模块一中定义
   - [ ] Phase 5 的 unmet_constraints 覆盖模块二的所有约束字段
   - [ ] dialogue_hint 为每个模块都编写
   - [ ] pitfalls 至少 5 条
   - [ ] 默认值与字段定义一致

3. **占位符检查**

   ```bash
   grep -n '{{' <skill_path>  # 应返回空
   ```

4. **交付**

   向用户展示：
   - 生成的 skill 路径
   - skill 名称和 description
   - 4 个模块的字段概览表
   - 冲突规则数量
   - 建议的后续步骤（如"实现 resolver 脚本"、"测试触发词"）

5. **Completion Criteria**

   - [ ] frontmatter 校验通过
   - [ ] 结构一致性检查全部通过
   - [ ] 无残留占位符
   - [ ] skill 已通过 skill_manage 注册
   - [ ] 用户已确认交付

---

## Pipeline Flow Summary

```
用户描述领域
    │
    ▼
G1: 领域分析 ──┬── domain_definition
               ├── 6 维特征
               └── info_purposes (3种用途)
    │
    ▼
G2: 对象解析 ──┬── resolver 策略
               ├── input_mapping
               └── output_schema
    │
    ▼
G3: 模块设计 ──┬── 模块一: 基础信息 (用途1字段)
               ├── 模块二: 约束探测 (用途2字段 + 冲突规则)
               ├── 模块三: 领域特色 (增值服务)
               ├── 模块四: 全局校验 (用途3基准)
               └── 信息用途映射表
    │
    ▼
G4: Skill 生成 ─┬── 加载模板
                ├── 填充变量
                ├── 写入 SKILL.md
                └── 生成辅助文件(按需)
    │
    ▼
G5: 校验交付 ──┬── frontmatter 校验
               ├── 结构一致性
               ├── 占位符检查
               └── 交付确认
```

---

## Reference Implementation

`interactive-task-food` skill 是本生成器的参考实现。生成新 skill 时，以下对照关系适用：

| 本 pipeline 产出 | interactive-task-food 中的对应 |
|------------------|-------------------------------|
| G1 domain_definition | domain: food-finding |
| G1 info_purposes(1) | 模块一: dining_time, party_size, dietary_preference, city, ... |
| G1 info_purposes(2) | 模块二: has_pregnant, has_pet, need_private_room, ... |
| G1 info_purposes(2) | 模块三: need_parking, license_plate, is_birthday, ... |
| G1 info_purposes(3) | 模块四 + Phase 5: unmet_constraints |
| G2 resolver | resolve_restaurants (chinese-poi-search) |
| G3 模块二冲突规则 | has_pet + need_private_room 冲突等 |
| G4 模板填充 | SKILL.md 完整内容 |

---

## Reuse Strategy (Phase 4 & 5)

生成的目标 skill 的 Phase 4（发起交互）和 Phase 5（结果整理）**复用现有基础设施**，不需要重新设计：

### Phase 4: 复用 hermes-nexus

- 服务地址、API 格式、调用方式完全复用
- 仅需定制 system_prompt 中的**角色**和**交互对象描述**
- 在生成的 skill 中引用 hermes-nexus 的 API，标注角色差异

> 如目标领域需要不同的交互渠道（非语音外呼），在生成的 skill 中标注 `channel: <type>`，但默认复用 hermes-nexus 的 TerminalChannel。

### Phase 5: 复用后置判断框架

- 结果输出结构复用（reservation_confirmed, confirmed_details, unmet_constraints, alternatives, user_decision）
- 仅需替换领域特定的字段名（如 "restaurant_name" -> "hospital_name"）
- 在生成的 skill 中标注领域字段映射

---

## Common Pitfalls

1. **模块二和模块三混淆** -- 模块二的字段影响对象筛选，模块三的字段不影响选择但需要确认。如果拿不准，问"这个字段是否影响搜索条件？"，是 -> 模块二，否 -> 模块三。

2. **冲突规则遗漏** -- 启发式冲突发现不是穷举。生成后建议用户补充领域经验中的已知冲突。

3. **resolver 过度设计** -- 如果 chinese-poi-search 已能覆盖（大多数实体门店场景），不要新建 tool。POI 搜索支持关键词自定义，很多领域可以直接用。

4. **字段过多** -- 模块一控制在 6-8 个 required_fields，模块二 5-7 个，模块三 4-6 个。超出时考虑将部分降级为 optional 或合并。

5. **Phase 4/5 重复造轮子** -- 这两个 phase 的基础设施（hermes-nexus、后置判断框架）是通用的，生成时只需引用和映射字段，不要重新设计。

6. **description 不够 trigger-focused** -- 生成的 skill 的 description 必须以"Use when..."开头，前 57 字符内包含核心触发词。

7. **业务知识库留空但没标注** -- 初始生成时 business_knowledge_base 为空是正常的，但必须标注 `status: empty` 和可积累的内容类型。

---

## Verification Checklist

- [ ] G1: domain_definition 已确认（domain, action, object_type, description）
- [ ] G1: 6 个领域特征维度已分析
- [ ] G1: 信息项已按 3 种用途分类
- [ ] G2: resolver 策略已确定（poi_search / api / user_provided / hybrid）
- [ ] G2: input_mapping 和 output_schema 已定义
- [ ] G3: 模块一 required_fields 已生成（6-8 个）
- [ ] G3: 模块二 required_fields 已生成（5-7 个）
- [ ] G3: 模块二冲突检测规则已生成（至少覆盖主要冲突）
- [ ] G3: 模块三领域特色字段已生成（4-6 个）
- [ ] G3: 模块四汇总格式已定义
- [ ] G3: 信息用途映射表已完成
- [ ] G3: 早终止条件已定义
- [ ] G4: SKILL.md 已生成，无残留占位符
- [ ] G4: frontmatter 合法（name, description, version, ...）
- [ ] G4: 4 个模块的 YAML 结构完整
- [ ] G4: JSON 输出格式包含 sections 字段
- [ ] G4: Phase 4 引用 hermes-nexus
- [ ] G4: Phase 5 复用后置判断框架
- [ ] G5: frontmatter 校验通过
- [ ] G5: 结构一致性检查通过
- [ ] G5: skill 已注册（skill_manage action=create）
- [ ] G5: 用户已确认交付
