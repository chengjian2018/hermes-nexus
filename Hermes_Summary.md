# 深入 Hermes Agent 源码

> 基于 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) v0.19.0 版本线的深度源码分析
> 
> **本项目是基于 https://github.com/luyao618/Claude-Code-Source-Study V0.9.0版本进行的更新变动与一些个人解读**

---

## 目录

### Part 1: 全局架构（4 篇）

| # | 章节 | 关键词 |
|---|------|--------|
| [01](docs/01-项目全景.md) | **项目全景** | 技术栈选型、模块依赖全景、文件依赖链 |
| [02](docs/02-启动链路.md) | **启动链路** | argparse 路由、profile 预处理、env/bootstrap、Agent 初始化 |
| [03](docs/03-配置系统.md) | **配置系统** | 多层配置合并、profile 机制、gateway 配置桥接、config migration |
| [04](docs/04-状态持久化.md) | **状态持久化** | SessionDB、SQLite WAL、FTS5 全文搜索、session 分裂与链接 |

[part 1 总结](docs/part_one_summary.md)

### Part 2: AI 核心（6 篇）

| # | 章节 | 关键词 |
|---|------|--------|
| [05](docs/05-对话循环.md) | **对话循环** | 消息推进、tool_calls 处理、iteration budget、中断恢复 |
| [06](docs/06-System-Prompt-工程.md) | **System Prompt 工程** | 分段构建、context file 注入与安全扫描、skills index |
| [07](docs/07-上下文管理.md) | **上下文管理** | head/tail 保护、tool output 预裁剪、iterative summary |
| [08](docs/08-多模型适配.md) | **多模型适配** | OpenAI 兼容层、Anthropic 原生适配、metadata 注册表、smart routing |
| [09](docs/09-辅助客户端与成本控制.md) | **辅助客户端与成本控制** | 辅助 LLM 调用、凭证池与轮转、定价引擎、rate limit 追踪 |
| [10](docs/10-错误处理与韧性.md) | **错误处理与韧性** | 错误分类器、retry 策略、优雅降级、模型回退 |

[part 1 总结](docs/part_two_summary.md)

### Part 3: 工具系统（5 篇）

| # | 章节 | 关键词 |
|---|------|--------|
| [11](docs/11-工具注册表设计.md) | **工具注册表设计** | AST 自发现、ToolEntry `__slots__`、toolset 分组、import chain |
| [12](docs/12-终端工具深度剖析.md) | **终端工具深度剖析** | 命令执行生命周期、前置安全检查、持久会话、后台进程注册表 |
| [13](docs/13-文件与代码工具.md) | **文件与代码工具** | 文件读写/搜索/patch、代码沙箱执行 |
| [14](docs/14-Web-与浏览器工具.md) | **Web 与浏览器工具** | Parallel + Firecrawl 搜索、Browserbase 自动化、CamoFox 反检测 |
| [15](docs/15-MCP-协议实现.md) | **MCP 协议实现** | MCP 客户端/服务端双向实现、OAuth 认证 |

[part 1 总结](docs/part_three_summary.md)

### Part 4: 智能体系统（4 篇）

| # | 章节 | 关键词 |
|---|------|--------|
| [16](docs/16-子智能体委托.md) | **子智能体委托** | context 隔离、toolset 限制、batch 并行、MAX_DEPTH 防递归 |
| [17](docs/17-记忆系统.md) | **记忆系统** | MemoryManager 编排、Provider/Plugin 扩展、MEMORY.md 持久化、FTS5 搜索 |
| [18](docs/18-技能系统.md) | **技能系统** | 技能发现/加载/执行、Skills Hub 生态、自我改进、安全沙箱 |
| [19](docs/19-安全体系.md) | **安全体系** | Tirith 安全引擎、path traversal 防护、prompt injection 扫描、URL 安全 |

[part 1 总结](docs/part_four_summary.md)

### Part 5: 平台与基础设施（4 篇）

| # | 章节 | 关键词 |
|---|------|--------|
| [20](docs/20-CLI-交互界面.md) | **CLI 交互界面** | Rich + prompt_toolkit TUI、slash 命令注册表、KawaiiSpinner、皮肤引擎 |
| [21](docs/21-消息网关.md) | **消息网关** | 统一网关架构、会话上下文组装、消息投递、跨平台 slash 命令 |
| [22](docs/22-终端后端.md) | **终端后端** | local/docker/ssh/daytona/modal/singularity 的统一抽象 |
| [23](docs/23-批处理与轨迹基础设施.md) | **批处理与轨迹基础设施** | Cron 调度、batch run、trajectory 压缩、agent environment 衔接 |

[part 1 总结](docs/part_five_summary.md)

