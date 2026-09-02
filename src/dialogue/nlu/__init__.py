"""NLU stage 包 — 意图识别与槽位抽取。

平铺模块 ``src/dialogue/nlu.py`` 迁移为包；旧导入路径
``from src.dialogue.nlu import FSMNLU`` 继续可用。

Exports:
    - ``BaseNLU``: NLU stage 抽象基类。
    - ``FSMNLU``: FSM 模块的意图识别与状态转移。
    - ``RouteNLU``: 顶层路由模块的意图分类与分发。
"""

from src.dialogue.nlu.nlu import BaseNLU, FSMNLU, RouteNLU

__all__ = ["BaseNLU", "FSMNLU", "RouteNLU"]
