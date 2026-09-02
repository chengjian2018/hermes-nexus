"""NLG stage 包 — 回复生成。

平铺模块 ``src/dialogue/nlg.py`` 迁移为包；旧导入路径
``from src.dialogue.nlg import FSMNLG`` 继续可用。

Exports:
    - ``BaseNLG``: NLG stage 抽象基类。
    - ``FSMNLG``: FSM 模块的回复生成。
    - ``RouteNLG``: 顶层路由模块的回复生成。
"""

from src.dialogue.nlg.nlg import BaseNLG, FSMNLG, RouteNLG

__all__ = ["BaseNLG", "FSMNLG", "RouteNLG"]
