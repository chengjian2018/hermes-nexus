"""ClarifyStage 单测 —— 触发判定、检索组装、门控与生成。"""

import pytest

from src.clarify.rule import ClarifyRouteRule
from src.clarify.stage import ClarifyStage
from src.dialogue.base import DialogueContext
from src.dialogue.recaller import (
    KeywordRecallPath,
    MultiPathRecaller,
    ScoreThresholdFilter,
    WeightedScoreFusion,
)


# ============================================================================
# 测试脚手架
# ============================================================================

KB_DOCS = [
    {"id": "fee_policy", "content": "除车价外仅收取上牌费与服务费，无其他收费",
     "metadata": {"keywords": ["收费", "服务费", "上牌费"]}},
    {"id": "insurance", "content": "保险可在店内购买，也可自行购买",
     "metadata": {"keywords": ["保险"]}},
]


def make_ctx(next_node="clarify", topic="费用", keywords=None, query="还要收别的钱吗"):
    """构造带澄清触发的 DialogueContext。"""
    ctx = DialogueContext(session_id="t", user_query=query)
    ctx.nlu_result = {
        "next_node": next_node,
        "slots": {"topic": topic, "keywords": keywords or ["额外收费"]},
    }
    return ctx


def make_stage(kb_docs=None, rule=None):
    """构造挂内存关键词召回的 ClarifyStage（不依赖 ES）。"""
    recaller = MultiPathRecaller(
        recall_paths=[KeywordRecallPath(name="kb", documents=kb_docs or KB_DOCS)],
        filters=[ScoreThresholdFilter(threshold=0.1)],
        fusion=WeightedScoreFusion(),
    )
    return ClarifyStage(recaller=recaller, rule=rule or ClarifyRouteRule())


def make_gen_stage(mode_to_return, captured):
    """LLM 生成 mock：记录收到的 prompt，返回带模式标记的回复。"""

    def fake_generate(prompt: str, *args, **kwargs) -> str:
        captured.append(prompt)
        return f"[clarify:{mode_to_return}] 回复内容"

    stage = make_stage()
    stage._generate = fake_generate
    return stage


# ============================================================================
# 触发判定
# ============================================================================

class TestTrigger:

    def test_not_clarify_intent_passthrough(self):
        """next_node 非 clarify → 纯透传，不检索不生成。"""
        ctx = make_ctx(next_node="buy_ask_budget")
        captured = []
        stage = make_gen_stage("kb", captured)
        ctx2 = stage.execute(ctx)
        assert captured == []                       # 未调用生成
        assert ctx2.metadata["clarify"]["triggered"] is False

    def test_metadata_reset_each_turn(self):
        """上一轮残留的 clarify 元数据在本轮被重置。"""
        ctx = make_ctx(next_node="buy_ask_budget")
        ctx.metadata["clarify"] = {"triggered": True, "mode": "kb"}
        captured = []
        stage = make_gen_stage("kb", captured)
        stage.execute(ctx)
        assert ctx.metadata["clarify"] == {"triggered": False}

    def test_triggered_writes_metadata(self):
        ctx = make_ctx()
        captured = []
        stage = make_gen_stage("kb", captured)
        stage.execute(ctx)
        info = ctx.metadata["clarify"]
        assert info["triggered"] is True
        assert info["mode"] in ("kb", "fallback", "mixed")
        assert info["open_slots"] == {"topic": "费用", "keywords": ["额外收费"]}
        assert "query" in info


# ============================================================================
# 检索与门控
# ============================================================================

class TestRecallAndRoute:

    def test_kb_hit_uses_kb_mode(self):
        """高分命中（关键词重叠加分）→ kb 模式，prompt 含召回内容。"""
        ctx = make_ctx()
        captured = []
        stage = make_gen_stage("kb", captured)
        stage.execute(ctx)
        assert ctx.metadata["clarify"]["mode"] == "kb"
        assert "上牌费与服务费" in captured[0]      # 召回内容进了 prompt

    def test_no_recall_falls_back(self):
        """空知识库 → fallback（无召回默认轨道二）。"""
        ctx = make_ctx()
        captured = []
        stage = make_gen_stage("fallback", captured)
        stage.recaller = MultiPathRecaller(
            recall_paths=[KeywordRecallPath(name="kb", documents=[])],
        )
        stage.execute(ctx)
        assert ctx.metadata["clarify"]["mode"] == "fallback"
        assert ctx.nlg_result["content"].startswith("[clarify:fallback]")

    def test_search_error_falls_back(self):
        """检索异常 → fallback，不抛出。"""
        class BoomPath(KeywordRecallPath):
            def recall(self, query, ctx, **kwargs):
                raise RuntimeError("es down")

        ctx = make_ctx()
        stage = ClarifyStage(
            recaller=MultiPathRecaller(recall_paths=[BoomPath(name="kb")]),
        )
        stage._generate = lambda prompt, *a, **k: "[clarify:fallback] 兜底"
        stage.execute(ctx)
        assert ctx.metadata["clarify"]["mode"] == "fallback"
        assert ctx.nlg_result["content"] == "[clarify:fallback] 兜底"

    def test_nlg_result_written(self):
        ctx = make_ctx()
        stage = make_gen_stage("kb", [])
        stage.execute(ctx)
        assert ctx.nlg_result == {"content": "[clarify:kb] 回复内容"}


# ============================================================================
# 检索 query 组装
# ============================================================================

class TestQueryBuild:

    def test_query_combines_user_query_topic_keywords(self):
        """检索 query = user_query + topic + keywords 拼接。"""
        seen_queries = []

        class SpyPath(KeywordRecallPath):
            def recall(self, query, ctx, **kwargs):
                seen_queries.append(query)
                return super().recall(query, ctx, **kwargs)

        ctx = make_ctx()
        stage = ClarifyStage(
            recaller=MultiPathRecaller(recall_paths=[SpyPath(name="kb")]),
        )
        stage._generate = lambda p, *a, **k: "ok"
        stage.execute(ctx)
        assert seen_queries == ["还要收别的钱吗 费用 额外收费"]
