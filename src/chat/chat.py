import logging
from typing import Dict, Optional, Any

from chat.loop import loop
from dialogue import DialogueContext
from src.chat.context_lifecycle import TurnLifecycle
from config.config import get_llm_config
from src.chat.session import Session
from src.dialogue.module import ModuleType
from src.chat.response import ChatResult, build_chat_result
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


def _default_skeleton(module) -> list:
    """默认管线骨架（槽位延迟解析，不绑定节点）。

    [PreRecallSlot, QuerySlot, PostRecallSlot, GenerateSlot]；
    FSM/ROUTE 的差异（advance / clarify 插入）由 GenerateSlot 展开处理
    （stage_slots.resolve_stage），骨架本身全模块类型同形。
    """
    return [PreRecallSlot(), QuerySlot(), PostRecallSlot(), GenerateSlot()]


def _refresh_llm_config(cxt, pattern_code, module_code, node_code, llm_override=None) -> None:
    """按当前位置解析 LLM 配置并写入 cxt.llm_config（spec §4，R1-R4 共用）。

    本函数必须留在 chat 模块：R1-R3 的 get_llm_config 经本命名空间解析
    （tests/test_llm_refresh.py 等以 patch("src.chat.chat.get_llm_config")
    为锚点），并作为 callable 注入各 handler——handler 不自 import。
    """
    cxt.llm_config = get_llm_config(
        pattern_code=pattern_code,
        module_code=module_code,
        node_code=node_code,
        override=llm_override,
    )
    # cxt.llm_config = get_llm_config(
    #     pattern_code=session.pattern_code or cxt.metadata.get("pattern_code", ""),
    #     module_code=module_code or cxt.current_module_code or "",
    #     node_code=node_code or cxt.current_node_code or "",
    #     override=cxt.metadata.get("llm_override"),
    # )


def _fsm_node_transition(cxt: DialogueContext) -> None:
    """FSM 轮末节点转移。
    todo 记录轨迹

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
    next_node = cxt.node_map[next_node_code]
    if hasattr(next_node, "module_code"):
        cxt.current_module_code = next_node.module_code


def _run_stages(cxt, module, pattern) -> None:
    """顺序执行管线 stages（槽位按 node > module > pattern 延迟解析）。

    顺序执行 stages（槽位延迟解析在 resolve_stage 内）。
    """
    stages = pattern.stages or _default_skeleton()

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
            try:
                cxt = concrete.execute(cxt)
                logger.debug("Stage '%s' 执行完成", concrete.stage_name)
            except Exception as e:
                logger.error(
                    "Stage '%s' 执行异常: %s", concrete.stage_name, e,
                    exc_info=True
                )
                raise


def _run_fsm_pipline(session: Session, cxt, pattern, module):
    _run_stages(cxt, module, pattern)
    _fsm_node_transition(cxt)
    next_node = pattern.node_map.get(cxt.current_node_code, None)
    if next_node and next_node.is_end:
        cxt.actions.append([{"conversation_end": True}])
    if cxt.nlg_result:
        cxt.nlg_result = {}
    res = build_chat_result(text=cxt.nlg_result.get("content", ""), cxt=cxt)
    return res


def _run_route_pipline(session: Session, cxt, pattern, module):
    _run_stages(cxt, module, pattern)

    return {}


def chat_turn(
        query: str,
        session_id: str,
        all_sessions: Dict[str, Session]
) -> ChatResult:
    session = all_sessions.get(session_id)
    cxt = session.cxt

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

    session.cxt.metadata["pattern_code"] = session.pattern_code

    # 注入 dispatch_graph（launch/store 恢复路径均不注入，setdefault 一处覆盖）
    _lifecycle.bind_pattern(session.cxt, pattern)

    current_module = pattern.module_map[
        session.cxt.current_module_code or pattern.entry_module_code
        ]

    if cxt.current_node_code is None:
        if current_module.module_nodes:
            first_node = current_module.module_nodes[0]
            cxt.current_node_code = first_node.node_code
            logger.info(
                "首次进入模块 %s，使用首节点: %s",
                current_module.module_code,
                first_node.node_code,
            )
        else:
            raise ValueError(f"模块 '{current_module.module_code}' 无可用节点")

    # cxt.llm_config = get_llm_config(
    #     pattern_code=session.pattern_code or cxt.metadata.get("pattern_code", ""),
    #     module_code=module_code or cxt.current_module_code or "",
    #     node_code=node_code or cxt.current_node_code or "",
    #     override=cxt.metadata.get("llm_override"),
    # )
    try:
        _refresh_llm_config(cxt,
                            pattern_code=session.pattern_code or cxt.metadata.get("pattern_code", ""),
                            module_code=current_module_code or cxt.current_module_code or "",
                            node_code=cxt.current_node_code or "",
                            llm_override=cxt.metadata.get("llm_override"))
    except Exception as e:
        logger.error("加载 LLM 配置失败: %s", e)
        return ChatResult(text=f"LLM 配置加载失败: {e}")

    cur_node = cxt.node_map.get(cxt.current_node_code)
    if cur_node is None:
        raise ValueError(
            f"节点 '{cxt.current_node_code}' 不存在于 node_map 中"
        )

    if current_module.type == ModuleType.AGENT:
        reply = loop()
    elif current_module.type == ModuleType.ROUTE:
        stages = pattern.stages or _default_skeleton()
        logger.info(
            "Pipeline 开始: session=%s, module=%s, node=%s, stages=%s",
            cxt.session_id,
            current_module.module_code,
            cxt.current_node_code,
            [s.stage_name for s in stages],
        )
        for stage in stages:
            for concrete in resolve_stage(stage, cxt, current_module, pattern):
                try:
                    cxt = concrete.execute(cxt)
                    logger.debug("Stage '%s' 执行完成", concrete.stage_name)
                    if "_nlu" in concrete.stage_name:
                        # 判断next_node是否为agent类型的module
                        # 且没有实际回答
                        nlu_result = cxt.nlu_result or {}
                        nlg_result = cxt.nlg_result or {}
                        next_node = nlu_result.get("next_node", "")
                        if next_node in cxt.node_map and (hasattr(cxt.node_map[next_node], "module_code")) and len(
                                nlg_result) == 0:
                            # 更新cxt
                            cxt.current_node_code = next_node
                            cxt.current_module_code = getattr(cxt.node_map[next_node], "module_code")
                            # 进入agent回复
                            reply = loop()

                except Exception as e:
                    logger.error(
                        "Stage '%s' 执行异常: %s", concrete.stage_name, e,
                        exc_info=True
                    )
                    raise

    else:
        result = _run_fsm_pipline(session, cxt, pattern, current_module)


def chat(query: str, session_id: str, all_sessions: Dict[str, Session]) -> ChatResult:
    """兼容入口：处理一轮对话，返回回复文本（等价 chat_turn(...).text）。"""
    return chat_turn(query, session_id, all_sessions)
