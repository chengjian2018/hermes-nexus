"""
模块处理器基类 — ModuleHandler 接口 + FSM/ROUTE 共享的管线执行骨架。

单模块单轮的处理按模块类型拆分到三个 handler（agent.py / fsm.py / route.py），
本模块提供：
- ModuleHandler     ：处理器接口（输入 session+module，输出 TurnResult）
- PipelineHandler   ：FSM/ROUTE 共享的「节点解析 → R3 刷新 → stage 执行」骨架
- default_skeleton  ：默认四槽位管线骨架

不变式：LLM 配置刷新函数经构造注入（refresh_llm），不自 import——
R1-R3 的 get_llm_config 必须经 chat 模块命名空间解析（测试 patch 语义）。
"""

import logging
from abc import ABC, abstractmethod
from typing import Callable

from src.chat.loop import TurnResult
from src.chat.session import Session
from src.dialogue.stage_slots import (
    GenerateSlot,
    PostRecallSlot,
    PreRecallSlot,
    QuerySlot,
    resolve_stage,
)

logger = logging.getLogger(__name__)


class ModuleHandler(ABC):
    """单模块单轮处理器接口。

    handle() 返回 TurnResult：reply 与 dispatch_event 互斥
    （dispatch_event 非空 = 本模块不出文本，由 chat 编排层跳到目标模块续答）。

    Args:
        refresh_llm: LLM 配置刷新函数（chat 层注入；R2/R3 经它解析
            get_llm_config，保持测试 patch 锚点在 chat 模块命名空间）
    """

    def __init__(self, refresh_llm: Callable[..., None]) -> None:
        self.refresh_llm = refresh_llm

    @abstractmethod
    def handle(self, session: Session, module,
               force_close: bool = False) -> TurnResult:
        """处理当前模块一轮。force_close 为 max_hops 耗尽时的强制收尾标记。"""
        ...


class PipelineHandler(ModuleHandler):
    """FSM/ROUTE 共享骨架：节点解析 → R3 配置刷新 → stage 顺序执行。

    差异部分（转移语义）由子类 handle() 的后段实现：
    - FsmHandler   ：next_node 跳转（fsm.py）
    - RouteHandler ：jump_module 静默分发 / root 重置（route.py）
    """

    def _resolve_entry_node(self, cxt, module) -> None:
        """确定当前节点（首次进入模块时取首节点），写入 cxt.current_node_code。

        节点解析（首次进入模块取首节点 + node_map 校验）。
        """
        if cxt.current_node_code is None:
            if module.module_nodes:
                first_node = module.module_nodes[0]
                cxt.current_node_code = first_node.node_code
                logger.info(
                    "首次进入模块 %s，使用首节点: %s",
                    module.module_code,
                    first_node.node_code,
                )
            else:
                raise ValueError(f"模块 '{module.module_code}' 无可用节点")

        cur_node = cxt.node_map.get(cxt.current_node_code)
        if cur_node is None:
            raise ValueError(
                f"节点 '{cxt.current_node_code}' 不存在于 node_map 中"
            )

    def _run_stages(self, cxt, module, pattern) -> None:
        """顺序执行管线 stages（槽位按 node > module > pattern 延迟解析）。

        顺序执行 stages（槽位延迟解析在 resolve_stage 内）。
        """
        stages = self._stages(pattern, module)

        logger.info(
            "Pipeline 开始: session=%s, module=%s, node=%s, stages=%s",
            cxt.session_id,
            module.module_code,
            cxt.current_node_code,
            [s.stage_name for s in stages],
        )

        # Execute each stage in order; slots resolve against the *current* node
        # at execution time
        for stage in stages:
            for concrete in resolve_stage(stage, cxt, module, pattern):
                try:
                    cxt = concrete.execute(cxt)
                    logger.debug("Stage '%s' 执行完成", concrete.stage_name)
                except Exception as e:
                    logger.error(
                        "Stage '%s' 执行异常: %s", concrete.stage_name, e,
                        exc_info=True
                    )
                    raise

    def _stages(self, pattern, module) -> list:
        """管线骨架：pattern 注册优先，缺省回落默认四槽位。"""
        return pattern.stages or default_skeleton(module)


def default_skeleton(module) -> list:
    """默认管线骨架（槽位延迟解析，不绑定节点）。

    [PreRecallSlot, QuerySlot, PostRecallSlot, GenerateSlot]；
    FSM/ROUTE 的差异（advance / clarify 插入）由 GenerateSlot 展开处理
    （stage_slots.resolve_stage），骨架本身全模块类型同形。
    """
    return [PreRecallSlot(), QuerySlot(), PostRecallSlot(), GenerateSlot()]
