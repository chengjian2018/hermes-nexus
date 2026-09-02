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

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ModuleType(Enum):
    """Module type enum."""
    AGENT = "agent"   # pure LLM agent replies directly
    FSM = "fsm"       # finite state machine (multi-layer node transitions)
    ROUTE = "route"   # root router + intent menu


@dataclass
class ModuleLink:
    """邻接声明：A 的 sub_modules 里的一条边。

    一字段两职责：既是对 transfer 合法目标的声明（转移图边集），
    又定义 A 上下文中 B 的投影厚度（知识/工具借出配置）。
    """

    target: str
    lend_knowledge: bool = True
    lend_tools: Optional[List[str]] = None

    def __post_init__(self):
        self.lend_tools = self.lend_tools or []


def _normalize_links(sub_modules: Optional[List[Any]]) -> List[ModuleLink]:
    """把 str / ModuleLink 混合列表归一化为 List[ModuleLink]。

    str 写法（旧兼容）自动包装为 lend_knowledge=True、lend_tools=[]。
    """
    links: List[ModuleLink] = []
    for item in sub_modules or []:
        if isinstance(item, ModuleLink):
            links.append(item)
        elif isinstance(item, str):
            links.append(ModuleLink(target=item))
        else:
            raise ValueError(f"sub_modules 元素必须是 str 或 ModuleLink: {item!r}")
    return links


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
        agent_stage: module-level Agent stage instance (optional, default when unset).
        generate/pre_recall/query/post_recall: 管线槽位配置（node 级最高优先级）。
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
        sub_modules: Optional[List[Any]] = None,
        use_tools: Optional[List[Any]] = None,
        base_prompt: Optional[str] = None,
        base_nlu_prompt: Optional[str] = None,
        base_nlg_prompt: Optional[str] = None,
        generate: Optional[Any] = None,
        pre_recall: Optional[Any] = None,
        query: Optional[Any] = None,
        post_recall: Optional[Any] = None,
        agent_stage: Optional[Any] = None,
        enable_clarify: bool = False,
        is_end: Optional[bool] = False,
        answer_examples: Optional[List[str]] = None,
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

        # 管线槽位配置（三层优先级 node > module > pattern，执行期由
        # stage_slots.resolve_stage 延迟解析；generate 支持单 stage 或
        # {"nlu":…, "nlg":…} dict）
        self.generate = generate
        self.pre_recall = pre_recall
        self.query = query
        self.post_recall = post_recall

        self.sub_modules = _normalize_links(sub_modules)
        self.answer_examples = answer_examples or []

        self.agent_stage = agent_stage

        # 双轨澄清开关：FSM 模块开启后接入 ClarifyStage（详见 src/clarify/）
        self.enable_clarify = enable_clarify

        self.is_end = is_end

        for legacy in ("nlu_stage", "nlg_stage"):
            if legacy in (kwargs or {}):
                logger.warning(
                    "[module] %s=%r 已废弃：槽位配置请改用 generate="
                    "{'nlu':…, 'nlg':…} 或单 stage（stage_slots.py）",
                    legacy, kwargs[legacy],
                )

        # Extra attributes
        for key, value in (kwargs or {}).items():
            setattr(self, key, value)

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} "
            f"code={self.module_code!r} type={self.type.value!r}>"
        )

    def to_projection_text(self) -> str:
        """模块头部投影：供邻接 module 的 agent prompt 注入（inject 原语）。

        只含头部四字段（name/description/todo/answer_examples），不含内部
        流程 prompt —— 流程深度不投影，深入需 transfer（spec §1.2）。
        """
        parts = []
        if self.module_name:
            parts.append(f"- 定义：【{self.module_name}】{self.module_description or ''}")
        if self.module_todo_description:
            parts.append(f"- 职责：{self.module_todo_description}")
        if self.answer_examples:
            examples = "；".join(self.answer_examples)
            parts.append(f"- 回答范式：「{examples}」")
        return "\n".join(parts)


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

