"""
AGENT 模块可插拔执行器接口 — 预留 agent 后端的替换点。

现状 agent 执行硬连线到 loop.run_agent（ReAct 工具循环）。本模块把
"怎么跑一个 agent 模块"抽象为 AgentRunner 协议：AgentHandler 只依赖协议，
默认实现 LoopAgentRunner 委托 loop.run_agent，行为不变；后续接其他
agent 后端（如 planner-executor、外部 agent 服务）时提供新实现注入即可，
不动 chat 编排层。

不加 registry（CLAUDE.md：不引入新全局单例）——注入点为 chat()/chat_turn()
的可选参数 agent_runner。
"""

import logging
from typing import Any, Dict, Protocol, runtime_checkable

from src.chat.loop import TurnResult, run_agent
from src.chat.session import Session

logger = logging.getLogger(__name__)


@runtime_checkable
class AgentRunner(Protocol):
    """AGENT 模块执行器插件接口。

    实现方约定：
    - 输入：session（含 cxt 历史/槽位）、模块、已解析的 llm_config
    - 输出：TurnResult（reply 与 dispatch_event 互斥；actions 预留）
    - force_close=True 时不产生新的转移（max_hops 耗尽强制收尾）
    """

    def run(self, session: Session, module,
            llm_config: Dict[str, Any], force_close: bool = False) -> TurnResult:
        ...


class LoopAgentRunner:
    """默认实现：委托 loop.run_agent（现状唯一执行路径）。"""

    def run(self, session: Session, module,
            llm_config: Dict[str, Any], force_close: bool = False) -> TurnResult:
        return run_agent(session, module, llm_config, force_close=force_close)
