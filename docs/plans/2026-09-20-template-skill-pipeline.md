# 设计共识：话术模版生成链路（元 skill × hermes-nexus）

日期：2026-09-20 ｜ 状态：设计已确认（三轮设计拷问收敛），待拆实施计划 ｜ 涉及分层：应用层（`main.py` 新端点、新 `templates/` 目录）+ 应用层 skill 资料（`interactive-task-skill-generator/`）

---

## 0. 目标与共识（决策记录）

改造 `interactive-task-skill-generator`（元 skill + 模板骨架）并扩展 hermes-nexus 服务端：
元 skill 调研领域后产出**话术模版**（声明式 pattern JSON）与**领域交互子 skill**；
子 skill 懒注册模版到服务端、触发自主任务会话（服务端 pattern 与对端多轮交互）、
轮询结果并做后置判断。经三轮设计拷问收敛的 18 项决策：

| # | 决策点 | 结论 |
|---|---|---|
| Q1 | FSM/Agent 选择 | 元 skill 调研后自动判断（结构化流程→FSM，开放对话→Agent；可混排），给出推荐+理由，注册时可覆盖 |
| Q2 | 调研素材 | 混合：可选资料目录输入，缺失部分 web 搜索补齐 |
| Q3 | 校验深度 | schema + 结构 + 引用三层，**collect-all**（收集全部错误一次返回，带 JSON 路径定位），非首个错误即停 |
| Q4 | API 安全 | 绑定 127.0.0.1、无鉴权、请求来源字段日志留痕（本机自用工具） |
| Q5 | 轮询语义 | 分层返回：status + 最终 result + 中间过程（当前模块、最近消息），对调试 FSM 有用 |
| Q6 | 生效方式 | 模版=数据，动态加载即生效（无需重启）；生成的客户端代码为独立 skill 目录，不进服务进程 |
| Q7 | "两个 skill" | `interactive-task-skill-generator/SKILL.md`（生成器）与 `templates/domain-skill-template.md`（子 skill 骨架）都改 |
| Q8 | 架构反转 | **话术模版 = 服务端 pattern**，对话逻辑从 skill 侧下沉到 hermes-nexus；子 skill 退化为薄客户端。旧"skill 侧 4 模块话术"作废 |
| Q9 | 注册机制 | 声明式 JSON → 校验 → 编译 Pattern → **动态注册进现有 PatternRegistry**（不新增第五个 registry，不动 AST 发现）；原始 JSON 落盘持久化、启动重放；同 code 重复注册 = 覆盖更新 |
| Q10 | 校验实现 | 独立 collect-all 校验器（不动框架内核）；Pattern 真实构造仅作最终冒烟确认 |
| Q11 | 驱动模型 | **服务端自主跑完**（fire-and-forget）：launch 带任务上下文 + 对端配置，pattern 后台跑到终态，调用方纯轮询；现有同步 `/api/v1/chat` 不动 |
| Q12 | 子 skill 定位 | 与【用户】对话收集需求 → 模版对齐（查→无则注册）→ 触发任务 → 轮询 → 后置判断汇报。注册 API 的实际调用方是子 skill（懒注册），不是元 skill |
| Q13 | 版本一致性 | **本地为准**：查询接口返回内容 hash；子 skill 发现 hash 不一致即覆盖重注册 |
| Q14 | 结果结构 | 结构化快照 + 原始消息：`{finish_reason: end_module\|max_turns\|timeout\|error, end_module_code, filled_slots, turn_count, messages}`；语义判断（代聊成败）留给子 skill 后置判断阶段 |
| Q15 | mock 对端 | `counterpart: {mode: "scripted"\|"llm"\|"channel", script?, role_prompt?}`；scripted（脚本逐轮）与 llm（角色扮演）都实现，channel（真实渠道）枚举预留不实现 |
| Q16 | 预校验 | 新增 validate-only 端点（同一套 validator）；元 skill 交付前自校验，模版不合法不交付 |
| Q17 | 服务探活 | 新增 `GET /api/v1/health`；子 skill 脚本探活 → 尝试自动拉起（nohup）→ 手动指引兜底。顺带核实修正 README 启动方式与实际代码的出入 |
| Q18 | 旧产物 | `interactive-task-food/`、`interactive-task-car-sales`（`~/.hermes/`）本轮不动，标记旧架构参考；新链路跑通后用真实领域重新生成参考实现。`chinese-poi-search/references/late-night-filtering.md:37` 疑似泄露高德 Key，顺手换环境变量占位 |

---

## 1. 端到端链路

```
① 用户："帮我做个 xxx 领域的代聊机器人"
   → 元 skill 生效（hermes-nexus 假设已在后台运行）
   → 调研领域 → 产出话术模版（pattern JSON）+ 领域子 skill（含模版副本）
   → 交付前自校验（调 validate 接口，不合法不交付）

② 用户："帮我代聊"
   → 子 skill 生效，与【用户】多轮对话收集本次任务需求（对象/约束/底线）→ task_info
   → （可选）resolver 筛选候选对象（如 chinese-poi-search 找餐厅）
   → 模版对齐：GET templates/{code} → 不存在或 hash 不一致 → POST templates 注册/覆盖
   → 触发自主任务：POST /tasks（task_info + counterpart 配置）

③ 服务端：pattern 与【对端】多轮交互（scripted / llm mock）
   → 期间开放 API 查询任务状态与会话历史
   → 终态（is_end 模块 / max_turns / timeout / error）

④ 子 skill 轮询拿结果 → 后置判断（语义判断成败）→ 向用户汇报
```

**分工原则**：skill 侧对话 = 收集用户需求；服务端对话 = 代聊对端；
话术逻辑全部下沉服务端，skill 不再内置话术。

---

## 2. 服务端新增（hermes-nexus）

### 2.1 端点（挂 `main.py`，沿用 `{code, message, status, data}` 响应包）

| 端点 | 作用 |
|---|---|
| `GET /api/v1/health` | 探活 |
| `POST /api/v1/templates/validate` | 只校验不注册，返回全量 errors/warnings（每条带 JSON 路径定位） |
| `POST /api/v1/templates` | 校验 → 编译 → 动态注册 → 落盘 |
| `GET /api/v1/templates/{code}` | 存在性 + 内容 hash + 基本信息 |
| `POST /api/v1/tasks` | 触发自主任务：`{pattern_code, task_info, counterpart, max_turns?, timeout_s?}` → `{task_id, session_id}` |
| `GET /api/v1/tasks/{task_id}` | 轮询：status（pending/running/done/failed）；运行中给 current_module/recent_messages；终态给 result 快照 |

现有 `GET /api/v1/sessions/{id}/messages` 复用为会话历史查询，不新做。

### 2.2 新组件

- **模版编译器**：JSON → Pattern/Module/Node 对象（FSM/ROUTE/AGENT 混排）
- **collect-all 校验器**（三层）：
  1. JSON schema 层：字段齐全、类型正确、枚举合法
  2. 结构层：转移引用的状态/模块存在、可达性、无死循环、存在 is_end 模块、入口模块存在
  3. 引用层：use_tools 引用的工具在 tools registry 存在、pattern_llm 配置交叉校验
- **持久化**：`data/templates/{code}.json` 原子写；启动时扫描重放注册
- **自主执行引擎**：后台线程跑 pattern↔对端循环（对端回复作为下一轮 query 喂入
  `chat_turn`；llm 对端按 llm_default 解析），is_end / max_turns / timeout / error 即停；
  任务记录入 SQLite

### 2.3 声明式模版的表达力边界

JSON 模版表达：prompt（base/node 级）、槽位、节点、转移边、工具 ACL、子模块边、
四槽位 stage 配置。**不表达 Python 代码**（自定义 messages_builder、agent_hooks 一律
走默认实现）——校验器的明示规则，属声明式模版能力上限；需要代码级定制时手写
pattern 文件走 AST 发现（如 customer_agent 的老路）。

---

## 3. 元 skill 新工作流（G1–G5 重写）

- **G1 领域调研**：混合素材 → 领域定义 + 6 维启发式（沿用 domain-heuristics.md）+
  **FSM/Agent/混排推荐与理由**（Q1）
- **G2 对端分析**（新增）：代聊对象是谁、怎么扮演（对端角色 role_prompt 建议、
  scripted 脚本要点）；resolver 策略沿用 poi-search → 其他 skill → 公开 API → 用户提供优先级链
- **G3 话术模版设计**：产出 pattern JSON（模块/节点/转移/prompt/槽位/工具）
- **G4 子 skill 生成**：
  - SKILL.md（新五段流程：需求收集 → 模版对齐 → 触发 → 轮询 → 后置判断）
  - `references/template.json`（模版副本，Q13 的本地基准）
  - `scripts/trigger_task.py`（探活→自动拉起→hash 对齐懒注册→触发→指数退避轮询；
    纯标准库无三方依赖；绝对路径，修掉旧相对导入 P0）
  - 可选 resolver 脚本；安装 `~/.hermes/skills/productivity/interactive-task-{domain}/` 走 skill_manage
- **G5 交付前自校验**：调 validate 接口硬门槛（不合法不交付）+ 结构一致性检查
  （冲突字段存在性等，适配新 schema）

`templates/domain-skill-template.md` 按子 skill 新流程整体重写；旧模板中过时内容
（不存在的 `/api/v1/health` 旧文档、已打平的 `src/` 路径、同步 chat 调用）全部清除。

---

## 4. 非目标（本轮不做）

框架内核改造、真实渠道接入（channel 仅预留枚举）、鉴权、多版本模版并存、
food/car-sales 升级迁移。

---

## 5. 实施顺序（草案，细化见后续实施 plan）

1. 服务端模版链路：health + validate/register/get + 编译器 + 校验器 + 落盘重放
2. 服务端任务链路：tasks 端点 + 自主执行引擎 + 轮询
3. 元 skill 与模板重写（与 schema 对齐）
4. 选一个真实领域全链路试跑（作为新参考实现，接替 food 的地位）
5. 测试沿用 `tests/` 离线风格（fake_provider 打桩 LLM）
