# Domain Skill Template

目标 skill 的骨架模板。Phase G4 中加载此模板并填充变量。

> 所有 `{{VARIABLE}}` 占位符在 G4 阶段被替换为实际内容。
> `{{CONDITIONAL_BLOCK:xxx}}` / `{{END_CONDITIONAL}}` 标记的块在条件不满足时整块删除。

---

```markdown
---
name: interactive-task-{{DOMAIN_NAME}}
description: "Use when user wants to {{ACTION_DESCRIPTION}}. Domain skill for {{DOMAIN_LABEL}} with 5-phase pipeline: information input, object resolution, structured output (4-module dialogue flow), interaction initiation, and result compilation."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hci, {{DOMAIN_TAG}}, {{DOMAIN_NAME}}, domain-skill]
    related_skills: [interactive-task-food]
---

# Interactive Task: {{DOMAIN_LABEL}} (Domain Skill)

## Overview

Domain-specific specialization of the interactive-task skill framework. Pre-configured for {{DOMAIN_LABEL}}.

Uses a **5-phase pipeline** to handle the full lifecycle:

1. **Phrase 1: 信息输入** - define what information to collect (for filtering, confirmation, and post-judgment)
2. **Phrase 2: 获取可交互对象** - {{RESOLVER_DESCRIPTION}}
3. **Phrase 3: 信息输出** - structured JSON output with 4-module dialogue flow
4. **Phrase 4: 发起交互** - 复用 hermes-nexus 交互服务
5. **Phrase 5: 交互信息整理输出** - compile results and output

The dialogue with the user follows a **4-module flow** within Phrase 3:

[模块一: 基础信息收集] ◀─────────────┐
  │ (获取: {{MODULE_1_SUMMARY}})                 │
  ▼                                   │
[模块二: 约束探测与消解] ◀─────────────┤
  │ (获取: {{MODULE_2_SUMMARY}})            │
  │ ├─> (内部循环: 冲突检测 -> 方案推荐)      
  ▼                                   │
[模块三: 领域特色要求] ◀─────────────┤
  │ (获取: {{MODULE_3_SUMMARY}})                │
  ▼                                   │
[模块四: 全局校验与确认] ───(用户要求修改)─┘
  │ (确认无误)
  ▼
[结束会话]

**During any module, if constraints cannot be satisfied, end the session immediately.**

## When to Use

Triggers:
{{TRIGGERS}}

Don't use for:
- {{NON_TRIGGERS}}

## Domain Definition

```yaml
domain: {{DOMAIN_NAME}}
description: "{{DOMAIN_DESCRIPTION}}"
version: "1.0.0"

triggers:
{{TRIGGERS_YAML}}
```

---

## Phrase 1: 信息输入 (Information Input)

### Purpose

Define what information needs to be collected from the user. Information serves **three distinct purposes**:

#### (1) 用于 Phrase 2 筛选可交互对象

{{FILTERING_PURPOSE_DESC}}

#### (2) 用于与交互对象确认（联动 Phrase 3 信息输出模板）

{{CONFIRMATION_PURPOSE_DESC}}

#### (3) 用于后置判断

{{POST_JUDGMENT_PURPOSE_DESC}}

### Mapping: 3 Purposes -> 4 Modules

| 用途 | 对应模块 | 信息项 |
|------|----------|--------|
| **(1) 筛选可交互对象** | 模块一: 基础信息收集 | {{FILTERING_FIELDS}} |
| **(2) 与交互对象确认** | 模块二: 约束探测与消解 | {{CONFIRMATION_FIELDS_M2}} |
| | 模块三: 领域特色要求 | {{CONFIRMATION_FIELDS_M3}} |
| **(3) 后置判断** | 模块四: 全局校验与确认 | {{POST_JUDGMENT_FIELDS}} |

### Collection Strategy

- **Batch within module**: 同一模块内的字段一次性询问
- **Progress across modules**: 模块一 -> 模块二 -> 模块三 -> 模块四，逐步推进
- **Early exit**: 任一模块中约束无法满足，立即结束会话
- **Natural language**: 使用自然语言标签，不暴露内部字段名

---

## Phrase 2: 获取可交互对象 (Get Interactive Objects)

{{RESOLVER_SECTION}}

---

## Phrase 3: 信息输出 (Information Output)

按 JSON 格式输出。**输出中必须包含 `sections` 字段（List）**。每个 section 必须包含：

1. **该 section 的任务描述**（`description` 字段）
2. **已收集的信息项**（`items` 数组，每项含 field/label/value/source）
3. **完成状态**（`status` 和 `criteria_met`）

### 3.1 Module Flow

**关键规则：在任一模块中，如果约束无法满足，立即结束会话，不继续后续模块。**

#### 模块一: 基础信息收集

```yaml
module:
  name: basic_info
  label: 基础信息收集
  goal: "{{MODULE_1_GOAL}}"

  required_fields:
{{MODULE_1_REQUIRED_FIELDS}}

  optional_fields:
{{MODULE_1_OPTIONAL_FIELDS}}

  completion_criteria: "{{MODULE_1_CRITERIA}}"
  dialogue_hint: "{{MODULE_1_HINT}}"
```

#### 模块二: 约束探测与消解

```yaml
module:
  name: constraint_resolution
  label: 约束探测与消解
  goal: "{{MODULE_2_GOAL}}"

  required_fields:
{{MODULE_2_REQUIRED_FIELDS}}

  completion_criteria: "{{MODULE_2_CRITERIA}}"
  dialogue_hint: "{{MODULE_2_HINT}}"
  optional: false

  conflict_resolution_loop:
    description: "对于探测到的约束，自动检测冲突并提供消解方案"
    rules:
{{MODULE_2_CONFLICTS}}

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

```yaml
module:
  name: domain_specific
  label: 领域特色要求
  goal: "{{MODULE_3_GOAL}}"

  required_fields:
{{MODULE_3_REQUIRED_FIELDS}}

  completion_criteria: "{{MODULE_3_CRITERIA}}"
  dialogue_hint: "{{MODULE_3_HINT}}"
  optional: true
```

#### 模块四: 全局校验与确认

```yaml
module:
  name: global_validation
  label: 全局校验与确认
  goal: "汇总所有信息，用户确认或修改，最终锁定"

  steps:
    - step: summary_display
      description: "以自然语言汇总三个模块的收集结果"
      format: |
{{MODULE_4_SUMMARY_FORMAT}}

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
{{EARLY_TERMINATION_EXAMPLES}}
```

### 3.2 JSON Output Format

```json
{
  "task_id": "task_<YYYYMMDD>_<HHMMSS>",
  "task_type": "{{DOMAIN_NAME}}",
  "status": "ready_to_dispatch | partial | blocked | awaiting_resolver | constraint_failed",
  "timestamp": "<ISO 8601 with timezone>",
  "summary": "<one-line natural language summary>",
  "sections": [
    {
      "name": "basic_info",
      "label": "基础信息收集",
      "description": "{{MODULE_1_GOAL}}",
      "status": "completed",
      "items": [
        {{MODULE_1_JSON_EXAMPLE}}
      ],
      "completion_criteria": "{{MODULE_1_CRITERIA}}",
      "criteria_met": true,
      "dialogue_summary": "<对话摘要>"
    },
    {
      "name": "constraint_resolution",
      "label": "约束探测与消解",
      "description": "{{MODULE_2_GOAL}}",
      "status": "completed",
      "items": [
        {{MODULE_2_JSON_EXAMPLE}}
      ],
      "completion_criteria": "{{MODULE_2_CRITERIA}}",
      "criteria_met": true,
      "dialogue_summary": "<对话摘要>"
    },
    {
      "name": "domain_specific",
      "label": "领域特色要求",
      "description": "{{MODULE_3_GOAL}}",
      "status": "completed",
      "items": [
        {{MODULE_3_JSON_EXAMPLE}}
      ],
      "completion_criteria": "{{MODULE_3_CRITERIA}}",
      "criteria_met": true,
      "dialogue_summary": "<对话摘要>"
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
      "object_id": "<id>",
      "name": "<name>",
      "address": "<address>",
      "phone": "<phone>",
      "extra_info": { ... }
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
| LLM 角色 | {{PHRASE_4_ROLE}} |
| 交互对象描述 | {{PHRASE_4_OBJECT_DESC}} |
| 渠道 | {{PHRASE_4_CHANNEL}} |

### 调用方式

```bash
# 健康检查
curl -s http://localhost:8000/api/v1/health

# 发起对话（请求体 = Phrase 3 JSON 输出）
curl -s -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d @phrase3_output.json
```

### Agent 操作步骤

1. 健康检查 -> 若未启动，提示用户运行 `cd ~/py_projects/hermes-nexus && conda activate hermes_nexus && python main.py`
2. 准备请求体（Phrase 3 JSON，取 interaction_objects[0]）
3. POST /api/v1/chat
4. 收集 messages 和 status
5. 传递给 Phrase 5

> 注意：hermes-nexus 的 system_prompt 模板默认针对餐厅场景。如需适配本领域，需修改 `src/build_prompt.py` 中的 `SYSTEM_PROMPT_TEMPLATE`，或在请求中通过 summary 字段传递角色信息。

---

## Phrase 5: 交互信息整理输出 (Interaction Info Organization & Output)

### Purpose

交互完成后，整理结果，输出确认信息。复用后置判断框架。

### Output Structure

```yaml
phrase_5_output:
  fields:
    - name: interaction_confirmed
      label: {{PHRASE_5_CONFIRMED_LABEL}}
      type: boolean

    - name: object_name
      label: {{PHRASE_5_OBJECT_LABEL}}
      type: string

    - name: confirmed_details
      label: 已确认的详情
      type: object
      description: "Phrase 4 交互中确认的所有细节"

    - name: unmet_constraints
      label: 未满足的约束
      type: array

    - name: alternatives
      label: 备选方案
      type: array

    - name: user_decision
      label: 用户最终决定
      type: enum
      options: [accept, decline, modify_constraints, try_alternative]

  process:
    - step: collect_responses
    - step: evaluate_constraints
    - step: present_result
    - step: handle_decision
```

### Result Summary Format

{{PHRASE_5_SUMMARY_FORMAT}}

---

## Domain Knowledge

### Common Defaults

```yaml
common_defaults:
{{DEFAULTS_YAML}}
```

### Dialogue Patterns

```yaml
dialogue_patterns:
{{DIALOGUE_PATTERNS_YAML}}
```

### Pitfalls

```yaml
pitfalls:
{{PITFALLS_YAML}}
```

---

## Business Knowledge Base

```yaml
business_knowledge_base:
  description: "业务知识库。默认为空，可根据实际使用不断积累。"
  status: empty
  rules: []
  examples: []
  notes: |
    可积累的内容类型：
    - 特定对象的经验数据
    - 特定区域的规律
    - 特定类型的约束规律
    - 季节性规律
    - 用户偏好积累
```

---

## Verification Checklist

- [ ] 意图已分析（Action + Object + Stated constraints）
- [ ] 模块一：所有 required_fields 已确认
- [ ] 模块二：所有约束字段已确认（含默认值）
- [ ] 模块二：冲突检测已完成，所有冲突已消解
- [ ] 模块三：领域特色字段已确认或跳过
- [ ] 模块四：全局汇总已展示，用户已确认
- [ ] 约束失败时设置了 status: constraint_failed
- [ ] Phrase 2: resolver 已调用（或工具未配置时输出 awaiting_resolver）
- [ ] 至少一个交互对象已筛选（或用户取消）
- [ ] Phrase 4: 已通过 hermes-nexus 调用
- [ ] Phrase 5: 交互结果已整理，含满足/不满足约束标记
- [ ] JSON 输出包含 sections 字段
- [ ] 业务知识库已检查
```

---

## Template Variable Reference

| 变量 | 类型 | 来源 | 必填 | 说明 |
|------|------|------|------|------|
| `{{DOMAIN_NAME}}` | string | G1 | Y | kebab-case，如 hospital-appointment |
| `{{DOMAIN_LABEL}}` | string | G1 | Y | 中文标签，如 医院挂号 |
| `{{DOMAIN_DESCRIPTION}}` | string | G1 | Y | 一句话描述 |
| `{{DOMAIN_TAG}}` | string | G1 | Y | 标签，如 hospital |
| `{{ACTION_DESCRIPTION}}` | string | G1 | Y | description 中的动作描述 |
| `{{TRIGGERS}}` | list | G1 | Y | 触发词，markdown 列表格式 |
| `{{TRIGGERS_YAML}}` | list | G1 | Y | 触发词，YAML 格式 |
| `{{NON_TRIGGERS}}` | list | G1 | Y | 反触发词 |
| `{{FILTERING_PURPOSE_DESC}}` | text | G1 | Y | 用途1说明 |
| `{{CONFIRMATION_PURPOSE_DESC}}` | text | G1 | Y | 用途2说明 |
| `{{POST_JUDGMENT_PURPOSE_DESC}}` | text | G1 | Y | 用途3说明 |
| `{{FILTERING_FIELDS}}` | list | G1 | Y | 用途1字段名列表 |
| `{{CONFIRMATION_FIELDS_M2}}` | list | G1 | Y | 用途2模块二字段列表 |
| `{{CONFIRMATION_FIELDS_M3}}` | list | G1 | Y | 用途2模块三字段列表 |
| `{{POST_JUDGMENT_FIELDS}}` | list | G1 | Y | 用途3字段列表 |
| `{{RESOLVER_DESCRIPTION}}` | text | G2 | Y | resolver 策略描述 |
| `{{RESOLVER_SECTION}}` | block | G2 | Y | 完整的 Phrase 2 章节 |
| `{{MODULE_1_*}}` | various | G3 | Y | 模块一所有变量 |
| `{{MODULE_2_*}}` | various | G3 | Y | 模块二所有变量 |
| `{{MODULE_3_*}}` | various | G3 | Y | 模块三所有变量 |
| `{{MODULE_4_*}}` | various | G3 | Y | 模块四所有变量 |
| `{{EARLY_TERMINATION_EXAMPLES}}` | list | G3 | Y | 早终止示例 |
| `{{PHRASE_4_ROLE}}` | string | G3 | Y | LLM 扮演的角色 |
| `{{PHRASE_4_OBJECT_DESC}}` | string | G3 | Y | 交互对象描述 |
| `{{PHRASE_4_CHANNEL}}` | string | G2 | Y | 交互渠道 |
| `{{PHRASE_5_*}}` | various | G3 | Y | Phrase 5 相关变量 |
| `{{DEFAULTS_YAML}}` | yaml | G3 | Y | 默认值 |
| `{{DIALOGUE_PATTERNS_YAML}}` | yaml | G3 | Y | 对话模式 |
| `{{PITFALLS_YAML}}` | yaml | G1+G3 | Y | 领域 pitfalls |
