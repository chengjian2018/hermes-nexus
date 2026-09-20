---
name: interactive-task-skill-generator
description: Use when user asks to build a domain chat-agent ("帮我做个xx领域的代聊机器人"/"为xx场景生成话术模版和交互skill"). Researches the domain, designs a server-side dialogue template (FSM/Agent pattern JSON), and generates a thin domain sub-skill that lazily registers the template and triggers/polls autonomous negotiation tasks on hermes-nexus. Not for editing existing sub-skills or pure Q&A domains without counterpart dialogue.
version: 2.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [skill-generator, dialogue-template, pattern, domain-skill, counterpart]
    related_skills: [chinese-poi-search, interactive-task-food]
---

# Interactive Task Skill Generator

## Overview

给定一个领域（如餐厅订位、医院挂号、闲鱼砍价），产出**两件交付物**：

1. **话术模版**：声明式 pattern JSON（FSM/Agent/混排），注册进 hermes-nexus
   后由**服务端**执行对话——与"对端"（可 mock 的第三方：店家、客服、买家）
   多轮交互直到终态。
2. **领域交互子 skill**：薄客户端。与**用户**对话收集本次任务需求，然后
   懒注册模版 → 触发自主任务会话 → 轮询结果 → 后置判断汇报。

架构分工（与 v1 的本质区别——话术全部下沉服务端，skill 不再内置对话逻辑）：

```
用户："帮我做个 xxx 领域的代聊机器人"
  └─ 元 skill（本 skill）：G1 调研 → G2 对端分析 → G3 话术模版设计
                           → G4 生成子 skill → G5 validate 硬门槛交付

用户："帮我代聊"（之后任何时刻）
  └─ 子 skill：与用户多轮收集 task_info
               → trigger_task.py ensure（模版 hash 对齐懒注册）
               → trigger_task.py run（POST /tasks 触发自主会话）
               → 轮询 GET /tasks/{id}（期间可查 /sessions/{id}/messages）
               → 后置判断（成败/条件满足）→ 向用户汇报
```

hermes-nexus 服务默认跑在 `http://127.0.0.1:8000`；`trigger_task.py` 会探活
并在必要时自动拉起（`GET /api/v1/health` 不通则后台启动并等待就绪）。

## When to Use

**适用**：
- "帮我做一个 xx 领域的代聊机器人 / 话术模版 / 交互 skill"
- "为 xx 场景生成会跟对方谈的 skill"（订位、砍价、预约、询价、催办……）
- 描述"信息收集 + 对象筛选 + 与对端协商确认"的任务形态

**不适用**：
- 修改/修复已生成的子 skill（直接编辑其 SKILL.md，不走本 skill）
- 纯信息查询、无对端多轮对话的简单领域
- 需要自定义 messages_builder / agent_hooks 等代码级定制（声明式模版表达
  不了；手写 pattern 文件放进 hermes-nexus `dialogue/` 走 AST 发现）

## Prerequisites

- hermes-nexus 对话引擎服务（承载话术模版执行与任务会话，默认已在后台运行；
  `trigger_task.py` 每次调用前自动探活，调用方无需关心部署位置）
- `chinese-poi-search`（仅当领域需要 POI/商家检索做对象解析时）
- `hermes-agent-skill-authoring` 规范（skill 文件格式）
- 本 skill 目录内的资产：
  - `templates/domain-skill-template.md`（子 skill 骨架）
  - `templates/trigger_task.py`（触发/轮询客户端，G4 原样复制）
  - `references/pattern-template-example.json`（模版格式蓝本）
  - `references/domain-heuristics.md`（G1 六维启发式手册）

---

## Phase G1: 领域调研 (Domain Research)

### Goal

吃透领域的对话形态，产出 `domain_definition`、信息用途分类，以及
**FSM/Agent/混排的形态推荐**（G3 与注册接口都依赖它）。

### Steps

1. **素材收集（混合来源）**

   - 若用户给了资料目录/文档（业务规则、示例对话、话术手册）：以它为准
   - 缺失的部分用 web 搜索补齐（领域术语、常见纠纷点、行业惯例）
   - 在产出中标注每条结论的来源（用户提供 / 搜索）；搜索结果只做归纳，
     不编造领域数字（价格、时效、政策）

2. **六维启发式分析**

   按 `references/domain-heuristics.md` 逐维评估：时间敏感性 / 对象可变性 /
   约束复杂度 / 增值服务 / 信息不对称 / 失败兜底。

3. **信息三用途分类**（代聊视角）

   | 用途 | 说明 | 去向 |
   |------|------|------|
   | (1) 对象筛选 | 定位与谁交互（哪家店/哪个商品） | 子 skill Phase 2（resolver，可选） |
   | (2) 对端交互 | 与对端沟通/协商时要收集或确认的 | **话术模版**的模块与槽位 |
   | (3) 后置判断 | 判断任务成败的基准 | 子 skill Phase 5 判定规则 |

4. **形态推荐（必产出）**

   `recommended_form: fsm | agent | mixed` + 一段 `form_rationale`：

   - 流程结构化强、字段可穷举（挂号、订位收集段）→ **fsm**
   - 对话开放、依赖临场应变（砍价、投诉处理）→ **agent**
   - 常见答案：收集段 FSM + 谈判收尾 Agent → **mixed**（参考蓝本即此形态）

5. **Completion Criteria**

   - [ ] domain_definition（domain/action/object_type/counterpart_type/description）
   - [ ] 六维分析完成，`domain-heuristics.md` 的 checklist 过一遍
   - [ ] 信息三用途表填完
   - [ ] recommended_form + rationale 已给出

---

## Phase G2: 对端分析 (Counterpart Analysis)

### Goal

搞清楚**对端是谁、怎么演**——这决定 mock 质量与任务可测性。

### Steps

1. **对端画像**：谁接电话/回消息（值班经理？客服？卖家？），其决策权限、
   常见话术、底线与让步空间。

2. **编写 `counterpart_hint.role_prompt`**（模版字段，llm mock 对端的 system）：

   - 身份 + 场景 + 生意状况（"周五周六满座"决定谈判难度）
   - 可让与不可让（"可改时间/降级大厅；不接受砍价"）
   - 语气约束（口语化、简短、礼貌但坚持规定）

3. **编写 `counterpart_hint.scripted_replies`**（4-6 条）：

   覆盖一条典型成功路径（含至少一次波折），用于确定性回归测试。

4. **resolver 策略（可选，仅当需要对象筛选）**

   优先级：chinese-poi-search → 其他已安装 skill → 公开 API → 用户提供。
   需新建脚本时规划 `scripts/<tool_name>.py`，签名
   `resolve_<objects>(**params) -> list[dict]`，未实现标
   `status: awaiting_implementation`。

5. **Completion Criteria**

   - [ ] role_prompt 已写（含身份/让步空间/语气）
   - [ ] scripted_replies ≥ 4 条且覆盖一条含波折的路径
   - [ ] resolver 策略已定（或明确"本领域不需要"）

---

## Phase G3: 话术模版设计 (Dialogue Template Design)

### Goal

产出符合服务端 schema 的 pattern JSON——**对照
`references/pattern-template-example.json` 蓝本逐段对齐**。

### 模版字段速查

| 字段 | 层 | 说明 |
|------|----|------|
| `code/name/description` | 顶层 | code 全小写下划线，不得撞内置 pattern（`customer_agent`/`xianyu_agent`） |
| `recommended_form/form_rationale` | 顶层 | G1 结论原样落入 |
| `counterpart_hint` | 顶层 | G2 产出（role_prompt + scripted_replies） |
| `entry_module_code` | 顶层 | 入口模块 |
| `modules[].type` | 模块 | `agent` / `fsm` / `route` |
| `modules[].base_prompt` | 模块 | agent 模块的 system 主体（必写） |
| `modules[].sub_modules` | 模块 | 模块转移边：`["close"]` 或 `[{target, lend_tools}]` |
| `modules[].use_tools` | 模块 | 工具名数组（须已在服务端注册且 ACL 授予本 pattern） |
| `modules[].nodes[]` | fsm/route | 节点：`node_code/node_name/node_description/node_todo_description` |
| `nodes[].node_slots` | 节点 | `{槽位名: 描述}`，NLU 抽取提示 |
| `nodes[].sub_nodes` | 节点 | FSM 合法后继（**只声明目标集合**，实际跳转由 NLU 输出 next_node） |
| `nodes[].jump_module` | 节点 | 跳模块（菜单/收尾场景） |
| `is_end` | 模块/节点 | 终态标志——任务引擎据此判定 end_module；**必须有可达终态** |

### 设计要点

- 模块粒度：一个"对话任务段"一个模块；深聊交给转移（sub_modules），
  不做巨型模块
- FSM 节点 = "当前在收集/确认什么"；`node_todo_description` 写给 NLU、
  `node_description` 写给 NLG，两者分开写
- 槽位名用英文 snake_case，描述给中文示例值
- agent 模块的 base_prompt 必须包含：身份、任务信息引用（会话信息块）、
  协商原则、达成后的复述要求、失败时的缺口说明要求
- **表达力边界**：stage 实例 / messages_builder / agent_hooks 写不进 JSON
  （校验会剔除并告警）；需要时手写 pattern 文件

### Completion Criteria

- [ ] 模版 JSON 可被解析，字段全部在速查表内
- [ ] 至少一个可达的 is_end（模块或节点）
- [ ] 所有 sub_modules/sub_nodes/jump_module 指向存在的目标
- [ ] use_tools 只引用存在的工具（不确定就不写）
- [ ] counterpart_hint 完整（G2 产出）

---

## Phase G4: 子 skill 生成 (Sub-skill Generation)

### Goal

组装并安装领域交互子 skill。

### Steps

1. **填充骨架**：读取 `templates/domain-skill-template.md`，替换全部
   `{{变量}}`（变量对照表见骨架文末）。
2. **复制客户端脚本**：`templates/trigger_task.py` **原样复制**到子 skill
   的 `scripts/trigger_task.py`（不改内容；环境差异用脚本自带参数解决，
   不进脚本内容）。
3. **落模版副本**：G3 的模版 JSON 写入子 skill
   `references/template.json`——这是 hash 对齐的**本地基准**（本地为准：
   与服务端不一致时覆盖重注册）。
4. **可选 resolver**：G2 规划的 `scripts/<tool_name>.py`（绝对路径 import，
   不用相对路径）。
5. **安装**：写入
   `~/.hermes/skills/productivity/interactive-task-{{DOMAIN_NAME}}/`，
   使用 `skill_manage(action='create')` 注册。

### 目录结构

```
interactive-task-{{DOMAIN_NAME}}/
├── SKILL.md                     # 填充后的骨架（五段新流程）
├── scripts/
│   ├── trigger_task.py          # 原样复制，探活/懒注册/触发/轮询
│   └── <resolver>.py            # 可选
└── references/
    └── template.json            # 话术模版副本（hash 对齐基准）
```

### Completion Criteria

- [ ] SKILL.md 无残留 `{{` 占位符（`grep -n '{{'` 为空）
- [ ] scripts/trigger_task.py 与源文件一致
- [ ] references/template.json 与 G3 产出一致
- [ ] skill 已安装且 frontmatter 合法

---

## Phase G5: 校验与交付 (Validation & Delivery)

### Goal

**不合法不交付**——模版质量在生成时刻闭环，不留到用户说"帮我代聊"时爆雷。

### Steps

1. **服务端校验硬门槛（必做）**

   ```bash
   # 探活（不通时脚本会自动拉起服务）
   python scripts/trigger_task.py ensure --template references/template.json

   # validate-only（不注册）
   curl -s -X POST http://127.0.0.1:8000/api/v1/templates/validate \
     -H "Content-Type: application/json" \
     -d "{\"template\": $(cat references/template.json)}"
   ```

   **`ok: false` 时必须修复全部 error 再交付**；warnings 逐条评估
   （TOOL_ACL / NO_TERMINAL 通常要处理）。error code 速查（全部带 JSON
   路径定位，`data.errors[].{path, code, message}`）：

   | code | 含义 | 修法 |
   |------|------|------|
   | MISSING_FIELD / CODE_FORMAT / ENUM_INVALID / TYPE_ERROR | schema 层 | 按路径补齐/改型 |
   | DUPLICATE_CODE | module/node code 撞车（node_map 扁平） | 改名 |
   | ENTRY_MISSING / DANGLING_EDGE / SELF_LOOP / UNAUTHORIZED_LEND | 结构层 | 补目标/去自环/lend_tools ⊆ 目标 use_tools |
   | TOOL_UNKNOWN / BUILTIN_CONFLICT | 引用层 | 删工具引用 / 换 code |

2. **结构一致性检查**：与 `references/pattern-template-example.json` 对照，
   字段形态一致；每个 fsm 节点有 sub_nodes（终态节点可为空数组）。

3. **占位符检查**：`grep -n '{{' SKILL.md` 为空。

4. **frontmatter 校验**（沿用内嵌片段）：

   ```python
   import yaml, re, pathlib
   content = pathlib.Path(skill_path).read_text()
   assert content.startswith("---"), "Frontmatter must start with ---"
   m = re.search(r'\n---\s*\n', content[3:])
   fm = yaml.safe_load(content[3:m.start()+3])
   assert "name" in fm and "description" in fm
   assert len(fm["description"]) <= 1024 and len(content) <= 100_000
   ```

5. **交付汇报**：子 skill 路径、模版 code 与形态推荐、模块/槽位概览、
   scripted 测试路径一条、后续建议（"对我说 xx 领域的帮我代聊试试"）。

### Completion Criteria

- [ ] validate 接口 `ok: true`（0 error）
- [ ] warnings 已逐条处置或明确说明保留理由
- [ ] 占位符/frontmatter/结构三项通过
- [ ] 子 skill 已通过 skill_manage 注册
- [ ] 用户已确认交付

---

## Pipeline Flow Summary

```
G1 领域调研 ──→ G2 对端分析 ──→ G3 话术模版(JSON) ──→ G4 子skill组装安装 ──→ G5 validate硬门槛交付
 素材混合        role_prompt      pattern schema          trigger_task.py        POST /templates/validate
 六维启发式      scripted回复     FSM/Agent/混排           template.json副本      全error清零
 形态推荐        resolver策略     槽位/转移/终态           skill_manage create    交付汇报
```

运行态（用户视角）：用户说"帮我代聊" → 子 skill 收集需求 → `trigger_task.py
ensure`（探活+自动拉起+hash 对齐懒注册）→ `run`（触发+轮询）→ 后置判断 → 汇报。

## Reference Implementation

- **模版蓝本**：`references/pattern-template-example.json`（餐厅订位，
  mixed 形态，validate 0 error 0 warning）
- **新架构参考实现**：`interactive-task-food`（v3.0.0，薄客户端五段流程 +
  `food_booking` 模版 + trigger_task.py，G4 生成的子 skill 长这样就对了）
- `interactive-task-car-sales`：**旧架构产物**（话术在 skill 侧、Phase 4
  直连同步 chat），仅作历史对照，不要模仿其 API 调用方式

## Common Pitfalls

1. **模版 code 撞内置 pattern**（customer_agent/xianyu_agent）→ 注册被拒
   （BUILTIN_CONFLICT），起名时先 `GET /api/v1/templates` 看一眼
2. **节点转移当条件写**：`sub_nodes` 只是合法后继集合，不要写业务条件；
   实际跳转由 NLU 依据 `node_todo_description` 判定
3. **无可达终态**：任务只能靠 max_turns 收束（NO_TERMINAL warning），
   谈判类模版务必给收尾模块/节点 is_end
4. **use_tools 引了没授权的工具**：工具注册默认全拒绝（TOOL_ACL warning），
   运行时工具不可见——宁可不用工具，把规则写进 base_prompt
5. **scripted_replies 太顺**：没有波折的脚本测不出谈判分支，至少含一次
   "不能满足初始要求→给替代方案"
6. **忘复制 trigger_task.py 或改动它**：子 skill 就失去探活/懒注册能力；
   原样复制，路径问题用参数解决
7. **绕过 G5 直接交付**：坏模版会延迟暴露到用户首次"帮我代聊"，
   validate 硬门槛是生成时刻的质量闭环

## Verification Checklist

- [ ] 模版通过 `POST /api/v1/templates/validate`（0 error）
- [ ] 子 skill 安装目录三件套齐全（SKILL.md / scripts/trigger_task.py /
      references/template.json）
- [ ] `grep -n '{{'` 无残留
- [ ] 手动冒烟：`python scripts/trigger_task.py run --template references/template.json
      --counterpart-mode scripted --script-json '<scripted_replies>' --kickoff '<首条>'
      --max-turns 10` 能跑出终态结果
- [ ] 交付汇报已给用户
