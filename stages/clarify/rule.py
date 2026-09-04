"""ClarifyRouteRule —— R1 规则门控（纯函数，不碰 LLM）。

输入召回结果与澄清槽位（topic / keywords），输出三选一模式：
- "kb"       : 轨道一 —— 召回置信度高，基于业务知识库回答
- "fallback" : 轨道二 —— 无召回或置信度低，问题响应 + 强拉回
- "mixed"    : 模糊地带 —— 部分业务知识 + 问题响应

门控规则（详见 spec 5.3）：
- 召回为空 → fallback（无召回时的默认轨道）
- 修正后 top score >= t_high → kb
- t_low <= 修正后 top score < t_high → mixed
- 修正项：topic / keywords 与 chunk 的 metadata.keywords（biz_keyword）重叠，
  top score 加 keyword_bonus
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


class ClarifyRouteRule:
    """R1 门控规则。"""

    def __init__(
        self,
        t_high: float = 0.6,
        t_low: float = 0.3,
        keyword_bonus: float = 0.1,
    ):
        self.t_high = t_high
        self.t_low = t_low
        self.keyword_bonus = keyword_bonus

    def route(
        self,
        recall_results: List[Dict[str, Any]],
        topic: str,
        keywords: List[str],
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """门控判别，返回 (mode, 加分修正后的结果副本)。

        Args:
            recall_results: 融合重排后的召回结果（标准化格式）。
            topic: NLU 澄清槽位 —— 偏题问题主题。
            keywords: NLU 澄清槽位 —— 偏题问句关键词列表。

        Returns:
            (mode, adjusted_results)。adjusted_results 为浅拷贝列表，
            top 结果命中关键词重叠时 score 已加成。
        """
        if not recall_results:
            return "fallback", []

        adjusted = [dict(r) for r in recall_results]
        top = adjusted[0]

        if self._has_overlap(top, topic, keywords):
            top["score"] = round(top.get("score", 0.0) + self.keyword_bonus, 6)

        top_score = top.get("score", 0.0)
        if top_score >= self.t_high:
            mode = "kb"
        elif top_score >= self.t_low:
            mode = "mixed"
        else:
            mode = "fallback"

        return mode, adjusted

    @staticmethod
    def _has_overlap(
        top: Dict[str, Any],
        topic: str,
        keywords: List[str],
    ) -> bool:
        """topic / keywords 是否与 top chunk 的业务关键词重叠。

        chunk 关键词取 ``metadata.keywords``（biz_keyword 字段标准化后所在位置），
        逐词互查包含关系。
        """
        chunk_keywords = [
            str(k) for k in (top.get("metadata", {}).get("keywords") or [])
        ]
        terms = [topic] + [str(k) for k in keywords if k]
        for term in terms:
            if not term:
                continue
            for ck in chunk_keywords:
                if term in ck or ck in term:
                    return True
        return False
