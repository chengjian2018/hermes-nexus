# hermes-nexus

人机交互 Mock 服务：**Pipeline 式对话引擎 + FastAPI 服务**。

一个用于快速搭建、调试和验证多形态对话流程的框架——FSM 状态机、ROUTE 菜单分发、AGENT 自由对话（ReAct + 工具调用）三种模块形态可在一个 Pattern 内混排，管线各槽位（召回/改写/生成）可插拔，LLM Provider、对话 Pattern、工具、外部消息渠道全部走注册机制自动发现。

## 核心特性

- **三种模块形态**
  - `FSM`：状态机流程，节点间显式转移
  - `ROUTE`：菜单分发，按意图路由到子节点
  - `AGENT`：自由对话 + 工具调用（ReAct 循环，工具授权过滤）
- **管线槽位轴**：`pre_recall → query → post_recall → generate` 四槽位，node > module > pattern 三层延迟解析；generate 支持双形态（NLU+NLG 两阶段，或 unified 单次调用合并产出）
- **偏题澄清（clarify）**：可按模块开关，检测用户偏题时主动澄清而非硬答
- **统一阶段（unified）**：单次 LLM 调用 + structured output 一次产出回复/转移/槽位，每轮 2 次调用降为 1 次
- **四级注册中心**：pattern / tool / llm provider / channel，模块级 `registry.register()` + AST 自动发现，无需改框架代码即可接入
- **话术模版动态注册**：声明式 pattern JSON（FSM/Agent/混排）经 collect-all 三层校验（全量错误带 JSON 路径）编译注册进现有 registry，同 code 覆盖、落盘重放、无需重启；配 SIG 元 skill（`interactive-task-skill-generator/`）实现"调研领域→生成模版→生成子 skill"链路
- **自主任务引擎**：`POST /api/v1/tasks` 触发 pattern 与 mock 对端（scripted 脚本 / llm 角色扮演）多轮交互直到终态，fire-and-forget + 轮询取结果快照
- **多渠道接入（Channel）**：声明式 `ChannelSpec` + 通用 webhook handler，内置闲鱼（xianyu）适配
- **会话治理与持久化**：内存治理（TTL 过期 + LRU 逐出）+ SQLite 审计流水（write-through，支持重启恢复）
- **调试 CLI**：交互 REPL、单问单答、方向键菜单选择 pattern/LLM、verbose 调试输出

## 内置示例 Pattern

| Pattern | 说明 |
|---|---|
| `xianyu_agent` | 闲鱼卖家客服（复刻 xianyu-auto-reply：本地关键词意图检测，议价轮数控制） |

## 快速开始

### 环境

- Python 3.11（`.venv`）
- 依赖安装（阿里云镜像）：

```bash
uv pip install --python .venv/bin/python \
  --index-url https://mirrors.aliyun.com/pypi/simple \
  -r requirements.txt
```

### 配置

在 `config/local_config.yaml`（已 gitignore）编写 LLM 配置：

```yaml
llm_providers:            # 连接层：provider 连接信息
  openai:
    api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: DASHSCOPE_API_KEY   # API key 从环境变量读
    timeout: 60
    max_retries: 2

llm_default:              # 编排层：默认模型选择
  code: openai
  model: qwen3.8-flash
  temperature: 0.7
  max_tokens: 2048
  enable_thinking: false

pattern_llm: {}           # pattern/module/node 级覆盖，留空全走默认
```

### 启动服务

```bash
export DASHSCOPE_API_KEY=sk-xxx
.venv/bin/python main.py          # 绑定 127.0.0.1:8000（本机 mock 服务，无鉴权设计）
                                  # 或 uvicorn main:app --host 127.0.0.1
```

主要接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health` | 探活 + 已注册 pattern 清单 |
| POST | `/api/v1/launch` | 创建会话 |
| POST | `/api/v1/chat` | 对话轮次（同步） |
| GET | `/api/v1/sessions` | 会话列表（审计） |
| GET | `/api/v1/sessions/{id}/messages` | 会话消息流水 |
| POST | `/api/v1/templates/validate` | 话术模版只校验不注册（全量 errors/warnings） |
| POST | `/api/v1/templates` | 注册话术模版（校验→编译→动态注册→落盘，同 code 覆盖） |
| GET | `/api/v1/templates/{code}` | 模版查询（template/builtin 来源 + 内容 hash） |
| POST | `/api/v1/tasks` | 触发自主任务（pattern 与 mock 对端跑到终态，返回 task_id） |
| GET | `/api/v1/tasks/{task_id}` | 轮询任务（运行中带中间过程，终态带结果快照） |

外部渠道 webhook（如闲鱼）由 channel registry 自动挂载，见 `channel/`。

### 调试 CLI

```bash
.venv/bin/python cli.py chat --pattern xianyu_agent -vv    # 交互 REPL + 完整调试
.venv/bin/python cli.py ask "这个还包邮吗" --session-id t1    # 单问单答
.venv/bin/python cli.py list patterns                        # 列出已注册 pattern/tool/llm
.venv/bin/python cli.py sessions                             # 列出持久化会话
```

不带 `--pattern/--llm` 启动时出方向键交互菜单。

## 项目结构

```
main.py                  FastAPI 入口 + 会话治理(TTL/LRU) + channel 接线
cli.py                   调试 CLI（REPL / ask / list / sessions）
prompt.py                全局 prompt 模板（node > module > class 三级覆盖）
chat/                    chat() 主循环 · Session · Agent ReAct 循环 · SQLite store
dialogue/                对话引擎内核
  base.py                PipelineStage / DialogueContext 契约
  pattern.py module.py node.py   Pattern→Module→Node 三级结构
  stage_slots.py         管线槽位：四槽位 + 三层解析
  xianyu_agent_route.py customer_agent_route.py    业务 pattern（应用层）
stages/                  管线 stage 实现（框架扩展层）
  nlu/ nlg/              两阶段形态 stage（意图识别 / 回复生成）
  unified.py             统一阶段（单次调用 NLU+NLG）
  query/ recaller/       查询改写 / 召回重排槽位 stage
  clarify/               偏题澄清（rule + prompts + stage）
llm/                     Provider 注册中心 + OpenAICompatible 实现
tools/                   工具注册中心 + 内置工具（calculator/weather/knowledge）
templates/               话术模版链路：schema/validator/compiler/store + API 路由（声明式 pattern JSON → 动态注册）
tasks/                   自主任务链路：TaskEngine（后台跑 pattern×对端）+ TaskStore + API 路由
channel/                 外部消息渠道适配（ChannelSpec + 通用 handler + 闲鱼）
augmentation/            输入增强（时间增强等）
config/                  配置加载 + local_config.yaml（gitignored）
database/                存储定义（SQLite 知识库 knowledge_store，scope 隔离）
interactive-task-skill-generator/   元 skill：调研领域→生成话术模版→生成子 skill（skill 侧资料，不参与服务运行）
tests/                   全离线测试（fake_provider 打桩 LLM）
```

分层纪律与依赖方向详见 [ARCHITECTURE.md](ARCHITECTURE.md)（框架内核 / 框架扩展 / 应用层三级改动纪律）与 [CLAUDE.md](CLAUDE.md)。

## 扩展接入

一切新能力走注册机制，框架代码零改动：

- **新对话流程** → `dialogue/<name>_route.py`，模块级 `registry.register()`
- **新领域话术模版**（数据形态，不写代码）→ `POST /api/v1/templates` 注册声明式 pattern JSON（蓝本见 `interactive-task-skill-generator/references/pattern-template-example.json`）
- **新工具** → `tools/<name>_tool.py`，AST 自动发现
- **新 LLM Provider** → `llm/<name>_provider.py`
- **新消息渠道** → `channel/<name>.py`，实现 `ChannelSpec` 并注册

## 测试

```bash
export DASHSCOPE_API_KEY=xxx && .venv/bin/python -m pytest tests/ -q
```

全离线（`tests/fake_provider.py` 打桩 LLM），改框架后必须全绿再提交。
