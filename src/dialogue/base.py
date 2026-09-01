"""
Dialogue system base types — PipelineStage, SessionMessage, DialogueContext

All stages and modules depend on these standard types to keep session storage
and context passing consistent.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional



# ============================================================================
# Pipeline stage base class
# ============================================================================

class PipelineStage(ABC):
    """A pluggable step in the Pipeline.

    Each stage implements ``execute(ctx) -> ctx`` and can be freely combined in Pattern.stages.
    """

    stage_name: str = ""

    @abstractmethod
    def execute(self, ctx: DialogueContext) -> DialogueContext:
        """Run this stage's logic and return the modified context."""
        ...

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.stage_name!r}>"


# ============================================================================
# Standardized session message
# ============================================================================

@dataclass
class SessionMessage:
    """Standardized session message format.

    All messages produced by stages use this format to keep session storage consistent.
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    stage: str = ""  # source stage: pre_recall / query_rewrite / nlu / nlg / agent / state_update
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "stage": self.stage,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SessionMessage":
        return cls(
            role=d.get("role", "user"),
            content=d.get("content", ""),
            stage=d.get("stage", ""),
            metadata=d.get("metadata", {}),
        )


# ============================================================================
# Dialogue context (data carrier throughout the Pipeline)
# ============================================================================

@dataclass
class DialogueContext:
    """Dialogue context flowing through the whole Pipeline.

    Every PipelineStage receives and returns this object; all intermediate results are stored here.

    # metadata 键约定（module dispatch 机制使用，见 dispatch.py）：
    #   dispatch_graph       : Dict[str, Set[str]]  合法转移边（chat 启动时注入）
    #   dispatch_log         : List[Dict]           本轮转移链（每轮开头清空）
    #   handoff_context      : Dict                 最近一次转移的承接信息
    #   served_by_projection : Dict{module, source}  A 借投影答轮：借方模块与来源域
    """

    session_id: str
    user_query: str

    # Session history (standardized message list)
    history: List[SessionMessage] = field(default_factory=list)

    # Recall results before query rewrite
    pre_recall_results: List[Dict[str, Any]] = field(default_factory=list)

    # Query list after rewrite
    rewritten_queries: List[str] = field(default_factory=list)

    # Recall results after query rewrite
    post_recall_results: List[Dict[str, Any]] = field(default_factory=list)

    # NLU result: {"intent": str, "slots": {...}, "confidence": float}
    nlu_result: Optional[Dict[str, Any]] = None

    # NLG result
    nlg_result: Optional[Dict[str, Any]] = None

    # Agent direct reply result
    agent_result: Optional[Dict[str, Any]] = None

    # Current state
    current_module_code: Optional[str] = None
    current_node_code: Optional[str] = None
    filled_slots: Dict[str, Any] = field(default_factory=dict)

    # Extra metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Task base info
    task_basic_info: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Pipeline infrastructure (injected by the pipeline runner; stages need not store it)
    # ------------------------------------------------------------------
    node_map: Dict[str, Any] = field(default_factory=dict)
    module_map: Dict[str, Any] = field(default_factory=dict)
    llm_config: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    def add_message(
        self,
        role: str,
        content: str,
        stage: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append a standardized message to history."""
        self.history.append(
            SessionMessage(
                role=role,
                content=content,
                stage=stage,
                metadata=metadata or {},
            )
        )

    def format_history(self, max_turns: int = 10) -> str:
        """Format the last N turns as text for prompt injection."""
        # Keep only user and assistant messages, drop system / tool etc.
        filtered = [msg for msg in self.history if msg.role in ("user", "assistant")]
        recent = filtered[-max_turns * 2 :]  # user + assistant come in pairs
        if not recent:
            return "（暂无历史对话）"
        lines = []
        for msg in recent:
            lines.append(f"{msg.role}: {msg.content}")
        return "\n".join(lines)

    def format_slots(self) -> str:
        """Format filled slots as JSON for prompt injection."""
        if self.filled_slots:
            return json.dumps(self.filled_slots, ensure_ascii=False, indent=2)
        return "{}"

    def format_recall_info(self) -> str:
        """Format recall results for prompt injection; post-rewrite results take priority."""
        results = self.post_recall_results or self.pre_recall_results
        if results:
            return json.dumps(results, ensure_ascii=False, indent=2)
        return "暂无召回信息"

    def format_rewritten_queries(self) -> str:
        """Format rewrite results as text for prompt injection."""
        if self.rewritten_queries:
            return "\n".join(self.rewritten_queries)
        return ""

    # ------------------------------------------------------------------
    # Current node / module accessors
    # ------------------------------------------------------------------

    def get_current_node(self) -> Optional[Any]:
        """Return the current node instance from node_map (None when unset)."""
        if not self.current_node_code:
            return None
        return self.node_map.get(self.current_node_code)

    def get_current_module(self) -> Optional[Any]:
        """Return the current module instance from module_map (None when unset)."""
        if not self.current_module_code:
            return None
        return self.module_map.get(self.current_module_code)

    # ------------------------------------------------------------------
    # Node / module slot formatting — delegation to the data layer
    # (node.py / module.py own the formatting; ctx only resolves "which node/module")
    # ------------------------------------------------------------------

    def format_cur_node(self, stage: str = "nlu") -> str:
        """Format the current node as prompt-ready text (slot: cur_node).

        Stage-specific variants — NLU and NLG need different facets of the node:
        - "nlu": name + todo description + slot definitions (what to collect/decide)
        - "nlg": name + node description (what scenario the reply is grounded in)
        - "full": all fields (used by retrieval stages: query rewrite / recall)

        Args:
            stage: which stage's facet to format ("nlu" / "nlg" / "full").
        """
        node = self.get_current_node()
        if node is None:
            return "暂无当前节点信息"

        formatters = {
            "nlu": node.to_nlu_prompt_text,
            "nlg": node.to_nlg_prompt_text,
        }
        formatter = formatters.get(stage, node.to_prompt_text)
        return formatter()

    def format_next_nodes(self) -> str:
        """Format the current node's sub-node list as prompt-ready text (slot: next_node)."""
        node = self.get_current_node()
        return (
            node.format_sub_nodes(self.node_map)
            if node is not None
            else "暂无后续节点信息"
        )

    def format_answer_pattern(self) -> str:
        """Format the current node's answer examples as prompt-ready text (slot: answer_pattern)."""
        node = self.get_current_node()
        return (
            node.format_answer_examples()
            if node is not None
            else "暂无回答范式"
        )

    def format_task_info(self) -> str:
        """Format the task info as prompt-ready text (slot: task_info).

        Reads ``task_basic_info`` first; falls back to ``metadata["task_info"]``
        (the key written by the launch layer from the dialogue request).
        """
        task_info = self.task_basic_info or self.metadata.get("task_info") or {}

        parts = []
        for key, value in task_info.items():
            parts.append(f"{key}: {value}")

        return "\n".join(parts) if parts else "暂无任务基础信息"


# ============================================================================
# Prompt slot plumbing — fixed slot vocabulary shared by all stages
# ============================================================================

# 固定槽位词表：所有环节的 prompt 模板共用同一套 {__key__} 占位符。
# 拼接逻辑放在数据所属层：
#   - node 层    : cur_node（按 stage 输出不同 facet）/ next_node / answer_pattern  (node.py)
#   - module 层  : task_info                              (module.py)
#   - ctx 层     : query / query_rewrite / recall_info / history / filled_slots
# stage 层（nlu / nlg / query / recaller）只做「槽位名 → 数据层格式化方法」的映射，
# 不再各自实现拼接。
#
# cur_node 的 stage facet 约定：
#   - nlu : name + todo_description + slots   —— 理解任务：判断意图、按模板抽槽
#   - nlg : name + description                —— 生成任务：回复所依托的场景描述
#   - full: 全字段                             —— 检索类 stage（query 改写 / recall）

def fill_prompt_template(template: str, slots: Dict[str, str]) -> str:
    """Replace ``{__key__}`` placeholders in the template with their values.

    Uses ``str.replace()`` one by one; keys absent from the template are safely ignored.
    """
    for key, value in slots.items():
        template = template.replace(f"{{__{key}__}}", value)
    return template


def resolve_prompt_template(
    ctx: DialogueContext,
    prompt_attr: str,
    default_template: Optional[str],
) -> Optional[str]:
    """Resolve a stage's prompt template by priority.

    Priority: node level > module level > *default_template*.

    Args:
        ctx: current dialogue context.
        prompt_attr: override attribute name on node/module (e.g. ``base_nlu_prompt``).
        default_template: fallback template (may be None to keep each consumer's built-in).
    """
    node = ctx.get_current_node()
    if node is not None:
        node_prompt = getattr(node, prompt_attr, None)
        if node_prompt:
            return node_prompt

    module = ctx.get_current_module()
    if module is not None:
        module_prompt = getattr(module, prompt_attr, None)
        if module_prompt:
            return module_prompt

    return default_template


