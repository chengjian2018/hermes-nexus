"""TimeAugQueryRewriter —— 时间增强确定性查询改写测试。

契约：
- 纯规则零 LLM：有时间实体时标注追加进 rewritten_queries[0]
- 无时间实体时 rewritten_queries = [原 query]（与 LLM 版兜底一致）
- time_base 取 ctx.metadata["time_base"]（未注入用当前时间）
- 槽位机制兼容：is_valid_stage 通过、QuerySlot 三层解析可命中
"""

import time as _time

import pytest

from src.augmentation import augment_time
from src.dialogue.base import DialogueContext
from src.dialogue.node import BaseNode
from src.dialogue.module import FSMModule
from src.dialogue.query import TimeAugQueryRewriter
from src.dialogue.stage_slots import QuerySlot, is_valid_stage, resolve_stage

# 固定基准：2026-09-03 10:00:00（周四）—— 下周一 = 2026-09-07
TIME_BASE = _time.mktime(_time.strptime("2026-09-03 10:00:00", "%Y-%m-%d %H:%M:%S"))


def _ctx(query="我下周一可以去", time_base=TIME_BASE):
    ctx = DialogueContext(session_id="t", user_query=query)
    if time_base is not None:
        ctx.metadata["time_base"] = time_base
    return ctx


def test_valid_stage_duck_type():
    assert is_valid_stage(TimeAugQueryRewriter())


def test_time_entity_augmented():
    ctx = TimeAugQueryRewriter().execute(_ctx())
    assert ctx.rewritten_queries == ["我下周一(2026-09-07)可以去"]


def test_no_time_entity_passthrough():
    ctx = TimeAugQueryRewriter().execute(_ctx(query="这个多少钱"))
    assert ctx.rewritten_queries == ["这个多少钱"]


def test_time_span_augmented():
    ctx = TimeAugQueryRewriter().execute(_ctx(query="明天下午3点到5点有空吗"))
    assert ctx.rewritten_queries == ["明天下午3点到5点(2026-09-04 15:00~17:00)有空吗"]


def test_time_base_defaults_to_now(monkeypatch):
    # 未注入 time_base 时走 augment_time 的默认（当前时间）——
    # 用 monkeypatch 验证传给 augment_time 的 time_base 为 None
    calls = {}

    def _fake_augment(text, time_base=None):
        calls["time_base"] = time_base
        return text

    monkeypatch.setattr("src.dialogue.query.time_aug.augment_time", _fake_augment)
    ctx = DialogueContext(session_id="t", user_query="下周一发货吗")
    TimeAugQueryRewriter().execute(ctx)
    assert calls["time_base"] is None
    assert ctx.rewritten_queries == ["下周一发货吗"]


def test_query_slot_resolves_to_time_aug_rewriter():
    """槽位三层解析：module 层配置 TimeAugQueryRewriter 可被 QuerySlot 命中。"""
    ctx = DialogueContext(session_id="t", user_query="q")
    ctx.current_module_code = "m1"
    ctx.current_node_code = "n1"
    module = FSMModule(
        module_code="m1", module_name="m1", module_description="d",
        module_todo_description="t", sub_modules=[],
        module_nodes=[BaseNode(node_code="n1", node_name="节点一")],
        query=TimeAugQueryRewriter(),
    )
    out = resolve_stage(QuerySlot(), ctx, module, None)
    assert len(out) == 1
    assert isinstance(out[0], TimeAugQueryRewriter)


def test_augment_time_consistency():
    """改写结果与 augment_time 直调一致（透传契约）。"""
    for query in ("我下周一可以去", "这个多少钱"):
        ctx = TimeAugQueryRewriter().execute(_ctx(query=query))
        assert ctx.rewritten_queries == [augment_time(query, time_base=TIME_BASE)]
