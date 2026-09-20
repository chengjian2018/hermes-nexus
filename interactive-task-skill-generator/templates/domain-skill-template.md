# Domain Skill Template

目标子 skill 的骨架模板。元 skill 的 Phase G4 加载此模板并填充变量。

> 所有 `{{VARIABLE}}` 占位符在 G4 阶段被替换为实际内容。
> `{{CONDITIONAL_BLOCK:xxx}}` / `{{END_CONDITIONAL}}` 标记的块在条件不满足时整块删除。
> 本模板就是子 skill 的 SKILL.md 全文（不再包裹代码围栏），填充后直接落盘。

---

---
name: interactive-task-{{DOMAIN_NAME}}
description: "Use when user wants to {{ACTION_DESCRIPTION}} (e.g. '{{TRIGGER_EXAMPLES}}'). Thin domain client for {{DOMAIN_LABEL}}: collects task requirements from the user, lazily registers the dialogue template on hermes-nexus, triggers an autonomous counterpart conversation, polls the result, and reports the post-judgment outcome."
version: 2.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hci, {{DOMAIN_TAG}}, {{DOMAIN_NAME}}, domain-skill, counterpart]
    related_skills: [interactive-task-skill-generator, chinese-poi-search]
---

# Interactive Task: {{DOMAIN_LABEL}} (Domain Skill)

## Overview

{{DOMAIN_LABEL}} 领域的**代聊薄客户端**：对话话术全部在 hermes-nexus 服务端
（由 `references/template.json` 话术模版编译执行），本 skill 只负责三件事——
向用户收集任务需求、触发并轮询自主任务会话、对结果做后置判断并汇报。

```
用户："{{TRIGGER_EXAMPLES}}"
  Phase 1 与用户多轮 → task_info
  Phase 2 {{RESOLVER_SHORT_DESC}}（可选）
  Phase 3 trigger_task.py ensure —— 模版对齐（探活/自动拉起/hash 对齐/懒注册）
  Phase 4 trigger_task.py run   —— 触发任务 + 轮询终态
  Phase 5 后置判断 → 汇报（成功确认项 / 未满足项 / 替代方案 / 用户决定）
```

服务端约定：hermes-nexus 对话引擎（承载话术模版执行与任务会话）**默认已在
后台运行**（`http://127.0.0.1:8000`）；脚本每次调用前自动探活，环境异常时
自行处理——本 skill 不涉及、也不需要知道服务的部署位置。

## When to Use

- 用户说："{{TRIGGERS}}"
- 用户给出对象与要求并希望**由 skill 代为与对方沟通确认**

不适用：纯信息查询、只想了解{{DOMAIN_LABEL}}知识（直接回答，不起任务）。

---

## Phase 1: 需求收集 (Collect Requirements from the User)

**与用户**多轮对话，产出本次任务的 `task_info`。必收字段：

| 字段 | 说明 | 示例 |
|------|------|------|
{{TASK_INFO_FIELDS}}

可选字段（按需追问，不逐条盘问）：{{OPTIONAL_INFO_FIELDS}}

收满即止；用户没给的关键字段给默认值前先确认。产出 JSON 形如：

```json
{{TASK_INFO_EXAMPLE}}
```

---

## Phase 2: 对象解析 (Resolve Target Objects) {{CONDITIONAL_BLOCK:resolver}}

{{RESOLVER_SECTION}}

{{END_CONDITIONAL}}

---

## Phase 3: 模版对齐 (Ensure Template Registered)

**不要**自己 curl 服务端——用自带脚本（探活、自动拉起、hash 对齐、懒注册
一次完成）：

```bash
python scripts/trigger_task.py ensure --template references/template.json
```

行为（无需干预，失败才介入）：

1. `GET /api/v1/health` 探活；不通则后台拉起 hermes-nexus 并等待就绪
2. `GET /api/v1/templates/{{PATTERN_CODE}}` 查询：
   - 未注册 → `POST /api/v1/templates` 注册（懒注册，仅首次需要）
   - 已注册且 hash 与本地副本一致 → 直接复用
   - hash 不一致 → 本地为准，覆盖重注册（元 skill 重新生成过新版）
   - 返回 `source: builtin` → **停止并报告冲突**（不得覆盖内置 pattern）
3. 注册被拒（`400`）→ stderr 打印全量校验错误（带 JSON 路径），
   原样转述给用户并建议重新生成模版

---

## Phase 4: 触发任务 (Trigger Autonomous Task)

组装 kickoff（首条喂给服务端话术模版的消息，**用自然语言**，不是 JSON）：

```
{{KICKOFF_TEMPLATE}}
```

触发并轮询到终态（stdout 输出结果 JSON）：

```bash
python scripts/trigger_task.py run \
    --template references/template.json \
    --kickoff "<Phase 1 组织的首条消息>" \
    --task-info-json '<Phase 1 的 task_info JSON>' \
    --counterpart-mode llm
```

参数速查：

| 参数 | 说明 |
|------|------|
| `--counterpart-mode llm` | 对端由 LLM 按模版 `counterpart_hint.role_prompt` 扮演（默认；真实感强） |
| `--counterpart-mode scripted --script-json '[...]'` | 对端按脚本逐轮回复（确定性测试） |
| `--max-turns N` / `--timeout-s S` | 轮次/时限上限（默认 20 / 600） |
| `--no-ensure` | 刚跑过 ensure 时跳过模版对齐 |

轮询期间：任务在后台线程执行；用户想看进展时查询
`GET /api/v1/sessions/<session_id>/messages`（session_id 在触发输出的 stderr
日志里）。结果 JSON 关键字段：

```json
{
  "status": "done | failed",
  "finish_reason": "end_module | max_turns | timeout | counterpart_exhausted | error",
  "turn_count": 6,
  "result": {
    "end_module_code": "…", "end_node_code": "…",
    "filled_slots": {"…": "…"},
    "messages": [{"role": "user|assistant", "content": "…"}]
  }
}
```

---

## Phase 5: 后置判断与汇报 (Post-judgment & Report)

对 Phase 4 的 `result.messages` 做语义判断，输出：

```json
{
  "interaction_confirmed": true,
  "confirmed_details": {"字段": "值"},
  "unmet_constraints": [],
  "alternatives_offered": [],
  "user_decision_needed": false,
  "summary": "一句话结论"
}
```

### 判定规则（本领域）

{{POST_JUDGMENT_RULES}}

### 汇报格式

向用户汇报：结论（成/败/需决策）+ 确认明细（日期/对象/关键条款）+
未满足项与替代方案（如有）+ 建议下一步。**引用对话原文佐证关键承诺**
（对端说过的确认语），不凭空概括。

---

## Domain Knowledge

{{DOMAIN_KNOWLEDGE}}

### Common Defaults

{{DEFAULTS}}

### Pitfalls

{{PITFALLS}}

---

## Verification Checklist

- [ ] Phase 1 task_info 必收字段齐全
- [ ] Phase 3 ensure 成功（或明确报告冲突/校验错误）
- [ ] Phase 4 拿到终态结果（非 running 中断）
- [ ] Phase 5 判定字段完整，关键结论有对话原文佐证
- [ ] 用户已收到汇报

---

## Template Variable Reference

| 变量 | 来源 | 说明 |
|------|------|------|
| `{{DOMAIN_NAME}}` / `{{DOMAIN_LABEL}}` / `{{DOMAIN_TAG}}` | G1 | kebab 名 / 中文标签 / tag |
| `{{ACTION_DESCRIPTION}}` / `{{TRIGGERS}}` / `{{TRIGGER_EXAMPLES}}` | G1 | 动作描述 / 触发词列表 / 触发例句 |
| `{{TASK_INFO_FIELDS}}` / `{{OPTIONAL_INFO_FIELDS}}` / `{{TASK_INFO_EXAMPLE}}` | G1 用途(1) | 必收/可选字段表与示例 |
| `{{RESOLVER_SECTION}}` / `{{RESOLVER_SHORT_DESC}}` | G2 | resolver 配置（不需要则整块删除） |
| `{{PATTERN_CODE}}` | G3 | 模版 code（= references/template.json 的 code） |
| `{{KICKOFF_TEMPLATE}}` | G1/G3 | kickoff 首条消息的组织模板 |
| `{{POST_JUDGMENT_RULES}}` | G1 用途(3) | 成败判定规则 |
| `{{DOMAIN_KNOWLEDGE}}` / `{{DEFAULTS}}` / `{{PITFALLS}}` | G1 | 领域知识/默认值/陷阱 |
