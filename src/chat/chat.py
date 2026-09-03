"""
Dialogue processing — 轮次编排器（orchestrator）。

职责收窄为"一轮对话的编排"：session 定位 → cxt 轮次生命周期 → 同轮 hop
循环 → 产出 ChatResult。单模块的处理按类型分派到 handlers 包：

- AGENT → handlers/agent.py（委托可插拔 AgentRunner，默认 loop.run_agent）
- FSM   → handlers/fsm.py（stages 执行 + next_node 跳转）
- ROUTE → handlers/route.py（stages 执行 + jump_module 静默分发 / root 重置）

cxt 字段生命周期（每轮重置 / 跨轮保留 / 增量更新）统一由
context_lifecycle.TurnLifecycle 管理，本模块只在正确的时序点调用。

入口：
- chat_turn() ：新全量入口，返回 ChatResult（text + 预留 actions + 转移链）
- chat()      ：兼容入口（main.py / cli.py / 既有测试），等价 chat_turn().text
"""

import logging
from typing import Dict, Optional

from config.config import get_llm_config
from src.chat.agents import AgentRunner
from src.chat.context_lifecycle import TurnLifecycle
from src.chat.handlers import resolve_handler
from src.chat.response import ChatResult, build_chat_result
from src.chat.session import Session
from src.chat.loop import TurnResult, run_agent  # noqa: F401 (兼容再导出)

logger = logging.getLogger(__name__)

# cxt 字段生命周期唯一管理者（无状态，模块级复用）
_lifecycle = TurnLifecycle()


def _refresh_llm_config(session: Session, module_code: str = "",
                        node_code: str = "") -> None:
    """按当前位置解析 LLM 配置并写入 cxt.llm_config（spec §4，R1-R4 共用）。

    本函数必须留在 chat 模块：R1-R3 的 get_llm_config 经本命名空间解析
    （tests/test_llm_refresh.py 等以 patch("src.chat.chat.get_llm_config")
    为锚点），并作为 callable 注入各 handler——handler 不自 import。
    """
    cxt = session.cxt
    cxt.llm_config = get_llm_config(
        pattern_code=session.pattern_code or cxt.metadata.get("pattern_code", ""),
        module_code=module_code or cxt.current_module_code or "",
        node_code=node_code or cxt.current_node_code or "",
        override=cxt.metadata.get("llm_override"),
    )


def chat_turn(
    query: str,
    session_id: str,
    all_sessions: Dict[str, Session],
    agent_runner: Optional[AgentRunner] = None,
) -> ChatResult:
    """处理一轮用户对话，返回完整产出（文本 + 预留动作 + 转移链）。

    1. 按 session_id 定位 session，cxt 轮首重置（lifecycle.begin_turn）
    2. 校验 pattern / 入口模块，解析 LLM 配置（R1）
    3. 同轮 hop 循环（max_hops）：按模块类型分派 handler；
       dispatch_event → 跳目标模块同轮续答；超跳数 force_close 强制收尾
    4. 轮末 history 追加（lifecycle.end_turn），快照产出 ChatResult

    Args:
        query: 用户输入
        session_id: 会话 ID
        all_sessions: 全局 session 表
        agent_runner: 可选 agent 执行器（仅 AGENT 模块用；缺省 loop.run_agent）

    Returns:
        ChatResult: .text 为出口回复（异常/早错路径同样落 text）
    """
    # ------------------------------------------------------------------
    # 1. 定位 session；轮首重置（user_query 覆写 + 每轮字段归零 +
    #    dispatch 记账清空——hop 之前恰好一次）
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

    # Write the resolved module code back to cxt: stages and transitions
    # (e.g. _RouteNodeAdvance / fsm_node_transition) read it from cxt.
    session.cxt.current_module_code = current_module_code

    current_module = pattern.module_map.get(current_module_code)
    if current_module is None:
        logger.warning("模块不存在: %s", current_module_code)
        return ChatResult(text=f"模块 '{current_module_code}' 不存在")

    # Ensure LLM config is injected into the context（R1：每轮按当前位置解析，
    # override 优先；spec §4）
    session.cxt.metadata["pattern_code"] = session.pattern_code
    try:
        _refresh_llm_config(session)
    except Exception as e:
        logger.error("加载 LLM 配置失败: %s", e)
        return ChatResult(text=f"LLM 配置加载失败: {e}")

    # Record user message
    session.cxt.add_message("user", query, stage="chat")

    # 注入 dispatch_graph（launch/store 恢复路径均不注入，setdefault 一处覆盖）
    _lifecycle.bind_pattern(session.cxt, pattern)

    # ------------------------------------------------------------------
    # 3. Reentry loop: consume same-turn dispatch events (spec §3.1)
    # ------------------------------------------------------------------
    max_hops = getattr(pattern, "max_hops", 2)
    try:
        for hop in range(max_hops):
            current_module = pattern.module_map[
                session.cxt.current_module_code or pattern.entry_module_code
            ]
            handler = resolve_handler(
                current_module.type, _refresh_llm_config,
                agent_runner=agent_runner)
            result = handler.handle(session, current_module)

            if result.dispatch_event is None:
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
            handler = resolve_handler(
                current_module.type, _refresh_llm_config,
                agent_runner=agent_runner)
            result = handler.handle(session, current_module, force_close=True)
            response = result.reply or ""
    except Exception as e:
        logger.exception("对话处理异常: session=%s", session_id)
        response = f"对话处理异常: {e}"

    # ------------------------------------------------------------------
    # 4. 轮末：history 追加 assistant 消息（增量），快照产出
    # ------------------------------------------------------------------
    _lifecycle.end_turn(session.cxt, response)

    return build_chat_result(response, session.cxt)


def chat(query: str, session_id: str, all_sessions: Dict[str, Session],
         agent_runner: Optional[AgentRunner] = None) -> str:
    """兼容入口：处理一轮对话，返回回复文本（等价 chat_turn(...).text）。"""
    return chat_turn(query, session_id, all_sessions,
                     agent_runner=agent_runner).text


# ---------------------------------------------------------------------------
# 兼容再导出（测试锚点，签名不变）
# ---------------------------------------------------------------------------

from src.chat.handlers.base import default_skeleton as _default_skeleton          # noqa: F401,E402
from src.chat.handlers.fsm import fsm_node_transition as _handle_node_transition  # noqa: F401,E402
