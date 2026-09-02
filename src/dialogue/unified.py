"""
Unified stage —— 单次调用 + structured output：一次 LLM 调用同时产出
意图/槽位（NLU）与回复话术（NLG）。

与默认 NLU → NLG 两阶段的差异：
- 每轮只发起一次串行 LLM 调用（延迟/成本约减半）；
- 决策（next_node/slots）与话术（reply）出自同一次推理，天然自洽；
- next_node 由代码按「当前节点合法转移边」做硬校验，非法取值回落为
  保持当前节点（确定性 guard，不依赖 prompt 约束）。

输出协议（单次调用返回的 JSON）：
    {"reply": "给用户的回复话术", "next_node": "xx", "slots": {"slot1": ""}}

阶段产物拆写：
- ctx.nlu_result = {"next_node", "slots"}      —— 下游节点跳转零改动
- ctx.nlg_result = {"content": reply}          —— 下游回复提取零改动
- ctx.metadata["unified"] = 观测信息（invalid_next_node / parse_failed 等）

接入方式（module 级 generate 注入，经默认骨架 GenerateSlot 解析命中）：
    FSMModule(generate=FSMUnifiedNLU())
    RouteModule(generate=RouteUnifiedNLU())
（node 级 generate 优先于 module 级，见 stage_slots.py；PassThroughNLG
保留为独立工具类，generate 单 stage 形态下不再需要占位 NLG）

与双轨澄清组合（enable_clarify=True 的 FSM 模块）：
    管线装配为 [统一阶段, ClarifyStage, PassThroughNLG]。
    统一阶段按模板中的偏题特例输出 next_node="clarify"（合法集放行），
    ClarifyStage 覆写 nlg_result 生成澄清回复，PassThroughNLG 放行；
    澄清轮共 2 次 LLM 调用（统一 + 澄清生成），与两阶段+澄清持平，
    正常轮仍为 1 次。

ROUTE 模块下统一阶段生成的回复已依据所选菜单节点的回答范式，
管线中随后的 route_advance / jump_module 分发逻辑不受影响。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from src.dialogue.base import (
    DialogueContext,
    PipelineStage,
    fill_prompt_template,
)
from src.dialogue.nlu.nlu import BaseNLU
from src.prompt import (
    FSM_UNIFIED_DEFAULT_PROMPT,
    ROUTE_UNIFIED_DEFAULT_PROMPT,
)

logger = logging.getLogger(__name__)


class _UnifiedBaseNLU(BaseNLU):
    """统一阶段基类：一次调用完成理解与生成，拆写 nlu_result / nlg_result。

    子类只需提供默认模板与日志文案；解析容错与失败重试复用
    ``BaseNLU._execute_with_retry``（重试 prompt 由本类覆写为三字段协议）。
    """

    # 解析重试耗尽后的兜底话术：保证本轮有回复且保持当前节点
    fallback_reply = "抱歉，我没能理解您的意思，请您换个说法再告诉我一次。"

    def __init__(
        self,
        response_format: Optional[Dict[str, Any]] = None,
        fallback_reply: Optional[str] = None,
    ):
        """
        Args:
            response_format: 可选的 API 级结构化约束（如 ``{"type": "json_object"}``），
                经 provider ``**kwargs`` 透传进请求 payload。默认 None —— 仅靠
                prompt 协议约束输出格式，跨 provider 通用；服务端支持时建议开启。
            fallback_reply: 解析重试耗尽后的兜底回复，默认使用类属性文案。
        """
        self.response_format = response_format
        if fallback_reply is not None:
            self.fallback_reply = fallback_reply

    # ------------------------------------------------------------------
    # LLM 调用：在 BaseNLU 基础上按需透传 response_format
    # ------------------------------------------------------------------

    def _call_llm(
        self, prompt: str, llm_config: Optional[Dict[str, Any]] = None
    ) -> str:
        """Call the LLM (single call per turn) and return the raw response text."""
        if llm_config is None:
            from config.config import get_llm_config
            llm_config = get_llm_config()

        from src.llm.resolve import build_provider

        provider = build_provider(llm_config)
        extra_kwargs: Dict[str, Any] = {}
        if self.response_format is not None:
            extra_kwargs["response_format"] = self.response_format

        result = provider.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            model=llm_config["model"],
            temperature=llm_config.get("temperature", 0.7),
            max_tokens=llm_config.get("max_tokens", 2048),
            **extra_kwargs,
        )

        content = result.get("content", "")
        logger.debug("Unified LLM 返回: %s", content[:200])
        return content

    # ------------------------------------------------------------------
    # 候选节点与合法转移边
    # ------------------------------------------------------------------

    def _candidate_node_codes(self, cxt: DialogueContext) -> List[str]:
        """当前节点的合法转移目标（sub_nodes）；无当前节点时为空。"""
        node = cxt.get_current_node()
        return list(node.sub_nodes) if node is not None else []

    def _valid_next_values(self, cxt: DialogueContext) -> set:
        """next_node 的合法取值集合：候选节点编码 + 空串（保持当前节点）。

        模块开启双轨澄清（enable_clarify=True）时额外放行 "clarify"：
        触发后由 ClarifyStage 覆写 nlg_result 生成澄清回复，
        节点跳转守卫按 metadata["clarify"] 跳过，不会真的跳到不存在的节点。
        """
        valid = set(self._candidate_node_codes(cxt)) | {""}
        module = cxt.get_current_module()
        if module is not None and getattr(module, "enable_clarify", False):
            valid.add("clarify")
        return valid

    def _format_valid_values(self, cxt: DialogueContext) -> str:
        """合法取值列表的 prompt 文本（嵌入模板的 next_node 合法取值段）。"""
        return json.dumps(sorted(self._valid_next_values(cxt)), ensure_ascii=False)

    # ------------------------------------------------------------------
    # 候选节点回答范式 —— 统一阶段需要"目的地的话术风格"才能一次生成
    # ------------------------------------------------------------------

    def _format_next_node_pattern(self, cxt: DialogueContext) -> str:
        """候选后续节点（含回答范式）的 prompt 文本。

        相比两阶段 NLU 的 next_node 槽位（仅编码+名称+描述），这里额外带出
        每个候选节点的槽位定义与回答范式：模型选节点与写回复在同一次推理内完成，
        回复风格即所选节点的回答范式。
        """
        parts: List[str] = []
        for code in self._candidate_node_codes(cxt):
            sub_node = cxt.node_map.get(code)
            if sub_node is None:
                parts.append(f"- 节点编码: {code}")
                continue

            seg = [f"- 节点编码: {code}"]
            if sub_node.node_name:
                seg.append(f"  节点名称: {sub_node.node_name}")
            if sub_node.node_description:
                seg.append(f"  节点描述: {sub_node.node_description}")
            if sub_node.node_slots:
                seg.append(
                    "  槽位定义: "
                    + json.dumps(sub_node.node_slots, ensure_ascii=False)
                )
            if sub_node.answer_examples:
                for example in sub_node.answer_examples:
                    seg.append(f"  回答范式: {example}")
            parts.append("\n".join(seg))

        if not parts:
            return "暂无候选后续节点（当前为终节点，next_node 输出空字符串）"
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Prompt 装配（复用 BaseNLU 模板优先级 node > module > default）
    # ------------------------------------------------------------------

    def _build_template_kwargs(self, cxt: DialogueContext) -> Dict[str, str]:
        """统一阶段的槽位词表：在 NLU 词表基础上增加回答范式与合法取值。"""
        return {
            "cur_node": cxt.format_cur_node(stage="nlu"),
            "cur_answer_pattern": cxt.format_answer_pattern(),
            "next_node_pattern": self._format_next_node_pattern(cxt),
            "valid_next_values": self._format_valid_values(cxt),
            "query": cxt.user_query,
            "query_rewrite": cxt.format_rewritten_queries(),
            "recall_info": cxt.format_recall_info(),
            "filled_slots": cxt.format_slots(),
            "history": cxt.format_history(),
            "task_info": cxt.format_task_info(),
        }

    def _build_retry_prompt(self, original_prompt: str, failed_output: str) -> str:
        """解析失败重试 prompt：修正为 reply/next_node/slots 三字段协议。"""
        return (
            "## 原始任务\n"
            f"{original_prompt}\n\n"
            "## 上一次输出（格式不符合 JSON 规范，请修正）\n"
            f"{failed_output}\n\n"
            "## 修正要求\n"
            "请严格按照以下 JSON 格式重新输出，不要包含任何额外内容：\n\n"
            '{"reply": "给用户的回复话术", "next_node": "xx", "slots": {...}}\n\n'
            "注意：\n"
            "1. reply 是面向用户的自然语言回复\n"
            "2. next_node 必须在候选后续节点中存在（无法推进时输出空字符串）\n"
            "3. slots 按照给定节点的 slots 模版进行抽取\n"
            "4. 只输出 JSON 对象，不要包裹 markdown 代码块或其他文字"
        )

    # ------------------------------------------------------------------
    # 单次调用主逻辑：解析 → 硬校验 → 拆写 nlu_result / nlg_result
    # ------------------------------------------------------------------

    def _execute_unified(self, ctx: DialogueContext) -> None:
        """一次调用并拆写产物；任何失败都降级为兜底回复，不向上抛异常。"""
        prompt = self.prompt_build(ctx)
        parsed = self._execute_with_retry(prompt, ctx.llm_config)

        unified_meta: Dict[str, Any] = {"triggered": True}

        if "raw" in parsed:
            # 解析重试耗尽：保持当前节点 + 兜底话术
            logger.warning(
                "统一阶段解析失败（含重试），使用兜底回复: session=%s",
                ctx.session_id,
            )
            unified_meta["parse_failed"] = True
            ctx.nlu_result = {"next_node": "", "slots": {}}
            ctx.nlg_result = {"content": self.fallback_reply, "fallback": True}
        else:
            next_node = str(parsed.get("next_node", "") or "").strip()
            valid_values = self._valid_next_values(ctx)

            if next_node not in valid_values:
                # 硬 guard：非法转移边 → 保持当前节点。
                # 被拒的若为 clarify 信号（模块未开双轨澄清），模型的 reply 多为
                # "帮您确认一下"类承接承诺，而后续没有澄清环节兑现 —— 回复一并替换为兜底。
                logger.warning(
                    "统一阶段 next_node '%s' 不在合法转移边 %s 中，保持当前节点: %s",
                    next_node,
                    sorted(valid_values),
                    ctx.current_node_code,
                )
                unified_meta["invalid_next_node"] = next_node
                next_node = ""

            reply = str(parsed.get("reply", "") or "").strip() or self.fallback_reply
            if unified_meta.get("invalid_next_node") == "clarify":
                reply = self.fallback_reply
            ctx.nlu_result = {
                "next_node": next_node,
                "slots": parsed.get("slots", {}) or {},
            }
            ctx.nlg_result = {"content": reply}

        unified_meta["reply"] = ctx.nlg_result["content"]
        ctx.metadata["unified"] = unified_meta


# ============================================================================
# FSM 模块统一阶段
# ============================================================================

class FSMUnifiedNLU(_UnifiedBaseNLU):
    """FSM 模块统一阶段：一次结构化调用完成意图/槽位抽取与回复生成。

    配套 ``PassThroughNLG`` 注入 module.nlg_stage 后，FSM 管线由
    [NLU, NLG] 两次 LLM 调用变为 [统一阶段, 占位 NLG] 单次调用；
    节点跳转与槽位合并逻辑（_handle_node_transition）零改动。
    """

    stage_name = "fsm_unified"

    def _default_prompt_template(self) -> str:
        return FSM_UNIFIED_DEFAULT_PROMPT

    def prompt_build(self, cxt: DialogueContext) -> str:
        prompt_template = self._resolve_prompt_template(cxt)
        kwargs = self._build_template_kwargs(cxt)
        return self._fill_template(prompt_template, kwargs)

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        self._execute_unified(ctx)
        logger.info(
            "FSM 统一阶段完成: session=%s, next_node=%s, reply_len=%d",
            ctx.session_id,
            ctx.nlu_result.get("next_node", ""),
            len(ctx.nlg_result.get("content", "")),
        )
        return ctx


# ============================================================================
# ROUTE 模块统一阶段
# ============================================================================

class RouteUnifiedNLU(_UnifiedBaseNLU):
    """ROUTE 模块统一阶段：一次结构化调用完成意图分类与菜单节点回复生成。

    候选为路由根节点的菜单节点（含各自回答范式）；管线随后的
    route_advance 会把当前节点切到所选菜单，jump_module 分发不受影响。
    """

    stage_name = "route_unified"

    def _default_prompt_template(self) -> str:
        return ROUTE_UNIFIED_DEFAULT_PROMPT

    def prompt_build(self, cxt: DialogueContext) -> str:
        prompt_template = self._resolve_prompt_template(cxt)
        kwargs = self._build_template_kwargs(cxt)
        return self._fill_template(prompt_template, kwargs)

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        self._execute_unified(ctx)
        logger.info(
            "Route 统一阶段完成: session=%s, next_node=%s, reply_len=%d",
            ctx.session_id,
            ctx.nlu_result.get("next_node", ""),
            len(ctx.nlg_result.get("content", "")),
        )
        return ctx


# ============================================================================
# 占位 NLG —— 与统一阶段配对
# ============================================================================

class PassThroughNLG(PipelineStage):
    """占位 NLG 阶段：保留统一阶段已写入的 nlg_result，跳过二次生成。

    用法：``module.nlg_stage = PassThroughNLG()``，与统一阶段
    （FSMUnifiedNLU / RouteUnifiedNLU）配对，替换默认 NLG 的第二次 LLM 调用。

    澄清轮等其他先行阶段覆写 nlg_result 时同样直接放行；
    若管线中没有阶段先行生成 nlg_result（装配错误），本轮回复为空并告警。
    """

    stage_name = "nlg_pass_through"

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        if ctx.nlg_result is None:
            logger.warning(
                "PassThroughNLG 未检测到已生成的 nlg_result，"
                "请确认统一阶段已配置为 module 的 nlu_stage: session=%s",
                ctx.session_id,
            )
            ctx.nlg_result = {"content": ""}
        else:
            logger.debug(
                "PassThroughNLG 跳过生成（沿用已写入回复）: session=%s",
                ctx.session_id,
            )
        return ctx
