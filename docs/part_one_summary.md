---
title: part one 总结
status: DRAFT
---
## 02-启动链路
### 总结
```python
# 完整链路
HermesCLI.run->HermesCLI.run.process_loop(后台持续监控)->HermesCLI.chat->HermesCLI.chat.run_agent->AIAgent.run_conversation->agent.conversation_loop.run_conversation

# 在agent.conversation_loop.run_conversation中
#会有turn_context/turn_summary/turn_retry_state/turn_finalizer函数进行辅助

#llm_api_call循环出现在agent.conversation_loop.run_conversation中，采用react方式直至response中无工具调用

#整个循环中任意环节可打断
```

### 完整时序图
```mermaid
sequenceDiagram
    participant User
    participant Shell
    participant main as hermes_cli/main.py
    participant cli as cli.py（模块级）
    participant HermesCLI
    participant AIAgent as run_agent.AIAgent

    User->>Shell: hermes
    Shell->>main: main()
    
    Note over main: Phase 2: argparse 路由
    main->>main: argparse → cmd_chat()
    
    Note over main: Phase 3: 启动前检查
    main->>main: _has_any_provider_configured()
    main->>main: prefetch_update_check()（后台）
    main->>main: sync_skills(quiet=True)
    
    Note over cli: Phase 1: 模块级 Bootstrap
    main->>cli: import cli → 触发模块级代码
    cli->>cli: load_cli_config()
    cli->>cli: setup_logging("cli")
    cli->>cli: init_skin_from_config()
    cli->>cli: neuter_async_httpx_del()
    
    Note over cli: Phase 4: CLI 主协调器
    main->>cli: cli.main(**kwargs)
    cli->>cli: 解析工具集 & 技能
    
    Note over HermesCLI: Phase 5: 实例初始化
    cli->>HermesCLI: __init__(model, toolsets, ...)
    HermesCLI->>HermesCLI: 配置合并
    HermesCLI->>HermesCLI: SessionDB 初始化
    HermesCLI->>HermesCLI: 状态机设置
    
    Note over HermesCLI: Phase 6: REPL
    HermesCLI->>HermesCLI: run()
    HermesCLI->>HermesCLI: show_banner()
    HermesCLI-->>User: 显示 banner + 提示
    
    User->>HermesCLI: 第一条消息
    
    Note over HermesCLI: Phase 7: 惰性初始化
    HermesCLI->>HermesCLI: _init_agent()
    HermesCLI->>HermesCLI: _ensure_runtime_credentials()
    
    Note over AIAgent: Phase 8: Agent 构造
    HermesCLI->>AIAgent: __init__(model, credentials, ...)
    AIAgent->>AIAgent: API 模式检测
    AIAgent->>AIAgent: 客户端初始化
    AIAgent->>AIAgent: 回调注册
    
    AIAgent-->>HermesCLI: Agent 就绪
    HermesCLI->>AIAgent: run_conversation(message)
```
---
## 03-配置系统
### 总结
Hermes 的配置系统展示了一个成熟 CLI 框架应有的配置管理水准：

1. **四层覆盖**保证了灵活性——从硬编码默认到 CLI 参数，每层都可以精确覆盖
2. **`_deep_merge()`** 让用户只需关注差异，不必复制完整配置
3. **Config Migration** 让版本升级对用户透明，无需手动修改配置文件
4. **Gateway 配置桥** 优雅地解决了跨进程配置传递问题
5. **Profile 系统** 通过目录级隔离提供了简单可靠的多环境支持
6. **安全加固** 从文件权限到凭证净化，层层防护

### 配置数据流：从启动到就绪

Hermes 的配置加载并非一次完成，而是分阶段、分层级地逐步构建。以下是一次完整的配置加载时序：

```mermaid
flowchart TD
    A["用户执行 hermes 命令"] --> B["env_loader.load_hermes_dotenv()"]
    B --> B1["清理 .env 腐败行"]
    B1 --> B2["python-dotenv 加载到 os.environ"]
    B2 --> B3["凭证 ASCII 净化"]
    B3 --> B4["可选: 加载 project/.env"]
    B4 --> C["config.load_config()"]
    C --> C1["deepcopy(DEFAULT_CONFIG)"]
    C1 --> C2["读取 ~/.hermes/config.yaml"]
    C2 --> C3["_deep_merge(defaults, user_config)"]
    C3 --> C4["_normalize_root_model_keys()"]
    C4 --> C5["_normalize_max_turns_config()"]
    C5 --> C6["_expand_env_vars()"]
    C6 --> D["CLI 参数覆盖"]
    D --> D1["HERMES_MODEL → config.model"]
    D1 --> D2["HERMES_PROVIDER → config.provider"]
    D2 --> E{"Gateway 模式?"}
    E -->|是| F["run_gateway(): 配置→环境变量桥"]
    F --> G["gateway/config.py: load_gateway_config()"]
    E -->|否| H["直接使用 config dict"]

    style A fill:#e1f5fe
    style C fill:#fff3e0
    style F fill:#fce4ec
```
---
## 04-状态持久化
### 总结
Hermes 的状态持久化层是一个教科书级的 SQLite 工程实践。在一个 1238 行的文件中，它实现了：

- **完整的会话生命周期管理**——从创建到分裂再到剪枝
- **高并发写入安全**——WAL + 双层锁 + 随机抖动重试
- **高效全文搜索**——FTS5 content-sync 模式 + 精心设计的查询清洗
- **灵活的谱系追踪**——通过 `parent_session_id` 链式关联
- **渐进式 Schema 演进**——幂等迁移确保向后兼容
