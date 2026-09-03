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
- **多渠道接入（Channel）**：声明式 `ChannelSpec` + 通用 webhook handler，内置闲鱼（xianyu）适配
- **会话治理与持久化**：内存治理（TTL 过期 + LRU 逐出）+ SQLite 审计流水（write-through，支持重启恢复）
- **调试 CLI**：交互 REPL、单问单答、方向键菜单选择 pattern/LLM、verbose 调试输出

## 内置示例 Pattern

| Pattern | 说明 |
|---|---|
| `car_sales_route` | 卖车流程（FSM + ROUTE 混合示例） |
| `car_sales_agent` | Agent 多模块示例（自由对话 + 工具） |
| `car_sales_unified_route` | 统一阶段示例（单次调用 NLU+NLG 合一） |
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
.venv/bin/python main.py          # 或 uvicorn main:app
```

主要接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/launch` | 创建会话 |
| POST | `/api/v1/chat` | 对话轮次 |
| GET | `/api/v1/sessions` | 会话列表（审计） |
| GET | `/api/v1/sessions/{id}/messages` | 会话消息流水 |

外部渠道 webhook（如闲鱼）由 channel registry 自动挂载，见 `src/channel/`。

### 调试 CLI

```bash
.venv/bin/python cli.py chat --pattern car_sales_route -vv   # 交互 REPL + 完整调试
.venv/bin/python cli.py ask "我想买车" --session-id t1        # 单问单答
.venv/bin/python cli.py list patterns                        # 列出已注册 pattern/tool/llm
.venv/bin/python cli.py sessions                             # 列出持久化会话
```

不带 `--pattern/--llm` 启动时出方向键交互菜单。

## 项目结构

```
main.py                  FastAPI 入口 + 会话治理(TTL/LRU) + channel 接线
cli.py                   调试 CLI（REPL / ask / list / sessions）
src/
  chat/                  chat() 主循环 · Session · Agent ReAct 循环 · SQLite store
  dialogue/              对话引擎内核
    base.py              PipelineStage / DialogueContext 契约
    pattern.py module.py node.py   Pattern→Module→Node 三级结构
    stage_slots.py       管线槽位：四槽位 + 三层解析
    unified.py           统一阶段（单次调用 NLU+NLG）
    dispatch.py          模块间分发原语（同轮移交/回弹拒绝）
    nlu/ nlg/ query/ recaller/   管线 stage 实现（框架扩展层）
    car_sales_*.py       示例 pattern（应用层）
    xianyu_agent_route.py 闲鱼客服 pattern（应用层）
  clarify/               偏题澄清（rule + prompts + stage）
  llm/                   Provider 注册中心 + OpenAICompatible 实现
  tools/                 工具注册中心 + 内置工具（calculator/weather/workorder）
  channel/               外部消息渠道适配（ChannelSpec + 通用 handler + 闲鱼）
  prompt.py              全局 prompt 模板（node > module > class 三级覆盖）
config/                  配置加载 + local_config.yaml（gitignored）
tests/                   全离线测试（fake_provider 打桩 LLM）
```

分层纪律与依赖方向详见 [ARCHITECTURE.md](ARCHITECTURE.md)（框架内核 / 框架扩展 / 应用层三级改动纪律）与 [CLAUDE.md](CLAUDE.md)。

## 扩展接入

一切新能力走注册机制，框架代码零改动：

- **新对话流程** → `src/dialogue/<name>_route.py`，模块级 `registry.register()`
- **新工具** → `src/tools/<name>_tool.py`，AST 自动发现
- **新 LLM Provider** → `src/llm/<name>_provider.py`
- **新消息渠道** → `src/channel/<name>.py`，实现 `ChannelSpec` 并注册

## 测试

```bash
export DASHSCOPE_API_KEY=xxx && .venv/bin/python -m pytest tests/ -q
```

全离线（`tests/fake_provider.py` 打桩 LLM），改框架后必须全绿再提交。
