"""NLG stage 包 — 回复生成。

Exports:
    - ``BaseNLG``: NLG stage 抽象基类。
    - ``FSMNLG``: FSM 模块的回复生成。
    - ``RouteNLG``: 顶层路由模块的回复生成。
"""

from stages.nlg.nlg import BaseNLG, FSMNLG, RouteNLG

__all__ = ["BaseNLG", "FSMNLG", "RouteNLG"]
