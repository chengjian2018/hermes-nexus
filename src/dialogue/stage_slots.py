"""管线槽位 —— pre_recall / query / post_recall / generate 四槽位轴，
执行期三层延迟解析（node > module > pattern）+ 校验降级。

pattern.stages（或默认骨架）声明的是管线**形状**：
- 具体 stage：原样执行（作者显式选择，可组成无 NLU、unified 单次调用等
  任意形态；不做校验不替换）
- 槽位：执行到该位置时由 runner 调 resolve_stage 解析

generate 双形态与惰性子部件（核心设计）：
- 配置形态：单 stage（如 unified，一次调用自写 nlu_result/nlg_result）或
  dict {"nlu": s1, "nlg": s2}（恰含两键且值合法，缺一即整层非法）
- GenerateSlot 不在解析时绑定具体 stage，而是展开为结构：
    ROUTE             → [nlu部件, _RouteNodeAdvance, nlg部件]
    FSM+enable_clarify → [nlu部件, ClarifyStage,       nlg部件]
    FSM 默认           → [nlu部件,                     nlg部件]
  两个子部件在**各自执行时刻**独立做三层解析——ROUTE 下 nlg 部件在
  advance 切到菜单节点后解析，菜单节点级 nlg 当轮生效（时机修复）；
  FSM 下 ClarifyStage 先置 metadata["clarify"] 再跑 NLG，跳过守卫语义不变。
  single 形态的边界情形（双向闭合，靠 metadata["_generate_single_stage"]）：
  - root single + 菜单 dict：nlg 部件重新解析取菜单 dict，root 的 single
    已在 nlu 部件执行——nlg 部件正常执行菜单 nlg；
  - root builtin/其他 + 菜单 single：nlu 部件执行的是 root 解析结果
    （如 builtin dict 的 nlu 位），菜单 single 从未运行——nlg 部件解析到
    不同的 single stage 时补执行（警告 + 执行），防静默空回复。

降级规则（全部槽位统一）：某层配置非法 → 警告 + 降级下一层；三层全空/
全非法 → 召回/改写槽位 no-op（空列表），generate 落 builtin（按
module.type：FSMNLU/FSMNLG 或 RouteNLU/RouteNLG）。
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from config.config import get_llm_config
from src.dialogue.base import DialogueContext, PipelineStage
from src.dialogue.module import ModuleType

logger = logging.getLogger(__name__)

# generate single 形态守卫的 metadata 键：nlu 部件记录已执行的 single stage
# 对象，nlg 部件消费后即删（仅在同轮 generate 展开内存活，不跨轮泄漏）。
_GENERATE_SINGLE_STAGE_KEY = "_generate_single_stage"


# ============================================================================
# 槽位 sentinel
# ============================================================================

class StageSlot(PipelineStage):
    """槽位基类：占位 marker，不实现执行逻辑。

    直接 execute（未经 runner 解析）立即抛错，fail fast 防止骨架被
    误当具体管线裸跑（如 visualize / 自定义 runner）。
    """

    stage_name = "stage_slot"

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        raise NotImplementedError(
            f"{type(self).__name__} 是管线骨架槽位，必须经 "
            "resolve_stage() 解析为具体 stage 后执行"
        )


class PreRecallSlot(StageSlot):
    """预召回槽位：三层属性名 ``pre_recall``。"""

    stage_name = "pre_recall_slot"


class QuerySlot(StageSlot):
    """查询改写槽位：三层属性名 ``query``。"""

    stage_name = "query_slot"


class PostRecallSlot(StageSlot):
    """后召回槽位：三层属性名 ``post_recall``。"""

    stage_name = "post_recall_slot"


class GenerateSlot(StageSlot):
    """生成槽位：三层属性名 ``generate``，single/dict 双形态（见模块 docstring）。"""

    stage_name = "generate_slot"


# ============================================================================
# 校验与规整
# ============================================================================

def is_valid_stage(obj: Any) -> bool:
    """鸭子类型 stage 校验：有 callable 的 ``execute``。"""
    return (
        obj is not None
        and hasattr(obj, "execute")
        and callable(obj.execute)
    )


def normalize_generate(value: Any) -> Optional[Tuple[str, Any, Any]]:
    """把 ``generate`` 配置值规整为分类元组；非法返回 None。

    Returns:
        ("dict", nlu, nlg) —— dict 恰含 nlu/nlg 两键且值均合法
        ("single", stage, None) —— 单 stage 形态（unified 等）
        None —— 缺键/多键/值非法/类型错误
    """
    if isinstance(value, dict):
        if set(value.keys()) != {"nlu", "nlg"}:
            return None
        nlu, nlg = value["nlu"], value["nlg"]
        if is_valid_stage(nlu) and is_valid_stage(nlg):
            return ("dict", nlu, nlg)
        return None
    if is_valid_stage(value):
        return ("single", value, None)
    return None


# ============================================================================
# 三层配置读取（node > module > pattern）
# ============================================================================

def _layered_values(attr: str, ctx: DialogueContext, module: Any,
                    pattern: Any) -> List[Tuple[str, Any]]:
    """按 node > module > pattern 顺序收集已配置层 (层名, 原始值)。"""
    layers: List[Tuple[str, Any]] = []
    node = ctx.get_current_node()
    if node is not None:
        layers.append(("node", getattr(node, attr, None)))
    layers.append(("module", getattr(module, attr, None)))
    if pattern is not None:
        layers.append(("pattern", getattr(pattern, attr, None)))
    return [(name, v) for name, v in layers if v is not None]


def _resolve_generate(ctx: DialogueContext, module: Any,
                      pattern: Any) -> Tuple[str, Any, Any]:
    """generate 三层解析（含整层降级）；全空/全非法 → builtin 分类元组。"""
    for layer_name, value in _layered_values("generate", ctx, module, pattern):
        normalized = normalize_generate(value)
        if normalized is not None:
            return normalized
        logger.warning(
            "[stage_slots] %s 层 generate 配置非法（dict 须恰含 nlu/nlg 两键"
            "且值合法，或为带 execute 的单 stage），整层降级: %r",
            layer_name, value,
        )
    # builtin 兜底（按 module.type）
    if getattr(module, "type", None) == ModuleType.ROUTE:
        from src.dialogue.nlu import RouteNLU
        from src.dialogue.nlg import RouteNLG
        return ("dict", RouteNLU(), RouteNLG())
    from src.dialogue.nlu import FSMNLU
    from src.dialogue.nlg import FSMNLG
    return ("dict", FSMNLU(), FSMNLG())


# ============================================================================
# generate 惰性子部件
# ============================================================================

class _GenerateNLUPart(PipelineStage):
    """generate 的 nlu 部件：执行时刻三层解析，dict 取 nlu / single 整体执行。

    single 形态执行后把 stage 对象记入 metadata（供 nlg 部件比对，见
    _GenerateNLGPart 的守卫说明）。
    """

    stage_name = "generate_nlu_part"

    def __init__(self, module: Any, pattern: Any):
        self.module = module
        self.pattern = pattern

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        kind, nlu, _nlg = _resolve_generate(ctx, self.module, self.pattern)
        # single 形态时 nlu 位即整个 stage
        stage = nlu
        if kind == "single":
            ctx.metadata[_GENERATE_SINGLE_STAGE_KEY] = stage
        return stage.execute(ctx)


class _GenerateNLGPart(PipelineStage):
    """generate 的 nlg 部件：重新三层解析（此时节点可能已切到菜单）。

    dict 形态 → 执行 nlg；single 形态 → 与 nlu 部件记录的 single stage 比对：
    同一对象 → no-op（已在 nlu 部件执行，unified 自写 nlg_result）；
    不同对象 → 菜单节点级 single 在 root 轮从未运行（root 解析出的是
    builtin/其他层），补执行并警告，防静默空回复。比对后清除 metadata 键，
    避免跨轮残留旧对象。
    """

    stage_name = "generate_nlg_part"

    def __init__(self, module: Any, pattern: Any):
        self.module = module
        self.pattern = pattern

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        kind, nlu, nlg = _resolve_generate(ctx, self.module, self.pattern)
        if kind == "single":
            # single 形态时 nlu 位即整个 stage（nlg 位为 None）
            stage = nlu
            executed = ctx.metadata.pop(_GENERATE_SINGLE_STAGE_KEY, None)
            if executed is stage:
                return ctx
            logger.warning(
                "[stage_slots] nlg 部件解析到与 nlu 部件不同的 single stage "
                "（节点已切换且菜单层为 single 形态，原 nlu 位 stage 为 %r），"
                "在 nlg 部件补执行: %s",
                type(executed).__name__ if executed is not None else None,
                stage.stage_name,
            )
            return stage.execute(ctx)
        ctx.metadata.pop(_GENERATE_SINGLE_STAGE_KEY, None)
        return nlg.execute(ctx)


# ============================================================================
# ROUTE 菜单推进（自 chat.py 迁入；R4 刷新语义不变）
# ============================================================================

class _RouteNodeAdvance(PipelineStage):
    """ROUTE-only stage: advance to the intent-menu node selected by NLU.

    Runs between the generate nlu/nlg parts so the reply is generated from
    the selected menu node's config instead of the root's. The selection is
    validated against the route module's own node list; invalid selections
    keep the current node (root) unchanged.
    """

    stage_name = "route_advance"

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        module = (
            ctx.module_map.get(ctx.current_module_code)
            if ctx.current_module_code
            else None
        )
        if module is None or getattr(module, "type", None) != ModuleType.ROUTE:
            return ctx

        next_node_code = (ctx.nlu_result or {}).get("next_node", "")
        if next_node_code and any(
            n.node_code == next_node_code for n in module.module_nodes
        ):
            logger.info(
                "ROUTE 命中菜单节点: %s → %s",
                ctx.current_node_code,
                next_node_code,
            )
            ctx.current_node_code = next_node_code
            # R4：菜单节点 node 级 LLM 配置当轮生效（spec §4；pattern_code
            # 取 R1 写入的 metadata，ROUTE 每轮从 root 出发永不定居菜单节点）
            ctx.llm_config = get_llm_config(
                pattern_code=ctx.metadata.get("pattern_code", ""),
                module_code=ctx.current_module_code or "",
                node_code=next_node_code,
                override=ctx.metadata.get("llm_override"),
            )
        return ctx


# ============================================================================
# 默认澄清 stage 工厂（自 chat.py _default_clarify_stage 迁入）
# ============================================================================

def default_clarify_stage():
    """构建默认 ClarifyStage：内存关键词召回 + 默认门控。

    生产环境应在 module 上显式配置 clarify_stage（挂 ES 召回路径）；
    默认实例保证开箱可用。
    """
    from src.clarify import ClarifyRouteRule, ClarifyStage
    from src.dialogue.recaller import (
        KeywordRecallPath,
        MultiPathRecaller,
        ScoreThresholdFilter,
        WeightedScoreFusion,
    )

    return ClarifyStage(
        recaller=MultiPathRecaller(
            recall_paths=[],
            filters=[ScoreThresholdFilter(threshold=0.1)],
            fusion=WeightedScoreFusion(),
        ),
        rule=ClarifyRouteRule(),
    )


# ============================================================================
# 槽位解析入口
# ============================================================================

def resolve_stage(stage: Any, ctx: DialogueContext, module: Any,
                  pattern: Any = None) -> List[Any]:
    """返回待执行 stage 列表：槽位延迟解析，非槽位原样放行 ``[stage]``。

    - GenerateSlot → 结构列表（nlg 部件在节点切换后独立解析，见模块 docstring）
    - 召回/改写槽位 → 三层解析取第一个合法层 ``[stage]``；全空/全非法 → ``[]``
    """
    if not isinstance(stage, StageSlot):
        return [stage]

    if isinstance(stage, GenerateSlot):
        parts: List[Any] = [_GenerateNLUPart(module, pattern)]
        if getattr(module, "type", None) == ModuleType.ROUTE:
            parts.append(_RouteNodeAdvance())
        elif getattr(module, "enable_clarify", False):
            parts.append(getattr(module, "clarify_stage", None)
                         or default_clarify_stage())
        parts.append(_GenerateNLGPart(module, pattern))
        return parts

    attr = stage.stage_name.removesuffix("_slot")
    for layer_name, value in _layered_values(attr, ctx, module, pattern):
        if is_valid_stage(value):
            return [value]
        logger.warning(
            "[stage_slots] %s 层 %s 配置非法（stage 需有 callable execute），"
            "降级下一层: %r",
            layer_name, attr, value,
        )
    return []
