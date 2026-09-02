"""Recaller stage 包 — 多路召回 + 过滤 + 融合 + 重排。

平铺模块 ``src/dialogue/recaller.py`` 迁移为包；旧导入路径
``from src.dialogue.recaller import MultiPathRecaller`` 继续可用。

Exports:
    - ``MultiPathRecaller``: 多路召回主 stage（含 ``PreRecaller``/``PostRecaller`` 预设）。
    - 召回路径: ``KeywordRecallPath`` ``EmbeddingRecallPath`` ``ESRecallPath``
      ``LLMRecallPath`` ``CustomRecallPath``。
    - 过滤器: ``DedupFilter`` ``ScoreThresholdFilter`` ``MaxResultsFilter``
      ``FieldFilter`` ``FilterChain``。
    - 融合器: ``ReciprocalRankFusion`` ``WeightedScoreFusion`` ``RoundRobinFusion``。
    - 重排器: ``ScoreBasedReranker`` ``DiversityReranker`` ``LLMReranker``。
"""

from src.dialogue.recaller.recaller import (
    CustomRecallPath,
    DedupFilter,
    DiversityReranker,
    EmbeddingRecallPath,
    ESRecallPath,
    FieldFilter,
    FilterChain,
    KeywordRecallPath,
    LLMRecallPath,
    LLMReranker,
    MaxResultsFilter,
    MultiPathRecaller,
    PostRecaller,
    PreRecaller,
    ReciprocalRankFusion,
    RoundRobinFusion,
    ScoreBasedReranker,
    ScoreThresholdFilter,
    WeightedScoreFusion,
)

__all__ = [
    "CustomRecallPath",
    "DedupFilter",
    "DiversityReranker",
    "EmbeddingRecallPath",
    "ESRecallPath",
    "FieldFilter",
    "FilterChain",
    "KeywordRecallPath",
    "LLMRecallPath",
    "LLMReranker",
    "MaxResultsFilter",
    "MultiPathRecaller",
    "PostRecaller",
    "PreRecaller",
    "ReciprocalRankFusion",
    "RoundRobinFusion",
    "ScoreBasedReranker",
    "ScoreThresholdFilter",
    "WeightedScoreFusion",
]
