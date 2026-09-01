# 纯 Agent Pattern：Module Dispatch 设计（inject / transfer 双原语）

- 日期：2026-09-01
- 状态：待评审
- 分层归属：框架扩展（module.py / loop.py / chat.py 部分）+ 框架内核（chat.py 重入循环）
- 前置讨论：本 spec 源自多轮头脑风暴，推翻了早期"handoff + next-turn"方案，收敛为"inject 投影 + transfer 静默转移"双原语

## 1. 背景与目标

现有 `car_sales_route.py` 验证了 FSM / ROUTE 两种 pattern。本设计补齐第三种：**纯 agent pattern**——多个 AgentModule 组成团队，用户全程无"转接感"，且为后续语音场景（DM 预算 500~800ms）预留延迟最优路径。

### 1.1 体验目标（硬性约束）

1. **同轮回复（same-turn）**：跨域问题当轮得到实质回复，不允许"帮你转接XXX"这类话术占一轮
2. **源 agent 回答**：inject 场景下 A 保持人格直接回复；transfer 场景下 B 直接接话，用户对切换无感知
3. **语音预算**：inject 路径零额外 LLM 调用（最优）；transfer 路径 +1 次调用（B 流式接话）

### 1.2 能力模型

一个 module 的能力 = 知识（头部描述）+ 工具（use_tools）+ 流程（内部 FSM/prompt 深层）。
- 知识和工具**可投影**给邻接 module（inject）
- 流程**不可投影**——深入流程必须 transfer，这是应保留的边界，不是缺陷

### 1.3 双原语定义

| 原语 | 谁说话 | 控制权 | 适用 |
|---|---|---|---|
| **inject** | 源 agent A | A 保持持有 | 一句话能答 / 单次工具能解决的跨域请求 |
| **transfer** | 目标 agent B | 移交给 B，sticky 持有 | 多轮深入流程 |

编排模式兼容性（Azure 五模式）：框架只需实现这两个原语（含嵌套/并行/环防护），Sequential/Concurrent/Group chat/Magentic 是原语之上的编排配方（prompt 层组合），不在本 spec 范围。

## 2. 数据模型

### 2.1 ModuleLink（新增，src/dialogue/module.py）

```python
@dataclass
class ModuleLink:
    target: str                                        # 目标 module_code
    lend_knowledge: bool = True                        # 借出头部投影
    lend_tools: List[str] = field(default_factory=list)  # 按名借出工具
```

`BaseModule.sub_modules` 兼容升级（不改构造签名，`__init__` 内归一化）：

- 旧写法 `sub_modules=["after_sales"]` 自动包装为 `ModuleLink(target, lend_knowledge=True, lend_tools=[])`
- 默认 `lend_knowledge=True`：纯 transfer 边是少数（敏感域显式配 False），知识投影零权限风险
- **目标为 FSM/ROUTE module 时知识投影同样默认开启**（与 AGENT 目标同规则）：投影内容仍取模块头部（description / todo / answer_examples），不含节点层信息；FSM/ROUTE 通常无 use_tools，lend_tools 自然为空

**开关语义**：不配置投影（`lend_knowledge=False, lend_tools=[]`）的边只剩 transfer 语义。不设独立 `enable_inject` 布尔字段。

### 2.2 一字段两职责

归一化后的 `sub_modules: List[ModuleLink]` 同时是：
- **转移图边集**：transfer 工具的合法目标集合（不在图中的目标 LLM 不可见）
- **投影清单**：每条边的 lend 配置决定注入多少

### 2.3 BaseModule 新增字段

`answer_examples: Optional[List[str]]`（可选，默认空）——AGENT module 头部级回答范式，供投影块使用（对齐 node 的同名字段语义）。

### 2.4 Pattern 注册期建图与 fail fast（src/dialogue/pattern.py）

`Pattern.__init__` 构建 module_map 后追加：
- 构建 `dispatch_graph: Dict[str, Set[str]]`
- ROUTE 菜单节点的 `jump_module` 推导进图（source=route_menu）
- 校验（不通过即 raise，拒绝进 registry）：
  - 悬空边：`link.target` 不在 module_map
  - 越权借出：`link.lend_tools ⊄ target.use_tools`
  - 自环：`link.target == module_code`
  - AGENT module 的边指向 FSM/ROUTE module 合法（混合 pattern），不拦

### 2.5 跨类型统一跳转（混合 pattern）

dispatch() 对目标 module 类型无感知，AGENT / FSM / ROUTE 统一转移语义：

- **transfer 目标为 FSM**：dispatch 落在模块入口（`current_node_code=None`），重入循环以该 FSM 的 pipeline（NLU→NLG）消费**同一句 query**，从首节点起流程；reason 已随 tool result 落 history，FSM 的 NLU/NLG 模板经 history 槽位自然获得承接上下文，无需专用注入点
- **transfer 目标为 ROUTE**：route NLU 分类后若命中带 `jump_module` 的菜单节点，再次产生 dispatch 事件，重入循环继续——agent→route→fsm 可在一次 `MAX_HOPS=2` 内组合完成；菜单节点的 answer_examples 由 pattern 作者按无感体验书写（框架不强制改写话术）
- FSM/ROUTE 目标不执行工具借出（它们没有 agent loop，lend_tools 无意义）；若误配，`lend_tools ⊄ target.use_tools` 校验因二者通常 use_tools 为空而天然拦截

## 3. 执行流

### 3.1 chat 层重入循环（src/chat/chat.py）

```python
for hop in range(MAX_HOPS):        # 文本=2，语音=1
    module = cxt.module_map[cxt.current_module_code]
    result = run_module(session, module)
    if result.dispatch is None:
        cxt.add_message("assistant", result.reply)
        return result.reply        # 唯一用户出口
    # dispatch 发生：源 reply 不出口（保留进 history），reason 进 history
    # 以目标模块消费同一句 query 重跑
# 超跳数：当前模块强制收尾（prompt 追加"直接回应用户，勿再移交"）
```

### 3.2 TurnResult 与 run_module 统一执行器

`TurnResult(reply: Optional[str], dispatch: Optional[ModuleDispatch])`：

- AGENT 路径 `run_agent()`（loop.py）：多轮 tool calling 中遇 transfer tool call → 调 dispatch() → **立即 return**，不内部续跑
- FSM 路径 `run_pipeline()`：现有 NLU→NLG 逻辑，不产生 dispatch（FSM 节点内转移走 next_node，不经 dispatch）
- ROUTE 路径 `run_pipeline()`：route NLU 分类 → **跳过 route NLG，直接产生 dispatch 事件返回**（静默分发）——目标模块在重入循环中消费同一句 query；route NLG 仅在分类命中无 jump_module 的菜单节点（如闲聊）时执行，即 ROUTE 作为转移链终点时才说话

现有 `[jump xx]` 文本标签机制整体移除（2026-09-01 终审修订：grep 零残留，无消费方，不做兼容正则）。

### 3.3 run_agent 细节（src/chat/loop.py）

工具列表 = 自有工具 + 借入工具（原样 schema，不重写 description）+ transfer 工具（逐边生成）。

借入工具执行：走全局 `tool_registry.dispatch`；结果 metadata 记 `lent_by: <target>`（审计 + 领域记账）。

工具调用轮内的处理：
- 普通工具 / 借入工具 → 执行、回填、继续（现有逻辑）
- transfer_to_X → dispatch 校验（二次防线）→ return TurnResult(dispatch)

A 在 transfer 轮若输出了 content：**不出口，但保留进 history** 给 B 参考（内部历史 ≠ 用户可见，信息单向无损）。

## 4. Prompt 组装（src/prompt.py 新增模板常量）

system prompt 五块结构（module > default 两级覆盖，复用 resolve 思路）：

1. `module.base_prompt` —— A 自己的人格
2. **邻接投影块**（每条 lend_knowledge 边一片）：

   ```markdown
   ## 邻接能力：售后维保（after_sales）
   - 定义：保养预约、维修工单、保险理赔的查询与办理
   - 职责：查改保养预约、跟踪维修工单进度
   - 回答范式：「已为您把保养预约改到{时间}，请按时到店。」
   - 可借工具：reschedule_maintenance, query_workorder
   ```

3. **团队规则块**（框架默认模板）：单步直接答 / 深入才 transfer / transfer 轮不对用户说话
4. **承接块**（transfer 后 B 的首轮条件注入）：reason + "直接以自己身份接续回复，不要描述转接过程"
5. **记账回看块**（A 上轮借答过则注入）："继续该话题→简单追问继续答，深入→transfer"

transfer 工具 schema 的 description 由目标 module 头部生成，**分工规则写进工具 description**（"不适用：一句话或单次工具能解决的请求——那类直接自己处理"）。

prompt 组装后 log 总长度，超阈值 warning（投影膨胀观测，不做自动裁剪）。

## 5. 错误处理

| 失败面 | 防 | 兜底 |
|---|---|---|
| 幻觉转移目标 | 工具仅按邻接图生成，非法目标不可见 | dispatch() 二次校验，错误回填 tool result，loop 继续 |
| 移交环 | 注册期拒绝自环；轮内 MAX_HOPS | 超限强制收尾；同轮 A→B→A 回弹 dispatch_log 检测拒绝；跨轮 dispatch_log 已清、返回原模块合法（sticky 逃生语义，2026-09-01 终审修订与实现对齐） |
| 借入工具失败 | — | 与自有工具同路，`_execute_tool` 现有捕获回填，LLM 自行换路；`lent_by` 照记 |
| 投影膨胀 | lend_tools 按名限量；知识块 3-4 行硬上限 | 组装后超长 warning；不自动裁剪（该修拓扑） |

## 6. 状态与记账（DialogueContext 扩展，src/dialogue/base.py）

- `dispatch_log`（metadata 内）：转移链，防环 + 可观测
- `served_by_projection`（metadata 内）：A 借答时记录来源域，驱动记账回看块
- sticky 语义：transfer 后 `current_module_code` 停留 B，直至 B 显式再 transfer；不自动回收
- `MAX_HOPS`：Pattern 级可配（`max_hops` 属性，默认 2）；语音场景由启动层设 1

### 6.1 tool 往返落 history（顺手修复的现存缺陷）

`loop.py` 的 messages 现为局部变量，tool call 往返不落 `cxt.history`。本设计将 tool call + result 以 `SessionMessage(role="tool")` 落 history（dataclass 已支持 tool role，字段早已预留）——**交付范围：tool 往返落 history 供审计与落盘（store messages 表持久化）；跨轮 LLM 回放（assistant tool_calls 对注入下一轮 messages）不在本设计范围**（2026-09-01 终审修订：落地回放需扩 SessionMessage 落 assistant tool_calls 结构，改动面大且增益有限，defer 至有明确长程工具记忆需求时另行设计）。

## 7. 测试策略

三层：
1. **注册期**（纯离线）：ModuleLink 归一化 / 转移图构建 / 三类 fail fast / 投影文本生成
2. **执行流**（mock provider 脚本化响应）：inject 路径（借答+记账+lent_by）/ transfer 路径（重入+承接块+A content 不出口）/ 防环 / 幻觉目标 / sticky
3. **端到端冒烟**（真实 key）：`car_sales_agent` 演示 pattern（reception + sales_consult + after_sales + complaint，含全投影/纯知识/纯 transfer 三类边），3 条脚本化对话人工评审

## 8. 改动清单

| 文件 | 改动 | 分层 |
|---|---|---|
| `src/dialogue/module.py` | ModuleLink + sub_modules 归一化 + answer_examples | 框架扩展 |
| `src/dialogue/pattern.py` | dispatch_graph 建图 + 注册期校验 | 框架扩展 |
| `src/dialogue/base.py` | DialogueContext 记账字段（metadata 约定） | 内核（轻） |
| `src/chat/chat.py` | 重入循环 + run_module + TurnResult | 内核 |
| `src/chat/loop.py` | run_agent 改造：投影工具/transfer 拦截/ToolMessage 落盘 | 内核 |
| `src/prompt.py` | 投影块/团队规则/承接/回看模板常量 | 应用 |
| `src/dialogue/car_sales_agent.py` | 演示 pattern（新） | 应用 |

## 9. 不做的事（YAGNI 边界）

- 不做 call/return（agent-as-tool）原语——单步工具 A 借入直调，多步流程 transfer，中间态无不可替代价值
- 不做五种编排模式的专用代码——均为两原语的 prompt 层配方
- 不做投影自动裁剪、module 级 inject 总开关、非 sticky 持有模式
- 语音专属优化（流式 provider、per-stage 小模型）另行 spec，本设计预留接口（MAX_HOPS 可配、inject 零调用路径）
