"""install_booking pattern — outbound install-booking call FSM（自 nexus-kit
apps/install_booking_agent 迁移，手绘 FSM 转写的节点图与守卫机制原样保留）。

Business background: the customer just bought furniture / an appliance;
the service desk calls THEM to book the installer's visit (the on-site
installation service).
The assistant is always the caller — the opening is a connect-event-driven
greeting (self-introduction + purpose), and the flow walks the sketch:
confirm address → check arrival → negotiate visit time → book → close.

迁移映射（nexus-kit 扁平 FSM → 本项目 Pattern/Module/Node 三层）:

    nexus-kit                                    hermes-nexus
    ────────────────────────────────────────────────────────────────────
    Pattern(pattern_type="fsm", nodes=[...])     Pattern(modules=[FSMModule(
    entry_node_code="install_greet"                module_nodes=[...])])——首节点
                                                  即入口（chat._resolve_entry_node
                                                  取 module_nodes[0]）
    pattern.stages 骨架 + plugin registry 字符串  默认骨架 + 槽位实例注入：
    stage code 解析                                pattern.query=TimeAugQueryRewriter() /
                                                  module.generate=InstallBookingUnifiedNLU() /
                                                  module.enable_clarify + module.clarify_stage
    node.stages={"clarify": ...} 每节点声明        module.enable_clarify=True（模块级准入，
    （统一阶段按当前节点放行 clarify）              等价于"每个节点都开"）
    node.base_nlu_prompt 每节点重复模块级模板      module.base_nlu_prompt（resolve_prompt_template
                                                  三层解析 node > module > default）
    stage 类 async execute                        同步 execute

task_info contract (injected by the launch layer):
    - product_name / address / user_name / order_id — order facts the call
      grounds in;
    - available_slots: ["YYYY-MM-DD HH:MM-HH:MM", ...] — the installer's
      bookable windows. When present, the guarded unified stage
      (dialogue/booking_stages.BookingGuardUnifiedNLU, bound here as
      ``InstallBookingUnifiedNLU``) runs a deterministic booking-time guard
      on every visit-time pick: a bookable request is annotated and
      proceeds; an unbookable one is rerouted to install_recommend. Every
      transition into install_recommend gets its reply deterministically
      rewritten from the schedule (InstallRecommendNLG, zero extra LLM).

Flow (16 nodes, one FSM module; sketch oval → node code → branches):

    after start (greeting + confirm service)
                                 install_greet          Yes→address check / No→end
    ask if address XX matches    install_confirm_addr   match→arrival check / no→end
    have you received the goods  install_check_arrival  yes→time negotiation / no→ETA
    do you know the arrival time install_ask_eta        knows→time window / doesn't→availability
    ask for a time window        install_time_window    offers time→negotiation / not→availability
    is now convenient            install_available      yes→time negotiation / no→callback time
    ask when he wants the visit  install_ask_time       specific date→install_specific_date /
                                                       nearest→install_nearest /
                                                       neither→recommend
    recommend                    install_recommend      customer picks→specific date/nearest / loop back to negotiate
    specific date                install_specific_date  bookable→time confirm (guard check) / not bookable→guard reroutes to recommend
    nearest                      install_nearest        bookable→time confirm / not bookable→guard reroutes to recommend
    visit time (confirm time)    install_confirm_time   customer confirms→end / reschedule→time negotiation
    end                          install_end            is_end (call wrap-up)

Supplemented nodes (beyond the sketch, per the follow-up requirements):

    install_decline     generic decline node: user does not want to book /
                        already installed / quality issue / return /
                        not-the-owner etc. intents; empathetic reply then to
                        end (every business node has an edge to it — the
                        generic exit channel outside the sketch)
    install_ask_callback next contact time: when the user is busy now /
                        does not want to book now, ask for and record the
                        next call time, then close politely
    install_reschedule  reschedule node: after time confirmation the client
                        changes their mind; re-enter time negotiation
                        (keeping the original time slot, allowed to be
                        overwritten by the new time)
    install_callback_default unusable callback times (beyond 2 weeks / past /
                        vague) reroute here: propose the default 3-days-later
                        callback, close on the customer's answer (two beats)

The sketch's two terminal ovals (address mismatch / customer-declined end,
booking-completed end) merge into one ``install_end`` node — both are "polite
phone close, hang up"; the reply paradigm for the closing turn comes from
install_end's answer_examples (the unified stage styles the reply after the
CHOSEN next node). The generic decline intents go through install_decline
first (a scenario-specific empathetic line) and then install_end — two beats,
because the decline reply and the goodbye deserve different wordings.

Known deliberate simplifications:
    - The dual-track clarify is keyword-gated (no off-topic branch in the
      sketch): off-flow turns are answered by the FAQ keyword table and
      pulled back, never a recall round.
    - Logistics arrival info has no external system integration: the
      install_ask_eta "do you know the arrival time" is taken from the
      customer's own account; a task_info logistics_eta field could enrich
      this (reserved).
    - Outbound telecom events such as no-answer / busy / hang-up are not
      handled at the dialogue layer (the channel layer's job); only the
      post-connect dialogue flow is covered here.
    - Reschedule rounds are uncapped: the install_reschedule →
      install_ask_time loop converges through natural dialogue (the FSM
      has no budget — each turn advances exactly one node; every reschedule
      re-passes the bookable guard).

Registration: module-level ``registry.register(Pattern(...))``, auto-discovered
by AST scan (dialogue/install_booking_route.py).
"""

import logging

from dialogue.booking_stages import (
    BookingGuardUnifiedNLU,
    KeywordClarifyStage,
    ScheduleRecommendNLG,
)
from dialogue.module import FSMModule
from dialogue.node import BaseNode
from dialogue.pattern import Pattern
from dialogue.register import registry
from stages.query import TimeAugQueryRewriter

logger = logging.getLogger(__name__)


# ============================================================================
# FAQ keyword table — keyword-only business detection for the clarify stage
# ============================================================================

# Ordered: specific entries first (checked top-down, first hit wins).
# The customer on an outbound install-booking call asks off-flow questions
# mid-negotiation ("does installation cost money", "how long is the
# warranty"...); any-keyword hit ⇒ kb track (the entry's answer template,
# {product_name} etc. substituted from task_info), no hit ⇒ fallback track.
FAQ_ENTRIES = [
    {
        "topic": "费用",
        "keywords": ["收费", "费用", "多少钱", "收费吗", "要钱吗", "免费吗",
                     "额外的钱", "收钱"],
        "answer": (
            "本次{product_name}的上门安装是包安装服务，安装本身不额外收费；"
            "如有加打孔、加支架等特殊需求，师傅会先报价，您确认后再做。"
        ),
    },
    {
        "topic": "保修",
        "keywords": ["保修", "质保", "三包", "坏了怎么办", "维修"],
        "answer": (
            "{product_name}整机按国家三包政策提供保修，安装后如果出现质量"
            "问题可以联系商家安排售后，您放心。"
        ),
    },
    {
        "topic": "安装时长",
        "keywords": ["多久", "多长时间", "装完", "要几个小时", "几个小时",
                     "麻烦吗"],
        "answer": (
            "常规安装大概 1 个小时左右，具体看现场情况，师傅上门前会先和"
            "您沟通，一般不影响您正常安排。"
        ),
    },
    {
        "topic": "自装咨询",
        "keywords": ["自己装", "我自己会装", "不用师傅", "自装"],
        "answer": (
            "可以的，您要是方便自己安装也可以不来师傅；不过建议还是让"
            "师傅上门，安装同时会帮您验机调试，后续使用更放心～"
        ),
    },
    {
        "topic": "改地址",
        "keywords": ["换地址", "改地址", "不在地址", "送到别的地方",
                     "另一个地址"],
        "answer": (
            "如果安装地址有变化，建议您先在订单里修改收货地址或联系商家"
            "更新，我们按更新后的地址安排师傅上门。"
        ),
    },
    {
        "topic": "催物流",
        "keywords": ["怎么还没到", "物流怎么这么慢", "快递到哪了", "催一下",
                     "什么时候发货"],
        "answer": (
            "物流进度这边帮您记录反馈，您也可以在订单页查看实时物流；"
            "货到之后我们再约师傅上门就来得及。"
        ),
    },
]


def match_faq(text: str):
    """Pure keyword containment against the FAQ table (specific-first).

    Returns the first entry whose keyword list intersects the text; None
    when no entry hits.
    """
    if not text:
        return None
    for entry in FAQ_ENTRIES:
        if any(kw in text for kw in entry["keywords"]):
            return entry
    return None


# ============================================================================
# Unified-stage template override (module-level base_nlu_prompt)
# ============================================================================

INSTALL_UNIFIED_PROMPT = """## 任务描述
你是一位家具/电器品牌的售后客服，正在【主动外呼】一位刚购买了商品的客户，
目标是在这通电话里完成上门安装服务的预约：核对地址 → 确认到货 → 约定师傅上门时间。
你在一次响应内同时完成「理解用户」与「生成回复」：根据当前节点与候选后续节点
判断用户意图、抽取槽位，并依据所选节点的回答范式直接生成回复话术。

## 特殊意图识别（任何节点都可能听到）
客户在流程任何阶段都可能出现以下拒绝意图，命中时跳转「通用拒绝承接」节点，不要继续推进预约：
- 不想预约 / 不需要上门安装
- 商品已经安装过了
- 商品有质量问题（共情记录，不要尝试继续约时间）
- 已经退货了
- 接电话的不是本人
客户表示现在没空、暂时不想预约（但没有拒绝安装本身）时，跳转「下次联系时间」节点。

## 特殊情况：下次联系时间的答复（下次联系时间节点）
客户给出下次来电时间后，按改写结果中的时间标注判断（标注只出现在未来两周内的有效时间上）：
- 有时间标注 → 合适的联系方式时间：跳转「通话结束」，reply 复述该时间并道别；
- 无时间标注（时间太远超过两周 / 已是过去时间 / 说"都行"没给具体时间）→
  跳转「默认改约三天」节点，reply 礼貌说明改约到 3 天后左右再联系并征询客户意见。
在「默认改约三天」节点上，客户应答（同意/异议）后跳转「通话结束」完成收尾。

## 人设描述
亲切、利落、有条理的电话客服：你打给客户，先自报家门说明来意，再一步步把预约事项确认清楚。
每轮回复简短口语化（两句话以内，电话节奏），一次只确认一件事，客户听不清时耐心重复。
不编造任务信息中没有的内容，地址、商品、时间等信息一律以【任务信息】为准；
推荐档期时优先引用【任务信息】中的可预约时间列表（available_slots）。
严格遵循模版输出。

## 输入内容
### 任务信息
{__task_info__}

### 当前节点信息
{__cur_node__}

### 当前节点回答范式（保持当前节点时使用）
{__cur_answer_pattern__}

### 候选后续节点（含回答范式）
{__next_node_pattern__}

### 用户输入
{__query__}

### 改写结果
{__query_rewrite__}

### 已填充槽位
{__filled_slots__}

### 对话历史
{__history__}

## 输出内容
以 JSON 对象输出，一次包含回复与决策，示例格式：
{{"reply": "给客户的回复话术", "next_node": "xx", "slots": {{"slot1": "", "slot2": []}}}}

要求：
1. next_node 只能从候选后续节点的节点编码中选择；用户输入不足以推进流程时输出空字符串 ""，此时 reply 按当前节点回答范式继续确认。
2. reply 严格遵循所选 next_node 节点的回答范式的结构、语气和风格，将抽取到的槽位信息自然融入。
3. slots 按当前节点信息中的槽位定义模版抽取，用户未提及的槽位留空。
4. reply 与 next_node、slots 必须自洽：回复内容所引导的下一步就是 next_node 所在的节点。
5. 时间类槽位优先采用改写结果中已解析的绝对时间。
6. 只输出 JSON 对象，不要包裹 markdown 代码块或任何其他文字。

## next_node 合法取值
{__valid_next_values__}

## 特殊情况：客户输入与当前节点待办无关
当客户在流程中问起与预约待办无关的业务问题（例如安装是否收费、保修多久、
安装要多久、能不能自己装、改地址、催物流等），不要强行推进预约，改为输出：
{{"next_node": "clarify", "slots": {{"topic": "主题", "keywords": ["关键词1", "关键词2"]}}, "reply": "简短承接语"}}
解释：
1. topic 用短词概括客户问题的主题（如"费用"、"保修"、"物流"）。
2. keywords 列出客户问题中的关键词。
3. reply 只需简短承接（如"这个问题我说一下"），正式回答由后续澄清环节生成。
4. 仅当客户输入明显与当前节点待办无关时才使用该输出；正常回答待办问题时禁止使用。
"""


# ============================================================================
# Scenario stage subclasses — the shared guard machinery bound to install
# node codes and wording (dialogue/booking_stages.py holds the machinery)
# ============================================================================

class InstallRecommendNLG(ScheduleRecommendNLG):
    """Schedule-backed recommendation NLG for the install scenario
    (deterministic, zero LLM)."""

    stage_name = "install_recommend_nlg"

    UNBOOKABLE_LEAD = "师傅档期排不开了，"
    RECOMMEND_LEAD = "最近可以约 "
    RECOMMEND_TAIL = "，您看哪个时间段合适？"


class InstallBookingUnifiedNLU(BookingGuardUnifiedNLU):
    """Booking guard + schedule rewrite + callback triage, on the install
    node graph (see dialogue/booking_stages.py)."""

    stage_name = "install_unified"

    # Rebound node codes (the install FSM's sketch)
    BOOKING_TARGETS = frozenset({"install_specific_date", "install_nearest"})
    RECOMMEND_NODE = "install_recommend"
    CALLBACK_NODE = "install_ask_callback"
    CALLBACK_DEFAULT_NODE = "install_callback_default"
    END_NODE = "install_end"

    # Install wording for the deterministic replies
    CALLBACK_CLOSE_TEXT = "感谢您的接听，祝您生活愉快，再见！"
    CALLBACK_DEFAULT_PROPOSAL = "您说的时间有点远呢，那我们先约 {day} 左右再给您来电话确认，您看可以吗？"

    RECOMMEND_NLG_CLS = InstallRecommendNLG


# ============================================================================
# Phone-call-shaped clarify prompts (kb / fallback — no mixed zone in
# keyword gating)
# ============================================================================

INSTALL_CLARIFY_KB_PROMPT = """## 任务描述
你是一位家具/电器品牌的售后客服，正在与客户进行上门安装预约的电话沟通。
客户在流程中问了一个业务问题，关键词卡控已命中常见问题答案（【FAQ 答案】）。
请先用一两句话把 FAQ 答案口语化地讲给客户，然后把对话拉回预约主线，重新询问当前节点待办的问题。

## 人设描述
亲切利落的电话客服：先把客户的疑问答清楚，再自然地继续推进预约。
每轮回复两句话以内（电话节奏），不编造 FAQ 答案之外的信息。

## 输入内容
### 客户问题
{__query__}

### 问题主题
{__topic__}

### 问题关键词
{__keywords__}

### FAQ 答案（唯一事实源，禁止编造补充）
{__faq_answer__}

### 当前节点信息（预约主线）
{__cur_node__}

### 对话历史
{__history__}

### 任务信息
{__task_info__}

## 输出内容
以纯文本格式输出，直接念给客户的回复话术。
要求：
1. 第一句基于 FAQ 答案回应客户问题，不添加 FAQ 之外的承诺。
2. 第二句拉回预约主线，重新询问当前节点待办的问题（一次只问一件事）。
"""

INSTALL_CLARIFY_FALLBACK_PROMPT = """## 任务描述
你是一位家具/电器品牌的售后客服，正在与客户进行上门安装预约的电话沟通。
客户在流程中问了一个与预约无关、关键词卡控未命中的问题。
请先礼貌承接并诚实告知这个问题稍后核实，然后把对话拉回预约主线，重新询问当前节点待办的问题。

## 人设描述
亲切利落的电话客服：不冷落客户的问题，也不编造答案，尽快温和地回到预约主线。
每轮回复两句话以内（电话节奏）。

## 输入内容
### 客户问题
{__query__}

### 问题主题
{__topic__}

### 问题关键词
{__keywords__}

### 当前节点信息（预约主线）
{__cur_node__}

### 对话历史
{__history__}

### 任务信息
{__task_info__}

## 输出内容
以纯文本格式输出，直接念给客户的回复话术。
要求：
1. 第一句简短承接客户的问题并诚实告知稍后核实，不要编造答案。
2. 第二句拉回预约主线，重新询问当前节点待办的问题（一次只问一件事）。
"""

INSTALL_CLARIFY_PROMPTS = {
    "kb": INSTALL_CLARIFY_KB_PROMPT,
    "fallback": INSTALL_CLARIFY_FALLBACK_PROMPT,
}


class InstallKeywordClarifyStage(KeywordClarifyStage):
    """The keyword-gated clarify, bound to the install FAQ table and
    prompts."""

    stage_name = "install_clarify"

    FAQ_MATCHER = staticmethod(match_faq)
    CLARIFY_PROMPTS = INSTALL_CLARIFY_PROMPTS
    CLARIFY_FALLBACK_REPLY = (
        "抱歉，这个问题我这边确认一下。咱们继续约安装时间"
        "好吗？您什么时间方便？"
    )


# ============================================================================
# Nodes — the sketch's ovals + supplemented scenarios, in flow order
# (module_nodes[0] / install_greet is the entry; the clarify admission
# switch and the unified template ride the module, not every node)
# ============================================================================

install_greet = BaseNode(
    node_code="install_greet",
    node_name="外呼开场",
    node_description=(
        "电话接通后的开场：自报家门（品牌售后客服）、说明来意（客户购买"
        "的商品需要上门安装）、确认客户方便接听"
    ),
    node_todo_description="播报外呼开场白，确认客户是否需要上门安装服务",
    node_slots={
        "service_needed": "客户是否需要上门安装服务（是/否）",
    },
    sub_nodes=["install_confirm_addr", "install_end", "install_decline"],
    answer_examples=[
        "您好，请问是{user_name}先生/女士吗？我是{product_name}品牌售后客服，"
        "您购买的商品可以安排师傅上门安装，现在方便聊两句吗？",
        "您好打扰了，这里是{product_name}售后服务中心，给您来电是想帮您"
        "预约上门安装，您这边方便吗？",
    ],
)

install_confirm_addr = BaseNode(
    node_code="install_confirm_addr",
    node_name="地址核对",
    node_description="复述订单收货地址，请客户核对是否一致（师傅按此地址上门）",
    node_todo_description="核对上门安装地址是否一致，一致则进入到货确认",
    node_slots={
        "address_confirmed": "地址是否一致（是/否）",
        "address": "客户口径的安装地址（不一致时记录）",
    },
    sub_nodes=["install_check_arrival", "install_end", "install_decline"],
    answer_examples=[
        "先跟您核对一下地址：师傅上门是到{address}，对吗？",
        "麻烦确认下，安装地址是{address}这一处吧？",
    ],
)

install_check_arrival = BaseNode(
    node_code="install_check_arrival",
    node_name="到货确认",
    node_description="确认商品是否已经送达客户地址（师傅需货到后才能上门安装）",
    node_todo_description="询问商品是否已到货，已到货直接约时间，未到货先问物流",
    node_slots={
        "arrived": "商品是否已到货（是/否）",
    },
    sub_nodes=["install_ask_time", "install_ask_eta", "install_decline"],
    answer_examples=[
        "好的～请问您的{product_name}现在已经送到{address}了吗？",
        "商品这边显示近期送达，您那边签收了吗？",
    ],
)

install_ask_eta = BaseNode(
    node_code="install_ask_eta",
    node_name="到货时间询问",
    node_description="未到货时询问客户是否知道大概的到货时间",
    node_todo_description="询问是否知道到货时间，知道则请客户给个方便的时间段",
    node_slots={
        "eta_known": "客户是否知道到货时间（是/否）",
        "eta": "客户知道的到货时间",
    },
    sub_nodes=["install_time_window", "install_available", "install_decline"],
    answer_examples=[
        "还没到也没关系～您知道大概什么时候能送到吗？",
        "您那边有物流的预计送达时间吗？跟我说说就好。",
    ],
)

install_time_window = BaseNode(
    node_code="install_time_window",
    node_name="时间段询问",
    node_description="客户知道到货时间后，请客户讲一个方便接收/安装的时间段",
    node_todo_description="收集客户方便上门安装的时间段",
    node_slots={
        "time_window": "客户提供的方便时间段",
    },
    sub_nodes=["install_ask_time", "install_available", "install_decline"],
    answer_examples=[
        "那您哪个时间段在家方便？我好帮您约师傅～",
        "您说个大概的时间段（比如周末白天），我来协调师傅上门。",
    ],
)

install_available = BaseNode(
    node_code="install_available",
    node_name="上门方便确认",
    node_description="确认客户近期是否方便安排师傅上门安装",
    node_todo_description="询问客户是否方便上门，方便则进入时间协商",
    node_slots={
        "available": "客户是否方便上门（是/否）",
    },
    # Unavailable no longer ends directly: supplemented scenario — no time
    # right now / unwilling to book now → ask for a callback time
    sub_nodes=["install_ask_time", "install_ask_callback", "install_decline"],
    answer_examples=[
        "了解～那最近方便安排师傅上门安装吗？",
        "您这边近期方便约个时间安装吗？",
    ],
)

install_ask_time = BaseNode(
    node_code="install_ask_time",
    node_name="上门时间协商",
    node_description=(
        "核心调度节点：询问客户希望师傅什么时间上门；说不出具体时间则"
        "主动推荐档期，给出具体日期则记录，要最近的则走最近档期"
    ),
    node_todo_description="收集客户期望的上门时间，按客户口径分发到推荐/具体日期/最近",
    node_slots={
        "visit_time": "客户期望的上门时间",
    },
    sub_nodes=["install_recommend", "install_specific_date", "install_nearest",
               "install_decline"],
    answer_examples=[
        "请问您希望师傅什么时间上门呢？方便给个大概日期或时间段吗？",
        "您哪天在家方便？我可以帮您查最近的安装档期～",
    ],
)

install_recommend = BaseNode(
    node_code="install_recommend",
    node_name="档期推荐",
    node_description=(
        "客户说不出时间或所给时间不可约时，按师傅排班（任务信息的"
        "available_slots）主动推荐可约档期，客户选定后进入对应节点"
    ),
    node_todo_description="给出2-3个可约档期供客户选择，等待客户挑选",
    node_slots={
        "recommended_slots": "已推荐的档期列表",
        "chosen_slot": "客户选定的推荐档期",
    },
    sub_nodes=["install_specific_date", "install_nearest", "install_ask_time",
               "install_decline"],
    answer_examples=[
        "要不这样，最近可以约明天上午10点或后天下午2-4点，您看哪个合适？",
        "我这边推荐周末上午的档口，师傅上门安装也从容些，您觉得呢？",
    ],
)

install_specific_date = BaseNode(
    node_code="install_specific_date",
    node_name="具体日期约定",
    node_description=(
        "客户给出具体日期/时间，复述确认并锁定师傅上门时间；所给时间是否"
        "可约由统一阶段后的可约守卫校验（不可约会被改道到档期推荐）"
    ),
    node_todo_description="记录具体日期与时间，可约则确认锁定，不可约守卫改道推荐",
    node_slots={
        "visit_date": "上门日期",
        "visit_hour": "上门时间（几点/时段）",
    },
    sub_nodes=["install_confirm_time", "install_ask_time", "install_decline"],
    answer_examples=[
        "好的，那就给您约在{visit_date}{visit_hour}，师傅到时会提前联系您～",
        "收到～{visit_date}这个时间可以安排，我帮您登记上了。",
    ],
)

install_nearest = BaseNode(
    node_code="install_nearest",
    node_name="最近档期安排",
    node_description="客户要最近的上门时间，按最近可约档期复述确认",
    node_todo_description="给出最近可约时间并确认，客户不同意则回环重新协商",
    node_slots={
        "visit_time": "最近可约的上门时间",
    },
    sub_nodes=["install_confirm_time", "install_ask_time", "install_decline"],
    answer_examples=[
        "最快可以安排最近的档期上门，时间临近师傅会提前联系您～",
        "帮您插了最近的安装档期，您留意下师傅的电话哦。",
    ],
)

install_confirm_time = BaseNode(
    node_code="install_confirm_time",
    node_name="上门时间确认",
    node_description=(
        "最终确认节点：复述锁定的上门时间与地址，确认无误后收尾；"
        "客户此时改约则转入改约节点重新协商"
    ),
    node_todo_description="复述上门时间等待客户最终确认，改约则重新协商",
    node_slots={
        "visit_time": "最终确认的上门时间",
        "address": "上门安装地址",
    },
    # supplemented scenario: reschedule after confirmation → install_reschedule
    sub_nodes=["install_end", "install_reschedule", "install_decline"],
    answer_examples=[
        "跟您最后确认下：{visit_time}师傅到{address}上门安装，没问题吧？",
        "那就定啦——{visit_time}上门，地址{address}，辛苦您届时在家等一下～",
    ],
)

install_reschedule = BaseNode(
    node_code="install_reschedule",
    node_name="改约重协商",
    node_description=(
        "客户在时间确认后要求改期：致歉并作废原时间，重新进入上门时间"
        "协商（新时间会重新过可约守卫）"
    ),
    node_todo_description="确认改约意向后重新协商上门时间",
    node_slots={
        "rescheduled": "是否发生改约（是）",
        "prev_visit_time": "改约前的原上门时间",
    },
    sub_nodes=["install_ask_time", "install_decline"],
    answer_examples=[
        "没问题，改期很方便～那我们重新约一下，您什么时间方便？",
        "好的好的，原来的时间帮您取消，您看约到什么时候合适？",
    ],
)

install_ask_callback = BaseNode(
    node_code="install_ask_callback",
    node_name="下次联系时间",
    node_description=(
        "客户现在没空或暂时不想预约时，询问并记录下次来电时间（这是联系"
        "时间，不是上门时间，不进可约守卫）。答复三分支由统一阶段的确定"
        "性守卫裁定：时间合适（未来两周内）→直接进通话结束播报客户时间；"
        "太远（超过两周）/过去/未给出→改走「默认改约三天」节点"
    ),
    node_todo_description="收集下次来电联系时间，按答复分支收尾",
    node_slots={
        "callback_time": "下次来电联系时间",
        "callback_source": "时间来源（customer=客户给定 / default=默认3天）",
    },
    sub_nodes=["install_end", "install_callback_default", "install_decline"],
    answer_examples=[
        "理解理解～那您看我们什么时候再联系您方便？我记一下时间。",
        "好的，那不打扰了，您方便的时候我们什么时候再打给您？",
    ],
)

install_callback_default = BaseNode(
    node_code="install_callback_default",
    node_name="默认改约三天",
    node_description=(
        "客户给的下次联系时间太远（超过两周）、已是过去时间、或说都行/"
        "未给出时间时，改约默认 3 天后再联系：播报默认联系时间并征询"
        "客户意见，客户应答后进入通话结束（两拍收尾，与通用拒绝承接"
        "同构；分支裁定与默认时间由统一阶段的确定性守卫给出）"
    ),
    node_todo_description="播报默认3天后再联系，等待客户应答后收尾",
    node_slots={
        "callback_time": "默认下次来电联系时间（今天+3天）",
        "callback_source": "时间来源（default=默认3天）",
    },
    sub_nodes=["install_end"],
    answer_examples=[
        "那我们先约 3 天后左右再给您来电话确认，您看可以吗？",
        "您说的这个时间有点远呢，我们先 3 天后再联系您方便吗？",
    ],
)

install_decline = BaseNode(
    node_code="install_decline",
    node_name="通用拒绝承接",
    node_description=(
        "通用退出通道：客户不想预约/已安装过/商品有质量问题/已退货/"
        "非本人等意图，按场景共情回应，然后转入通话结束"
    ),
    node_todo_description="识别拒绝意图并共情回应，转通话结束",
    node_slots={
        "decline_reason": "拒绝原因（不想预约/已安装/质量问题/退货/非本人）",
    },
    sub_nodes=["install_end"],
    answer_examples=[
        "好的，那就不安排上门安装了～有需要您随时联系我们，感谢接听。",
        "了解，既然已经安装好了就不打扰了，祝您使用愉快～",
        "非常抱歉给您带来困扰，质量问题我这边帮您记录反馈，稍后会有"
        "专人联系您处理，请您留意来电。",
        "好的，退货的话安装预约就帮您取消了，祝您生活愉快。",
        "不好意思打扰了，那我跟{user_name}先生/女士再确认时间，感谢您的接听。",
    ],
)

install_end = BaseNode(
    node_code="install_end",
    node_name="通话结束语",
    node_description=(
        "通话收尾：预约完成、约好下次联系、客户拒绝、地址不符等所有"
        "终止路径的礼貌收尾（is_end 终节点，进入即结束会话）"
    ),
    node_todo_description="礼貌收尾，感谢客户接听，结束通话",
    node_slots={},
    sub_nodes=[],
    is_end=True,
    answer_examples=[
        "好的，那就不打扰您了～稍后有需要随时来电，祝您生活愉快，再见～",
        "感谢您的接听与配合，那我们{visit_time}见，祝您使用愉快，再见～",
        "好的，地址问题建议您联系卖家或平台核实哦，感谢接听，再见～",
    ],
)


# ============================================================================
# Module + pattern registration — the whole flow is one FSM module
# ============================================================================

install_booking_module = FSMModule(
    module_code="install_booking",
    module_name="安装预约外呼流程",
    module_description=(
        "手绘 FSM 转写的安装预约外呼：核对地址 → 确认到货 → 协商师傅上门"
        "时间 → 最终确认；通用拒绝与下次联系时间补充通道"
    ),
    module_todo_description="按节点链推进安装预约外呼直至收尾",
    module_nodes=[
        install_greet,
        install_confirm_addr,
        install_check_arrival,
        install_ask_eta,
        install_time_window,
        install_available,
        install_ask_time,
        install_recommend,
        install_specific_date,
        install_nearest,
        install_confirm_time,
        install_reschedule,
        install_ask_callback,
        install_callback_default,
        install_decline,
        install_end,
    ],
    # The guarded unified stage (single call + deterministic post-processing)
    generate=InstallBookingUnifiedNLU(),
    # Clarify admission is module-level (the nexus-kit per-node declaration
    # collapsed into the switch the unified stage reads)
    enable_clarify=True,
    clarify_stage=InstallKeywordClarifyStage(),
    # Module-level unified-stage template override (node > module > default)
    base_nlu_prompt=INSTALL_UNIFIED_PROMPT,
)

install_booking_pattern = Pattern(
    code="install_booking",
    name="安装预约外呼助手（手绘FSM转写）",
    description=(
        "对话管理：FSM 统一阶段推进安装预约外呼——核对地址、确认到货、"
        "协商师傅上门时间（可约守卫 + 档期推荐/具体日期/最近三路）、最终"
        "确认与改约；通用拒绝与下次联系时间补充通道；相对时间先经时间"
        "增强改写为绝对时间"
    ),
    entry_module_code="install_booking",
    modules=[install_booking_module],
    # Pattern-level query slot: relative visit times are resolved to absolute
    # before the unified prompt AND before the booking guard parses them
    query=TimeAugQueryRewriter(),
)

registry.register(install_booking_pattern)
