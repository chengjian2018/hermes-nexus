"""Channel 适配层 —— 外部消息源接入引擎的 endpoint 集合。

每个 channel 一个模块，导出 router 工厂（如 ``build_xianyu_router``），
由 main.py 注入引擎操作（get/launch/chat）后 include_router 接线；
channel 模块自身不感知会话治理与 LLM，可独立离线测试。

新增渠道：``src/channel/<name>.py`` + main.py 一行接线。
"""
