"""
Module — abstract base class for dialogue modules.

Modules come in three types with different dialogue flows:
- AGENT  : pure LLM agent replies directly, no state machine
- FSM    : finite state machine with transitions across node layers (NLU → NLG path)
- ROUTE  : root router + intent menu for top-level dispatch (NLU → NLG path)

Each module can configure NLU / NLG / Agent stage instances at its own level;
the framework default implementation is used when unset.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional


class ModuleType(Enum):
    """Module type enum."""
    AGENT = "agent"   # pure LLM agent replies directly
    FSM = "fsm"       # finite state machine (multi-layer node transitions)
    ROUTE = "route"   # root router + intent menu


class BaseModule:
    """Base class for dialogue modules.

    Each module represents an independent dialogue capability unit; it may
    contain nodes (FSM type) or run directly as an agent (AGENT type).

    Attributes:
        type: module type, determines the dialogue flow.
        module_code: unique module code.
        module_name: module name.
        module_description: module description.
        module_todo_description: module todo description.
        module_nodes: list of nodes in the module (used by FSM/ROUTE types).
        use_tools: list of tools available to the module.
        base_prompt: module base prompt (used by AGENT type).
        nlu_stage: module-level NLU stage instance (optional, default when unset).
        nlg_stage: module-level NLG stage instance (optional, default when unset).
        agent_stage: module-level Agent stage instance (optional, default when unset).
        enable_clarify: dual-track clarify switch; when True the FSM module
            integrates ClarifyStage (see src/clarify/).
    """

    type: ModuleType = ModuleType.AGENT

    def __init__(
        self,
        module_code: Optional[str] = None,
        module_name: Optional[str] = None,
        module_description: Optional[str] = None,
        module_todo_description: Optional[str] = None,
        module_nodes: Optional[List[Any]] = None,
        sub_modules: Optional[List[str]] = None,
        use_tools: Optional[List[Any]] = None,
        base_prompt: Optional[str] = None,
        base_nlu_prompt: Optional[str] = None,
        base_nlg_prompt: Optional[str] = None,
        nlu_stage: Optional[Any] = None,
        nlg_stage: Optional[Any] = None,
        agent_stage: Optional[Any] = None,
        enable_clarify: bool = False,
        is_end: Optional[bool] = False,
        **kwargs,
    ):
        self.module_code = module_code
        self.module_name = module_name
        self.module_description = module_description
        self.module_todo_description = module_todo_description
        self.module_nodes = module_nodes or []
        self.use_tools = use_tools or []
        self.base_prompt = base_prompt
        self.base_nlu_prompt = base_nlu_prompt
        self.base_nlg_prompt = base_nlg_prompt
        self.sub_modules = sub_modules or []

        # Module-level stage instances (priority over default implementations)
        self.nlu_stage = nlu_stage
        self.nlg_stage = nlg_stage
        self.agent_stage = agent_stage

        # 双轨澄清开关：FSM 模块开启后接入 ClarifyStage（详见 src/clarify/）
        self.enable_clarify = enable_clarify

        self.is_end = is_end

        # Extra attributes
        for key, value in (kwargs or {}).items():
            setattr(self, key, value)

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} "
            f"code={self.module_code!r} type={self.type.value!r}>"
        )


# ---------------------------------------------------------------------------
# Convenience subclasses
# ---------------------------------------------------------------------------

class AgentModule(BaseModule):
    """Pure Agent module — replies directly via LLM, no state machine."""

    type = ModuleType.AGENT


class FSMModule(BaseModule):
    """Finite state machine module — multi-layer node transitions, NLU → NLG path."""

    type = ModuleType.FSM


class RouteModule(BaseModule):
    """Route module — root router + intent menu, for top-level dispatch."""

    type = ModuleType.ROUTE

