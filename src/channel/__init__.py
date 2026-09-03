"""Channel 适配层 —— 外部消息源接入引擎的 endpoint 集合。

每个渠道一个模块（如 ``xianyu.py``），实现 ChannelSpec 声明（载荷 schema、
session 派生、task_info 映射、成功响应契约）并模块级 ``registry.register()``
自注册；AST 自动发现（register.py），共性流程在 webhooks.py 通用 handler
（token 校验/过期过滤/get-or-create/错误码，结构上不可绕过）。引擎操作由
main.py 经 EngineOps 注入，channel 模块不感知会话治理与 LLM，可独立离线
测试。

新增渠道：``src/channel/<name>.py`` 实现 ChannelSpec + registry.register()，
main.py 无需改动（自动发现 + 自动生成 router）。
"""
