# Implementation Plan: Agent Loop Hooks

日期：2026-09-04 ｜ 状态：待人工审 ｜ 涉及分层：框架内核（`src/chat/loop.py`、`src/dialogue/pattern.py`、`src/dialogue/module.py`）

按 CLAUDE.md 内核流程执行：本 plan 人工审通过后，小步 commit，每步全量 pytest：

```
export DASHSCOPE_API_KEY=... && .venv/bin/python -m pytest tests/ -q
```

---

## 0. 目标与共识（决策记录）

给 AGENT 模块的 agent loop（`loop.run_agent`）加可插拔 hooks，参照 Customer-Agent
的定制化诉求（构建 system prompt 前取数注入、更正工具调用）。经设计拷问收敛的决策：

| 决策点 | 结论 |
|---|---|
| 定位 | hooks = **多点叠加**；改 messages 归 `messages_builder`（产物终态，hook 只读）；换执行器归 `AgentRunner`（本次**不动**，agents.py 保持现状） |
| 能力等级 | ②变更：可改写工具调用（args + name）与工具结果、可注入 system prompt 片段；**不做③控制**（无拦截/丢弃/制造移交） |
| 范围 | 仅 agent loop；FSM/ROUTE、chat 编排层不接（结构上成立：只有 AGENT 路径进 `run_agent`） |
| 声明 | `pattern.agent_hooks`（dict-of-lists）；module 层 `agent_hooks` **整体替换**（不 merge，同 stage 槽位语义） |
| 错误语义 | 防御性：hook 异常吞掉记日志 + **回退原值**，绝不阻断对话 |
| AgentRunner | 不接线、不删除，留作独立决定 |
| 工具名纠错（双层） | P4 hook 先做确定性改名（别名映射）；主流程兜底校验**最终 name ∈ allowed_names**，非法即不执行、回填带可用工具清单的错误信息，模型下一轮自纠 |

### 五条硬规则（安全与持久化）

1. **transfer 判定先行且不可改写**：用 LLM 原始响应判定 transfer；transfer 轮
   P4 不触发；改写只作用于非 transfer 轮的普通调用。
2. **执行前校验最终 name（P4 改写后）∈ 本轮已解析 tools 集合（own + lent）**，
   且不得带 `transfer_to_` 前缀。非法一律**不执行**——覆盖三种来源：LLM 原始
   调用的幻觉名、hook 改写被拒后回落到的原名、走私 transfer 前缀。处理方式：
   回填带可用工具清单的错误信息，模型下一轮自纠（§3.3）。`lent_by` 溯源按
   最终 name。附带封堵现状漏洞：`tool_registry.dispatch` 只查注册不查 ACL
   （register.py:770-772），幻觉命中"已注册但未授权本 pattern"的 name 时
   两层 ACL 被旁路——主流程校验后此路不通。
3. **`tool_call_id` 永不改**（协议配对与回放守卫命脉）。
4. **三处一致记录改写后实况**（in-loop `messages` / history assistant 载荷 /
   tool 行），tool 行 metadata 加 `{"rewritten": true, "original": {...}}` 审计。
5. 观察点签名无返回值语义，框架不接其输出。

---

## 1. 变更清单

| 文件 | 动作 | 内容 |
|---|---|---|
| `src/chat/agent_hooks.py` | **新增** | 点位常量、7 个事件类型、`RewriteToolCall`、声明解析、dispatcher |
| `src/chat/loop.py` | 修改 | 挂 7 个点位；普通工具路径顺序重排；错误回填分支同构接入 |
| `src/dialogue/pattern.py` | 修改 | `Pattern.__init__` 显式声明 `agent_hooks` 参数 |
| `src/dialogue/module.py` | 修改 | `BaseModule.__init__` 显式声明 `agent_hooks` 参数 |
| `tests/test_agent_hooks.py` | **新增** | 用例矩阵（§5） |
| `ARCHITECTURE.md` | 修改 | 模块依赖图 / 公共契约 / 什么代码放哪 / 测试 四节同步 |

不改动：`src/chat/chat.py`、`src/chat/agents.py`、`src/chat/messages.py`
（`build_agent_messages` 入口与默认构建不动，`messages_builder` 契约不变）。

---

## 2. 新模块 `src/chat/agent_hooks.py`

### 2.1 点位与事件类型

```python
HOOK_POINTS = (
    "on_agent_start",    # P1 进 loop、build system prompt 前（注入）
    "on_llm_call",       # P2 每轮 LLM 调用前（观察）
    "on_llm_response",   # P3 每轮 LLM 返回后（观察）
    "on_tool_call",      # P4 单工具执行前（变更：name/args）
    "on_tool_result",    # P5 单工具执行后、落 history 前（变更：结果串）
    "on_transfer",       # P6 transfer 命中写事件时（观察）
    "on_agent_end",      # P7 三个出口（观察）
)
```

事件 dataclass（全部携带 `session_id` / `module_code`）：

| 事件 | 字段 | 返回契约 |
|---|---|---|
| `AgentStartEvent` | `cxt` | `Optional[str]` 片段 |
| `LLMCallEvent` | `round_idx, messages, model` | 无（忽略） |
| `LLMResponseEvent` | `round_idx, content, tool_calls` | 无 |
| `ToolCallEvent` | `round_idx, tool_name, args` | `Optional[RewriteToolCall]` |
| `ToolResultEvent` | `round_idx, tool_name, tool_call_id, result` | `Optional[str]` |
| `TransferEvent` | `round_idx, target, reason` | 无 |
| `AgentEndEvent` | `rounds, outcome, reply, transfer_target`；outcome ∈ `"reply" / "transfer" / "max_rounds"` | 无 |

```python
@dataclass
class RewriteToolCall:
    name: Optional[str] = None   # None = 不改
    args: Optional[Dict[str, Any]] = None
```

只读纪律（docstring 声明，不强制）：`AgentStartEvent.cxt`、
`LLMCallEvent.messages` 为引用传递，hook 不得原地修改；框架不做深拷贝
（每轮深拷贝 messages 代价不成比例）。要改 messages 的诉求归
`messages_builder`。

### 2.2 声明解析

```python
def resolve_agent_hooks(module, pattern) -> Dict[str, List[Callable]]:
```

- `module.agent_hooks` 非空 → 整体替换；否则用 `pattern.agent_hooks`；皆空 → `{}`。
- 校验（防御，同 stage_slots 降级风格）：入参非 dict / 点位名不在
  `HOOK_POINTS` / 值非 callable → warning 跳过该项，不抛错。

### 2.3 dispatcher

```python
def fire(hooks, point, event) -> None
```
观察点通用：逐 hook try/except，异常 `logger.exception` 后继续下一个。

```python
def collect_fragments(hooks, event) -> List[str]
```
P1 用：逐 hook 收 `Optional[str]`，异常丢弃该片段记日志。

```python
def rewrite_tool_call(hooks, event, allowed_names) -> Tuple[str, Dict, Optional[Dict]]
```
P4 用，**链式**：hook₁ 的改写反映进 event 后再喂 hook₂（声明序）。返回
`(final_name, final_args, original)`；`original` 为
`{"name": 原名, "args": 原args}`，仅当实际发生改写时非 None。守卫内建：

- 改写 name ∉ `allowed_names`（own+lent 名集合）→ 拒绝该次改名 + warning；
- 改写 name 带 `transfer_to_` 前缀 → 拒绝该次改名 + warning（规则 2，双保险）；
- 同一返回值中合法部分照常应用（name 被拒、args 照用）；
- hook 异常 → 该 hook 返回视作 None，用进 chain 前的当前值继续。

拒绝改写后回落到的名字若仍非法（原始调用本就是幻觉名），由 loop 侧最终
校验兜底（§3.3）——dispatcher 只负责"不让改写变得更糟"，拦截非法调用是
主流程的职责。

```python
def rewrite_tool_result(hooks, event) -> Tuple[str, Optional[str]]
```
P5 用，链式同上；返回 `(final_result, original)`。

---

## 3. `loop.py` 挂载改动

### 3.1 入口（P1）

```python
hooks = resolve_agent_hooks(module, session.pattern)          # run_agent 开头
event_base = ...  # session_id / module_code
fragments = collect_fragments(hooks, AgentStartEvent(...)) if hooks else []
system_prompt = _build_system_prompt(module, cxt, extra_blocks=fragments)
```

`_build_system_prompt(module, cxt, extra_blocks=None)`：fragments 非空时以
`"\n\n".join` 拼为单块，加标题 `## 扩展上下文`，追加在"已填充槽位"块之后。
force_close 后缀与 `_PROMPT_LENGTH_WARN` 检查保持在块拼接**之后**（告警
覆盖注入后的真实长度）。执行前 `grep -rn "_build_system_"` 确认无外部调用方。

### 3.2 每轮（P2 / P3）

`provider.chat_completion` 前后各一行 fire（有 hooks 才构造事件），
`messages` 传引用（只读纪律）。

### 3.3 普通工具路径顺序重排（P4 / P5，规则 4 的实现）

现状（`loop.py:206-228`）：先落 assistant 载荷（原始 tool_calls）再执行。
改为：

```
1. 逐 tc：_parse_args → rewrite_tool_call（P4 链 + 守卫）
   → 应用回 tc：tc["function"]["name"]/["arguments"]（args 经
   json.dumps(ensure_ascii=False) 重序列化；序列化失败保留原 args + warning）
   —— 原地改写 LLM 返回的 tc dict，使 in-loop messages / history 载荷 /
   执行三处共用改写后单一事实源
2. messages.append(assistant 终态) + cxt.add_message("assistant",
   encode_tool_call_content(content, tool_calls))
3. 逐 tc：**最终 name 校验**（规则 2 的权威检查点，allowed = own+lent）——
   a. **合法**：`_execute_tool` → `rewrite_tool_result`（P5 链）→
      `cxt.add_message("tool", result, metadata={tool_name, tool_call_id,
      **({"rewritten": True, "original": ...} if 改写)})` +
      `messages.append(tool 行)`；lent_by 溯源按最终 name。
   b. **非法**（幻觉名 / 改写被拒后仍非法 / transfer 前缀）：**不执行**。
      tool 行内容为错误回填（与模型自纠闭环）：

      ```python
      json.dumps({
          "error": f"工具 '{name}' 不存在或本轮不可用。"
                   f"可用工具：{sorted(allowed)}。"
                   f"请从可用工具中重新选择，或直接回应用户。"
      }, ensure_ascii=False)
      ```

      metadata 沿用 transfer 分支的合成行约定
      `{"synthetic": True, "tool_name": ..., "tool_call_id": ...}`；
      **P5 不触发**（非真实执行）；assistant 载荷原样记录该 tc（回放配对
      完整）；继续 loop，模型看到错误与可用清单后自纠。错误措辞不区分
      "未注册"与"已注册未授权"，不向模型泄露 ACL 判定细节。
      反复幻觉由 `_MAX_TOOL_ROUNDS` 兜底。
```

`allowed_names` = own + lent schemas 的 name 集合（该分支内 tc 必无
transfer 前缀，transfer 判定已在前面完成——规则 1）。

### 3.4 transfer 两分支

- **命中分支**（`loop.py:179-204`，静默移交）：不执行工具 → P4/P5 不触发；
  写事件处 fire `TransferEvent`（P6）；return 前 fire
  `AgentEndEvent(outcome="transfer", transfer_target=target)`（P7）。
- **目标非法错误回填分支**（`loop.py:146-173`）：同轮非 transfer tc 仍真执行。
  与 3.3 同构接入（建议抽内部辅助函数复用：P4 链 → 最终 name 校验 → 落
  assistant 载荷 → 逐 tc（transfer 条回填 err 串、合法条 `_execute_tool` +
  P5、非法条错误回填）→ 落 tool 行），消除覆盖面空洞。**P5 只对真实
  `_execute_tool` 结果触发**，不对合成/回填串触发。该分支对非法 transfer
  目标本就有"错误回填继续 loop，让 LLM 自行换路"的习语（`loop.py:144`
  注释）——§3.3 的非法 name 处理即此习语在普通工具路径的泛化，两条路径
  行为收敛为同一种。

### 3.5 出口（P7）

三个 return 点：直接答 `outcome="reply"`（带 reply）、移交见 3.4、超轮次
`outcome="max_rounds"`（带兜底 reply）。force_close 轮 P1 照常（片块在
"勿再移交"后缀之前），transfer 工具未注入故 P6 天然不触发。

---

## 4. `pattern.py` / `module.py` 字段声明

- `Pattern.__init__` 增参 `agent_hooks: Optional[Dict[str, list]] = None`，
  存属性 + docstring 一行（现走 `**kwargs` setattr 可跑，但按 `messages_builder`
  先例显式声明，换取文档与签名可见性）。
- `BaseModule.__init__` 同样增参。`AgentModule` 无需单独改动。

---

## 5. 测试计划（`tests/test_agent_hooks.py`）

沿用 `test_agent_inject_transfer.py` 习语：模块级注册 mock 工具
（`tool_registry.register` + `allowed_patterns` 控制 ACL），
`patch("src.chat.loop.build_provider")` 注入脚本化 provider。

| # | 用例 | 断言要点 |
|---|---|---|
| 1 | P1 注入 | system prompt 含片段与 `## 扩展上下文`；多 hook 声明序拼接；force_close 后缀仍在最后 |
| 2 | P1 失败 | hook 抛异常 → 对话照常、prompt 无该片段、有日志 |
| 3 | P4 args 改写 | 工具收到改写后 args；history assistant 载荷含改写后 args；tool 行 metadata 含 rewritten + original |
| 4 | P4 name 改写（合法） | 改为另一 own 工具；`lent_by` 溯源跟随改写后 name |
| 5 | P4 name 改写（ACL 拦截） | 改为 `acl_locked_tool`（注册但未授权本 pattern）→ 保留原名 + warning |
| 6 | P4 改写为 transfer_to_X | 保留原名 + warning（规则 2） |
| 7 | P4 链式 | hook₂ 看到 hook₁ 改写后的 event 值 |
| 8 | P4 失败 | hook 抛异常 → 原始 name/args 执行 |
| 9 | P5 结果改写 | 回填 messages 与 history 均为改写后结果；失败回退原值 |
| 10 | transfer 轮 | P6 收到 target/reason；该轮 P4/P5 未触发 |
| 11 | 错误回填分支 | 非法 transfer 目标 + 混排普通工具 → 普通 tc 走 P4/P5，transfer tc 不走 |
| 12 | P7 三出口 | reply / transfer / max_rounds 的 outcome 与字段 |
| 13 | module 整体替换 | module.agent_hooks 配置后 pattern 级 hook 不触发 |
| 14 | ROUTE 不触发 | ROUTE 模块跑一轮，hooks 全静默 |
| 15 | tool_call_id 不变 | 改写后载荷中 id 与 LLM 原始返回一致 |
| 16 | 观察点冒烟 | P2/P3 事件字段齐全（round_idx/messages/model/content/tool_calls） |
| 17 | 声明校验降级 | 未知点位名 / 非 callable → warning 跳过不崩 |
| 18 | 回放一致性 | 改写后的 history 经 `default_build_messages` 回放，tool 配对守卫不降级 |
| 19 | 幻觉名（未注册） | 错误回填含可用工具清单；不执行（handler 未被调）；循环继续，scripted provider 次轮自纠（换合法工具或直接答） |
| 20 | 已注册未授权名 | 同样拦截不执行——封堵 `dispatch` 无 ACL 检查的旁路（register.py:770） |
| 21 | 非法名 tool 行 | metadata 带 synthetic 标记；P5 未触发；该轮 history 回放配对守卫不降级 |

另跑全量 pytest 确认既有锚点：`patch("src.chat.loop.build_provider")`
与 `patch("src.chat.chat.get_llm_config")` 均不受影响（两处构建路径不动）。

---

## 6. 提交切分

1. `feat(hooks): agent_hooks 模块——点位事件 + 声明解析 + dispatcher；pattern/module 字段声明`
   （纯新增 + 字段，无行为变化；dispatcher 单测即 §5 #13/#17）
2. `feat(hooks): loop 挂观察点 P2/P3/P6/P7 与 P1 注入块`
   （不动执行顺序；§5 #1/#2/#10/#12/#14/#16）
3. `feat(hooks): P4/P5 变更语义 + 主流程工具名校验（错误回填自纠）——顺序重排 + ACL/transfer 守卫 + 审计 metadata`
   （§5 #3–#9/#11/#15/#18–#21，本 plan 唯一的高风险步，单独成 commit 便于回滚）
4. `docs(architecture): 同步 agent hooks 机制`

每 commit 前全量 pytest 全绿。

---

## 7. 风险与缓解

- **顺序重排改 persist 时机**（commit 3）：assistant 载荷从"执行前落"改为
  "P4 改写后落"。若 P4 环节整体异常（不应发生，dispatcher 已吞），回退路径
  是原始 tc 原样落盘——与现状等价。测试 #3/#18 锁行为。
- **原地改写 LLM 返回的 tc dict**：三处（执行/载荷/回填）共用单一事实源，
  是刻意选择；docstring 显式说明。id 永不动（测试 #15 锁）。
- **prompt 长度告警口径变化**：`_PROMPT_LENGTH_WARN` 现在量的是注入后长度
  ——符合"观测真实发送物"的既有意图。
- **回放守卫**：改写只动 name/args 字符串，不动结构，配对逻辑不受影响
  （测试 #18/#21 锁）。
- **主流程校验封堵既有 ACL 旁路（行为变化）**：现状幻觉命中"已注册未
  授权"名会经 `dispatch` 真实执行；本次起统一拦截并错误回填。这是有意的
  收紧，测试 #20 锁定；若既有测试依赖该旁路需同步修正（预期没有）。
- **自纠轮次预算**：错误回填带可用工具清单，自纠通常一轮完成；反复幻觉
  由 `_MAX_TOOL_ROUNDS=10` 兜底，最终走"处理超时"出口（P7 outcome=
  "max_rounds"）。

## 8. 明确不做

AgentRunner 接线/删除；chat.py 任何改动；FSM/ROUTE 挂点；③控制权
（拦截/丢弃调用/制造移交）——指 **hook 不拥有**否决权；§3.3 的非法 name
拦截是框架主流程行为（确定性规则，非 hook 决策），不属此列；hook 写 cxt
的注入通道；async hook；新全局 registry。
