"""
Dialogue processing module — handles dialogue based on session and user query.

Supports two dialogue modes:
1. AGENT mode: module has no nodes, goes through the agent loop (loop.py);
   builds system_prompt from module, injects tools, runs multi-turn tool calling dialogue
2. FSM/ROUTE mode: invokes pattern-registered stages for pipeline processing;
   performs node transitions afterwards:
   - FSM   : jump to the node identified by NLU's next_node
   - ROUTE : advance to the intent-menu node selected by NLU for reply generation,
             then either dispatch to the menu node's ``jump_module`` sub-module or
             reset back to the root node (router stays at root between turns)
"""

import logging
from typing import Dict

from src.chat.session import Session
from src.chat.loop import TurnResult, run_agent  # noqa: F401 (run_agent 供外部引用)
from src.dialogue.dispatch import ModuleDispatch, dispatch
from src.dialogue.module import ModuleType
from src.dialogue.stage_slots import (
    GenerateSlot,
    PostRecallSlot,
    PreRecallSlot,
    QuerySlot,
    resolve_stage,
)
from config.config import get_llm_config

logger = logging.getLogger(__name__)


def _refresh_llm_config(session: Session, module_code: str = "",
                        node_code: str = "") -> None:
    """按当前位置解析 LLM 配置并写入 cxt.llm_config（spec §4，R1-R4 共用）。

    override（cxt.metadata["llm_override"]）存在即采用；解析失败向上抛
    （R1 包装为显式错误回复，R2-R4 走 chat() 的统一异常处理）。
    """
    cxt = session.cxt
    cxt.llm_config = get_llm_config(
        pattern_code=session.pattern_code or cxt.metadata.get("pattern_code", ""),
        module_code=module_code or cxt.current_module_code or "",
        node_code=node_code or cxt.current_node_code or "",
        override=cxt.metadata.get("llm_override"),
    )


def _refresh_llm_config_from_ctx(cxt) -> None:
    """stage 内版本：pattern/module code 取自 cxt（R4 专用）。"""
    cxt.llm_config = get_llm_config(
        pattern_code=cxt.metadata.get("pattern_code", ""),
        module_code=cxt.current_module_code or "",
        node_code=cxt.current_node_code or "",
        override=cxt.metadata.get("llm_override"),
    )


def chat(query: str, session_id: str, all_sessions: Dict[str, Session]) -> str:
    """Handle a user dialogue request.

    1. Take query and locate the session by session_id
    2. Locate current position in cxt by module_code / node_code
    3. For AGENT modules go through the agent loop (loop.py);
       otherwise invoke pattern-registered stages for pipeline processing
    4. After processing, perform node transition; route type always stays at root

    Args:
        query: user input text
        session_id: session ID
        all_sessions: global session dict, keyed by session_id with Session values

    Returns:
        str: generated reply text
    """
    # ------------------------------------------------------------------
    # 1. Take query and locate the session by session_id
    # ------------------------------------------------------------------
    session = all_sessions.get(session_id)
    if session is None:
        logger.warning("会话不存在: %s", session_id)
        return "会话不存在，请先发起对话任务"

    session.cxt.user_query = query

    pattern = session.pattern
    if pattern is None:
        logger.warning("会话 %s 未绑定对话模板", session_id)
        return "对话模板未配置"

    # ------------------------------------------------------------------
    # 2. Locate current position in cxt by module_code / node_code
    # ------------------------------------------------------------------
    current_module_code = session.cxt.current_module_code or pattern.entry_module_code
    if not current_module_code:
        logger.warning("会话 %s 未找到入口模块", session_id)
        return "入口模块未配置"

    # Write the resolved module code back to cxt: stages and transitions
    # (e.g. _RouteNodeAdvance / _handle_node_transition) read it from cxt.
    session.cxt.current_module_code = current_module_code

    current_module = pattern.module_map.get(current_module_code)
    if current_module is None:
        logger.warning("模块不存在: %s", current_module_code)
        return f"模块 '{current_module_code}' 不存在"

    # Ensure LLM config is injected into the context（R1：每轮按当前位置解析，
    # override 优先；spec §4）
    session.cxt.metadata["pattern_code"] = session.pattern_code
    try:
        _refresh_llm_config(session)
    except Exception as e:
        logger.error("加载 LLM 配置失败: %s", e)
        return f"LLM 配置加载失败: {e}"

    # Record user message
    session.cxt.add_message("user", query, stage="chat")

    # ------------------------------------------------------------------
    # 3. Reentry loop: consume same-turn dispatch events (spec §3.1)
    # ------------------------------------------------------------------
    # launch/store 恢复路径均不注入 dispatch_graph，此处 setdefault 一处覆盖
    # 所有入口；无图时 dispatch 全拒绝、ROUTE 静默分发静默失效。
    session.cxt.metadata.setdefault(
        "dispatch_graph", getattr(pattern, "dispatch_graph", {}) or {}
    )

    # 每轮开头清空转移链与承接上下文（承接块 spec §4 为"首轮条件注入"：
    # dispatch 同轮发生 → B 当轮消费 → 下一轮开头清除 = 恰好只注入首轮）
    session.cxt.metadata.pop("dispatch_log", None)
    session.cxt.metadata.pop("handoff_context", None)

    max_hops = getattr(pattern, "max_hops", 2)
    try:
        for hop in range(max_hops):
            current_module = pattern.module_map[
                session.cxt.current_module_code or pattern.entry_module_code
            ]
            if current_module.type == ModuleType.AGENT:
                result = _handle_agent_module(session, current_module)
            else:
                result = _run_pipeline(session, current_module)

            if getattr(result, "dispatch_event", None) is None:
                response = result.reply or ""
                break
            logger.info(
                "same-turn dispatch 第 %d 跳: → %s",
                hop + 1, result.dispatch_event.target_module_code,
            )
        else:
            # 超跳数：以当前模块强制收尾
            logger.warning("达到 max_hops=%d，强制收尾", max_hops)
            current_module = pattern.module_map[
                session.cxt.current_module_code or pattern.entry_module_code
            ]
            if current_module.type == ModuleType.AGENT:
                result = _handle_agent_module(
                    session, current_module, force_close=True)
                response = result.reply or ""
            else:
                result = _run_pipeline(session, current_module, force_close=True)
                response = result.reply or ""
    except Exception as e:
        logger.exception("对话处理异常: session=%s", session_id)
        response = f"对话处理异常: {e}"

    # Record assistant reply
    session.cxt.add_message("assistant", response, stage="chat")

    return response


# ---------------------------------------------------------------------------
# AGENT module handling — go through the agent loop
# ---------------------------------------------------------------------------

def _handle_agent_module(session: Session, module,
                         force_close: bool = False) -> TurnResult:
    """Agent loop handling for AGENT modules.

    Calls run_agent() in loop.py, which builds system_prompt from module,
    injects tools, and runs multi-turn LLM dialogue (with tool calling).

    Args:
        session: current session
        module: current module object (AgentModule)
        force_close: 强制收尾（max_hops 耗尽）：不注入 transfer 工具，
            prompt 末尾追加"勿再移交"提示

    Returns:
        TurnResult: reply 与 dispatch_event 互斥
    """
    logger.info("Agent 模块处理: module=%s", module.module_code)
    _refresh_llm_config(session, module_code=module.module_code)  # R2
    return run_agent(session, module, session.cxt.llm_config,
                     force_close=force_close)


# ---------------------------------------------------------------------------
# Pipeline processing
# ---------------------------------------------------------------------------

def _run_pipeline(session: Session, module,
                  force_close: bool = False) -> TurnResult:
    """Invoke pattern-registered stages for pipeline processing.

    Prefer the skeleton registered on the pattern (concrete stages run
    verbatim; slots resolve lazily by node > module > pattern); if unset,
    build the default four-slot skeleton (see stage_slots.py).

    Node transition after processing:
    - FSM type: jump according to next_node in the NLU result
    - ROUTE type: reply from the selected menu node, then dispatch to its
      ``jump_module`` sub-module or reset to root

    Args:
        session: current session
        module: current module object (FSMModule or RouteModule)
        force_close: 强制收尾（max_hops 耗尽）：跳过 dispatch 尝试（含
            jump_module 命中），直接消费 stages 生成的 NLG 回复，避免
            ROUTE 分发成功后返回空回复

    Returns:
        TurnResult: reply 与 dispatch_event 互斥（ROUTE 静默分发时为 event）
    """
    cxt = session.cxt
    pattern = session.pattern

    # ------------------------------------------------------------------
    # Determine current node (first node of the module on initial entry)
    # ------------------------------------------------------------------
    if cxt.current_node_code is None:
        if module.module_nodes:
            first_node = module.module_nodes[0]
            cxt.current_node_code = first_node.node_code
            logger.info(
                "首次进入模块 %s，使用首节点: %s",
                module.module_code,
                first_node.node_code,
            )
        else:
            raise ValueError(f"模块 '{module.module_code}' 无可用节点")

    cur_node = cxt.node_map.get(cxt.current_node_code)
    if cur_node is None:
        raise ValueError(
            f"节点 '{cxt.current_node_code}' 不存在于 node_map 中"
        )

    # R3：节点解析完按 module+node 刷新 LLM 配置（spec §4）
    _refresh_llm_config(session, module_code=module.module_code,
                        node_code=cxt.current_node_code)

    # ------------------------------------------------------------------
    # Get pipeline stages: pattern skeleton or module-type default skeleton.
    # 槽位（stage_slots.py 四槽位）在执行期按 node > module > pattern 延迟解析；
    # generate 展开为 nlg/nlu 惰性子部件（ROUTE 下 nlg 于菜单节点切换后解析）。
    # ------------------------------------------------------------------
    stages = pattern.stages or _default_skeleton(module)

    logger.info(
        "Pipeline 开始: session=%s, module=%s, node=%s, stages=%s",
        cxt.session_id,
        module.module_code,
        cur_node.node_code,
        [s.stage_name for s in stages],
    )

    # Execute each stage in order; slots resolve against the *current* node
    # at execution time
    for stage in stages:
        for concrete in resolve_stage(stage, cxt, module, pattern):
            try:
                cxt = concrete.execute(cxt)
                logger.debug("Stage '%s' 执行完成", concrete.stage_name)
            except Exception as e:
                logger.error(
                    "Stage '%s' 执行异常: %s", concrete.stage_name, e,
                    exc_info=True
                )
                raise

    # ------------------------------------------------------------------
    # 4. ROUTE silent dispatch: menu node with jump_module → dispatch
    # ------------------------------------------------------------------
    if module.type == ModuleType.ROUTE:
        if not force_close and not (cxt.metadata.get("clarify") or {}
                                    ).get("triggered"):
            cur_node = cxt.node_map.get(cxt.current_node_code)
            jump_target = getattr(cur_node, "jump_module", None) if cur_node else None

            # 槽位合并保留（原 _handle_node_transition ROUTE 分支职责）
            slots = (cxt.nlu_result or {}).get("slots", {})
            if slots:
                cxt.filled_slots.update(slots)
                logger.info("槽位更新: %s", slots)

            if jump_target and jump_target in cxt.module_map:
                event = ModuleDispatch(
                    target_module_code=jump_target, source="route_menu")
                if dispatch(cxt, event):
                    # dispatch() 已完成状态转移；event 仅作日志供 chat 层消费。
                    # 若 stages 里 NLG 已生成 nlg_result，静默分发路径不消费它。
                    return TurnResult(dispatch_event=event)
                # dispatch 失败（不邻接/回弹）→ 回退正常 NLG 回复路径
        # ROUTE 轮末统一重置回 root（含 force_close：跳过 dispatch 但不留在菜单节点，
        # 否则菜单节点无 sub_nodes，下一轮路由候选为空；澄清轮不走此分支，节点保持）
        root_code = module.module_nodes[0].node_code if module.module_nodes else None
        if not (cxt.metadata.get("clarify") or {}).get("triggered"):
            cxt.current_node_code = root_code
            logger.info("ROUTE 模块保持 root 节点: %s", root_code)
    elif module.type != ModuleType.ROUTE:
        # FSM：保持原节点转移逻辑（ROUTE 的 force_close/澄清路径不做 FSM 式跳转）
        _handle_node_transition(cxt, module)
    # ROUTE 澄清轮：节点保持当前，不做 FSM 式 next_node 跳转

    # Extract the reply generated by NLG
    nlg_result = cxt.nlg_result or {}
    response = nlg_result.get("content", "")

    return TurnResult(reply=response)


# ---------------------------------------------------------------------------
# Default pipeline construction
# ---------------------------------------------------------------------------

def _default_skeleton(module) -> list:
    """默认管线骨架（槽位延迟解析，不绑定节点）。

    [PreRecallSlot, QuerySlot, PostRecallSlot, GenerateSlot]；
    FSM/ROUTE 的差异（advance / clarify 插入）由 GenerateSlot 展开处理
    （stage_slots.resolve_stage），骨架本身全模块类型同形。
    """
    return [PreRecallSlot(), QuerySlot(), PostRecallSlot(), GenerateSlot()]


# ---------------------------------------------------------------------------
# Node transition
# ---------------------------------------------------------------------------

def _handle_node_transition(cxt, module) -> None:
    """Perform node transition after processing.

    Jump according to next_node in the NLU result:
    - FSM type: jump to the node identified by NLU's next_node
    - ROUTE type: the intent-menu node selected by NLU may declare a
      ``jump_module`` sub-module via attribute; when present the session is
      dispatched to that module (next turn starts at its first node), otherwise
      the router resets to its root node (router stays at root between turns)

    Also merges NLU-extracted slots into filled_slots.

    ROUTE 转移（含静默分发）职责已移入 _run_pipeline，本函数仅处理 FSM。

    Args:
        cxt: dialogue context
        module: current module object
    """
    # 澄清轮：跳过槽位合并（topic/keywords 不入 filled_slots），节点保持
    if (cxt.metadata.get("clarify") or {}).get("triggered"):
        logger.info("澄清轮，跳过槽位合并与节点跳转: node=%s", cxt.current_node_code)
        return

    nlu_result = cxt.nlu_result or {}
    slots = nlu_result.get("slots", {})

    # Merge slots
    if slots:
        cxt.filled_slots.update(slots)
        logger.info("槽位更新: %s", slots)

    # FSM type: jump according to next_node in the NLU result
    next_node_code = nlu_result.get("next_node", "")

    if not next_node_code:
        logger.info("NLU 未返回 next_node，保持当前节点: %s", cxt.current_node_code)
        return

    if next_node_code not in cxt.node_map:
        logger.warning(
            "NLU 返回的 next_node '%s' 不在 node_map 中，保持当前节点: %s",
            next_node_code,
            cxt.current_node_code,
        )
        return

    logger.info(
        "FSM 节点跳转: %s → %s",
        cxt.current_node_code,
        next_node_code,
    )
    cxt.current_node_code = next_node_code

# _RouteNodeAdvance 已迁入 stage_slots（dialogue 层自洽，chat 不再被反向依赖）；
# 此处 re-export 保持 chat_mod._RouteNodeAdvance 既有引用（test_llm_refresh R4）
from src.dialogue.stage_slots import _RouteNodeAdvance  # noqa: E402, F401
