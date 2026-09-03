"""
闲鱼卖家客服 Route 模式 —— 复刻 tmp_xianyu.XianyuReplyBot 的 agent 对话管理。

复刻来源：src/dialogue/tmp_xianyu.py（XianyuReplyBot / IntentRouter / 三领域 Agent），
prompt 模板取自 src/prompt.py 的 XIANYU_* 系列。

Pattern 结构（对齐 car_sales_route 的 ROUTE 模式）：

    xianyu_agent (Pattern, entry: xianyu_root, query=TimeAugQueryRewriter)
    └── xianyu_root (RouteModule)   全部节点留在路由模块，无 jump_module
        ├── xy_route_root      路由根节点（sub_nodes = 意图菜单）
        ├── xy_menu_price      议价菜单（议价次数未达上限）
        ├── xy_menu_price_refuse  议价拒绝菜单（达上限 → 固定话术，零 LLM）
        ├── xy_menu_tech       技术问答菜单
        └── xy_menu_default    通用客服菜单

对话管理映射（tmp_xianyu → 本框架）：
    IntentRouter 三级路由（tech 关键词/正则优先 → price 关键词/正则 → LLM 兜底）
      → XianyuIntentNLU：detect_intent 本地规则层（原关键词表并入 tmp_xianyu
        的词表与正则）+ XIANYU_NLU_PROMPT LLM 兜底（输出四类
        price/tech/no_reply/default，非法输出回落 default）
    ClassifyAgent 判为 no_reply（提示词爆破/与商品售卖无关）
      → FixedNLG 输出空回复；channel 契约 reply 为空 = 不发送
        （原实现返回 "-" 由外挂方自行过滤）
    PriceAgent 动态温度 min(0.3 + 0.15×议价轮次, 0.9)、TechAgent 0.4、
    DefaultAgent 0.7、max_tokens 500
      → FixedNLG._tuned_llm_config 按意图改写 llm_config 副本
    PriceAgent 注入 ▲当前议价轮次
      → NLU 把议价参数写入 filled_slots，NLG prompt 追加【议价设置】块
    _safe_filter 违禁词过滤（微信/QQ/支付宝/银行卡/线下）
      → FixedNLG._safe_filter
    _extract_bargain_count（从 system 消息回溯议价次数）
      → NLU 按用户消息 metadata.intent=price 计数（框架侧无 system 议价消息）
    议价轮数达上限 → 固定拒绝话术、零 LLM（tmp_xianyu 无此机制，以升温策略
    柔性守住底线；本 pattern 保留显式阈值拒绝，议价行为可预期、可测试）
      → xy_menu_price_refuse + answer_examples 文案 marker 短路

已知的刻意简化：
    - TechAgent 的 enable_search（DashScope extra_body）与 top_p=0.8 不透传：
      框架 BaseNLG._call_llm 签名固定，温度/长度经 llm_config 副本调优

消息入口：src/channel/xianyu.py（默认回复 API 外挂决策口），
设 XIANYU_CHANNEL_PATTERN=xianyu_agent 即接入。

注册方式：模块顶层 ``registry.register(Pattern(...))``，AST 扫描自动发现。
"""

import logging
import re

from src.dialogue.nlg import BaseNLG
from src.dialogue.nlu import BaseNLU
from src.dialogue.module import RouteModule
from src.dialogue.node import BaseNode
from src.dialogue.pattern import Pattern
from src.dialogue.query import TimeAugQueryRewriter
from src.dialogue.register import registry
from src.prompt import (
    XIANYU_DEFAULT_NLG_PROMPT,
    XIANYU_NLU_PROMPT,
    XIANYU_PRICE_NLG_PROMPT,
    XIANYU_TECH_NLG_PROMPT,
)

logger = logging.getLogger(__name__)

# ============================================================================
# 本地意图检测 —— 复刻 IntentRouter 的规则层（技术优先）
# ============================================================================

# 技术相关关键词（原关键词表 + tmp_xianyu IntentRouter tech 词表）
TECH_KEYWORDS = [
    "怎么用", "参数", "坏了", "故障", "设置", "说明书",
    "功能", "用法", "教程", "驱动",
    "规格", "型号", "连接", "对比",
]

# 价格相关关键词（原关键词表，含闲鱼语境的"刀""包个邮"等 +
# tmp_xianyu IntentRouter price 词表）
PRICE_KEYWORDS = [
    "便宜", "优惠", "刀", "降价", "价格", "多少钱",
    "能少", "还能", "最低", "底价", "实诚价", "到100", "能到",
    "包个邮", "砍价", "价",
]

# tmp_xianyu IntentRouter 正则层（在清洗后的文本上匹配）
TECH_PATTERNS = [r"和.+比"]
PRICE_PATTERNS = [r"\d+元", r"能少\d+"]

# 意图 → 菜单节点编码（refuse 由议价轮数控制另行改判；
# no_reply 落 default 菜单，由 NLG 按 intent 短路为空回复）
INTENT_TO_MENU = {
    "price": "xy_menu_price",
    "tech": "xy_menu_tech",
    "default": "xy_menu_default",
}

# 议价默认设置（复刻 _get_default_settings；账号级配置经
# ctx.metadata["bargain_settings"] 注入，未注入时用此默认）
DEFAULT_BARGAIN_SETTINGS = {
    "max_bargain_rounds": 3,
    "max_discount_percent": 10,
    "max_discount_amount": 100,
}


def detect_intent(message: str) -> str:
    """本地规则意图检测 —— 复刻 IntentRouter.detect 的关键词/正则两级。

    纯本地、零 LLM（LLM 兜底层在 XianyuIntentNLU，需 ctx.llm_config）。
    技术优先：金额与技术词并存时先归 tech（与 XIANYU_NLU_PROMPT 的
    分类标准一致）。

    Args:
        message: 买家消息

    Returns:
        意图: price / tech / default（default 表示本地未命中，交上层兜底）
    """
    # 复刻 IntentRouter：过滤表情/标点后匹配（\w 保留字母数字与下划线）
    text_clean = re.sub(r"[^\w一-龥]", "", message.lower())

    if any(kw in text_clean for kw in TECH_KEYWORDS):
        return "tech"
    if any(re.search(p, text_clean) for p in TECH_PATTERNS):
        return "tech"
    if any(kw in text_clean for kw in PRICE_KEYWORDS):
        return "price"
    if any(re.search(p, text_clean) for p in PRICE_PATTERNS):
        return "price"
    return "default"


def _effective_query(cxt) -> str:
    """取改写后的买家消息：query 槽位（TimeAugQueryRewriter）已在本轮
    generate 之前执行，rewritten_queries[0] 即时间增强结果；槽位 no-op
    或未配置时回落原 query。"""
    return (cxt.rewritten_queries or [cxt.user_query])[0]


def _get_bargain_settings(cxt) -> dict:
    """取议价设置：metadata 注入优先，缺省回 DEFAULT_BARGAIN_SETTINGS。"""
    settings = dict(DEFAULT_BARGAIN_SETTINGS)
    injected = cxt.metadata.get("bargain_settings") or {}
    settings.update({k: v for k, v in injected.items() if v is not None})
    return settings


def _count_bargain_rounds(cxt) -> int:
    """统计当前会话已发生的议价轮数。

    复刻原实现"统计该 chat 历史中 intent=price 的 user 消息数"：
    每轮 NLU 把 intent 写进该轮 user 消息的 metadata，此处按 metadata
    回溯计数（当前轮的 user 消息已入库，计入本数）。
    """
    count = 0
    for msg in cxt.history:
        if msg.role != "user":
            continue
        if (msg.metadata or {}).get("intent") == "price":
            count += 1
    return count


# ============================================================================
# NLU stage —— 本地规则分类 + LLM 兜底（ClassifyAgent）
# ============================================================================

class XianyuIntentNLU(BaseNLU):
    """闲鱼意图分类 stage —— 复刻 IntentRouter 规则层 + ClassifyAgent 兜底。

    契约与框架 NLU 一致（execute(ctx) -> ctx，写 ctx.nlu_result），
    挂在 RouteModule 模块级 generate dict 的 nlu 位（node 无覆盖时生效）。

    路由层级（复刻 IntentRouter.detect 的三级策略，技术优先）：
        1. 本地 tech 关键词/正则（detect_intent，零 LLM）
        2. 本地 price 关键词/正则（detect_intent，零 LLM）
        3. LLM 兜底（XIANYU_NLU_PROMPT，四类 price/tech/no_reply/default；
           调用失败或输出非法标签时回落 default）

    nlu_result 结构（对齐框架 NLU 契约）：
        {"next_node": <菜单节点编码>, "slots": {...}, "intent": <原始意图>}

    slots 复刻 generate_reply 的议价参数注入：
        bargain_count / max_bargain_rounds / max_discount_percent /
        max_discount_amount —— 经 filled_slots 供 NLG prompt 带入
    """

    stage_name = "xianyu_intent_nlu"

    # LLM 输出的合法标签（no_reply 最特定，先匹配）
    _VALID_INTENTS = ("no_reply", "price", "tech", "default")

    def _default_prompt_template(self) -> str:
        return XIANYU_NLU_PROMPT

    def prompt_build(self, cxt) -> str:
        """构建 LLM 兜底分类 prompt。

        XIANYU_NLU_PROMPT 只留了 {__task_info__}/{__history__} 两个槽位
        （BaseNLU 的 kwargs 词表不含 task_info，此处自行组装）；买家当前
        消息复刻 ClassifyAgent._build_messages 单列一段，不依赖 history 末行。
        """
        slots = {
            "task_info": cxt.format_task_info(),
            "history": cxt.format_history(),
        }
        prompt = self._fill_template(self._default_prompt_template(), slots)
        prompt += "\n### 买家消息\n" + _effective_query(cxt)
        return prompt

    def execute(self, ctx):
        # 1-2. 本地规则层（技术优先，零 LLM）
        intent = detect_intent(_effective_query(ctx))

        # 3. LLM 兜底：本地未命中（default）时走 ClassifyAgent
        if intent == "default":
            intent = self._classify_via_llm(ctx)

        # 当前轮 user 消息回填 intent（消息已入 history：chat() 先
        # add_message 再跑管线）——议价计数按此回溯
        for msg in reversed(ctx.history):
            if msg.role == "user":
                msg.metadata["intent"] = intent
                break

        next_node = INTENT_TO_MENU.get(intent, "xy_menu_default")
        settings = _get_bargain_settings(ctx)

        bargain_count = 0
        if intent == "price":
            # 复刻议价轮数控制：count 含当前轮，>= max_bargain_rounds
            # 即拒绝（第 max 次砍价收到固定拒绝话术）
            bargain_count = _count_bargain_rounds(ctx)
            if bargain_count >= settings["max_bargain_rounds"]:
                next_node = "xy_menu_price_refuse"

        # 议价参数随槽位合并进 filled_slots，供 NLG prompt 注入。
        # 注意 ROUTE 路径框架在 stages 之后才做 slots → filled_slots 合并，
        # 而 NLG 在 stages 内执行 —— 故此处同时直写 filled_slots（相同键，
        # 框架后续合并幂等），保证 NLG 当轮可见
        bargain_slots = {
            "bargain_count": bargain_count,
            "max_bargain_rounds": settings["max_bargain_rounds"],
            "max_discount_percent": settings["max_discount_percent"],
            "max_discount_amount": settings["max_discount_amount"],
        }
        ctx.filled_slots.update(bargain_slots)
        ctx.nlu_result = {
            "next_node": next_node,
            "intent": intent,
            "slots": bargain_slots,
        }
        return ctx

    def _classify_via_llm(self, ctx) -> str:
        """LLM 意图兜底 —— 复刻 ClassifyAgent（含 no_reply 反爆破类目）。"""
        try:
            raw = self._call_llm(self.prompt_build(ctx), ctx.llm_config)
        except Exception as e:
            logger.warning("LLM 意图兜底失败，回落 default: %s", e)
            return "default"
        return self._sanitize_intent(raw)

    @classmethod
    def _sanitize_intent(cls, raw: str) -> str:
        """清洗 LLM 分类输出：仅认四类标签，非法输出回落 default。"""
        text = (raw or "").strip().lower()
        for label in cls._VALID_INTENTS:
            if label in text:
                return label
        return "default"


# ============================================================================
# NLG stage —— no_reply 空回复 / 议价拒绝固定话术 / 意图级 prompt 生成
# ============================================================================

class FixedNLG(BaseNLG):
    """闲鱼 NLG stage —— 复刻三领域 Agent 的回复生成 + 安全过滤。

    三条路径（前两条零 LLM 短路）：
        1. intent=no_reply  → 空回复（channel 契约 reply 空 = 不发送；
           复刻原实现返回 "-"）
        2. 议价拒绝节点     → 固定拒绝话术（answer_examples 带 marker 文案）
        3. 意图菜单节点     → 节点 base_nlg_prompt（XIANYU_*_NLG_PROMPT）
           + 议价上下文（price 意图）+ 买家消息 → 单次 LLM + 违禁词过滤

    挂在模块级 generate dict 的 nlg 位（node 无覆盖时生效；_RouteNodeAdvance
    已先切好节点，本 stage 读到的当前节点即命中菜单）。节点级 generate 的
    nlg 优先于本 stage（node > module，stage_slots.py 三层解析）。
    """

    stage_name = "fixed_nlg"

    # 议价拒绝固定文案（复刻 ai_reply_engine 硬编码）
    REFUSE_TEXT = "抱歉，这个价格已经是最优惠的了，不能再便宜了哦！"
    # 命中标记：NLG 按当前节点 answer_examples 匹配到该文案即短路
    _MARKER = REFUSE_TEXT

    # 复刻 XianyuReplyBot._safe_filter 的违禁词表与替换文案
    BLOCKED_PHRASES = ("微信", "QQ", "支付宝", "银行卡", "线下")
    SAFE_REMINDER = "[安全提醒]请通过平台沟通"

    def _default_prompt_template(self) -> str:
        return XIANYU_DEFAULT_NLG_PROMPT

    def prompt_build(self, cxt) -> str:
        """构建意图级 NLG prompt。

        模板只留了 {__task_info__}/{__history__} 槽位；议价上下文与买家
        消息复刻 tmp_xianyu 的拼接方式（system 尾部追加议价轮次、
        user 消息单列）在此追加。
        """
        template = self._resolve_prompt_template(cxt)
        kwargs = self._build_template_kwargs(cxt)
        prompt = self._fill_template(template, kwargs)

        if (cxt.nlu_result or {}).get("intent") == "price":
            prompt += self._bargain_block(cxt)

        # 复刻 BaseAgent._build_messages：买家当前消息单列（user 角色）
        prompt += "\n### 买家消息\n" + _effective_query(cxt)
        return prompt

    def execute(self, ctx):
        intent = (ctx.nlu_result or {}).get("intent")

        # 1. no_reply：空回复，channel 不发送（零 LLM）
        if intent == "no_reply":
            ctx.nlg_result = {"content": ""}
            return ctx

        # 2. 议价拒绝节点：固定话术（零 LLM）
        node = ctx.get_current_node()
        if node is not None and any(
            self._MARKER in (ex or "") for ex in (node.answer_examples or [])
        ):
            ctx.nlg_result = {"content": self.REFUSE_TEXT}
            return ctx

        # 3. 意图菜单节点：单次 LLM 生成 + 违禁词过滤
        prompt = self.prompt_build(ctx)
        raw = self._call_llm(prompt, self._tuned_llm_config(ctx))
        ctx.nlg_result = {"content": self._safe_filter(raw.strip())}
        return ctx

    # ------------------------------------------------------------------
    # tmp_xianyu 各 Agent 生成策略的等价实现
    # ------------------------------------------------------------------

    def _bargain_block(self, cxt) -> str:
        """议价上下文块 —— 复刻 PriceAgent 的 ▲当前议价轮次注入。"""
        slots = cxt.filled_slots
        defaults = DEFAULT_BARGAIN_SETTINGS
        count = slots.get("bargain_count", 0)
        return (
            "\n【议价设置】\n"
            f"bargain_count: {count}\n"
            f"max_bargain_rounds: {slots.get('max_bargain_rounds', defaults['max_bargain_rounds'])}\n"
            f"max_discount_percent: {slots.get('max_discount_percent', defaults['max_discount_percent'])}\n"
            f"max_discount_amount: {slots.get('max_discount_amount', defaults['max_discount_amount'])}\n"
            f"▲当前议价轮次：{count}"
        )

    def _tuned_llm_config(self, ctx):
        """按意图调温 —— 复刻三 Agent 温度策略。

        PriceAgent 动态温度 min(0.3 + 0.15×议价轮次, 0.9)、TechAgent 0.4、
        DefaultAgent 0.7，max_tokens 统一 500。改写 llm_config 副本而非
        覆写 _call_llm（后者签名固定，且测试 spy 挂在 BaseNLG._call_llm）。
        """
        cfg = ctx.llm_config
        if not cfg:
            return cfg
        tuned = dict(cfg)
        intent = (ctx.nlu_result or {}).get("intent")
        if intent == "price":
            count = int(ctx.filled_slots.get("bargain_count", 0) or 0)
            tuned["temperature"] = min(0.3 + count * 0.15, 0.9)
        elif intent == "tech":
            tuned["temperature"] = 0.4
        else:
            tuned["temperature"] = 0.7
        tuned["max_tokens"] = 500
        return tuned

    @classmethod
    def _safe_filter(cls, text: str) -> str:
        """安全过滤 —— 复刻 XianyuReplyBot._safe_filter。"""
        if any(p in text for p in cls.BLOCKED_PHRASES):
            return cls.SAFE_REMINDER
        return text


# ============================================================================
# RouteModule —— 顶层路由：根节点 + 意图菜单（全部留在本模块）
# ============================================================================

xy_route_root = BaseNode(
    node_code="xy_route_root",
    node_name="闲鱼路由根节点",
    node_description="闲鱼卖家客服总入口，覆盖议价、技术问答与通用咨询三大场景",
    node_todo_description="识别买家消息意图（本地规则 + LLM 兜底），分发到议价/技术/通用菜单节点",
    sub_nodes=["xy_menu_price", "xy_menu_price_refuse", "xy_menu_tech", "xy_menu_default"],
    answer_examples=[
        "您好，在的。关于商品的问题都可以问我哦。",
    ],
)

xy_menu_price = BaseNode(
    node_code="xy_menu_price",
    node_name="议价",
    node_description="买家在砍价/询问优惠，需按议价策略让利但守住底线",
    node_todo_description="命中议价意图（未达轮数上限），生成阶梯让利回复",
    sub_nodes=[],
    base_nlg_prompt=XIANYU_PRICE_NLG_PROMPT,
    answer_examples=[
        "亲，价格已经很实惠啦，可以包邮哦。",
    ],
)

xy_menu_price_refuse = BaseNode(
    node_code="xy_menu_price_refuse",
    node_name="议价拒绝",
    node_description="议价轮数已达上限，礼貌坚持底价",
    node_todo_description="命中议价意图且轮数达上限，输出固定拒绝话术",
    sub_nodes=[],
    # 固定文案即 FixedNLG 的命中标记（marker 匹配则零 LLM 短路）
    answer_examples=[
        "抱歉，这个价格已经是最优惠的了，不能再便宜了哦！",
    ],
)

xy_menu_tech = BaseNode(
    node_code="xy_menu_tech",
    node_name="技术问答",
    node_description="买家咨询商品功能、用法、参数、故障等技术问题",
    node_todo_description="命中技术意图，基于商品信息简短作答",
    sub_nodes=[],
    base_nlg_prompt=XIANYU_TECH_NLG_PROMPT,
    answer_examples=[
        "支持蓝牙连接，说明书里有详细教程。",
    ],
)

xy_menu_default = BaseNode(
    node_code="xy_menu_default",
    node_name="通用客服",
    node_description="商品介绍、物流、售后等常规咨询",
    node_todo_description="未命中议价/技术关键词，按通用客服作答",
    sub_nodes=[],
    base_nlg_prompt=XIANYU_DEFAULT_NLG_PROMPT,
    answer_examples=[
        "亲，现货的，拍下后 48 小时内发货。",
    ],
)

xianyu_root = RouteModule(
    module_code="xianyu_root",
    module_name="闲鱼卖家客服总路由",
    module_description="复刻 tmp_xianyu.XianyuReplyBot 的意图路由与回复生成：本地规则 + LLM 兜底意图分类、意图级 prompt、议价轮数控制与动态温度",
    module_todo_description="对每条买家消息做意图检测，分发到议价/技术/通用菜单节点生成回复",
    module_nodes=[xy_route_root, xy_menu_price, xy_menu_price_refuse,
                  xy_menu_tech, xy_menu_default],
    generate={
        "nlu": XianyuIntentNLU(),
        # 模块级 NLG：no_reply/拒绝节点零 LLM 短路，其余菜单节点按
        # node.base_nlg_prompt 意图模板生成（温度/长度按意图调优）
        "nlg": FixedNLG(),
    },
)


# ============================================================================
# Pattern 注册 —— 顶层 registry.register，由 AST 扫描自动发现
# ============================================================================

xianyu_agent_pattern = Pattern(
    code="xianyu_agent",
    name="闲鱼卖家客服助手",
    description="对话管理：ROUTE 每轮独立意图检测（本地规则 + LLM 兜底）+ 议价轮数控制 + 意图级 prompt",
    entry_module_code="xianyu_root",
    modules=[xianyu_root],
    # 查询改写槽位：时间实体增强（零 LLM）——买家消息中的相对时间
    # （"明天下午"等）先解析为绝对时间标注，再进 NLU/NLG prompt
    query=TimeAugQueryRewriter(),
)

registry.register(xianyu_agent_pattern)
