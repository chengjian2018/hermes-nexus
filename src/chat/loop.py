"""
Agent dialogue loop — run_agent 双原语执行器（inject 投影直接答 / transfer 拦截立即返回）。

Supports:
- Two-layer tool filtering: pattern permissions + module.use_tools
- Lent tools from neighbor modules (via ModuleLink.lend_tools)
- transfer_to_XX tools generated per sub_modules link; on call, dispatch()
  transfers state immediately and the turn ends (spec §3.3)
- Tool round-trips recorded into DialogueContext history
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.chat.session import Session
from src.dialogue.base import DialogueContext, fill_prompt_template
from src.dialogue.dispatch import ModuleDispatch, dispatch
from src.llm.resolve import build_provider
from src.prompt import (
    AGENT_PROJECTION_RECALL_PROMPT,
    AGENT_TAKEOVER_PROMPT,
    AGENT_TEAM_RULES_PROMPT,
)
from src.tools.register import registry as tool_registry

logger = logging.getLogger(__name__)

TRANSFER_TOOL_PREFIX = "transfer_to_"

# Max tool calling rounds to prevent infinite loops
_MAX_TOOL_ROUNDS = 10

# 系统提示总长超过该值时告警（spec §5 投影膨胀观测）
_PROMPT_LENGTH_WARN = 4000


@dataclass
class TurnResult:
    """单模块单轮执行结果：reply 与 dispatch_event 互斥（spec §3.2）。"""

    reply: Optional[str] = None
    dispatch_event: Optional[ModuleDispatch] = None


def conversation(
    session: Session,
    module,
    llm_config: Dict[str, Any],
) -> str:
    """兼容 wrapper：调 run_agent，dispatch_event 为 None 时返回 reply。"""
    result = run_agent(session, module, llm_config)
    return result.reply or ""


def run_agent(
    session: Session,
    module,
    llm_config: Dict[str, Any],
    force_close: bool = False,
) -> TurnResult:
    """执行单个 AGENT 模块一轮：inject 直接答 / transfer 立即返回（spec §3.3）。

    Args:
        session: current session
        module: current module object (AgentModule)
        llm_config: LLM config dict with code, model, temperature, etc.
        force_close: 强制收尾（max_hops 耗尽）：追加"勿再移交"提示且不注入 transfer 工具

    Returns:
        TurnResult: reply 与 dispatch_event 互斥。
    """
    cxt = session.cxt
    provider = build_provider(llm_config)

    system_prompt = _build_system_prompt(module, cxt)
    if force_close:
        system_prompt = (system_prompt + "\n请直接回应用户，勿再移交。").strip()
    if len(system_prompt) > _PROMPT_LENGTH_WARN:
        logger.warning(
            "Agent system_prompt 过长 (%d 字符): session=%s, module=%s（投影膨胀观测）",
            len(system_prompt), cxt.session_id, module.module_code,
        )

    own_tools = _resolve_tools(module, session.pattern)
    lent_schemas, lent_by = _resolve_lent_tools(module, session.pattern)
    transfer_tools = [] if force_close else build_transfer_tools(module, cxt.module_map)
    tools = own_tools + lent_schemas + transfer_tools

    messages = _build_messages(system_prompt, cxt)

    model = llm_config["model"]
    temperature = llm_config.get("temperature", 0.7)
    max_tokens = llm_config.get("max_tokens", 2048)

    for round_idx in range(_MAX_TOOL_ROUNDS):
        logger.info(
            "Agent loop 第 %d 轮: session=%s, module=%s, tools=%d",
            round_idx + 1, cxt.session_id, module.module_code, len(tools),
        )

        if tools:
            result = provider.chat_completion(
                messages=messages, model=model, temperature=temperature,
                max_tokens=max_tokens, tools=tools, tool_choice="auto",
            )
        else:
            result = provider.chat_completion(
                messages=messages, model=model, temperature=temperature,
                max_tokens=max_tokens,
            )

        content = result.get("content", "") or ""
        tool_calls = result.get("tool_calls", []) or []

        # 无工具调用 → inject 原语：直接回答
        if not tool_calls:
            logger.info("Agent loop 完成，共 %d 轮", round_idx + 1)
            return TurnResult(reply=content)

        # 本轮工具调用里是否含 transfer
        transfer_call = next(
            (tc for tc in tool_calls
             if tc.get("function", {}).get("name", "").startswith(TRANSFER_TOOL_PREFIX)),
            None,
        )
        if transfer_call is not None:
            # A 的 content 不出口但保留进 history（spec §3.3）
            if content:
                cxt.add_message("assistant", content, stage="agent",
                                metadata={"suppressed": True})
            _execute_transfer(session, transfer_call)
            # 状态已由 _execute_transfer 内 dispatch() 转移；event 供 chat 层消费
            return TurnResult(dispatch_event=ModuleDispatch(
                target_module_code=transfer_call["function"]["name"][
                    len(TRANSFER_TOOL_PREFIX):],
                reason=_parse_args(transfer_call).get("reason", "")
                if isinstance(_parse_args(transfer_call), dict) else "",
                source="handoff_tool",
            ))

        # 普通工具调用：执行、落 history、回填
        messages.append({"role": "assistant", "content": content or None,
                         "tool_calls": tool_calls})
        for tc in tool_calls:
            name = tc.get("function", {}).get("name", "")
            args = _parse_args(tc)
            tool_result = _execute_tool(name, args)
            source = lent_by.get(name)
            if source:
                cxt.metadata["served_by_projection"] = source
            metadata = {"tool_name": name}
            if source:
                metadata["lent_by"] = source
            cxt.add_message("tool", tool_result, stage="agent", metadata=metadata)
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "content": tool_result})

    logger.warning(
        "Agent loop 达到最大轮次 %d，强制终止: session=%s",
        _MAX_TOOL_ROUNDS, cxt.session_id,
    )
    return TurnResult(reply="抱歉，处理超时，请稍后重试。")


# ---------------------------------------------------------------------------
# Projection / transfer tool builders
# ---------------------------------------------------------------------------

def build_projection_block(module, module_map) -> str:
    """邻接投影块：每条 lend_knowledge 边一片（spec §4 §3.2）。"""
    blocks = []
    for link in module.sub_modules:
        if not link.lend_knowledge:
            continue
        target = module_map.get(link.target)
        if target is None:
            continue
        parts = [f"## 邻接能力：{target.module_name}（{target.module_code}）"]
        parts.append(target.to_projection_text())
        if link.lend_tools:
            parts.append(f"- 可借工具：{', '.join(link.lend_tools)}")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def build_transfer_tools(module, module_map) -> list:
    """由 sub_modules 逐边生成 transfer 工具（spec §4 §3.3）。"""
    tools = []
    for link in module.sub_modules:
        target = module_map.get(link.target)
        if target is None:
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": f"{TRANSFER_TOOL_PREFIX}{link.target}",
                "description": (
                    f"移交给【{target.module_name}】。适用：该域的多轮深入流程。"
                    f"不适用：一句话或一次工具能解决的请求——那类直接自己处理。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"reason": {
                        "type": "string",
                        "description": "移交原因及已收集的用户信息摘要，供接手方无缝承接",
                    }},
                    "required": ["reason"],
                },
            },
        })
    return tools


# ---------------------------------------------------------------------------
# System Prompt construction
# ---------------------------------------------------------------------------

def _build_system_prompt(module, cxt: DialogueContext) -> str:
    """五块结构：base_prompt + 投影块 + 承接块 + 回看块 + 任务/槽位。"""
    parts = []

    if module.base_prompt:
        parts.append(module.base_prompt)

    projection = build_projection_block(module, cxt.module_map)
    if projection:
        parts.append(projection)
        parts.append(AGENT_TEAM_RULES_PROMPT)

    handoff = cxt.metadata.get("handoff_context")
    if handoff:
        parts.append(fill_prompt_template(AGENT_TAKEOVER_PROMPT, {
            "from_module": handoff.get("from", ""),
            "reason": handoff.get("reason", "") or "（无补充信息）",
        }))

    served = cxt.metadata.get("served_by_projection")
    if served:
        parts.append(fill_prompt_template(AGENT_PROJECTION_RECALL_PROMPT, {
            "projection_source": served,
        }))

    task_info = cxt.metadata.get("task_info", {})
    if task_info:
        parts.append("\n## 任务信息")
        for key, value in task_info.items():
            parts.append(f"- {key}: {value}")

    if cxt.filled_slots:
        parts.append("\n## 已填充槽位")
        parts.append(json.dumps(cxt.filled_slots, ensure_ascii=False, indent=2))

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool resolution and filtering
# ---------------------------------------------------------------------------

def _resolve_tools(module, pattern=None) -> List[Dict[str, Any]]:
    """Filter tool definitions by pattern permissions + module.use_tools.

    Two-layer filtering:
    1. **Pattern layer**: get the tool set allowed for the current pattern +
       module via :meth:`ToolRegistry.get_allowed_tools_for_pattern`.
    2. **Module layer**: if ``module.use_tools`` is non-empty, take the
       intersection; if empty, use all tools allowed by the pattern layer.
    """
    pattern_code = pattern.code if pattern is not None else ""
    module_code = module.module_code or ""

    if pattern_code:
        allowed_tool_names = tool_registry.get_allowed_tools_for_pattern(
            pattern_code, module_code
        )
    else:
        allowed_tool_names = tool_registry.get_allowed_tools_for_pattern(
            "*", module_code
        )

    use_tools = module.use_tools or []
    if use_tools:
        tool_names_from_module = set(use_tools)

        missing = tool_names_from_module - allowed_tool_names
        if missing:
            logger.warning(
                "模块 '%s' 声明的工具不可用: %s (未授权或未注册)",
                module_code, missing,
            )

        tool_names = allowed_tool_names & tool_names_from_module
    else:
        tool_names = allowed_tool_names

    if not tool_names:
        logger.info(
            "模块 '%s' (pattern='%s') 无可用工具",
            module_code, pattern_code,
        )
        return []

    tool_schemas = tool_registry.get_definitions(tool_names)

    logger.info(
        "模块 '%s' (pattern='%s') 可用工具: %s",
        module_code,
        pattern_code,
        [t.get("function", {}).get("name", "?") for t in tool_schemas],
    )
    return tool_schemas


def _resolve_lent_tools(module, pattern):
    """解析借入工具 schema 与 name→来源域映射（spec §3.3 权限）。

    Returns:
        (schemas, lent_by)：schemas 为 OpenAI 格式列表；lent_by 为
        {tool_name: 来源 module_code}。
    """
    schemas, lent_by = [], {}
    for link in module.sub_modules:
        if not link.lend_tools:
            continue
        target = (pattern.module_map if pattern else {}).get(link.target)
        if target is None:
            continue
        allowed = set(target.use_tools or []) & set(link.lend_tools)
        for schema in tool_registry.get_definitions(allowed):
            name = schema["function"]["name"]
            schemas.append(schema)
            lent_by[name] = link.target
    return schemas, lent_by


# ---------------------------------------------------------------------------
# Message list construction
# ---------------------------------------------------------------------------

def _build_messages(system_prompt: str, cxt) -> List[Dict[str, Any]]:
    """Build the message list sent to the LLM (system prompt + history)."""
    messages: List[Dict[str, Any]] = []

    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    for msg in cxt.history:
        if msg.role in ("user", "assistant"):
            messages.append({"role": msg.role, "content": msg.content})

    return messages


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _parse_args(tc) -> Dict[str, Any]:
    """Parse a tool_call's arguments string into a dict."""
    args_str = tc.get("function", {}).get("arguments", "{}")
    try:
        args = json.loads(args_str) if isinstance(args_str, str) else args_str
    except json.JSONDecodeError:
        args = {}
    return args if isinstance(args, dict) else {}


def _execute_tool(tool_name: str, tool_args: Dict[str, Any]) -> str:
    """Execute a single tool call, returning a JSON string result."""
    try:
        result = tool_registry.dispatch(tool_name, tool_args)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.exception("工具执行异常: %s", tool_name)
        return json.dumps({"error": f"工具执行失败: {e}"}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Transfer handling
# ---------------------------------------------------------------------------

def _execute_transfer(session: Session, transfer_call) -> None:
    """解析 transfer 工具调用并执行 dispatch()（含 reason）。"""
    cxt = session.cxt
    name = transfer_call.get("function", {}).get("name", "")
    target = name[len(TRANSFER_TOOL_PREFIX):]
    args = _parse_args(transfer_call)
    reason = args.get("reason", "") if isinstance(args, dict) else ""
    ok = dispatch(cxt, ModuleDispatch(target_module_code=target,
                                      reason=reason, source="handoff_tool"))
    if not ok:
        logger.warning("[transfer] 目标 %s 转移失败（不邻接/回弹），本轮继续", target)
