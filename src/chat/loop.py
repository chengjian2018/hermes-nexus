"""
Agent dialogue loop — builds system_prompt from module, injects tools, runs multi-turn dialogue.

Supports:
- Two-layer tool filtering: pattern permissions + module.use_tools
- Multi-turn tool calling (ReAct style)
- [jump xx] module/node jumps
"""

import json
import logging
import re
from typing import Any, Dict, List

from src.chat.session import Session
from src.llm.resolve import build_provider
from src.tools.register import registry as tool_registry

logger = logging.getLogger(__name__)

# Jump tag regex
_JUMP_PATTERN = re.compile(r'\[jump\s+(\w+)\]')

# Max tool calling rounds to prevent infinite loops
_MAX_TOOL_ROUNDS = 10


def conversation(
    session: Session,
    module,
    llm_config: Dict[str, Any],
) -> str:
    """Agent dialogue loop.

    Builds system_prompt from module, injects the tools declared by module,
    runs multi-turn LLM dialogue (with tool calling) until the LLM returns plain text.

    Args:
        session: current session
        module: current module object (AgentModule)
        llm_config: LLM config dict with code, model, temperature, etc.

    Returns:
        str: final reply text
    """
    cxt = session.cxt

    # Build the provider via the unified entry: yaml config values
    # (api_base / api_key / api_key_env / timeout / max_retries) are
    # forwarded as instantiate() overrides
    provider = build_provider(llm_config)

    # ------------------------------------------------------------------
    # 1. Build system_prompt from module
    # ------------------------------------------------------------------
    system_prompt = _build_system_prompt(module, cxt)

    # ------------------------------------------------------------------
    # 2. Tool injection: filter by pattern permissions + module.use_tools
    # ------------------------------------------------------------------
    tools = _resolve_tools(module, session.pattern)

    # ------------------------------------------------------------------
    # 3. Build initial message list
    # ------------------------------------------------------------------
    messages = _build_messages(system_prompt, cxt)

    # ------------------------------------------------------------------
    # 4. Agent multi-turn loop (with tool calling)
    # ------------------------------------------------------------------
    model = llm_config["model"]
    temperature = llm_config.get("temperature", 0.7)
    max_tokens = llm_config.get("max_tokens", 2048)

    for round_idx in range(_MAX_TOOL_ROUNDS):
        logger.info(
            "Agent loop 第 %d 轮: session=%s, module=%s, tools=%d",
            round_idx + 1,
            cxt.session_id,
            module.module_code,
            len(tools),
        )

        # Call LLM
        if tools:
            result = provider.chat_completion(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
                tool_choice="auto",
            )
        else:
            result = provider.chat_completion(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        content = result.get("content", "") or ""
        tool_calls = result.get("tool_calls", []) or []

        # No tool calls means the LLM gave the final reply
        if not tool_calls:
            logger.info("Agent loop 完成，共 %d 轮", round_idx + 1)
            response = content

            # Handle [jump xx] jump tags
            response = _process_jump_tags(response, session)
            return response

        # Tool calls present: execute tools and append results to messages
        logger.info(
            "Agent loop 第 %d 轮检测到 %d 个 tool calls",
            round_idx + 1,
            len(tool_calls),
        )

        # Append assistant message (with tool_calls)
        assistant_msg: Dict[str, Any] = {
            "role": "assistant",
            "content": content or None,
            "tool_calls": tool_calls,
        }
        messages.append(assistant_msg)

        # Execute each tool call and append its result
        for tc in tool_calls:
            tool_name = tc.get("function", {}).get("name", "")
            tool_args_str = tc.get("function", {}).get("arguments", "{}")
            tc_id = tc.get("id", "")

            try:
                tool_args = (
                    json.loads(tool_args_str)
                    if isinstance(tool_args_str, str)
                    else tool_args_str
                )
            except json.JSONDecodeError:
                tool_args = {}

            logger.info("执行工具: %s(%s)", tool_name, tool_args_str[:200])

            tool_result = _execute_tool(tool_name, tool_args)

            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": tool_result,
            })

    # Exceeded max rounds, force return
    logger.warning(
        "Agent loop 达到最大轮次 %d，强制终止: session=%s",
        _MAX_TOOL_ROUNDS,
        cxt.session_id,
    )
    return "抱歉，处理超时，请稍后重试。"


# ---------------------------------------------------------------------------
# System Prompt construction
# ---------------------------------------------------------------------------

def _build_system_prompt(module, cxt) -> str:
    """Build system_prompt from module.

    Concatenates the module's base_prompt with key info from the dialogue context (slots, task info, etc.).

    Args:
        module: current module object
        cxt: dialogue context

    Returns:
        str: complete system prompt
    """
    parts = []

    # Module base prompt
    base_prompt = module.base_prompt or ""
    if base_prompt:
        parts.append(base_prompt)

    # Task info
    task_info = cxt.metadata.get("task_info", {})
    if task_info:
        parts.append("\n## 任务信息")
        for key, value in task_info.items():
            parts.append(f"- {key}: {value}")

    # Filled slots
    if cxt.filled_slots:
        parts.append("\n## 已填充槽位")
        parts.append(json.dumps(cxt.filled_slots, ensure_ascii=False, indent=2))

    # Module jump instructions
    parts.append("""
## 模块跳转
如需切换到其他模块，请在回复末尾使用 [jump 模块code] 标识。
例如：[jump faq] 表示跳转到 faq 模块。
""")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool resolution and filtering
# ---------------------------------------------------------------------------

def _resolve_tools(module, pattern=None) -> List[Dict[str, Any]]:
    """Filter tool definitions by pattern permissions + module.use_tools.

    Two-layer filtering:
    1. **Pattern layer**: get the tool set allowed for the current pattern +
       module via :meth:`ToolRegistry.get_allowed_tools_for_pattern`.
       - tool's ``allowed_patterns`` is None → denied (deny by default)
       - tool's ``allowed_patterns`` contains ``"*"`` or the current pattern code → allowed
    2. **Module layer**: if ``module.use_tools`` is non-empty, take the
       intersection; if empty, use all tools allowed by the pattern layer.

    Args:
        module: current module object
        pattern: current dialogue pattern (optional, for permission checks)

    Returns:
        list: OpenAI-format tool definitions
    """
    # Step 1: Pattern-level access control
    pattern_code = pattern.code if pattern is not None else ""
    module_code = module.module_code or ""

    if pattern_code:
        allowed_tool_names = tool_registry.get_allowed_tools_for_pattern(
            pattern_code, module_code
        )
    else:
        # Without pattern context, only allow tools with allowed_patterns={"*": True}
        # (pure general-purpose tools); deny all tools without pattern permissions
        allowed_tool_names = tool_registry.get_allowed_tools_for_pattern(
            "*", module_code
        )

    # Step 2: Module-level filter (module.use_tools)
    use_tools = module.use_tools or []
    if use_tools:
        tool_names_from_module = set(use_tools)

        missing = tool_names_from_module - allowed_tool_names
        if missing:
            # Tool declared in module.use_tools but not authorized by pattern layer or not registered
            logger.warning(
                "模块 '%s' 声明的工具不可用: %s (未授权或未注册)",
                module_code, missing,
            )

        # Intersect pattern layer with module layer
        tool_names = allowed_tool_names & tool_names_from_module
    else:
        # No use_tools declared: use all tools allowed by pattern layer
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


# ---------------------------------------------------------------------------
# Message list construction
# ---------------------------------------------------------------------------

def _build_messages(system_prompt: str, cxt) -> List[Dict[str, Any]]:
    """Build the message list sent to the LLM.

    Includes the system prompt and user/assistant messages from dialogue history.

    Args:
        system_prompt: system prompt text
        cxt: dialogue context

    Returns:
        list: list of message dicts
    """
    messages: List[Dict[str, Any]] = []

    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    # Add dialogue history (keep only user and assistant messages)
    for msg in cxt.history:
        if msg.role in ("user", "assistant"):
            messages.append({"role": msg.role, "content": msg.content})

    return messages


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _execute_tool(tool_name: str, tool_args: Dict[str, Any]) -> str:
    """Execute a single tool call.

    Args:
        tool_name: tool name
        tool_args: tool arguments

    Returns:
        str: tool execution result (JSON string)
    """
    try:
        result = tool_registry.dispatch(tool_name, tool_args)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.exception("工具执行异常: %s", tool_name)
        return json.dumps({"error": f"工具执行失败: {e}"}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Jump tag handling
# ---------------------------------------------------------------------------

def _process_jump_tags(response: str, session: Session) -> str:
    """Handle [jump xx] jump tags in the LLM reply.

    Args:
        response: raw LLM reply text
        session: current session

    Returns:
        str: reply text with jump tags removed
    """
    matches = _JUMP_PATTERN.findall(response)
    if not matches:
        return response

    cxt = session.cxt
    for jump_target in matches:
        if jump_target in cxt.module_map:
            cxt.current_module_code = jump_target
            cxt.current_node_code = None
            logger.info("[jump] 跳转到模块: %s", jump_target)
        elif jump_target in cxt.node_map:
            cxt.current_node_code = jump_target
            target_node = cxt.node_map[jump_target]
            for mod_code, mod in cxt.module_map.items():
                if target_node in mod.module_nodes:
                    cxt.current_module_code = mod_code
                    logger.info(
                        "[jump] 跳转到节点: %s (模块: %s)", jump_target, mod_code
                    )
                    break
            else:
                logger.warning(
                    "[jump] 节点 '%s' 不属于任何已知模块", jump_target
                )
        else:
            logger.warning(
                "[jump] 跳转目标 '%s' 不在 module_map 或 node_map 中", jump_target
            )

    return _JUMP_PATTERN.sub("", response).strip()