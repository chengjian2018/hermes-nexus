"""repair_booking pattern — the install-booking outbound-call FSM adapted
to a REPAIR scenario（自 nexus-kit apps/repair_booking_agent 迁移；场景差异
原样保留，机制完全复用 dialogue/booking_stages.py 的共享守卫层——不重新实现）。

Business background: the customer's furniture / appliance is broken; the
service desk calls THEM to book the technician's visit (the on-site repair
service).
Same outbound semantics as install_booking (the assistant is always the
caller), with three deliberate differences:

1. NO arrival subtree — the customer already owns the item (the arrival
   check / arrival-time / time-window / availability-confirmation nodes
   are all gone): after the address check the flow goes straight into
   visit-time negotiation;
2. decline intents drop the quality-complaint / return exits (the quality
   complaint IS the repair reason here, returns are out of scope) and add
   the repair-specific ones (already fixed it myself / someone else fixed
   it);
3. after the visit time is CONFIRMED the call does not close — the
   assistant keeps asking for the item's fault information (so the
   technician brings the right parts and tools), records it, and only then
   hangs up.

Flow (14 nodes, one FSM module):

    repair_greet          outbound opening     Yes→address check / No→end
    repair_confirm_addr   address check        match→time negotiation / no→end
    repair_ask_time       visit-time negotiation  specific date→repair_specific_date /
                                                  nearest→repair_nearest /
                                                  neither→recommend /
                                                  busy now→callback time
    repair_recommend      schedule recommendation  customer picks→specific date/nearest / loop back to negotiate
    repair_specific_date  specific-date booking  bookable→time confirm (guard check) / not bookable→guard reroutes to recommend
    repair_nearest        nearest-slot booking  bookable→time confirm / not bookable→guard reroutes to recommend
    repair_confirm_time   visit-time confirm   customer confirms→fault-info ask / reschedule→reschedule renegotiation
    repair_reschedule     reschedule renegotiation  re-enter time negotiation
    repair_ask_fault      fault-info ask       describes fault→fault-info confirm / can't describe→stay and keep asking
    repair_confirm_fault  fault-info confirm   restate fault key points→call end
    repair_ask_callback   next contact time    customer time→end / unusable→default 3-day reschedule
    repair_callback_default default 3-day reschedule  customer answers→call end
    repair_decline        generic decline      empathetic reply→call end
    repair_end            closing line         is_end (polite hang-up after fault info is captured)

task_info contract (injected by the launch layer): same shape as the
install pattern — product_name / address / user_name / order_id /
available_slots (the technician's bookable windows). The booking-time guard,
schedule-backed recommend rewrite and callback triage all ride the shared
machinery (rebound class attributes — the scenario subclasses below).

Known deliberate simplifications (beyond the install pattern's):
    - Fault info is only captured verbatim (the fault_description slot); it
      does not connect to a diagnosis knowledge base; when the customer
      cannot describe it clearly, the flow stays on the fault-info node and
      keeps guiding — no clarify branch.
    - No quality-complaint decline exit (the quality demand IS the repair
      demand itself).

Registration: module-level ``registry.register(Pattern(...))``, auto-discovered
by AST scan (dialogue/repair_booking_route.py).
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
# FAQ keyword table — repair question families (the customer on a repair
# call asks about fees / warranty scope / what the technician brings /
# self-fix options / progress follow-up; arrival/logistics questions no
# longer apply). Same matching contract as the install table: specific-first
# keyword containment, any-keyword hit ⇒ kb track, answers may reference
# task_info fields.
# ============================================================================

FAQ_ENTRIES = [
    {
        "topic": "费用",
        "keywords": ["收费", "费用", "多少钱", "收费吗", "要钱吗", "免费吗",
                     "额外的钱", "收钱", "上门费"],
        "answer": (
            "本次{product_name}的上门维修在保修期内是免收上门费和维修费的；"
            "保外维修师傅会先检测报价，您确认后再修，不修不收钱。"
        ),
    },
    {
        "topic": "保修",
        "keywords": ["保修", "质保", "三包", "过保", "保修期"],
        "answer": (
            "{product_name}整机按国家三包政策提供保修，保修期内非人为损坏"
            "的故障免收上门费和维修费，您放心。"
        ),
    },
    {
        "topic": "维修时长",
        "keywords": ["多久", "多长时间", "修完", "要几个小时", "几个小时",
                     "麻烦吗"],
        "answer": (
            "常规维修大概 1 个小时左右，具体要看故障情况，师傅上门检测后"
            "会先告诉您大概时间，一般不影响您正常安排。"
        ),
    },
    {
        "topic": "配件",
        "keywords": ["带配件", "带零件", "换零件", "有配件吗", "原厂件",
                     "配件多少钱"],
        "answer": (
            "师傅上门会带常用配件，检测后如需更换会先报价，您确认后再换；"
            "常用配件一般当场就能换好。"
        ),
    },
    {
        "topic": "自修咨询",
        "keywords": ["自己修", "我自己会修", "不用师傅", "自修", "指导一下怎么修"],
        "answer": (
            "电器故障不建议您自行拆修哦，安全第一；建议还是让师傅上门"
            "检测，师傅会当面排查故障原因，修不好您也可以不修。"
        ),
    },
    {
        "topic": "进度查询",
        "keywords": ["什么时候来", "几点到", "迟到", "怎么还没来", "师傅到哪了"],
        "answer": (
            "维修进度这边帮您记录反馈，师傅上门前会提前电话联系您，"
            "您留意下来电就好。"
        ),
    },
]


def match_faq(text: str):
    """Pure keyword containment against the repair FAQ table (specific-first).

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

REPAIR_UNIFIED_PROMPT = """## 任务描述
你是一位家具/电器品牌的售后客服，正在【主动外呼】一位商品需要维修的客户，
目标是在这通电话里完成上门维修服务的预约：核对地址 → 约定师傅上门时间 → 采集故障信息。
你在一次响应内同时完成「理解用户」与「生成回复」：根据当前节点与候选后续节点
判断用户意图、抽取槽位，并依据所选节点的回答范式直接生成回复话术。

## 特殊意图识别（任何节点都可能听到）
客户在流程任何阶段都可能出现以下拒绝意图，命中时跳转「通用拒绝承接」节点，不要继续推进预约：
- 不想维修 / 不需要上门维修
- 已经自己修好了
- 已经找别人修过了
- 接电话的不是本人
客户表示现在没空、暂时不想预约（但没有拒绝维修本身）时，跳转「下次联系时间」节点。

## 特殊情况：下次联系时间的答复（下次联系时间节点）
客户给出下次来电时间后，按改写结果中的时间标注判断（标注只出现在未来两周内的有效时间上）：
- 有时间标注 → 合适的联系方式时间：跳转「通话结束」，reply 复述该时间并道别；
- 无时间标注（时间太远超过两周 / 已是过去时间 / 说"都行"没给具体时间）→
  跳转「默认改约三天」节点，reply 礼貌说明改约到 3 天后左右再联系并征询客户意见。
在「默认改约三天」节点上，客户应答（同意/异议）后跳转「通话结束」完成收尾。

## 特殊情况：故障信息采集（故障信息询问节点）
上门时间确认后进入故障信息采集：询问客户的{product_name}具体是什么故障
（比如不制冷、异响、门关不严、某个部件坏了）。
- 客户描述了故障 → 记录到 fault_description 槽位，跳转「故障信息确认」节点，
  reply 复述故障要点并准备收尾；
- 客户说不清故障 → 引导描述现象（哪里响、什么时候开始的），保持当前节点继续询问；
- 故障信息确认节点上客户补充/更正 → 更新槽位后进入「通话结束」。

## 人设描述
亲切、利落、有条理的电话客服：你打给客户，先自报家门说明来意，再一步步把维修预约事项确认清楚，
最后把故障情况问明白，好让师傅带对配件工具。
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
当客户在流程中问起与预约待办无关的业务问题（例如维修是否收费、保修多久、
师傅带不带配件、能不能自己修、进度查询等），不要强行推进预约，改为输出：
{{"next_node": "clarify", "slots": {{"topic": "主题", "keywords": ["关键词1", "关键词2"]}}, "reply": "简短承接语"}}
解释：
1. topic 用短词概括客户问题的主题（如"费用"、"保修"、"配件"）。
2. keywords 列出客户问题中的关键词。
3. reply 只需简短承接（如"这个问题我说一下"），正式回答由后续澄清环节生成。
4. 仅当客户输入明显与当前节点待办无关时才使用该输出；正常回答待办问题时禁止使用。
"""


# ============================================================================
# Scenario stage subclasses — the shared guard machinery rebound to repair
# node codes and wording (no mechanism re-implemented)
# ============================================================================

class RepairRecommendNLG(ScheduleRecommendNLG):
    """Schedule-backed recommendation NLG for the repair scenario
    (deterministic, zero LLM)."""

    stage_name = "repair_recommend_nlg"

    UNBOOKABLE_LEAD = "维修师傅档期排不开了，"
    RECOMMEND_LEAD = "最近可以约 "
    RECOMMEND_TAIL = "，您看哪个时间段合适？"


class RepairBookingUnifiedNLU(BookingGuardUnifiedNLU):
    """Booking guard + schedule rewrite + callback triage, on the repair
    node graph (see dialogue/booking_stages.py)."""

    stage_name = "repair_unified"

    # Rebound node codes (the repair FSM's sketch)
    BOOKING_TARGETS = frozenset({"repair_specific_date", "repair_nearest"})
    RECOMMEND_NODE = "repair_recommend"
    CALLBACK_NODE = "repair_ask_callback"
    CALLBACK_DEFAULT_NODE = "repair_callback_default"
    END_NODE = "repair_end"

    # Repair wording for the deterministic replies
    CALLBACK_CLOSE_TEXT = "感谢您的接听，祝您生活愉快，再见！"
    CALLBACK_DEFAULT_PROPOSAL = (
        "您说的时间有点远呢，那我们先约 {day} 左右再给您来电话确认，"
        "您看可以吗？"
    )

    RECOMMEND_NLG_CLS = RepairRecommendNLG


# ============================================================================
# Phone-call-shaped clarify prompts (kb / fallback)
# ============================================================================

REPAIR_CLARIFY_KB_PROMPT = """## 任务描述
你是一位家具/电器品牌的售后客服，正在与客户进行上门维修预约的电话沟通。
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

REPAIR_CLARIFY_FALLBACK_PROMPT = """## 任务描述
你是一位家具/电器品牌的售后客服，正在与客户进行上门维修预约的电话沟通。
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

REPAIR_CLARIFY_PROMPTS = {
    "kb": REPAIR_CLARIFY_KB_PROMPT,
    "fallback": REPAIR_CLARIFY_FALLBACK_PROMPT,
}


class RepairKeywordClarifyStage(KeywordClarifyStage):
    """The keyword-gated clarify, bound to the repair FAQ table and
    prompts."""

    stage_name = "repair_clarify"

    FAQ_MATCHER = staticmethod(match_faq)
    CLARIFY_PROMPTS = REPAIR_CLARIFY_PROMPTS
    CLARIFY_FALLBACK_REPLY = (
        "抱歉，这个问题我这边确认一下。咱们继续约维修时间"
        "好吗？您什么时间方便？"
    )


# ============================================================================
# Nodes — in flow order (module_nodes[0] / repair_greet is the entry)
# ============================================================================

repair_greet = BaseNode(
    node_code="repair_greet",
    node_name="外呼开场",
    node_description=(
        "电话接通后的开场：自报家门（品牌售后客服）、说明来意（客户报修的"
        "商品需要安排师傅上门维修）、确认客户方便接听"
    ),
    node_todo_description="播报外呼开场白，确认客户是否需要上门维修服务",
    node_slots={
        "service_needed": "客户是否需要上门维修服务（是/否）",
    },
    sub_nodes=["repair_confirm_addr", "repair_end", "repair_decline"],
    answer_examples=[
        "您好，请问是{user_name}先生/女士吗？我是{product_name}品牌售后客服，"
        "看到您的报修记录，给您安排师傅上门维修，现在方便聊两句吗？",
        "您好打扰了，这里是{product_name}售后服务中心，给您来电是想帮您"
        "约师傅上门检修，您这边方便吗？",
    ],
)

repair_confirm_addr = BaseNode(
    node_code="repair_confirm_addr",
    node_name="地址核对",
    node_description="复述订单地址，请客户核对是否一致（师傅按此地址上门）",
    node_todo_description="核对上门维修地址是否一致，一致则进入时间协商",
    node_slots={
        "address_confirmed": "地址是否一致（是/否）",
        "address": "客户口径的维修地址（不一致时记录）",
    },
    # Repair scenario has no arrival step: address confirmed goes straight
    # to time negotiation
    sub_nodes=["repair_ask_time", "repair_end", "repair_decline"],
    answer_examples=[
        "先跟您核对一下地址：师傅上门是到{address}，对吗？",
        "麻烦确认下，维修地址是{address}这一处吧？",
    ],
)

repair_ask_time = BaseNode(
    node_code="repair_ask_time",
    node_name="上门时间协商",
    node_description=(
        "核心调度节点：询问客户希望师傅什么时间上门维修；说不出具体时间则"
        "主动推荐档期，给出具体日期则记录，要最近的则走最近档期；"
        "现在没空暂时不想约则转下次联系时间"
    ),
    node_todo_description="收集客户期望的上门时间，按客户口径分发到推荐/具体日期/最近/下次联系",
    node_slots={
        "visit_time": "客户期望的上门时间",
    },
    sub_nodes=["repair_recommend", "repair_specific_date", "repair_nearest",
               "repair_ask_callback", "repair_decline"],
    answer_examples=[
        "请问您希望师傅什么时间上门维修呢？方便给个大概日期或时间段吗？",
        "您哪天在家方便？我可以帮您查最近的维修档期～",
    ],
)

repair_recommend = BaseNode(
    node_code="repair_recommend",
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
    sub_nodes=["repair_specific_date", "repair_nearest", "repair_ask_time",
               "repair_decline"],
    answer_examples=[
        "要不这样，最近可以约明天上午10点或后天下午2-4点，您看哪个合适？",
        "我这边推荐周末上午的档口，师傅上门检修也从容些，您觉得呢？",
    ],
)

repair_specific_date = BaseNode(
    node_code="repair_specific_date",
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
    sub_nodes=["repair_confirm_time", "repair_ask_time", "repair_decline"],
    answer_examples=[
        "好的，那就给您约在{visit_date}{visit_hour}，师傅到时会提前联系您～",
        "收到～{visit_date}这个时间可以安排，我帮您登记上了。",
    ],
)

repair_nearest = BaseNode(
    node_code="repair_nearest",
    node_name="最近档期安排",
    node_description="客户要最近的上门时间，按最近可约档期复述确认",
    node_todo_description="给出最近可约时间并确认，客户不同意则回环重新协商",
    node_slots={
        "visit_time": "最近可约的上门时间",
    },
    sub_nodes=["repair_confirm_time", "repair_ask_time", "repair_decline"],
    answer_examples=[
        "最快可以安排最近的档期上门检修，时间临近师傅会提前联系您～",
        "帮您插了最近的维修档期，您留意下师傅的电话哦。",
    ],
)

repair_confirm_time = BaseNode(
    node_code="repair_confirm_time",
    node_name="上门时间确认",
    node_description=(
        "最终确认节点：复述锁定的上门时间与地址；确认无误后不是收尾——"
        "维修场景还需继续采集故障信息（师傅要带对配件工具）；"
        "客户此时改约则转入改约节点重新协商"
    ),
    node_todo_description="复述上门时间等待客户最终确认，确认后转故障信息采集，改约则重新协商",
    node_slots={
        "visit_time": "最终确认的上门时间",
        "address": "上门维修地址",
    },
    # Repair-scenario key difference: after confirmation → fault-info
    # collection (not a direct close)
    sub_nodes=["repair_ask_fault", "repair_reschedule", "repair_decline"],
    answer_examples=[
        "跟您最后确认下：{visit_time}师傅到{address}上门维修，没问题吧？",
        "那就定啦——{visit_time}上门，地址{address}，辛苦您届时在家等一下～",
    ],
)

repair_reschedule = BaseNode(
    node_code="repair_reschedule",
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
    sub_nodes=["repair_ask_time", "repair_decline"],
    answer_examples=[
        "没问题，改期很方便～那我们重新约一下，您什么时间方便？",
        "好的好的，原来的时间帮您取消，您看约到什么时候合适？",
    ],
)

repair_ask_fault = BaseNode(
    node_code="repair_ask_fault",
    node_name="故障信息询问",
    node_description=(
        "维修场景的收尾前置环节：上门时间确认后，询问客户商品的具体故障"
        "情况（不制冷/异响/门关不严/部件损坏等），师傅按此带对配件工具；"
        "客户说不清时留在本节点继续引导描述现象"
    ),
    node_todo_description="询问维修商品的具体故障表现，收集 fault_description 槽位",
    node_slots={
        "fault_description": "客户描述的商品故障现象",
        "fault_since": "故障出现的大致时间（可选）",
    },
    sub_nodes=["repair_confirm_fault", "repair_decline"],
    answer_examples=[
        "好的～那顺便跟您确认下，您的{product_name}具体是什么问题呢？"
        "比如哪里响、不制冷还是门关不严？",
        "为了师傅上门带对配件，您跟我说说{product_name}的故障现象呗～",
    ],
)

repair_confirm_fault = BaseNode(
    node_code="repair_confirm_fault",
    node_name="故障信息确认",
    node_description=(
        "复述采集到的故障要点请客户确认（保证报修口径准确），确认后进入"
        "通话结束——获得故障信息后才挂机（两拍收尾，与通用拒绝承接同构）"
    ),
    node_todo_description="复述故障要点等待客户确认，确认后礼貌挂机",
    node_slots={
        "fault_description": "最终确认的故障描述",
    },
    sub_nodes=["repair_end"],
    answer_examples=[
        "好的，您的{product_name}是{fault_description}对吧，我记录好了，"
        "师傅上门会带好相应配件工具～",
        "明白啦，{fault_description}这个情况我帮您登记了，师傅会提前准备，"
        "您放心～",
    ],
)

repair_ask_callback = BaseNode(
    node_code="repair_ask_callback",
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
    sub_nodes=["repair_end", "repair_callback_default", "repair_decline"],
    answer_examples=[
        "理解理解～那您看我们什么时候再联系您方便？我记一下时间。",
        "好的，那不打扰了，您方便的时候我们什么时候再打给您？",
    ],
)

repair_callback_default = BaseNode(
    node_code="repair_callback_default",
    node_name="默认改约三天",
    node_description=(
        "客户给的下次联系时间太远（超过两周）、已是过去时间、或说都行/"
        "未给出时间时，改约默认 3 天后再联系：播报默认联系时间并征询"
        "客户意见，客户应答后进入通话结束（两拍收尾；分支裁定与默认时间"
        "由统一阶段的确定性守卫给出）"
    ),
    node_todo_description="播报默认3天后再联系，等待客户应答后收尾",
    node_slots={
        "callback_time": "默认下次来电联系时间（今天+3天）",
        "callback_source": "时间来源（default=默认3天）",
    },
    sub_nodes=["repair_end"],
    answer_examples=[
        "那我们先约 3 天后左右再给您来电话确认，您看可以吗？",
        "您说的这个时间有点远呢，我们先 3 天后再联系您方便吗？",
    ],
)

repair_decline = BaseNode(
    node_code="repair_decline",
    node_name="通用拒绝承接",
    node_description=(
        "通用退出通道：客户不想维修/已自行修好/已找别人修过/非本人等意图，"
        "按场景共情回应，然后转入通话结束（维修场景没有质量问题/退货"
        "出口——质量诉求就是维修诉求本身）"
    ),
    node_todo_description="识别拒绝意图并共情回应，转通话结束",
    node_slots={
        "decline_reason": "拒绝原因（不想维修/已自修/已找别人修/非本人）",
    },
    sub_nodes=["repair_end"],
    answer_examples=[
        "好的，那就不安排上门维修了～有需要您随时联系我们，感谢接听。",
        "了解，您自己已经修好了就不打扰了，祝您使用愉快～",
        "好的，您已经安排了别的师傅，我们这边就不重复上门了，祝您生活愉快。",
        "不好意思打扰了，那我跟{user_name}先生/女士再确认时间，感谢您的接听。",
    ],
)

repair_end = BaseNode(
    node_code="repair_end",
    node_name="通话结束语",
    node_description=(
        "通话收尾：预约完成且故障信息已采集、约好下次联系、客户拒绝、"
        "地址不符等所有终止路径的礼貌收尾（is_end 终节点，进入即结束会话）"
    ),
    node_todo_description="礼貌收尾，感谢客户接听，结束通话",
    node_slots={},
    sub_nodes=[],
    is_end=True,
    answer_examples=[
        "好的，那就不打扰您了～稍后有需要随时来电，祝您生活愉快，再见～",
        "感谢您的接听与配合，那我们{visit_time}见，师傅会带好配件，祝您使用愉快，再见～",
        "好的，地址问题建议您联系卖家或平台核实哦，感谢接听，再见～",
    ],
)


# ============================================================================
# Module + pattern registration — the whole flow is one FSM module
# ============================================================================

repair_booking_module = FSMModule(
    module_code="repair_booking",
    module_name="维修预约外呼流程",
    module_description=(
        "安装场景变体的维修预约外呼：核对地址 → 协商师傅上门时间 → 最终"
        "确认 → 采集故障信息再挂机；通用拒绝与下次联系时间补充通道"
    ),
    module_todo_description="按节点链推进维修预约外呼直至收尾",
    module_nodes=[
        repair_greet,
        repair_confirm_addr,
        repair_ask_time,
        repair_recommend,
        repair_specific_date,
        repair_nearest,
        repair_confirm_time,
        repair_reschedule,
        repair_ask_fault,
        repair_confirm_fault,
        repair_ask_callback,
        repair_callback_default,
        repair_decline,
        repair_end,
    ],
    generate=RepairBookingUnifiedNLU(),
    enable_clarify=True,
    clarify_stage=RepairKeywordClarifyStage(),
    base_nlu_prompt=REPAIR_UNIFIED_PROMPT,
)

repair_booking_pattern = Pattern(
    code="repair_booking",
    name="维修预约外呼助手（安装场景变体）",
    description=(
        "对话管理：FSM 统一阶段推进维修预约外呼——核对地址、协商师傅上门"
        "时间（可约守卫 + 档期推荐/具体日期/最近三路）、最终确认与改约、"
        "确认后继续采集商品故障信息再挂机；通用拒绝与下次联系时间补充"
        "通道；相对时间先经时间增强改写为绝对时间"
    ),
    entry_module_code="repair_booking",
    modules=[repair_booking_module],
    query=TimeAugQueryRewriter(),
)

registry.register(repair_booking_pattern)
