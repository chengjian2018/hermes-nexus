"""
闲鱼卖家客服 Route 模式 —— 复刻 xianyu-auto-reply 的 agent 对话管理。

复刻来源：zhinianboke/xianyu-auto-reply websocket/app/services/xianyu/ai_reply_engine.py
（每条买家消息：本地关键词意图检测 price/tech/default → 按意图选 system prompt →
携带商品信息 + 最近 10 条对话历史 + 议价计数与限制 → 单次 LLM 调用出 ≤40 字回复；
议价轮数达上限时返回固定拒绝话术，不调 LLM）。

Pattern 结构（对齐 car_sales_route 的 ROUTE 模式）：

    xianyu_agent (Pattern, entry: xianyu_root)
    └── xianyu_root (RouteModule)   全部节点留在路由模块，无 jump_module
        ├── xy_route_root      路由根节点（sub_nodes = 意图菜单）
        ├── xy_menu_price      议价菜单（议价次数未达上限）
        ├── xy_menu_price_refuse  议价拒绝菜单（达上限 → 固定话术，零 LLM）
        ├── xy_menu_tech       技术问答菜单
        └── xy_menu_default    通用客服菜单

对话流程（每轮）：
    1. ROUTE 轮末自动重置回 root，下一轮 RouteNLU 重新分类 —— 与 xianyu
       "每条消息独立做意图检测"的语义天然同构（无跨轮意图状态）
    2. XianyuIntentNLU（模块级 nlu_stage，本地关键词匹配，零 LLM）复刻
       detect_intent 的 price/tech/default 关键词表；议价轮数达标时改判
       refuse 意图，并复刻"当前议价次数/最大轮数/优惠上限"注入 prompt
    3. 菜单节点级 base_nlg_prompt 复刻三套意图 system prompt
       （语言约束：每句≤10字，总字数≤40字）+ 商品信息 + 对话历史
    4. xy_menu_price_refuse 配 FixedNLG 固定拒绝话术 —— 复刻
       "议价次数已达上限返回固定文案且不调 LLM"

消息入口：src/channel/xianyu.py（默认回复 API 外挂决策口），
设 XIANYU_CHANNEL_PATTERN=xianyu_agent 即接入。

注册方式：模块顶层 ``registry.register(Pattern(...))``，AST 扫描自动发现。
"""

from src.dialogue.base import PipelineStage
from src.dialogue.module import RouteModule
from src.dialogue.nlg.nlg import BaseNLG, RouteNLG
from src.dialogue.node import BaseNode
from src.dialogue.pattern import Pattern
from src.dialogue.register import registry
from src.prompt import XIANYU_PRICE_NLG_PROMPT, XIANYU_TECH_NLG_PROMPT, XIANYU_DEFAULT_NLG_PROMPT

# ============================================================================
# 本地意图检测 —— 复刻 ai_reply_engine.detect_intent 的关键词表
# ============================================================================

# 价格相关关键词（原文照搬，含闲鱼语境的"刀""包个邮"等）
PRICE_KEYWORDS = [
    "便宜", "优惠", "刀", "降价", "价格", "多少钱",
    "能少", "还能", "最低", "底价", "实诚价", "到100", "能到",
    "包个邮",
]

# 技术相关关键词
TECH_KEYWORDS = [
    "怎么用", "参数", "坏了", "故障", "设置", "说明书",
    "功能", "用法", "教程", "驱动",
]

# 意图 → 菜单节点编码（refuse 由议价轮数控制另行改判）
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
    """本地关键词意图检测 —— 复刻 ai_reply_engine.detect_intent。

    Args:
        message: 买家消息

    Returns:
        意图: price / tech / default
    """
    msg_lower = message.lower()
    if any(kw in msg_lower for kw in PRICE_KEYWORDS):
        return "price"
    if any(kw in msg_lower for kw in TECH_KEYWORDS):
        return "tech"
    return "default"


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
# NLU stage —— 本地关键词意图分类（零 LLM）
# ============================================================================

class XianyuIntentNLU(PipelineStage):
    """闲鱼意图分类 stage —— 复刻 detect_intent + 议价轮数控制。

    契约与框架 NLU 一致（execute(ctx) -> ctx，写 ctx.nlu_result），
    挂在 RouteModule 模块级 nlu_stage（node 无覆盖时生效）。

    nlu_result 结构（对齐框架 NLU 契约）：
        {"next_node": <菜单节点编码>, "slots": {...}, "intent": <原始意图>}

    slots 复刻 generate_reply 的议价参数注入：
        bargain_count / max_bargain_rounds / max_discount_percent /
        max_discount_amount —— 经 filled_slots 供 NLG 模板 {__filled_slots__}
        带入 prompt
    """

    stage_name = "xianyu_intent_nlu"

    def execute(self, ctx):
        intent = detect_intent(ctx.user_query)
        bargain_count = 0
        next_node = INTENT_TO_MENU[intent]
        settings = _get_bargain_settings(ctx)

        # 当前轮 user 消息回填 intent（消息已入 history：chat() 先
        # add_message 再跑管线）——原实现同样先落库后计数
        for msg in reversed(ctx.history):
            if msg.role == "user":
                msg.metadata["intent"] = intent
                break

        if intent == "price":
            # 复刻原语义：count 含当前轮，bargain_count >= max_bargain_rounds
            # 即拒绝（第 max 次砍价收到固定拒绝话术）
            bargain_count = _count_bargain_rounds(ctx)
            if bargain_count >= settings["max_bargain_rounds"]:
                next_node = "xy_menu_price_refuse"

        # 议价参数随槽位合并进 filled_slots，供 NLG prompt 注入。
        # 注意 ROUTE 路径框架在 stages 之后才做 slots → filled_slots 合并，
        # 而 NLG 在 stages 内执行 —— 故此处同时直写 filled_slots（相同键，
        # 框架后续合并幂等），保证 NLG 模板 {__filled_slots__} 当轮可见
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


# ============================================================================
# NLG stage —— 固定话术（议价拒绝节点专用，零 LLM）
# ============================================================================

class FixedNLG(BaseNLG):
    """固定话术 NLG —— 复刻"议价达上限返回固定文案，不调 LLM"。

    话术取节点 answer_examples[0]（与原实现的硬编码拒绝文案一致）。
    注意框架的 ROUTE 默认管线是「root 时刻构建 stages、NLU 后才切节点」，
    菜单节点的 nlg_stage 不会自动生效 —— 故本 stage 不依赖节点级注入，
    而是作为通用 stage 使用：命中的节点带 refuse marker（answer_examples
    硬编码拒绝文案）时直接短路，否则回落 LLM 生成。
    """

    stage_name = "fixed_nlg"

    # 议价拒绝固定文案（复刻 ai_reply_engine 硬编码）
    REFUSE_TEXT = "抱歉，这个价格已经是最优惠的了，不能再便宜了哦！"
    # 命中标记：NLG 按当前节点 answer_examples 匹配到该文案即短路
    _MARKER = REFUSE_TEXT

    def prompt_build(self, cxt):
        return ""

    def execute(self, ctx):
        node = ctx.get_current_node()
        if node is not None and any(
            self._MARKER in (ex or "") for ex in (node.answer_examples or [])
        ):
            ctx.nlg_result = {"content": self.REFUSE_TEXT}
            return ctx
        # 非拒绝节点：回落默认 RouteNLG 生成（复用框架实现）
        return RouteNLG().execute(ctx)


# ============================================================================
# RouteModule —— 顶层路由：根节点 + 意图菜单（全部留在本模块）
# ============================================================================

xy_route_root = BaseNode(
    node_code="xy_route_root",
    node_name="闲鱼路由根节点",
    node_description="闲鱼卖家客服总入口，覆盖议价、技术问答与通用咨询三大场景",
    node_todo_description="识别买家消息意图（本地关键词检测），分发到议价/技术/通用菜单节点",
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
    module_description="复刻 xianyu-auto-reply 的意图检测与回复生成：本地关键词意图分类 + 意图级 prompt + 议价轮数控制",
    module_todo_description="对每条买家消息做意图检测，分发到议价/技术/通用菜单节点生成回复",
    module_nodes=[xy_route_root, xy_menu_price, xy_menu_price_refuse,
                  xy_menu_tech, xy_menu_default],
    nlu_stage=XianyuIntentNLU(),
    # 模块级 NLG：拒绝节点零 LLM 短路，其余菜单节点回落框架 RouteNLG
    # （后者按 node.base_nlg_prompt 三级优先取本 pattern 的意图模板）
    nlg_stage=FixedNLG(),
)


# ============================================================================
# Pattern 注册 —— 顶层 registry.register，由 AST 扫描自动发现
# ============================================================================

xianyu_agent_pattern = Pattern(
    code="xianyu_agent",
    name="闲鱼卖家客服助手",
    description="复刻 xianyu-auto-reply agent 对话管理：ROUTE 每轮独立意图检测 + 议价轮数控制 + 意图级 prompt",
    entry_module_code="xianyu_root",
    modules=[xianyu_root],
)

registry.register(xianyu_agent_pattern)
