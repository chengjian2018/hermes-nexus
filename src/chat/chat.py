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
from src.dialogue.base import PipelineStage
from src.dialogue.dispatch import ModuleDispatch, dispatch
from src.dialogue.module import ModuleType
from src.dialogue.nlu import FSMNLU, RouteNLU
from src.dialogue.nlg import FSMNLG, RouteNLG
from config.config import get_llm_config

logger = logging.getLogger(__name__)


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

    # Ensure LLM config is injected into the context
    if session.cxt.llm_config is None:
        try:
            session.cxt.llm_config = get_llm_config()
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

    # 每轮开头清空转移链
    session.cxt.metadata.pop("dispatch_log", None)

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
                result = _run_pipeline(session, current_module)
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

def _handle_agent_module(session: Session, module, force_close: bool = False):
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
    from src.chat.loop import run_agent

    logger.info("Agent 模块处理: module=%s", module.module_code)
    return run_agent(session, module, session.cxt.llm_config,
                     force_close=force_close)


# ---------------------------------------------------------------------------
# Pipeline processing
# ---------------------------------------------------------------------------

def _run_pipeline(session: Session, module) -> str:
    """Invoke pattern-registered stages for pipeline processing.

    Prefer the stages list registered on the pattern; if unset, build the
    default NLU → NLG two-stage pipeline by module type.

    Node transition after processing:
    - FSM type: jump according to next_node in the NLU result
    - ROUTE type: reply from the selected menu node, then dispatch to its
      ``jump_module`` sub-module or reset to root

    Args:
        session: current session
        module: current module object (FSMModule or RouteModule)

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

    # ------------------------------------------------------------------
    # Get pipeline stages: prefer pattern-registered stages, else build default
    # ------------------------------------------------------------------
    stages = pattern.stages
    if not stages:
        # No registered stages: build default NLU → NLG two-stage pipeline by module type
        stages = _build_default_stages(cur_node, module)

    logger.info(
        "Pipeline 开始: session=%s, module=%s, node=%s, stages=%s",
        cxt.session_id,
        module.module_code,
        cur_node.node_code,
        [s.stage_name for s in stages],
    )

    # Execute each stage in order
    for stage in stages:
        try:
            cxt = stage.execute(cxt)
            logger.debug("Stage '%s' 执行完成", stage.stage_name)
        except Exception as e:
            logger.error(
                "Stage '%s' 执行异常: %s", stage.stage_name, e, exc_info=True
            )
            raise

    # ------------------------------------------------------------------
    # 4. ROUTE silent dispatch: menu node with jump_module → dispatch
    # ------------------------------------------------------------------
    if module.type == ModuleType.ROUTE and not (
        cxt.metadata.get("clarify") or {}
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

        # ROUTE 原转移职责（原 _handle_node_transition ROUTE 分支）：保持 root
        root_code = module.module_nodes[0].node_code if module.module_nodes else None
        cxt.current_node_code = root_code
        logger.info("ROUTE 模块保持 root 节点: %s", root_code)
    else:
        # FSM：保持原节点转移逻辑
        _handle_node_transition(cxt, module)

    # Extract the reply generated by NLG
    nlg_result = cxt.nlg_result or {}
    response = nlg_result.get("content", "")

    return TurnResult(reply=response)


# ---------------------------------------------------------------------------
# Default pipeline construction
# ---------------------------------------------------------------------------

class _RouteNodeAdvance(PipelineStage):
    """ROUTE-only stage: advance to the intent-menu node selected by RouteNLU.

    Runs between NLU and NLG so the reply is generated from the selected menu
    node's answer examples instead of the root node. The selection is validated
    against the route module's own node list; invalid selections keep the
    current node (root) unchanged.
    """

    stage_name = "route_advance"

    def execute(self, ctx):
        module = (
            ctx.module_map.get(ctx.current_module_code)
            if ctx.current_module_code
            else None
        )
        if module is None or getattr(module, "type", None) != ModuleType.ROUTE:
            return ctx

        next_node_code = (ctx.nlu_result or {}).get("next_node", "")
        if next_node_code and any(
            n.node_code == next_node_code for n in module.module_nodes
        ):
            logger.info(
                "ROUTE 命中菜单节点: %s → %s",
                ctx.current_node_code,
                next_node_code,
            )
            ctx.current_node_code = next_node_code

        return ctx


def _default_clarify_stage():
    """构建默认 ClarifyStage：内存关键词召回 + 默认门控。

    生产环境应在 pattern 定义中显式配置 module.clarify_stage
    （挂 ES 召回路径）；默认实例保证开箱可用。
    """
    from src.clarify import ClarifyRouteRule, ClarifyStage
    from src.dialogue.recaller import (
        KeywordRecallPath,
        MultiPathRecaller,
        ScoreThresholdFilter,
        WeightedScoreFusion,
    )

    return ClarifyStage(
        recaller=MultiPathRecaller(
            recall_paths=[],
            filters=[ScoreThresholdFilter(threshold=0.1)],
            fusion=WeightedScoreFusion(),
        ),
        rule=ClarifyRouteRule(),
    )


def _build_default_stages(cur_node, module) -> list:
    """Build the default pipeline by module type and node config.

    Priority (node > module > default):
    - NLU: node's nlu_stage > module's nlu_stage > default (FSMNLU / RouteNLU)
    - NLG: node's nlg_stage > module's nlg_stage > default (FSMNLG / RouteNLG)

    ROUTE modules additionally insert a ``_RouteNodeAdvance`` stage between
    NLU and NLG so the selected intent-menu node drives reply generation.

    Args:
        cur_node: current node object
        module: current module object

    Returns:
        list: [NLU_stage, (route_advance), NLG_stage]
    """
    # Resolve NLU stage
    if cur_node.nlu_stage is not None:
        nlu_stage = cur_node.nlu_stage
    elif module.nlu_stage is not None:
        nlu_stage = module.nlu_stage
    elif module.type == ModuleType.ROUTE:
        nlu_stage = RouteNLU()
    else:
        nlu_stage = FSMNLU()

    # Resolve NLG stage
    if cur_node.nlg_stage is not None:
        nlg_stage = cur_node.nlg_stage
    elif module.nlg_stage is not None:
        nlg_stage = module.nlg_stage
    elif module.type == ModuleType.ROUTE:
        nlg_stage = RouteNLG()
    else:
        nlg_stage = FSMNLG()

    if module.type == ModuleType.ROUTE:
        return [nlu_stage, _RouteNodeAdvance(), nlg_stage]

    # FSM 模块：启用双轨澄清时插入 ClarifyStage（NLU 之后、NLG 之前）
    if getattr(module, "enable_clarify", False):
        from src.clarify.stage import ClarifyStage  # 局部 import 防循环依赖

        clarify_stage = getattr(module, "clarify_stage", None) or _default_clarify_stage()
        return [nlu_stage, clarify_stage, nlg_stage]

    return [nlu_stage, nlg_stage]


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