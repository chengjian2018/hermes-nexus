"""Query Rewriter stage 包 — 查询改写。

平铺模块 ``src/dialogue/query.py`` 迁移为包；旧导入路径
``from src.dialogue.query import QueryRewriter`` 继续可用。

Exports:
    - ``BaseQueryRewriter``: 查询改写 stage 抽象基类。
    - ``QueryRewriter``: 默认查询改写实现。
"""

from src.dialogue.query.query import BaseQueryRewriter, QueryRewriter

__all__ = ["BaseQueryRewriter", "QueryRewriter"]
