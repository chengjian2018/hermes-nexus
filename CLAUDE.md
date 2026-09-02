# hermes-nexus

人机交互 Mock 服务：Pipeline 式对话引擎 + FastAPI 服务。

## 分层规则（改框架前必读）

| 层 | 位置 | 改动纪律 |
|---|---|---|
| **框架内核** | `src/dialogue/base.py` `module.py` `node.py` `pattern.py` `register.py`、`src/chat/session.py` `loop.py`、`src/llm/*`、`src/tools/register.py` | **串行做**：先出 implementation plan，人工审完再动手；小步 commit；改完跑全量 pytest；同步更新 ARCHITECTURE.md |
| **框架扩展** | `src/dialogue/nlu/` `nlg/` `query/` `recaller/`（包内实现文件）、`src/dialogue/unified.py`、`src/clarify/*` | 半并行：新加 stage 类较自由；改基类签名算内核改动 |
| **应用层** | `src/dialogue/car_sales_route.py` 等业务 pattern、`src/tools/*_tool.py`、`src/prompt.py` | **可自由并行**（建议 git worktree 隔离） |

约定：
- 新功能/新工具/新 pattern **一律走注册机制**（`registry.register()` 模块级自注册 + AST 自动发现），不许绕过注册表硬连线
- 新 stage 继承 `PipelineStage`，通过 `DialogueContext` 传数据，不改基类签名
- prompt 模板三级优先：node > module > class default
- 不引入新的全局单例；现有单例只有 pattern/tool/llm/channel 四个 registry

## 环境

- `.venv`（python3.11）；装依赖走阿里云镜像：`uv pip install --python .venv/bin/python --index-url https://mirrors.aliyun.com/pypi/simple <pkg>`
- 跑测试必须同命令 export key：`export DASHSCOPE_API_KEY=... && .venv/bin/python -m pytest tests/ -q`
- 本地配置 `config/local_config.yaml`（已 gitignore，含 key）

## 工作流

- 框架内核改动 → plan 先行（writing-plans skill）→ 人工审 → 执行 → pytest 全绿才提交
- 应用层并行任务 → git worktree（`git worktree add ../hermes-<task> -b <task> dev`），**最多活一天**，每天 rebase dev
- 一次会话只做一件事；commit 按逻辑单元分组，一个 commit 讲一件事
