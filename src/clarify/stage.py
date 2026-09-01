"""ClarifyStage —— 双轨澄清一体化 stage（判别 + 检索 + 门控 + 生成）。

插入位置：FSM 管线 NLU 与 NLG 之间（仅 enable_clarify=True 的模块接入）。

执行流程（详见 spec 5.1）：
1. 每轮重置 ctx.metadata["clarify"] = {"triggered": False}
2. 触发判定：nlu_result.next_node == "clarify"
3. 取固定澄清槽位 topic / keywords
4. 组装检索 query：user_query + topic + keywords
5. 知识库召回（专用 MultiPathRecaller）
6. ClarifyRouteRule 门控 → mode 三选一
7. 按 mode 选模板生成回复（本轮唯一的 NLG 调用），写 ctx.nlg_result
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.clarify.prompts import CLARIFY_PROMPTS
from src.clarify.rule import ClarifyRouteRule
from src.dialogue.base import DialogueContext, PipelineStage, fill_prompt_template
from src.dialogue.recaller import MultiPathRecaller

logger = logging.getLogger(__name__)

CLARIFY_NODE_CODE = "clarify"


class ClarifyStage(PipelineStage):
    """双轨澄清 stage。"""

    stage_name = "clarify_stage"

    def __init__(
        self,
        recaller: MultiPathRecaller,
        rule: Optional[ClarifyRouteRule] = None,
        default_prompt: Optional[str] = None,
    ):
        self.recaller = recaller
        self.rule = rule or ClarifyRouteRule()
        self.default_prompt = default_prompt

    # ------------------------------------------------------------------
    # LLM 生成（与 NLU/NLG 相同的调用链；测试中可整体替换）
    # ------------------------------------------------------------------

    def _generate(self, prompt: str, llm_config: Optional[Dict[str, Any]] = None) -> str:
        """调用 LLM 生成澄清回复。"""
        if llm_config is None:
            from config.config import get_llm_config
            llm_config = get_llm_config()

        from src.llm.resolve import build_provider
        provider = build_provider(llm_config)
        messages = [{"role": "user", "content": prompt}]
        result = provider.chat_completion(
            messages=messages,
            model=llm_config["model"],
            temperature=llm_config.get("temperature", 0.7),
            max_tokens=llm_config.get("max_tokens", 2048),
        )
        return result.get("content", "")

    # ------------------------------------------------------------------
    # 触发判定与槽位提取
    # ------------------------------------------------------------------

    @staticmethod
    def _is_triggered(ctx: DialogueContext) -> bool:
        nlu_result = ctx.nlu_result or {}
        return nlu_result.get("next_node") == CLARIFY_NODE_CODE

    @staticmethod
    def _extract_open_slots(ctx: DialogueContext) -> Dict[str, Any]:
        slots = (ctx.nlu_result or {}).get("slots", {}) or {}
        topic = slots.get("topic", "") or ""
        keywords = slots.get("keywords", []) or []
        if isinstance(keywords, str):
            keywords = [keywords]
        return {"topic": topic, "keywords": [str(k) for k in keywords]}

    # ------------------------------------------------------------------
    # 检索 query 组装与结果格式化
    # ------------------------------------------------------------------

    @staticmethod
    def _build_search_query(ctx: DialogueContext, open_slots: Dict[str, Any]) -> str:
        parts = [ctx.user_query, open_slots["topic"], *open_slots["keywords"]]
        return " ".join(p for p in parts if p)

    @staticmethod
    def _format_recall_for_prompt(results: List[Dict[str, Any]], top_n: int = 3) -> str:
        if not results:
            return "（无相关知识库内容）"
        lines = []
        for r in results[:top_n]:
            lines.append(f"- {r.get('id', '')}: {r.get('content', '')}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 检索执行（异常降级 → 空结果，门控自然走 fallback）
    # ------------------------------------------------------------------

    def _do_recall(self, ctx: DialogueContext, search_query: str) -> List[Dict[str, Any]]:
        """用组装好的 query 执行召回，返回融合结果；异常时返回空列表。

        MultiPathRecaller.execute 以 ctx.user_query 为检索文本并写入
        ctx.pre_recall_results —— 这里临时替换 user_query，执行后还原，
        避免污染下游 stage 看到的原始问句。
        """
        original_query = ctx.user_query
        original_recall = ctx.pre_recall_results
        try:
            ctx.user_query = search_query
            self.recaller.phase = "pre"
            self.recaller.execute(ctx)
            return list(ctx.pre_recall_results)
        except Exception as e:
            logger.warning("澄清检索异常，降级 fallback: %s", e, exc_info=True)
            return []
        finally:
            ctx.user_query = original_query
            ctx.pre_recall_results = original_recall

    # ------------------------------------------------------------------
    # PipelineStage 接口
    # ------------------------------------------------------------------

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        # 1. 每轮重置（防跨轮残留）
        ctx.metadata["clarify"] = {"triggered": False}

        # 2. 触发判定（模块开关由管线装配侧保证，stage 只看意图）
        if not self._is_triggered(ctx):
            return ctx

        open_slots = self._extract_open_slots(ctx)
        search_query = self._build_search_query(ctx, open_slots)

        # 3~6. 检索 + 门控（异常降级 fallback，永不阻塞主管线）
        recall_results = self._do_recall(ctx, search_query)
        try:
            mode, adjusted = self.rule.route(
                recall_results, open_slots["topic"], open_slots["keywords"]
            )
        except Exception as e:
            logger.warning("澄清门控异常，降级 fallback: %s", e, exc_info=True)
            mode, adjusted = "fallback", recall_results
        logger.info(
            "澄清门控: session=%s, mode=%s, top_score=%s, query=%r",
            ctx.session_id, mode,
            adjusted[0].get("score") if adjusted else None,
            search_query,
        )

        # 7. 按 mode 生成（本轮唯一的 NLG 调用）
        prompt = self._build_prompt(ctx, mode, open_slots, adjusted)
        try:
            content = self._generate(prompt, ctx.llm_config).strip()
        except Exception as e:
            logger.warning("澄清生成异常，使用兜底话术: %s", e, exc_info=True)
            content = "抱歉，这个问题我需要确认一下。我们继续刚才的任务好吗？"

        # 8. 写回
        ctx.nlg_result = {"content": content}
        ctx.metadata["clarify"] = {
            "triggered": True,
            "mode": mode,
            "recall_results": adjusted,
            "open_slots": open_slots,
            "query": search_query,
        }
        return ctx

    # ------------------------------------------------------------------
    # prompt 组装
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        ctx: DialogueContext,
        mode: str,
        open_slots: Dict[str, Any],
        recall_results: List[Dict[str, Any]],
    ) -> str:
        template = CLARIFY_PROMPTS.get(
            mode, self.default_prompt or CLARIFY_PROMPTS["fallback"]
        )
        keywords_text = "、".join(open_slots["keywords"]) or "（无）"
        slots = {
            "query": ctx.user_query,
            "topic": open_slots["topic"] or "（无）",
            "keywords": keywords_text,
            "recall_info": self._format_recall_for_prompt(recall_results),
            "cur_node": ctx.format_cur_node(stage="nlg"),
            "history": ctx.format_history(),
            "task_info": ctx.format_task_info(),
        }
        return fill_prompt_template(template, slots)
