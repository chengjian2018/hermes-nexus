"""Agent loop hooks —— run_agent 的多点叠加扩展机制（点位事件 + 声明解析 + dispatcher）。

定位（与既有扩展点的边界，见 docs/plans/2026-09-04-agent-loop-hooks.md）：
- hooks = **多点叠加**：同一点位挂 N 个 hook 按声明序执行，互不排斥；
  改 messages 归 ``module.messages_builder``（产物终态，hook 只读），
  换整个执行器归 ``AgentRunner``——本机制不提供第二种替换路径。
- 能力等级 = ②变更：可改写工具调用（name/args）与工具结果、可注入
  system prompt 片段；**无③控制权**（不能拦截/丢弃调用/制造移交——
  非法调用的拦截是 loop 主流程的确定性校验，不是 hook 决策）。

声明（pattern 级，module 层整体替换，不 merge——同 stage 槽位语义）::

    pattern.agent_hooks = {
        "on_agent_start":  [fetch_shop_data],   # P1 返回 Optional[str] 片段
        "on_tool_call":    [fix_tool_alias],    # P4 返回 Optional[RewriteToolCall]
        "on_tool_result":  [redact_secrets],    # P5 返回 Optional[str]
        ...
    }

错误语义（防御性）：hook 异常一律吞掉记日志 + 回退原值，绝不阻断对话；
变更 hook 失败 = 用进 chain 前的当前值继续。

只读纪律（docstring 约定，框架不做深拷贝——每轮拷贝 messages 代价不成
比例）：``AgentStartEvent.cxt`` 与 ``LLMCallEvent.messages`` 为引用传递，
hook 不得原地修改；要改 messages 的诉求走 messages_builder。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 全部合法点位（消费方见 loop.run_agent 的挂载点）
HOOK_POINTS = (
    "on_agent_start",    # P1 进 loop、build system prompt 前（注入）
    "on_llm_call",       # P2 每轮 LLM 调用前（观察）
    "on_llm_response",   # P3 每轮 LLM 返回后（观察）
    "on_tool_call",      # P4 单工具执行前（变更：name/args）
    "on_tool_result",    # P5 单工具执行后、落 history 前（变更：结果串）
    "on_transfer",       # P6 transfer 命中写跳转事件时（观察）
    "on_agent_end",      # P7 出口：reply / transfer / max_rounds（观察）
)

HookMap = Dict[str, List[Callable[..., Any]]]


# ============================================================================
# 事件类型（每点位一份；全部携带 session_id / module_code）
# ============================================================================

@dataclass
class AgentStartEvent:
    """P1：进 loop、build system prompt 前。返回 Optional[str] 注入片段。

    cxt 为引用传递（只读纪律）：hook 从中读取槽位/metadata 决定取什么数，
    但不写回——注入产物只经返回值走，避免跨轮/跨 hop 状态泄漏。
    """

    session_id: str
    module_code: str
    cxt: Any


@dataclass
class LLMCallEvent:
    """P2：每轮 LLM 调用前。观察（返回值被忽略）。messages 为引用（只读纪律）。"""

    session_id: str
    module_code: str
    round_idx: int
    messages: List[Dict[str, Any]]
    model: str


@dataclass
class LLMResponseEvent:
    """P3：每轮 LLM 返回后（content/tool_calls 已解析）。观察。"""

    session_id: str
    module_code: str
    round_idx: int
    content: str
    tool_calls: List[Dict[str, Any]]


@dataclass
class ToolCallEvent:
    """P4：单个工具执行前。返回 Optional[RewriteToolCall]（链式：前一 hook
    的改写反映进本 event 后再喂下一 hook）。"""

    session_id: str
    module_code: str
    round_idx: int
    tool_name: str
    args: Dict[str, Any]


@dataclass
class RewriteToolCall:
    """P4 的改写返回值：仅给定的字段生效（部分改写），None = 不改。"""

    name: Optional[str] = None
    args: Optional[Dict[str, Any]] = None


@dataclass
class ToolResultEvent:
    """P5：单个工具执行后、落 history 前。返回 Optional[str] 替换结果
    （链式同 P4；改写后的结果同时进 LLM 回填与落库，不分叉）。"""

    session_id: str
    module_code: str
    round_idx: int
    tool_name: str
    tool_call_id: str
    result: str


@dataclass
class TransferEvent:
    """P6：transfer 命中、写跳转事件时。观察。"""

    session_id: str
    module_code: str
    round_idx: int
    target: str
    reason: str


@dataclass
class AgentEndEvent:
    """P7：run_agent 三个出口。观察。

    outcome: "reply"（直接答，reply 即出口文本）/ "transfer"（静默移交，
    transfer_target 为目标模块）/ "max_rounds"（超轮次，reply 为兜底话术）。
    """

    session_id: str
    module_code: str
    rounds: int
    outcome: str
    reply: Optional[str] = None
    transfer_target: str = ""


# ============================================================================
# 声明解析（module 整体替换 pattern；非法配置降级跳过）
# ============================================================================

def resolve_agent_hooks(module: Any, pattern: Any = None) -> HookMap:
    """解析生效的 hooks：module.agent_hooks 非空整体替换，否则 pattern 级。

    防御性校验（同 stage_slots 降级风格）：声明非 dict / 点位名未知 /
    条目非 callable → warning 跳过该项，不抛错。
    """
    raw = getattr(module, "agent_hooks", None)
    if not raw:
        raw = getattr(pattern, "agent_hooks", None)
    if not raw:
        return {}

    if not isinstance(raw, dict):
        logger.warning(
            "[agent_hooks] agent_hooks 须为 {点位: [hook,...]} dict，忽略: %r", raw,
        )
        return {}

    hooks: HookMap = {}
    for point, entries in raw.items():
        if point not in HOOK_POINTS:
            logger.warning(
                "[agent_hooks] 未知点位 %r（合法点位: %s），跳过",
                point, ", ".join(HOOK_POINTS),
            )
            continue
        if not isinstance(entries, (list, tuple)):
            entries = [entries]
        valid = [h for h in entries if callable(h)]
        dropped = len(entries) - len(valid)
        if dropped:
            logger.warning(
                "[agent_hooks] 点位 %s 含 %d 个非 callable 条目，已跳过",
                point, dropped,
            )
        if valid:
            hooks[point] = valid
    return hooks


# ============================================================================
# Dispatcher（异常收口：吞掉记日志 + 回退原值）
# ============================================================================

def fire(hooks: HookMap, point: str, event: Any) -> None:
    """观察点通用派发：逐 hook 执行，异常吞掉记日志（返回值不接）。"""
    for hook in hooks.get(point, ()):
        try:
            hook(event)
        except Exception:
            logger.exception(
                "[agent_hooks] %s hook 异常（忽略，不影响对话）", point,
            )


def collect_fragments(hooks: HookMap, event: AgentStartEvent) -> List[str]:
    """P1 派发：收集注入片段（声明序）；异常 hook 的片段丢弃。"""
    fragments: List[str] = []
    for hook in hooks.get("on_agent_start", ()):
        try:
            frag = hook(event)
        except Exception:
            logger.exception(
                "[agent_hooks] on_agent_start hook 异常，丢弃该片段", 
            )
            continue
        if frag:
            fragments.append(str(frag))
    return fragments


def _normalize_rewrite(rw: Any) -> Optional[RewriteToolCall]:
    """容忍 RewriteToolCall 与同形 dict 两种返回形态；其余视作未改写。"""
    if isinstance(rw, RewriteToolCall):
        return rw
    if isinstance(rw, dict):
        name = rw.get("name")
        args = rw.get("args")
        if name is None and args is None:
            return None
        return RewriteToolCall(name=name, args=args)
    return None


def rewrite_tool_call(
    hooks: HookMap, event: ToolCallEvent, allowed_names: set,
    reserved_prefix: str = "",
) -> Tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]:
    """P4 派发：链式改写 name/args（hook₁ 的改写反映进 event 再喂 hook₂）。

    守卫（规则 2 的 dispatcher 层）：改写 name ∉ allowed_names（本轮 own+lent）
    或带 reserved_prefix（transfer_to_）→ 拒绝该次改名 + warning；同一返回值
    中合法部分照常应用（name 被拒、args 照用）。dispatcher 只保证"不让改写
    变得更糟"——拒绝后回落到的名字若仍非法（原始调用本就是幻觉名），由
    loop 主流程的最终校验兜底拦截。

    Returns:
        (final_name, final_args, original)：original 为
        ``{"name": 原名, "args": 原args}``，仅当实际发生改写时非 None（审计用）。
    """
    orig_name, orig_args = event.tool_name, event.args
    for hook in hooks.get("on_tool_call", ()):
        try:
            rw = _normalize_rewrite(hook(event))
        except Exception:
            logger.exception(
                "[agent_hooks] on_tool_call hook 异常，保留当前值继续",
            )
            continue
        if rw is None:
            continue

        if rw.args is not None:
            event.args = rw.args
        if rw.name is not None and rw.name != event.tool_name:
            if reserved_prefix and rw.name.startswith(reserved_prefix):
                logger.warning(
                    "[agent_hooks] 改写 name '%s' 带 reserved 前缀，拒绝改名"
                    "（保留 '%s'）", rw.name, event.tool_name,
                )
            elif rw.name not in allowed_names:
                logger.warning(
                    "[agent_hooks] 改写 name '%s' 不在本轮可用工具中，拒绝改名"
                    "（保留 '%s'）", rw.name, event.tool_name,
                )
            else:
                event.tool_name = rw.name

    original = None
    if event.tool_name != orig_name or event.args is not orig_args:
        original = {"name": orig_name, "args": orig_args}
    return event.tool_name, event.args, original


def rewrite_tool_result(
    hooks: HookMap, event: ToolResultEvent,
) -> Tuple[str, Optional[str]]:
    """P5 派发：链式改写结果串。

    Returns:
        (final_result, original)：original 仅当实际发生改写时非 None。
    """
    orig_result = event.result
    for hook in hooks.get("on_tool_result", ()):
        try:
            new = hook(event)
        except Exception:
            logger.exception(
                "[agent_hooks] on_tool_result hook 异常，保留当前值继续",
            )
            continue
        if new is None:
            continue
        event.result = new if isinstance(new, str) else str(new)
    original = None if event.result is orig_result else orig_result
    return event.result, original
