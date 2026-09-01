# Architecture

> 活的系统地图——每次框架改动后同步更新本文件。模块依赖方向、公共契约、“什么代码放哪”以此为准。

## 模块依赖图

```mermaid
flowchart TB
    main["main.py<br/>FastAPI 入口<br/>会话治理(TTL/LRU)"] --> chat["src/chat<br/>chat() 主循环<br/>Session · loop"]
    main --> preg["dialogue/register.py<br/>Pattern 注册中心"]
    main --> treg["tools/register.py<br/>Tool 注册中心"]

    chat --> base["dialogue/base.py<br/>PipelineStage<br/>DialogueContext<br/>SessionMessage"]
    chat --> nlu["dialogue/nlu.py<br/>FSMNLU · RouteNLU"]
    chat --> nlg["dialogue/nlg.py"]
    chat --> uni["dialogue/unified.py<br/>统一阶段(单次调用 NLU+NLG)<br/>FSMUnifiedNLU · RouteUnifiedNLU · PassThroughNLG"]
    chat --> loop2["chat/loop.py<br/>Agent ReAct 循环"]

    preg --> pattern["dialogue/pattern.py"]
    pattern --> base

    nlu --> resolve["llm/resolve.py<br/>build_provider()"]
    nlg --> resolve
    uni --> resolve
    loop2 --> resolve
    loop2 --> treg

    resolve --> llmreg["llm/register.py<br/>Provider 注册中心"]
    llmreg --> oai["llm/openai_provider.py<br/>OpenAICompatible"]

    subgraph 应用层
        carsales["dialogue/car_sales_route.py<br/>(示例 pattern)"]
        carsalesuni["dialogue/car_sales_unified_route.py<br/>(统一阶段示例 pattern)"]
        tools["tools/calculator_tool.py<br/>weather_tool.py"]
        clarify["src/clarify/<br/>偏题澄清"]
    end
    carsales -.-> preg
    carsalesuni -.-> preg
    tools -.-> treg
```

**依赖方向（不许反向）**：`main → chat → dialogue(base/stages) → llm`；应用层只通过 registry 挂进来。

## 核心概念

- **Pattern**：一个完整对话流程（如 car_sales_route），由多个 Module 组成，声明 stages 流水线
- **Module**：三种类型 `ROUTE`（菜单分发）/ `FSM`（状态机）/ `AGENT`（自由对话+工具）
- **Node**：FSM/ROUTE 内的状态节点；`sub_nodes` 构成转移图；节点级 NLU/NLG 可覆盖模块级
- **PipelineStage**：可插拔管线步骤，`execute(ctx) -> ctx`；ctx 即 `DialogueContext` 全程数据载体
- **统一阶段（unified.py）**：单次调用 + structured output 的 NLU+NLG 合一形态——一次 LLM 调用产出 `{"reply","next_node","slots"}`，拆写 `ctx.nlu_result`/`ctx.nlg_result`；`next_node` 由代码按合法转移边硬校验（开启 `enable_clarify` 的模块放行 `"clarify"`，与 ClarifyStage 组合成 `[统一, 澄清, PassThroughNLG]` 管线）。module 级注入（`nlu_stage=FSMUnifiedNLU()/RouteUnifiedNLU(), nlg_stage=PassThroughNLG()`），替换默认两阶段（每轮 2 次调用 → 1 次；澄清轮 2 次，与两阶段+澄清持平）
- **Session**：持有 `cxt`（DialogueContext）；每轮更新 `user_query`，轮末回写状态

## 公共契约（改动需走内核流程）

| 契约 | 位置 | 说明 |
|---|---|---|
| `PipelineStage.execute(ctx)` | `dialogue/base.py` | 所有 stage 的唯一接口 |
| `DialogueContext` 字段 | `dialogue/base.py` | stage 间数据交换全部经由 ctx，不另开通道 |
| `registry.register()` 自注册 | `dialogue/register.py` `tools/register.py` `llm/register.py` | 应用层接入框架的唯一方式（AST 扫描发现） |
| `build_provider(llm_config)` | `llm/resolve.py` | 所有 LLM 调用的统一入口 |
| `conversation(session, module, llm_config)` | `chat/loop.py` | Agent 模块对话循环入口 |

## 什么代码放哪

- 新业务对话流程 → `src/dialogue/<name>_route.py`，模块级 `registry.register()`
- 新工具 → `src/tools/<name>_tool.py`，自动被 AST 发现
- 新 LLM provider → `src/llm/<name>_provider.py`
- 新管线阶段 → `src/dialogue/<stage>.py` 继承 `PipelineStage`
- 模块要单次调用（NLU+NLG 合一）→ module 上配 `nlu_stage=FSMUnifiedNLU()/RouteUnifiedNLU()` + `nlg_stage=PassThroughNLG()`（见 `car_sales_unified_route.py` 示例；候选节点需声明 `answer_examples`）
- 全局 prompt 模板 → `src/prompt.py`（node/module 可覆盖）

## 测试

`tests/` 全离线（fake_provider 打桩 LLM）：`export DASHSCOPE_API_KEY=... && .venv/bin/python -m pytest tests/ -q`。
改框架后必须全绿再提交。
