"""
模块处理器包 — 按模块类型拆分的单模块单轮处理器。

路由规则（简单映射，非 registry）：
- ModuleType.AGENT → AgentHandler（委托 AgentRunner）
- ModuleType.FSM   → FsmHandler（next_node 跳转）
- ModuleType.ROUTE → RouteHandler（jump_module 静默分发 / root 重置）

handlers 每次 chat() 调用时构造（轻量，只持有注入的 callable），
无模块级可变单例。
"""

from src.chat.handlers.agent import AgentHandler
from src.chat.handlers.base import ModuleHandler, PipelineHandler, default_skeleton
from src.chat.handlers.fsm import FsmHandler, fsm_node_transition
from src.chat.handlers.route import RouteHandler
from src.dialogue.module import ModuleType

__all__ = [
    "AgentHandler",
    "FsmHandler",
    "RouteHandler",
    "ModuleHandler",
    "PipelineHandler",
    "default_skeleton",
    "fsm_node_transition",
    "resolve_handler",
]


def resolve_handler(module_type, refresh_llm, agent_runner=None) -> ModuleHandler:
    """按模块类型构造处理器。

    Args:
        module_type: ModuleType 枚举值
        refresh_llm: LLM 配置刷新函数（chat 层注入，保持 patch 锚点）
        agent_runner: 可选 agent 执行器（仅 AGENT 用；缺省 LoopAgentRunner）

    Raises:
        ValueError: 未知模块类型
    """
    if module_type == ModuleType.AGENT:
        return AgentHandler(refresh_llm, agent_runner=agent_runner)
    if module_type == ModuleType.FSM:
        return FsmHandler(refresh_llm)
    if module_type == ModuleType.ROUTE:
        return RouteHandler(refresh_llm)
    raise ValueError(f"未知模块类型: {module_type}")
