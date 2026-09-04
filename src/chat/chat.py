"""
Dialogue processing — 轮次编排器（orchestrator）。

职责收窄为"一轮对话的编排"：session 定位 → cxt 轮次生命周期 → 同轮 hop
循环 → 产出 ChatResult。单模块的处理按类型内联在本模块：

- AGENT → loop.run_agent（inject 直接答 / transfer 写 ModuleJumpEvent 返回）
- FSM   → _run_fsm_pipeline（stages 执行 + next_node 跳转）
- ROUTE → _run_route_pipeline（stages 执行 + 轮末重置 root）

模块跳转统一走 ModuleJumpEvent（写 cxt.actions，定义在 dialogue/base.py），
事件只由两个入口产生：

- ROUTE 跳到新模块：_run_stages 在每个 stage 执行后检测（仅 ROUTE 模块）——
  NLU 输出的 jump_module 字段、推进后菜单节点配置的 jump_module。命中即
  合并槽位、写 event、中断剩余 stages（源模块静默，不生成回复）。
- AGENT transfer 工具调用：loop.run_agent 直接写 event 返回。
- FSM 不产生事件：clarify 在循环内由 ClarifyStage 处理（覆写 nlg_result，
  不出循环），节点跳转由轮末 _fsm_node_transition 处理。
- 消费：chat_turn 的 hop 循环 _pop_jump_event → 重路由（写
  current_module_code、置空 current_node_code）→ 目标模块同轮续答；
  超跳数 force_close 强制收尾。
- 无邻接校验 / 回弹拒绝 / dispatch 记账——目标存在于 module_map 即合法，
  边界淡化，agent 与 route 跳转同构。

cxt 字段生命周期（每轮重置 / 跨轮保留 / 增量更新）统一由
context_lifecycle.TurnLifecycle 管理，本模块只在正确的时序点调用。

入口：
- chat_turn() ：全量入口，返回 ChatResult（text + 预留 actions）
- chat()      ：兼容入口（main.py / cli.py / 既有测试），等价 chat_turn().text
"""

import logging
from typing import Dict, Optional

from config.config import get_llm_config
from src.chat.context_lifecycle import TurnLifecycle
from src.chat.loop import TurnResult, run_agent  # noqa: F401（兼容再导出）
from src.chat.response import ChatResult, build_chat_result
from src.chat.session import Session
from src.dialogue.base import ModuleJumpEvent
from src.dialogue.module import ModuleType
from src.dialogue.stage_slots import (
    GenerateSlot,
    PostRecallSlot,
    PreRecallSlot,
    QuerySlot,
    resolve_stage,
)

logger = logging.getLogger(__name__)

# cxt 字段生命周期唯一管理者（无状态，模块级复用）
_lifecycle = TurnLifecycle()


# ============================================================================
# ModuleJumpEvent 通道操作（cxt.actions 是跳转事件的唯一载体）
# ============================================================================

def _peek_jump_event(cxt) -> Optional[ModuleJumpEvent]:
    """查看 cxt.actions 中是否已有跳转事件（不移除）。"""
    for item in cxt.actions:
        if isinstance(item, ModuleJumpEvent):
            return item
    return None


def _pop_jump_event(cxt) -> Optional[ModuleJumpEvent]:
    """取出 cxt.actions 中第一个跳转事件（消费即移除）。

    非跳转动作（dict 形态，如 conversation_end）原样留在 actions，
    由轮末 build_chat_result 快照进 ChatResult。
    """
    for i, item in enumerate(cxt.actions):
        if isinstance(item, ModuleJumpEvent):
            return cxt.actions.pop(i)
    return None


def _apply_jump(cxt, event: ModuleJumpEvent) -> None:
    """消费跳转事件：重路由到目标模块（淡化边界，仅校验存在性）。

    目标不存在时保持原位（注册期 pattern 已对 jump_module 配置 fail fast，
    此处兜底 NLU 幻觉输出）。
    """
    if event.target_module_code not in cxt.module_map:
        logger.warning(
            "[jump] 目标模块 '%s' 不存在，保持原模块: %s",
            event.target_module_code, cxt.current_module_code,
        )
        return
    logger.info(
        "[jump] %s → %s (source=%s)",
        cxt.current_module_code, event.target_module_code, event.source,
    )
    cxt.current_module_code = event.target_module_code
    # 节点置空：目标模块 _resolve_entry_node 取自身首节点
    cxt.current_node_code = None


def _detect_jump_after_stage(cxt, module, before_nlu) -> Optional[ModuleJumpEvent]:
    """ROUTE 模块 stage 执行后的跳转检测；FSM/AGENT 恒返回 None。

    事件只由 ROUTE 产生：FSM 的 clarify 在循环内由 ClarifyStage 处理
    （覆写 nlg_result，不出循环），节点跳转由轮末 _fsm_node_transition
    处理；AGENT 的移交走 transfer 工具（run_agent 写事件）。

    ROUTE 检测前先做菜单节点推进（原 _RouteNodeAdvance 职责并入此处）：
    next_node 命中本模块节点 → 切当前节点 + R4 节点级 LLM 配置当轮生效，
    使随后的 NLG 部件按菜单节点配置生成。

    模块跳转来源（按优先级）：
    1. ``nlu_result.jump_module``：NLU 直接输出的模块级跳转字段
    2. 推进后节点的 ``jump_module`` 配置：菜单节点自身声明要跳往的模块
       （推进已在上面完成，直接读当前节点）

    仅当 nlu_result 在本 stage 执行后被（覆）写才检测——避免 hop 续答阶段
    对残留旧 nlu_result 误检。目标不在 module_map / 自环时忽略，继续执行
    剩余 stages（LLM 幻觉容错）。
    """
    if module.type != ModuleType.ROUTE:
        return None
    if cxt.nlu_result is None or cxt.nlu_result is before_nlu:
        return None

    nlu_result = cxt.nlu_result
    target = ""
    source = ""

    # 菜单节点推进
    next_node_code = nlu_result.get("next_node", "")
    module_node_codes = {n.node_code for n in module.module_nodes}
    if next_node_code and next_node_code in module_node_codes:
        logger.info(
            "ROUTE 命中菜单节点: %s → %s",
            cxt.current_node_code, next_node_code,
        )
        cxt.current_node_code = next_node_code
        # R4：菜单节点 node 级 LLM 配置当轮生效（spec §4；pattern_code
        # 取 R1 写入的 metadata，ROUTE 每轮从 root 出发永不定居菜单节点）
        cxt.llm_config = get_llm_config(
            pattern_code=cxt.metadata.get("pattern_code", ""),
            module_code=cxt.current_module_code or "",
            node_code=next_node_code,
            override=cxt.metadata.get("llm_override"),
        )

    # 1) NLU 直接输出模块级跳转字段
    jump_field = nlu_result.get("jump_module", "")
    if isinstance(jump_field, str) and jump_field:
        target, source = jump_field, "nlu_jump"

    # 2) 推进后（或当前）节点配置了 jump_module（菜单分发）
    if not target:
        cur_node = cxt.node_map.get(cxt.current_node_code)
        node_jump = getattr(cur_node, "jump_module", None) if cur_node else None
        if node_jump:
            target, source = node_jump, "route_menu"

    if not target or target == cxt.current_module_code:
        return None
    if target not in cxt.module_map:
        logger.warning(
            "[jump] NLU 指示跳转目标 '%s' 不在 module_map 中，忽略", target,
        )
        return None

    return ModuleJumpEvent(
        target_module_code=target,
        reason=str(nlu_result.get("reason", "") or ""),
        source=source,
    )


# ============================================================================
# 管线执行
# ============================================================================

def _default_skeleton(module) -> list:
    """默认管线骨架（槽位延迟解析，不绑定节点）。

    [PreRecallSlot, QuerySlot, PostRecallSlot, GenerateSlot]；
    FSM/ROUTE 的差异（advance / clarify 插入）由 GenerateSlot 展开处理
    （stage_slots.resolve_stage），骨架本身全模块类型同形。
    """
    return [PreRecallSlot(), QuerySlot(), PostRecallSlot(), GenerateSlot()]


def _refresh_llm_config(session: Session, module_code: str = "",
                        node_code: str = "") -> None:
    """按当前位置解析 LLM 配置并写入 cxt.llm_config（spec §4，R1-R4 共用）。

    本函数必须留在 chat 模块：R1-R3 的 get_llm_config 经本命名空间解析
    （tests/test_llm_refresh.py 等以 patch("src.chat.chat.get_llm_config")
    为锚点），不引入 handlers 自 import。
    """
    cxt = session.cxt
    cxt.llm_config = get_llm_config(
        pattern_code=session.pattern_code or cxt.metadata.get("pattern_code", ""),
        module_code=module_code or cxt.current_module_code or "",
        node_code=node_code or cxt.current_node_code or "",
        override=cxt.metadata.get("llm_override"),
    )


def _fsm_node_transition(cxt, module) -> None:
    """FSM 轮末节点转移。

    按 NLU 结果的 next_node 跳转；澄清轮跳过槽位合并与跳转（topic/keywords
    不入 filled_slots，节点保持）。同时把 NLU 抽取的槽位增量合并进
    filled_slots。

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

    # Merge slots（增量：经 lifecycle 入口）
    _lifecycle.merge_slots(cxt, slots)

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


def _resolve_entry_node(cxt, module) -> None:
    """确定当前节点（首次进入模块时取首节点），写入 cxt.current_node_code。"""
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


def _run_stages(cxt, module, pattern, force_close: bool = False
                ) -> Optional[ModuleJumpEvent]:
    """顺序执行管线 stages（槽位按 node > module > pattern 延迟解析）。

    每个 stage 执行后做跳转检测（仅 ROUTE：_detect_jump_after_stage +
    stage 自写 event）：命中即合并槽位、写 ModuleJumpEvent 到 cxt.actions
    并中断剩余 stages——源模块本轮静默（NLG 不执行），由 chat 层 hop
    循环重路由到目标模块同轮续答。FSM 不产生事件（clarify 循环内由
    ClarifyStage 处理不出循环、节点跳转由轮末转移处理）。force_close
    （超跳数收尾）跳过检测，stages 跑完；其间 stage 写入的事件不参与
    控制流（不触发静默），仅留 actions 作观测。

    Returns:
        待消费的跳转事件（已写入 cxt.actions，由 chat_turn 的 hop 循环
        pop 消费）；None 表示无跳转。
    """
    stages = pattern.stages or _default_skeleton(module)

    logger.info(
        "Pipeline 开始: session=%s, module=%s, node=%s, stages=%s",
        cxt.session_id,
        module.module_code,
        cxt.current_node_code,
        [s.stage_name for s in stages],
    )

    # Execute each stage in order; slots resolve against the *current* node
    # at execution time
    for stage in stages:
        for concrete in resolve_stage(stage, cxt, module, pattern):
            before_nlu = cxt.nlu_result
            try:
                cxt = concrete.execute(cxt)
                logger.debug("Stage '%s' 执行完成", concrete.stage_name)
            except Exception as e:
                logger.error(
                    "Stage '%s' 执行异常: %s", concrete.stage_name, e,
                    exc_info=True
                )
                raise

            if force_close:
                continue

            # 检测 1：stage 直接写入了跳转事件（自定义 stage 通道）
            direct = _peek_jump_event(cxt)
            if direct is not None:
                logger.info(
                    "Stage '%s' 写入跳转事件: → %s",
                    concrete.stage_name, direct.target_module_code,
                )
                return direct

            # 检测 2：nlu_result 更新且指示跳转（NLU jump_module 字段 /
            # 推进后节点 jump_module 配置）
            event = _detect_jump_after_stage(cxt, module, before_nlu)
            if event is not None:
                # 槽位增量合并随跳转走（目标模块承接上下文）
                _lifecycle.merge_slots(
                    cxt, (cxt.nlu_result or {}).get("slots", {}))
                cxt.actions.append(event)
                logger.info(
                    "Stage '%s' 后检测到模块跳转: %s → %s (source=%s)",
                    concrete.stage_name, module.module_code,
                    event.target_module_code, event.source,
                )
                return event

    # stages 自然跑完即无跳转：stage 直接写的事件已被检测 1 在该 stage
    # 执行后捕获提前返回；force_close 下检测被跳过，其间写入的事件仅留
    # actions 作观测（轮末快照），不触发源模块静默
    return None


def _run_fsm_pipeline(session: Session, module, force_close: bool = False
                      ) -> TurnResult:
    """FSM 模块一轮：节点解析 → R3 刷新 → stages → next_node 跳转。

    FSM 不产生跳转事件（clarify 循环内处理、节点跳转轮末处理），
    _run_stages 返回值恒为 None。
    """
    cxt = session.cxt
    pattern = session.pattern

    _resolve_entry_node(cxt, module)

    # R3：节点解析完按 module+node 刷新 LLM 配置（spec §4）
    _refresh_llm_config(session, module_code=module.module_code,
                        node_code=cxt.current_node_code)

    _run_stages(cxt, module, pattern, force_close=force_close)

    # FSM：next_node 跳转（澄清轮守卫在转移函数内）
    _fsm_node_transition(cxt, module)

    # 终节点动作（预留通道）
    next_node = pattern.node_map.get(cxt.current_node_code)
    if next_node is not None and getattr(next_node, "is_end", False):
        cxt.actions.append({"conversation_end": True})

    nlg_result = cxt.nlg_result or {}
    return TurnResult(reply=nlg_result.get("content", ""))


def _run_route_pipeline(session: Session, module, force_close: bool = False
                        ) -> TurnResult:
    """ROUTE 模块一轮：节点解析 → R3 刷新 → stages → 轮末重置 root。

    模块跳转在 _run_stages 内检测（NLU jump_module 字段 / 菜单节点
    jump_module 配置），本函数不再做 jump_module 分发。跳转轮直接静默
    返回（hop 循环重路由，目标模块自解析入口节点）；跳到 AGENT/FSM 后
    目标模块跨轮承接后续轮次（agent 靠 history、FSM 靠节点位置），不回
    路由——只有当前还停在 ROUTE 模块时才轮末重置回 root（菜单节点无
    sub_nodes，不重置则下一轮路由候选为空）。ROUTE 不装配 ClarifyStage
    （仅 FSM+enable_clarify 插入，见 stage_slots.resolve_stage），且
    begin_turn 轮首已清 clarify，无澄清轮分支。
    """
    cxt = session.cxt
    pattern = session.pattern

    _resolve_entry_node(cxt, module)

    # R3：节点解析完按 module+node 刷新 LLM 配置（spec §4）
    _refresh_llm_config(session, module_code=module.module_code,
                        node_code=cxt.current_node_code)

    jump_event = _run_stages(cxt, module, pattern, force_close=force_close)

    # 跳转轮：槽位已在检测点合并，hop 循环重路由到目标模块同轮续答
    if jump_event is not None:
        return TurnResult()

    # 槽位合并（增量：经 lifecycle 入口）
    slots = (cxt.nlu_result or {}).get("slots", {})
    _lifecycle.merge_slots(cxt, slots)

    # 轮末重置回 root（含 force_close：跳过跳转检测但菜单节点无 sub_nodes，
    # 不重置则下一轮路由候选为空）
    root_code = module.module_nodes[0].node_code if module.module_nodes else None
    cxt.current_node_code = root_code
    logger.info("ROUTE 模块保持 root 节点: %s", root_code)

    nlg_result = cxt.nlg_result or {}
    return TurnResult(reply=nlg_result.get("content", ""))


def _run_agent_pipeline(session: Session, module, force_close: bool = False
                        ) -> TurnResult:
    """AGENT 模块一轮：R2 刷新 → loop.run_agent。

    transfer 工具调用由 run_agent 写 ModuleJumpEvent 到 cxt.actions，
    本函数不额外处理（chat 层 hop 循环统一消费）。
    """
    logger.info("Agent 模块处理: module=%s", module.module_code)
    _refresh_llm_config(session, module_code=module.module_code)  # R2
    return run_agent(session, module, session.cxt.llm_config,
                     force_close=force_close)


def _handle_module(session: Session, module, force_close: bool = False
                   ) -> TurnResult:
    """按模块类型分派单模块单轮处理。"""
    if module.type == ModuleType.AGENT:
        return _run_agent_pipeline(session, module, force_close=force_close)
    if module.type == ModuleType.ROUTE:
        return _run_route_pipeline(session, module, force_close=force_close)
    return _run_fsm_pipeline(session, module, force_close=force_close)


def chat_turn(
        query: str,
        session_id: str,
        all_sessions: Dict[str, Session],
) -> ChatResult:
    """处理一轮用户对话，返回完整产出（文本 + 预留动作）。

    1. 按 session_id 定位 session，cxt 轮首重置（lifecycle.begin_turn）
    2. 校验 pattern / 入口模块，解析 LLM 配置（R1）
    3. 同轮 hop 循环（max_hops）：按模块类型分派处理；消费 cxt.actions
       中的 ModuleJumpEvent → 重路由到目标模块同轮续答；
       超跳数 force_close 强制收尾
    4. 轮末 history 追加（lifecycle.end_turn），快照产出 ChatResult
    """
    # ------------------------------------------------------------------
    # 1. 定位 session；轮首重置（user_query 覆写 + 每轮字段归零——
    #    hop 之前恰好一次）
    # ------------------------------------------------------------------
    session = all_sessions.get(session_id)
    if session is None:
        logger.warning("会话不存在: %s", session_id)
        return ChatResult(text="会话不存在，请先发起对话任务")

    _lifecycle.begin_turn(session.cxt, query)

    pattern = session.pattern
    if pattern is None:
        logger.warning("会话 %s 未绑定对话模板", session_id)
        return ChatResult(text="对话模板未配置")

    # ------------------------------------------------------------------
    # 2. 定位入口模块（cxt.current_module_code 优先，回落 entry）
    # ------------------------------------------------------------------
    current_module_code = session.cxt.current_module_code or pattern.entry_module_code
    if not current_module_code:
        logger.warning("会话 %s 未找到入口模块", session_id)
        return ChatResult(text="入口模块未配置")

    # 写回 cxt：stages 与转移（跳转检测 / _fsm_node_transition）都从
    # cxt 读取当前位置
    session.cxt.current_module_code = current_module_code

    current_module = pattern.module_map.get(current_module_code)
    if current_module is None:
        logger.warning("模块不存在: %s", current_module_code)
        return ChatResult(text=f"模块 '{current_module_code}' 不存在")

    session.cxt.metadata["pattern_code"] = session.pattern_code

    # R1：每轮按当前位置解析 LLM 配置，override 优先（spec §4）
    try:
        _refresh_llm_config(session)
    except Exception as e:
        logger.error("加载 LLM 配置失败: %s", e)
        return ChatResult(text=f"LLM 配置加载失败: {e}")

    # Record user message
    session.cxt.add_message("user", query, stage="chat")

    # ------------------------------------------------------------------
    # 3. Reentry loop: consume same-turn jump events（cxt.actions 通道）
    # ------------------------------------------------------------------
    max_hops = getattr(pattern, "max_hops", 2)
    try:
        for hop in range(max_hops):
            current_module = pattern.module_map[
                session.cxt.current_module_code or pattern.entry_module_code
            ]
            result = _handle_module(session, current_module)

            event = _pop_jump_event(session.cxt)
            if event is None:
                response = result.reply or ""
                break
            logger.info(
                "same-turn jump 第 %d 跳: → %s (source=%s)",
                hop + 1, event.target_module_code, event.source,
            )
            _apply_jump(session.cxt, event)
        else:
            # 超跳数：先消费残留事件落到最后目标，再以该模块强制收尾
            logger.warning("达到 max_hops=%d，强制收尾", max_hops)
            pending = _pop_jump_event(session.cxt)
            if pending is not None:
                _apply_jump(session.cxt, pending)
            current_module = pattern.module_map[
                session.cxt.current_module_code or pattern.entry_module_code
            ]
            result = _handle_module(session, current_module, force_close=True)
            response = result.reply or ""
    except Exception as e:
        logger.exception("对话处理异常: session=%s", session_id)
        response = f"对话处理异常: {e}"

    # ------------------------------------------------------------------
    # 4. 轮末：history 追加 assistant 消息（增量），快照产出
    # ------------------------------------------------------------------
    _lifecycle.end_turn(session.cxt, response)

    return build_chat_result(response, session.cxt)


# ---------------------------------------------------------------------------
# 兼容再导出（测试锚点，签名不变）
# ---------------------------------------------------------------------------

_handle_node_transition = _fsm_node_transition  # noqa: F401（clarify 测试锚点）


def chat(query: str, session_id: str, all_sessions: Dict[str, Session]) -> str:
    """兼容入口：处理一轮对话，返回回复文本（等价 chat_turn(...).text）。"""
    return chat_turn(query, session_id, all_sessions).text
