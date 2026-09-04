# Architecture

> 活的系统地图——每次框架改动后同步更新本文件。模块依赖方向、公共契约、“什么代码放哪”以此为准。

## 模块依赖图

```mermaid
flowchart TB
    main["main.py<br/>FastAPI 入口<br/>会话治理(TTL/LRU)"] --> chat["chat/chat.py<br/>轮次编排器<br/>chat() / chat_turn()<br/>跳转检测 + hop 消费"]
    main --> preg["dialogue/register.py<br/>Pattern 注册中心"]
    main --> treg["tools/register.py<br/>Tool 注册中心"]
    main --> chan["channel/xianyu.py<br/>闲鱼外挂决策口适配<br/>(声明式 ChannelSpec · 通用 handler)"]

    chat --> lc["chat/context_lifecycle.py<br/>TurnLifecycle<br/>cxt 字段生命周期"]
    chat --> resp["chat/response.py<br/>ChatResult<br/>text + actions"]
    chat --> slots["dialogue/stage_slots.py<br/>管线槽位: 四槽位 sentinel + 三层解析"]
    chat --> base["dialogue/base.py<br/>PipelineStage<br/>DialogueContext<br/>ModuleJumpEvent"]
    chat --> store["chat/store.py<br/>SessionStore(SQLite)"]

    chat --> loop2["chat/loop.py<br/>Agent ReAct 循环<br/>工具授权过滤 · 借出工具解析<br/>transfer 写跳转事件"]
    loop2 --> msgs["chat/messages.py<br/>AGENT messages 构建<br/>module.messages_builder 槽位解析"]
    loop2 --> agents["chat/agents.py<br/>AgentRunner 协议<br/>(默认 LoopAgentRunner)"]

    preg --> pattern["dialogue/pattern.py"]
    pattern --> base

    slots --> nlu["dialogue/nlu/nlu.py<br/>FSMNLU · RouteNLU"]
    slots --> nlg["dialogue/nlg/nlg.py"]
    slots --> uni["dialogue/unified.py<br/>统一阶段(单次调用 NLU+NLG)<br/>FSMUnifiedNLU · RouteUnifiedNLU · PassThroughNLG"]
    nlu --> resolve["llm/resolve.py<br/>build_provider()"]
    nlg --> resolve
    uni --> resolve
    loop2 --> resolve
    loop2 --> treg
    lc --> base

    resolve --> llmreg["llm/register.py<br/>Provider 注册中心"]
    llmreg --> oai["llm/openai_provider.py<br/>OpenAICompatible"]

    subgraph 应用层
        xianyuagent["dialogue/xianyu_agent_route.py<br/>(闲鱼客服 pattern<br/>复刻 xianyu-auto-reply)"]
        kbagent["dialogue/knowledge_agent_route.py<br/>(知识库客服 pattern<br/>工具调用型 AGENT)"]
        tools["tools/calculator_tool.py<br/>weather_tool.py<br/>knowledge_tool.py"]
        clarify["src/clarify/<br/>偏题澄清"]
    end
    subgraph 存储层
        kbs["database/knowledge_store.py<br/>(SQLite 知识库<br/>scope 隔离)"]
    end
    kbagent -.-> preg
    kbs -.-> tools
    kbagent -.-> preg
    kbs -.-> tools
    xianyuagent -.-> preg
    tools -.-> treg
```

**依赖方向（不许反向）**：`main → chat → dialogue(base/stages) → llm`；应用层只通过 registry 挂进来。

## 核心概念

- **Pattern**：一个完整对话流程（如 xianyu_agent），由多个 Module 组成；stages 声明管线骨架（具体 stage 原样执行 + 槽位混排），另设 pattern 级四槽位默认（generate/query/pre_recall/post_recall，作三层解析的第三层）
- **Module**：三种类型 `ROUTE`（菜单分发）/ `FSM`（状态机）/ `AGENT`（自由对话+工具）
- **Node**：FSM/ROUTE 内的状态节点；`sub_nodes` 构成转移图；节点级 NLU/NLG 可覆盖模块级；节点级四槽位配置（generate/query/pre_recall/post_recall）全路径生效——含 ROUTE 菜单节点：generate 的 nlg 部件在 chat 层检测推进菜单节点后按菜单节点解析
- **模块跳转（ModuleJumpEvent）**：`dialogue/base.py` 定义的跳转事件，`cxt.actions` 为唯一载体，
  chat 编排器统一消费（无 dispatch 原语 / 邻接图 / 回弹拒绝——目标存在于 `module_map`
  即合法，边界淡化，agent transfer 与 ROUTE 菜单分发同构）。**事件只由两个入口产生**：
  - **ROUTE 跳到新模块**（stage 循环内每个 stage 执行后检测，
    `chat.ModuleJumpChannel.detect_after_stage`，仅 ROUTE 模块）：先按
    `nlu_result.next_node` 推进菜单节点并做 R4 节点级 LLM 配置当轮刷新；
    随后两类跳转来源——`nlu_result.jump_module`（NLU 直接输出，prompt 契约见
    ROUTE_NLU_DEFAULT_PROMPT 的 `{__jump_modules__}` 清单）优先，其次推进后节点的
    `node.jump_module` 配置。命中即合并槽位、写事件、**中断剩余 stages**（源模块静默，
    NLG 不执行）
  - **AGENT transfer 工具调用**：`loop.run_agent` 直接写事件返回（目标不在
    module_map 时错误回填 tool result 继续 loop）
  - **FSM 不产生事件**：clarify 在循环内由 ClarifyStage 处理（覆写 nlg_result，
    不出循环），节点跳转由轮末 `_fsm_node_transition` 处理
  - **消费**（`chat_turn` 的 hop 循环）：`ModuleJumpChannel.pop` 取出（消费即移除）→
    `reroute` 写 `current_module_code`、置空 `current_node_code` → 目标模块同轮
    续答；跳到 AGENT/FSM 后目标模块跨轮承接后续轮次（agent 靠 history、FSM 靠
    节点位置），不回路由——只有当前还停在 ROUTE 模块的轮次才轮末重置回 root
    （菜单节点无 sub_nodes，不重置则下一轮路由候选为空）。
    `max_hops`（默认 2）耗尽时消费残留事件后 force_close 强制收尾
    （跳过检测、不注入 transfer 工具、prompt 追加"勿再移交"）
  - **观测**：事件实例 `to_dict()` 后快照进 `ChatResult.actions`（hop 消费后不残留；
    仅超跳数未消费的事件可见）
- **管线槽位轴（stage_slots.py）**：`pre_recall → query → post_recall → generate` 四槽位；
  执行期三层解析 node > module > pattern，层配置非法（dict 缺键/多键/值非法、stage 无 execute）
  整层降级，全空时召回/改写槽位 no-op、generate 落 builtin（FSMNLU/FSMNLG 或 RouteNLU/RouteNLG）。
  generate 双形态：单 stage（unified 一次调用）或 dict `{"nlu":…, "nlg":…}`；展开为 nlu/nlg 两个
  惰性子部件，各自在执行时刻解析（FSM+enable_clarify：`[nlu, ClarifyStage, nlg]`；
  ROUTE 与 FSM 默认同形，菜单节点推进/跳转检测由 chat 层在 nlu 部件后做）
- **PipelineStage**：可插拔管线步骤，`execute(ctx) -> ctx`；ctx 即 `DialogueContext` 全程数据载体
- **统一阶段（unified.py）**：单次调用 + structured output 的 NLU+NLG 合一形态——一次 LLM 调用产出 `{"reply","next_node","slots"}`，拆写 `ctx.nlu_result`/`ctx.nlg_result`；`next_node` 由代码按合法转移边硬校验（开启 `enable_clarify` 的模块放行 `"clarify"`，ClarifyStage 在 generate 展开的 nlu/nlg 部件之间执行）。module 级注入（`generate=FSMUnifiedNLU()/RouteUnifiedNLU()`，generate 单 stage 形态，nlu 部件整体执行、nlg 部件 no-op），替换默认两阶段（每轮 2 次调用 → 1 次；澄清轮 2 次，与两阶段+澄清持平）
- **Session**：持有 `cxt`（DialogueContext）；每轮更新 `user_query`，轮末回写状态
- **chat 编排器（chat/chat.py）**：`chat()` 兼容入口（返回 str）/ `chat_turn()` 全量入口
  （返回 `ChatResult`：text + actions）。职责收窄为轮次编排：session 定位 → lifecycle
  轮首重置 → 定位入口模块 → 同轮 hop 循环（按 AGENT/FSM/ROUTE 分派单模块处理，
  消费 ModuleJumpEvent 重路由）→ 轮末 history 追加 + 产出快照。单模块处理内联在
  本模块：AGENT → `loop.run_agent`（R2 刷新）；FSM → stages + `next_node` 轮末跳转（R3）；
  ROUTE → stages + 轮末重置 root。LLM 配置每轮按当前位置刷新（R1 在 chat_turn 开头）
- **cxt 字段生命周期（chat/context_lifecycle.py）**：`TurnLifecycle` 声明式四类集合，
  改集合即改政策：
  - PERSISTENT（跨轮绝不删）：history / current_module_code / current_node_code /
    filled_slots / task_basic_info / node_map / module_map / metadata 四键
    （bargain_settings, task_info, llm_override, pattern_code）
  - PER_TURN_RESET（每轮轮首）：user_query 覆写、nlu/nlg/agent_result、
    pre/post_recall_results、rewritten_queries、actions、metadata 一键（unified）。
    begin_turn 每轮恰好一次、hop 之间绝不调（actions 里的跳转事件需同轮存活至
    hop 循环消费）
  - INCREMENTAL（增量）：history 追加（end_turn）、filled_slots 合并（merge_slots）
  - STAGE_MANAGED（轮首重置）：clarify（ClarifyStage 每轮自置）、served_by_projection
    （agent 借投影答轮记账，不跨轮残留）
- **SessionStore**：SQLite write-through 审计流水（sessions 快照 + messages 行级消息），
  兼重启恢复数据源；治理仍在内存，DB 非事实源（`chat/store.py`）
- **Channel**：外部消息源适配层（`src/channel/`，webhook 回调型）。声明式
  ChannelSpec（载荷 schema/session 派生/task_info 映射/成功响应契约，`base.py`）
  + 第 4 个 registry（AST 自动发现，`register.py`）+ 通用 handler
  （token 校验/过期过滤/get-or-create/session 前缀/错误码固定契约，
  `webhooks.py`，结构上不可绕过）；引擎操作经 EngineOps 由 main.py 注入，
  channel 模块不感知会话治理与 LLM。闲鱼适配把 `(account_id, chat_id)`
  派生为稳定 session_id 并对不存在会话自动 launch（get-or-create），
  错误一律非 200（对方 parse 契约：非 200 不发送）
- **xianyu_agent pattern**：闲鱼卖家客服流程（`dialogue/xianyu_agent_route.py`），
  复刻 xianyu-auto-reply 的 agent 对话管理：单 RouteModule 内 XianyuIntentNLU
  （本地关键词意图检测 price/tech/default，零 LLM，模块级 generate dict 的 nlu 位）+ 意图级
  节点 `base_nlg_prompt`（议价/技术/通用三套模板）+ 议价轮数控制（user 消息
  metadata 回标 intent 计数，达上限切拒绝节点走 FixedNLG 固定话术，零 LLM）。
  ROUTE 轮末回 root 与原实现"每条消息独立检测"同构；议价设置经
  `ctx.metadata["bargain_settings"]` 注入（账号级配置入口）

## 公共契约（改动需走内核流程）

| 契约 | 位置 | 说明 |
|---|---|---|
| `PipelineStage.execute(ctx)` | `dialogue/base.py` | 所有 stage 的唯一接口 |
| `resolve_stage(stage, ctx, module, pattern)` | `dialogue/stage_slots.py` | 槽位三层延迟解析器（node > module > pattern；校验整层降级；generate 双形态展开为惰性子部件） |
| `DialogueContext` 字段 | `dialogue/base.py` | stage 间数据交换全部经由 ctx，不另开通道 |
| `ModuleJumpEvent` | `dialogue/base.py` | 模块跳转事件（target_module_code/reason/source）：stage 循环检测 / agent transfer 写入 `cxt.actions`，chat 层 hop 循环消费重路由；`to_dict()` 为观测形态 |
| `registry.register()` 自注册 | `dialogue/register.py` `tools/register.py` `llm/register.py` | 应用层接入框架的唯一方式（AST 扫描发现） |
| `build_provider(llm_config)` | `llm/resolve.py` | 所有 LLM 调用的统一入口 |
| `get_llm_config(pattern_code, module_code, node_code, override)` | `config/config.py` | LLM 配置解析入口：`llm_providers` 连接层 ⊕ `llm_default`/`pattern_llm` 三层编排；`ctx.metadata["llm_override"]`（CLI 手动选择）最高优先级；chat 层每轮按当前位置刷新（R1-R4） |
| `run_agent(session, module, llm_config)` | `chat/loop.py` | Agent 模块对话循环入口（返回 TurnResult）；transfer 命中时事件写入 `cxt.actions`、reply 为空；`conversation()` 为兼容 wrapper |
| `chat_turn(query, session_id, all_sessions, agent_runner=None) -> ChatResult` | `chat/chat.py` | 全量轮次入口：text + actions；`chat()` 为兼容入口（等价 `.text`） |
| `AgentRunner.run(session, module, llm_config, force_close)` | `chat/agents.py` | AGENT 后端插件协议（默认 `LoopAgentRunner` 委托 loop.run_agent）；经 chat 入口可选参数注入 |
| `build_agent_messages(module, system_prompt, cxt)` | `chat/messages.py` | AGENT 模块 LLM messages 构建入口：`module.messages_builder`（`(system_prompt, cxt) -> messages`，system_prompt 为终态含 force_close 后缀）优先；未设/不可调用（告警）降级 `default_build_messages`（system + user/assistant 历史）。自定义 builder 遵守不可信数据纪律：外部文本不写入 system 角色 |
| `TurnLifecycle` 字段集合 | `chat/context_lifecycle.py` | cxt 字段生命周期唯一管理者：PERSISTENT / PER_TURN_RESET / INCREMENTAL / STAGE_MANAGED 四类声明式集合 + begin_turn / end_turn / merge_slots |
| `SessionStore` | `chat/store.py` | launch/轮末落盘、startup 恢复、审计查询；实例由 main.py 注入，非全局单例 |
| `KnowledgeStore` | `database/knowledge_store.py` | 知识库（商品/客服知识）连接持有者：scope 隔离（`{channel}:{account_id}`）、jieba 分词 LIKE 检索、`format_result` 消毒（untrusted 包裹，提示注入防御边界）；`get_knowledge_store()` 懒持有，main.py lifespan 释放；路径配置 `knowledge_db_path`（缺省 `data/knowledge.db`）。存储定义（表 DDL / 未来 ES 等 schema）统一放 `database/` 路径。**已知债务**：工具的 `account_id` 是 LLM 参数（从系统提示「任务信息」抄写）而非可信注入，真实渠道接入时需演进为 dispatch 侧身份注入 |
| `ChannelSpec` 协议 + `build_channel_router(spec, ops)` | `channel/base.py` `channel/webhooks.py` | 外部消息源适配的唯一形态：渠道声明差异 + 通用 handler 共性流程；`registry.register()` 自注册（AST 发现），main.py `discover_builtin_channels()` + `build_channel_routers(EngineOps(...))` 接线 |

## 什么代码放哪

- 新业务对话流程 → `src/dialogue/<name>_route.py`，模块级 `registry.register()`
- 新工具 → `src/tools/<name>_tool.py`，自动被 AST 发现；工具需要持久化存储时在 `database/` 下建 store 模块（照 `knowledge_store.py` idiom：原生 sqlite3 + 锁 + WAL，scope 隔离，输出过 `_clean_untrusted` 消毒），工具层只消费不定义存储
- 知识库填充演示数据 → `.venv/bin/python cli.py knowledge-seed --scope="xianyu:<account_id>"`（幂等）
- 新 LLM provider → `src/llm/<name>_provider.py`
- 新外部消息渠道（channel）→ `src/channel/<name>.py`，实现 ChannelSpec（`payload_model`/`parse`/`build_reply` + 环境变量声明）并模块级 `registry.register()`，AST 自动发现，main.py 无需改动；默认 pattern/token 走环境变量（如 `XIANYU_CHANNEL_PATTERN`）
- 新管线阶段 → `src/dialogue/<stage>.py` 继承 `PipelineStage`
- 换 agent 后端（如 planner-executor / 外部 agent 服务）→ 实现 `AgentRunner`
  协议（`chat/agents.py`），经 `chat()/chat_turn(agent_runner=...)` 注入；不加 registry
- 调整 cxt 某字段的轮次归属（跨轮保留 / 每轮重置）→ 改 `TurnLifecycle` 声明式集合
  （`chat/context_lifecycle.py`），不动流程代码
- 模块要单次调用（NLU+NLG 合一）→ module 上配 `generate=FSMUnifiedNLU()/RouteUnifiedNLU()`（见 `tests/test_unified_stage.py` 的内联示例；候选节点需声明 `answer_examples`）
- AGENT 模块要自定义发给 LLM 的 messages（截断历史 / few-shot / 注入动态数据）→ module 上配 `messages_builder=fn`，签名 `(system_prompt, cxt) -> messages`（见 `src/chat/messages.py`）
- 全局 prompt 模板 → `src/prompt.py`（node/module 可覆盖）

## 测试

`tests/` 全离线（fake_provider 打桩 LLM）：`export DASHSCOPE_API_KEY=... && .venv/bin/python -m pytest tests/ -q`。
改框架后必须全绿再提交。
