"""
Agent dialogue loop — run_agent 双原语执行器（inject 投影直接答 / transfer 写跳转事件）。

Supports:
- Two-layer tool filtering: pattern permissions + module.use_tools
- Lent tools from neighbor modules (via ModuleLink.lend_tools)
- transfer_to_XX tools generated per sub_modules link; on call, a
  ModuleJumpEvent is appended to cxt.actions and the module's turn ends —
  the chat layer consumes the event and reroutes (no adjacency check /
  rebound rejection; target existence in module_map is the only guard)
- Tool round-trips recorded into DialogueContext history
- Pluggable hooks at loop points (pattern.agent_hooks declaration; events,
  dispatch and guard semantics see src/chat/agent_hooks.py)
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.chat.agent_hooks import (
    AgentEndEvent,
    AgentStartEvent,
    LLMCallEvent,
    LLMResponseEvent,
    ToolCallEvent,
    ToolResultEvent,
    TransferEvent,
    collect_fragments,
    fire,
    resolve_agent_hooks,
    rewrite_tool_call,
    rewrite_tool_result,
)
from src.chat.messages import build_agent_messages
from src.chat.session import Session
from src.dialogue.base import (
    ModuleJumpEvent,
    encode_tool_call_content,
)
from src.llm.resolve import build_provider
from src.tools.register import registry as tool_registry

logger = logging.getLogger(__name__)

TRANSFER_TOOL_PREFIX = "transfer_to_"

# Max tool calling rounds to prevent infinite loops
_MAX_TOOL_ROUNDS = 10

# 系统提示总长超过该值时告警（投影膨胀观测）
_PROMPT_LENGTH_WARN = 4000


@dataclass
class TurnResult:
    """单模块单轮执行结果。

    reply 为空 + cxt.actions 含 ModuleJumpEvent = 本模块静默移交，
    chat 层 hop 循环消费事件重路由；其余情况 reply 即出口回复。
    actions 为预留通道（与 cxt.actions 同形），现有执行器不产生。
    """

    reply: Optional[str] = None
    actions: List[Dict[str, Any]] = field(default_factory=list)


def conversation(
    session: Session,
    module,
    llm_config: Dict[str, Any],
) -> str:
    """兼容 wrapper：调 run_agent，返回 reply（移交轮由 chat 层续答）。"""
    result = run_agent(session, module, llm_config)
    return result.reply or ""


def run_agent(
    session: Session,
    module,
    llm_config: Dict[str, Any],
    force_close: bool = False,
) -> TurnResult:
    """执行单个 AGENT 模块一轮：inject 直接答 / transfer 写跳转事件返回。

    Args:
        session: current session
        module: current module object (AgentModule)
        llm_config: LLM config dict with code, model, temperature, etc.
        force_close: 强制收尾（max_hops 耗尽）：追加"勿再移交"提示且不注入 transfer 工具

    Returns:
        TurnResult: reply 即回复；transfer 命中时 reply 为空，跳转事件
        已写入 cxt.actions（ModuleJumpEvent），由 chat 层统一消费。
    """
    cxt = session.cxt
    provider = build_provider(llm_config)

    # agent loop hooks（pattern 级声明，module 层 agent_hooks 整体替换；
    # 空声明时各挂载点零开销直通）
    hooks = resolve_agent_hooks(module, session.pattern)

    # P1 on_agent_start：进 loop、messages 组装前取数注入。片段经
    # extra_blocks 送达 builder（契约要求包含），hook 不写 cxt
    fragments = collect_fragments(
        hooks,
        AgentStartEvent(session_id=cxt.session_id,
                        module_code=module.module_code, cxt=cxt),
    ) if hooks else []

    own_tools = _resolve_tools(module, session.pattern)
    lent_schemas, lent_by = _resolve_lent_tools(module, session.pattern)
    transfer_tools = [] if force_close else build_transfer_tools(module, cxt.module_map)
    tools = own_tools + lent_schemas + transfer_tools
    # 主流程校验/ P4 守卫的本轮可用集合（own + lent；transfer 工具不在内——
    # transfer 轮不走工具派发，改名走私前缀由守卫与校验双重拦截）
    allowed_names = {t.get("function", {}).get("name", "")
                     for t in own_tools + lent_schemas}

    # Messages 一体化构建（system 内容与列表装配同源）：
    # module.messages_builder > pattern.messages_builder > 默认三段式
    messages = build_agent_messages(module, cxt, pattern=session.pattern,
                                    extra_blocks=fragments)
    # force_close 收尾后缀框架侧强制（控制流语义，任何 builder 不可破坏）
    if force_close:
        _append_force_close_suffix(messages)
    _warn_prompt_length(messages, cxt, module)

    model = llm_config["model"]
    temperature = llm_config.get("temperature", 0.7)
    max_tokens = llm_config.get("max_tokens", 2048)

    for round_idx in range(_MAX_TOOL_ROUNDS):
        logger.info(
            "Agent loop 第 %d 轮: session=%s, module=%s, tools=%d",
            round_idx + 1, cxt.session_id, module.module_code, len(tools),
        )

        # P2 on_llm_call：每轮 LLM 调用前（messages 引用传递，只读纪律）
        if hooks:
            fire(hooks, "on_llm_call", LLMCallEvent(
                session_id=cxt.session_id, module_code=module.module_code,
                round_idx=round_idx, messages=messages, model=model))

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

        # P3 on_llm_response：每轮 LLM 返回后（content/tool_calls 已解析）
        if hooks:
            fire(hooks, "on_llm_response", LLMResponseEvent(
                session_id=cxt.session_id, module_code=module.module_code,
                round_idx=round_idx, content=content,
                tool_calls=tool_calls))

        # 无工具调用 → inject 原语：直接回答
        if not tool_calls:
            logger.info("Agent loop 完成，共 %d 轮", round_idx + 1)
            # P7 on_agent_end：直接答出口
            if hooks:
                fire(hooks, "on_agent_end", AgentEndEvent(
                    session_id=cxt.session_id, module_code=module.module_code,
                    rounds=round_idx + 1, outcome="reply", reply=content))
            return TurnResult(reply=content)

        # 本轮工具调用里是否含 transfer
        transfer_call = next(
            (tc for tc in tool_calls
             if tc.get("function", {}).get("name", "").startswith(TRANSFER_TOOL_PREFIX)),
            None,
        )
        if transfer_call is not None:
            target = transfer_call["function"]["name"][len(TRANSFER_TOOL_PREFIX):]
            transfer_reason = _transfer_reason(transfer_call)

            # 目标不存在（无 sub_modules 边 / 幻觉调用）：错误回填继续 loop，
            # 让 LLM 自行换路（真实 OpenAI 兼容 API 要求每个 tool_call_id 有应答）
            if target not in cxt.module_map:
                logger.warning(
                    "[transfer] 目标 %s 不在 module_map 中，错误回填继续 loop",
                    target,
                )
                err = json.dumps(
                    {"error": "转移目标不存在，请直接回应用户"},
                    ensure_ascii=False)
                _dispatch_tool_calls(
                    cxt, module, messages, content, tool_calls,
                    hooks, allowed_names, lent_by, round_idx,
                    transfer_error=err)
                continue

            # transfer 命中：写跳转事件，本模块静默移交（content 不出口但
            # 保留进 history），chat 层消费事件重路由到目标模块同轮续答。
            # 该响应的每个 tool_call 都合成 tool 行（transfer 条记移交、其余
            # 记未执行），保证下一轮回放时 assistant.tool_calls 全配对
            cxt.add_message(
                "assistant",
                encode_tool_call_content(content or "", tool_calls),
                stage="agent",
                metadata={"suppressed": True},
            )
            for tc in tool_calls:
                name = tc.get("function", {}).get("name", "")
                if name.startswith(TRANSFER_TOOL_PREFIX):
                    synthetic = f"[已移交至模块 {target}]"
                else:
                    synthetic = "[未执行：本轮已移交]"
                cxt.add_message("tool", synthetic, stage="agent",
                                metadata={"synthetic": True,
                                          "tool_name": name,
                                          "tool_call_id": tc.get("id", "")})
            cxt.actions.append(ModuleJumpEvent(
                target_module_code=target,
                reason=transfer_reason,
                source="handoff_tool",
            ))
            logger.info(
                "[transfer] %s → %s（事件已写入 actions，移交 chat 层）",
                module.module_code, target,
            )
            # P6 on_transfer + P7 on_agent_end：移交出口
            if hooks:
                fire(hooks, "on_transfer", TransferEvent(
                    session_id=cxt.session_id, module_code=module.module_code,
                    round_idx=round_idx, target=target,
                    reason=transfer_reason))
                fire(hooks, "on_agent_end", AgentEndEvent(
                    session_id=cxt.session_id, module_code=module.module_code,
                    rounds=round_idx + 1, outcome="transfer",
                    transfer_target=target))
            return TurnResult()

        # 普通工具调用：P4 改写 → 主流程校验 → 执行 → P5 改写 → 落 history
        _dispatch_tool_calls(
            cxt, module, messages, content, tool_calls,
            hooks, allowed_names, lent_by, round_idx)

    logger.warning(
        "Agent loop 达到最大轮次 %d，强制终止: session=%s",
        _MAX_TOOL_ROUNDS, cxt.session_id,
    )
    # P7 on_agent_end：超轮次出口
    if hooks:
        fire(hooks, "on_agent_end", AgentEndEvent(
            session_id=cxt.session_id, module_code=module.module_code,
            rounds=_MAX_TOOL_ROUNDS, outcome="max_rounds",
            reply="抱歉，处理超时，请稍后重试。"))
    return TurnResult(reply="抱歉，处理超时，请稍后重试。")


# ---------------------------------------------------------------------------
# Tool round dispatch (P4/P5 + 主流程工具名校验)
# ---------------------------------------------------------------------------

def _dispatch_tool_calls(
    cxt, module, messages, content, tool_calls, hooks, allowed_names,
    lent_by, round_idx, transfer_error=None,
) -> None:
    """工具轮统一派发：P4 改写 → 校验 → 落 assistant 载荷 → 执行 → P5 → 落 tool 行。

    时序（规则 4）：先 P4 链式改写并应用回 tc（args 重序列化进
    ``tc["function"]["arguments"]``——原地改写 LLM 返回的 tc dict，使
    in-loop messages / history 载荷 / 执行三处共用改写后单一事实源），
    再落 assistant JSON 载荷；``tool_call_id`` 永不改（协议配对命脉）。

    主流程最终校验（规则 2 权威检查点）：最终 name ∉ allowed_names 一律
    **不执行**，tool 行回填带可用工具清单的错误信息（模型下一轮自纠；
    亦封堵 dispatch 只查注册不查 ACL 的旁路），metadata 沿用合成行约定
    ``{"synthetic": True}``。P5 只对真实 ``_execute_tool`` 结果触发，不对
    合成/回填串触发。

    transfer_error 非空 = transfer 目标非法的错误回填分支：transfer 条
    （前缀判定已先行，规则 1 不可改写、不执行）回填该错误串，普通条照常
    走 P4/校验/执行/P5 全流程。

    审计 metadata：改写发生时 ``rewritten=True`` +
    ``original_call``（P4，name/args 原值）/ ``original_result``（P5，原串）。
    """
    session_id = cxt.session_id
    module_code = module.module_code

    # 1. P4 链式改写 → 应用回 tc（transfer 条跳过：判定已先行）
    rewrite_audits = {}
    for idx, tc in enumerate(tool_calls):
        name = tc.get("function", {}).get("name", "")
        if transfer_error is not None and name.startswith(TRANSFER_TOOL_PREFIX):
            continue
        parsed_args = _parse_args(tc)
        if hooks:
            event = ToolCallEvent(
                session_id=session_id, module_code=module_code,
                round_idx=round_idx, tool_name=name, args=parsed_args)
            final_name, final_args, original = rewrite_tool_call(
                hooks, event, allowed_names,
                reserved_prefix=TRANSFER_TOOL_PREFIX)
        else:
            final_name, final_args, original = name, parsed_args, None
        if final_name != name:
            tc["function"]["name"] = final_name
        if final_args is not parsed_args:
            try:
                tc["function"]["arguments"] = json.dumps(
                    final_args, ensure_ascii=False)
            except (TypeError, ValueError) as e:
                logger.warning(
                    "[hooks] 改写后 args 无法序列化，保留原串: %s", e)
        if original is not None:
            rewrite_audits[idx] = original

    # 2. assistant 载荷（改写后实况；id 不动保证回放配对）
    messages.append({"role": "assistant", "content": content or None,
                     "tool_calls": tool_calls})
    cxt.add_message(
        "assistant",
        encode_tool_call_content(content or "", tool_calls),
        stage="agent",
    )

    # 3. 逐 tc：校验 → 执行（合法）/错误回填（非法、transfer 条）→ P5 → 落行
    for idx, tc in enumerate(tool_calls):
        name = tc.get("function", {}).get("name", "")
        call_id = tc.get("id", "")
        metadata = {"tool_name": name, "tool_call_id": call_id}

        if transfer_error is not None and name.startswith(TRANSFER_TOOL_PREFIX):
            result_content = transfer_error
        elif name not in allowed_names:
            logger.warning(
                "[tools] 工具 '%s' 不在本轮可用集合中，拦截不执行"
                "（幻觉/越权调用，错误回填供模型自纠）", name,
            )
            result_content = json.dumps({
                "error": (
                    f"工具 '{name}' 不存在或本轮不可用。"
                    f"可用工具：{sorted(allowed_names)}。"
                    f"请从可用工具中重新选择，或直接回应用户。"
                ),
            }, ensure_ascii=False)
            metadata["synthetic"] = True
        else:
            tool_result = _execute_tool(name, _parse_args(tc))
            result_original = None
            if hooks:
                event = ToolResultEvent(
                    session_id=session_id, module_code=module_code,
                    round_idx=round_idx, tool_name=name,
                    tool_call_id=call_id, result=tool_result)
                tool_result, result_original = rewrite_tool_result(hooks, event)
            result_content = tool_result

            source = lent_by.get(name)
            if source:
                cxt.metadata["served_by_projection"] = {
                    "module": module_code, "source": source,
                }
                metadata["lent_by"] = source
            if result_original is not None:
                metadata["rewritten"] = True
                metadata["original_result"] = result_original

        if idx in rewrite_audits:
            metadata["rewritten"] = True
            metadata["original_call"] = rewrite_audits[idx]

        cxt.add_message("tool", result_content, stage="agent",
                        metadata=metadata)
        messages.append({"role": "tool", "tool_call_id": call_id,
                         "content": result_content})


# ---------------------------------------------------------------------------
# Transfer tool builders（投影块构建已迁 src/chat/messages.py）
# ---------------------------------------------------------------------------

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
# （四块结构 + hooks 扩展块已迁 src/chat/messages.py 的 build_system_prompt，
#  与 messages 一体化构建同源；本模块仅保留框架侧强制项）
# ---------------------------------------------------------------------------

# force_close 收尾后缀（超跳数防死循环的控制流语义，任何 messages_builder
# 不可破坏——run_agent 在 builder 返回后由 _append_force_close_suffix 强制）
_FORCE_CLOSE_SUFFIX = "\n请直接回应用户，勿再移交。"


def _append_force_close_suffix(messages: List[Dict[str, Any]]) -> None:
    """追加收尾后缀到首条 system 行；无 system 行则前置一条。"""
    for m in messages:
        if m.get("role") == "system":
            m["content"] = (m.get("content") or "") + _FORCE_CLOSE_SUFFIX
            return
    messages.insert(0, {"role": "system",
                        "content": _FORCE_CLOSE_SUFFIX.strip()})


def _warn_prompt_length(messages, cxt, module) -> None:
    """system 行长度告警（投影膨胀观测；覆盖 hooks 注入与后缀后的真实长度）。"""
    system_row = next(
        (m for m in messages if m.get("role") == "system"), None)
    if system_row is None:
        return
    length = len(system_row.get("content") or "")
    if length > _PROMPT_LENGTH_WARN:
        logger.warning(
            "Agent system_prompt 过长 (%d 字符): session=%s, module=%s（投影膨胀观测）",
            length, cxt.session_id, module.module_code,
        )


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
        # 二次过滤：借出路径同样受 pattern 级工具 ACL 约束（deny-by-default），
        # 以借方（target 模块）为 ACL 名义主体——不架空 get_allowed_tools_for_pattern
        if not allowed:
            continue
        if pattern is not None:
            allowed &= tool_registry.get_allowed_tools_for_pattern(
                pattern.code, link.target)
        else:
            allowed = set()
        for schema in tool_registry.get_definitions(allowed):
            name = schema["function"]["name"]
            schemas.append(schema)
            lent_by[name] = link.target
    return schemas, lent_by


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

def _transfer_reason(transfer_call) -> str:
    """解析 transfer 工具调用参数中的移交上下文（reason）。"""
    args = _parse_args(transfer_call)
    reason = args.get("reason", "") if isinstance(args, dict) else ""
    return str(reason or "")
