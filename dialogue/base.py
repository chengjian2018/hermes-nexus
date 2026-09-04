"""
Dialogue system base types — PipelineStage, SessionMessage, DialogueContext

All stages and modules depend on these standard types to keep session storage
and context passing consistent.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)



# ============================================================================
# Module jump event (same-turn reroute primitive)
# ============================================================================

@dataclass
class ModuleJumpEvent:
    """一次模块跳转意图 —— 由 stage / agent 轮内产生，chat 层统一消费。

    产生方（写入 ``cxt.actions``）：
    - NLU 轮内检测：``nlu_result.jump_module`` 指向其他模块
    - ROUTE 菜单节点配置：``node.jump_module``（next_node 命中后）
    - AGENT transfer 工具调用：``transfer_to_{target}``

    消费方（chat 层 hop 循环）：校验目标存在于 module_map 即重路由
    （写 current_module_code、置空 current_node_code），淡化邻接边界。

    同时保留 dict 形态动作（如 ``{"conversation_end": True}``）的兼容：
    actions 列表内非本类型元素原样快照进 ChatResult.actions。
    """

    target_module_code: str
    reason: str = ""      # 移交上下文：供目标模块承接（注入 prompt）
    source: str = ""      # nlu_jump / route_menu / handoff_tool

    def to_dict(self) -> Dict[str, Any]:
        """观测形态：快照进 ChatResult.actions / cli 渲染用。"""
        return {
            "module_jump": {
                "target": self.target_module_code,
                "reason": self.reason,
                "source": self.source,
            }
        }


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

    tool 轨迹约定（跨轮完整回放给 LLM，见 chat/messages.py 回放守卫）——
    不新增独立字段/表列，全部骑在现有 JSON 通道上：
    - assistant 工具轮：content 为 ``{"content": str, "tool_calls": [...]}``
      JSON 载荷（``encode_tool_call_content`` 编码 / ``decode`` 解析）
    - tool 行：``metadata["tool_call_id"]`` 关联 LLM tool_call id
    - role ``summary``：历史压缩产生的 LLM 摘要行（回放时 user 角色 untrusted 包裹）
    """

    role: Literal["system", "user", "assistant", "tool", "summary"]
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


def encode_tool_call_content(
    content: str, tool_calls: List[Dict[str, Any]]
) -> str:
    """assistant 工具轮 content 的 JSON 载荷编码（Customer-Agent 同款约定）。"""
    return json.dumps(
        {"content": content, "tool_calls": tool_calls}, ensure_ascii=False)


def decode_tool_call_content(
    content: str,
) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """解析 assistant 行 JSON 载荷 → ``(文本, tool_calls)``；非工具轮返回 None。

    非工具轮判定：非 JSON / 无 ``tool_calls`` 键 / 空列表（loop 只在
    tool_calls 非空时编码，空列表视为普通文本行）。
    """
    if not content or not content.lstrip().startswith("{"):
        return None
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or not payload.get("tool_calls"):
        return None
    calls = payload["tool_calls"]
    if not isinstance(calls, list):
        return None
    return str(payload.get("content") or ""), calls


# ============================================================================
# Dialogue context (data carrier throughout the Pipeline)
# ============================================================================

@dataclass
class DialogueContext:
    """Dialogue context flowing through the whole Pipeline.

    Every PipelineStage receives and returns this object; all intermediate results are stored here.

    # metadata 键约定（stage / chat 层自管理）：
    #   served_by_projection : Dict{module, source}  A 借投影答轮：借方模块与来源域
    #   clarify              : Dict                  ClarifyStage 每轮自置自清
    """

    session_id: str
    user_query: str

    # Session history (standardized message list)
    history: List[SessionMessage] = field(default_factory=list)

    # 本轮 user 行在 history 中的下标（begin_turn 快照于 add user 之前；
    # default_build_messages 以此切分跨轮历史 / 显式 query / 本轮 hop 内行）
    turn_history_start: int = 0

    # 逐条 write-through 落库钩子（SessionStore.attach 注入；None = 未启用）。
    # 异常在 add_message 侧吞掉记日志，绝不阻断对话
    message_sink: Optional[Any] = None

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

    # Actions reserved for this turn (e.g. sends / transitions / external calls the reply should
    # trigger besides the text). Stages/handlers may append; the chat layer snapshots per turn
    # (see TurnLifecycle in chat/context_lifecycle.py — per-turn reset).
    # 模块跳转动作用 ModuleJumpEvent 实例承载（chat 层 hop 循环消费后重路由），
    # 其余 dict 形态动作原样快照进 ChatResult.actions。
    actions: List[Any] = field(default_factory=list)

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
        """Append a standardized message to history.

        tool 轨迹走 content/metadata 载荷（见 SessionMessage docstring）：
        assistant 工具轮 content 先经 ``encode_tool_call_content`` 编码；
        tool 行的 ``tool_call_id`` 放 metadata。
        """
        msg = SessionMessage(
            role=role,
            content=content,
            stage=stage,
            metadata=metadata or {},
        )
        self.history.append(msg)
        if self.message_sink is not None:
            try:
                self.message_sink(msg)
            except Exception:
                logger.exception(
                    "message_sink 写入失败（不影响对话）: session=%s role=%s stage=%s",
                    self.session_id, msg.role, msg.stage,
                )

    def format_history(self, max_turns: int = 10) -> str:
        """Format the last N turns as text for prompt injection."""
        # Keep only user and assistant messages with displayable text, drop
        # system / tool / summary. assistant 工具轮 content 为 JSON 载荷，
        # 取内层文本（内层为空则整行跳过——工具轮通常无对用户说话）
        lines: List[str] = []
        for msg in self.history:
            if msg.role not in ("user", "assistant"):
                continue
            text = msg.content
            decoded = decode_tool_call_content(msg.content)
            if decoded is not None:
                text = decoded[0]
            if text:
                lines.append(f"{msg.role}: {text}")
        recent = lines[-max_turns * 2 :]  # user + assistant come in pairs
        if not recent:
            return "（暂无历史对话）"
        return "\n".join(recent)
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

    def get_next_node(self):
        nlu_result = self.nlu_result or {}
        next_node = nlu_result.get("next_node", "")
        if next_node not in self.node_map:
            return None
        return self.node_map[next_node]


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

    def format_nlg_next_node(self, stage: str = "nlg") -> str:
        """Format the current node as prompt-ready text (slot: cur_node).

        Stage-specific variants — NLU and NLG need different facets of the node:
        - "nlu": name + todo description + slot definitions (what to collect/decide)
        - "nlg": name + node description (what scenario the reply is grounded in)
        - "full": all fields (used by retrieval stages: query rewrite / recall)

        Args:
            stage: which stage's facet to format ("nlu" / "nlg" / "full").
        """
        node = self.get_next_node()
        if node is None:
            return "暂无当前节点信息"

        formatters = {
            "nlu": node.to_nlu_prompt_text,
            "nlg": node.to_nlg_prompt_text,
        }
        formatter = formatters.get(stage, node.to_prompt_text)
        return formatter()
    
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

    def format_jump_modules(self) -> str:
        """Format the jumpable module list as prompt-ready text (slot: jump_modules).

        列出 module_map 中除当前模块外的全部模块（编码 + 名称 + 描述），
        供 NLU prompt 输出 jump_module 字段时参照。边界淡化：不问邻接图，
        只要目标在 module_map 中即合法跳转。
        """
        parts = []
        for code, module in self.module_map.items():
            if code == self.current_module_code:
                continue
            name = getattr(module, "module_name", "") or code
            desc = getattr(module, "module_description", "") or ""
            seg = f"- {code}（{name}）"
            if desc:
                seg += f"：{desc}"
            parts.append(seg)
        return "\n".join(parts) if parts else "暂无可跳转模块"

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
) -> Optional[str|None]:
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


