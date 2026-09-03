"""
AGENT 模块处理器 — 委托可插拔的 AgentRunner 执行。

行为等价原 chat._handle_agent_module；差异：run_agent 硬连线改为
经 AgentRunner 协议（默认 LoopAgentRunner，行为不变）。
"""

import logging

from src.chat.agents import AgentRunner, LoopAgentRunner
from src.chat.handlers.base import ModuleHandler
from src.chat.loop import TurnResult
from src.chat.session import Session

logger = logging.getLogger(__name__)


class AgentHandler(ModuleHandler):
    """AGENT 模块一轮处理：R2 刷新 → agent_runner.run()。

    Args:
        refresh_llm: LLM 配置刷新函数（chat 层注入）
        agent_runner: agent 执行器（缺省 LoopAgentRunner → loop.run_agent）
    """

    def __init__(self, refresh_llm, agent_runner: AgentRunner = None) -> None:
        super().__init__(refresh_llm)
        self.agent_runner = agent_runner or LoopAgentRunner()

    def handle(self, session: Session, module,
               force_close: bool = False) -> TurnResult:
        logger.info("Agent 模块处理: module=%s", module.module_code)
        self.refresh_llm(session, module_code=module.module_code)  # R2
        return self.agent_runner.run(
            session, module, session.cxt.llm_config, force_close=force_close)
