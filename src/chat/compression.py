"""会话历史压缩 —— token 超阈值时旧消息 LLM 摘要成 summary 行 + 保留最近 N 条。

借鉴 Customer-Agent SessionManager 的压缩设计，两处刻意不照抄的修正：
- retain 边界向前吸附到 assistant(tool_calls) 配对 run 起点——按条数硬切
  会把 tool 行切在界内、其 assistant 行切在界外，回放守卫整段降级
  （Customer-Agent 的存量缺陷）
- 摘要 LLM 调用任何异常 → 放弃压缩原样返回（铁律：摘要失败绝不删历史）

编排三方（LLM + store + cxt），故独立于 store（纯持久化层不耦合 LLM）。
"""

import logging
from typing import TYPE_CHECKING, Any, Dict, List

from src.dialogue.base import DialogueContext, SessionMessage
from src.llm.resolve import build_provider
from src.prompt import HISTORY_SUMMARY_PROMPT

if TYPE_CHECKING:
    from src.chat.session import Session
    from src.chat.store import SessionStore

logger = logging.getLogger(__name__)

# 单条消息进摘要拼接的截断长度（防超长 tool 结果撑爆摘要请求）
_SUMMARY_MSG_TRUNCATE = 200


# ============================================================================
# Token 估算（字符近似；无 tiktoken 依赖）
# ============================================================================

def _estimate_text(text: str) -> int:
    """CJK×2 + 其他×0.25 的字符近似估算（Customer-Agent 降级公式）。"""
    if not text:
        return 0
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    return int(cjk * 2 + (len(text) - cjk) * 0.25)


def estimate_tokens(history: List[SessionMessage]) -> int:
    """消息列表总 token 估算：各条 content/tool_calls + 每条 4 overhead。"""
    total = 0
    for msg in history:
        total += 4
        total += _estimate_text(msg.content)
        if msg.tool_calls:
            total += _estimate_text(str(msg.tool_calls))
    return total


def should_compress(
    history: List[SessionMessage], threshold: int, retain_count: int
) -> bool:
    """阈值 > 0 且估算超阈值且条数多到值得压（有旧消息可摘）。"""
    if threshold <= 0 or len(history) <= retain_count + 1:
        return False
    return estimate_tokens(history) > threshold


# ============================================================================
# 压缩执行
# ============================================================================

def _snap_to_pair_boundary(history: List[SessionMessage], split: int) -> int:
    """split 向前吸附到 assistant(tool_calls) 配对 run 的起点。

    若 split 落在某 tool 行上（其 run 起点是前面的 assistant(tool_calls)
    行），把 split 回退到该 assistant 行之前——界外不留下配对残段。
    """
    while 0 < split < len(history) and history[split].role == "tool":
        start = split - 1
        while (start >= 0 and history[start].role == "tool"):
            start -= 1
        if (start >= 0 and history[start].role == "assistant"
                and history[start].tool_calls):
            split = start
        else:
            break
    return split


def _build_summary_input(history: List[SessionMessage], end_idx: int) -> str:
    """旧消息拼接成摘要请求文本（role 标注 + 单条截断）。"""
    lines = []
    for msg in history[:end_idx]:
        content = (msg.content or "")[:_SUMMARY_MSG_TRUNCATE]
        if msg.role == "assistant" and msg.tool_calls:
            names = ",".join(
                tc.get("function", {}).get("name", "?")
                for tc in msg.tool_calls
            )
            content = f"{content} [调用工具: {names}]".strip()
        lines.append(f"[{msg.role}]: {content}")
    return "\n".join(lines)


def compress_history(
    session: "Session",
    store: "SessionStore",
    llm_config: Dict[str, Any],
    retain_count: int,
) -> bool:
    """执行压缩：旧消息 LLM 摘要 → summary 行 + 保留最近条数。

    Returns:
        是否压缩成功。任何一步失败（DB 不齐 / LLM 异常）都返回 False 且
        DB 与 cxt.history 原样未动。
    """
    cxt = session.cxt
    split = _snap_to_pair_boundary(
        cxt.history, max(0, len(cxt.history) - retain_count))
    if split <= 0:
        return False  # 全部都在保留窗口内，无旧消息可摘

    # DB/内存对齐校验：write-through 下二者应一致；不齐（如 sink 曾故障）
    # 绝不删——replace_history 内还会再校验一次（事务级兜底）
    try:
        db_history = store.get_history(session.session_id)
    except Exception:
        logger.exception("压缩前读 DB 失败，放弃: session=%s", session.session_id)
        return False
    if len(db_history) != len(cxt.history):
        logger.warning(
            "DB/内存消息数不齐，放弃压缩: session=%s db=%d mem=%d",
            session.session_id, len(db_history), len(cxt.history),
        )
        return False

    # 摘要 LLM 调用（复用当前位置的 llm_config——R1 刚刷新）
    prompt = HISTORY_SUMMARY_PROMPT.replace(
        "{__msg_count__}", str(split)
    ).replace(
        "{__dialog_text__}", _build_summary_input(cxt.history, split)
    )
    try:
        provider = build_provider(llm_config)
        result = provider.chat_completion(
            messages=[
                {"role": "system", "content": "你是一个对话摘要助手。"},
                {"role": "user", "content": prompt},
            ],
            model=llm_config["model"],
            temperature=llm_config.get("temperature", 0.3),
            max_tokens=llm_config.get("max_tokens", 1024),
        )
        summary = (result.get("content", "") or "").strip()
    except Exception:
        logger.exception(
            "摘要 LLM 调用失败，放弃压缩（历史原样保留）: session=%s",
            session.session_id,
        )
        return False
    if not summary:
        logger.warning("摘要为空，放弃压缩: session=%s", session.session_id)
        return False

    # DB 重排（事务内：对齐校验 → 删当代全部 → summary 最前 + retained 重插）
    try:
        store.replace_history(session, summary, keep_idx=split)
    except Exception:
        logger.exception(
            "DB 压缩重排失败，放弃（历史原样保留）: session=%s",
            session.session_id,
        )
        return False

    # 内存同步重建 + 轮内标记修正（不重设会导致 query 重复注入）
    summary_msg = SessionMessage(
        role="summary", content=summary, stage="compress")
    cxt.history = [summary_msg] + list(cxt.history[split:])
    cxt.turn_history_start = len(cxt.history)
    logger.info(
        "历史压缩完成: session=%s 摘要=%d 条 保留=%d 条 (split=%d)",
        session.session_id, split, len(cxt.history) - 1, split,
    )
    return True


def maybe_compress(session: "Session", store: "SessionStore") -> None:
    """压缩触发入口（chat_turn 在 R1 之后、add user 之前调用）。

    store None / 阈值 0 / 条数不足 → 静默跳过；失败仅记日志——压缩是
    优化，绝不阻断对话。
    """
    if store is None:
        return
    try:
        from config.config import get_session_compress_config
        threshold, retain_count = get_session_compress_config()
    except Exception:
        logger.exception("读取压缩配置失败，跳过压缩")
        return
    if not should_compress(session.cxt.history, threshold, retain_count):
        return
    llm_config = session.cxt.llm_config
    if not llm_config:
        logger.warning("llm_config 未解析，跳过压缩: session=%s",
                       session.session_id)
        return
    logger.info(
        "触发历史压缩: session=%s 估算 tokens=%d 阈值=%d",
        session.session_id,
        estimate_tokens(session.cxt.history),
        threshold,
    )
    try:
        compress_history(session, store, llm_config, retain_count)
    except Exception:
        logger.exception(
            "压缩执行异常（历史原样保留）: session=%s", session.session_id)
