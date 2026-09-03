"""
汽车销售 Route 模式 —— 顶层路由（ROUTE）＋ 意图菜单 ＋ FSM 子模块分发。

Pattern 结构（Pattern → Module → Node）：:

    car_sales_route (Pattern, entry: car_sales_root)
    ├── car_sales_root (RouteModule)
    │   ├── route_root         路由根节点（sub_nodes = 意图菜单）
    │   ├── menu_sales         购车咨询菜单 → jump_module: car_sales_buy
    │   ├── menu_after         售后咨询菜单 → jump_module: car_sales_after
    │   └── menu_chitchat      闲聊菜单   → 无 jump_module，留在路由模块
    ├── car_sales_buy (FSMModule)
    │   └── buy_ask_brand → buy_ask_budget → buy_ask_city → buy_confirm
    └── car_sales_after (FSMModule)
        └── after_ask_issue → after_ask_vehicle → after_confirm

对话流程：
    1. 会话入口是 RouteModule 的路由根节点
    2. RouteNLU 在根节点做顶层意图分类，输出 next_node（某个意图菜单节点）
    3. route_advance 阶段把当前节点切到命中的菜单节点，RouteNLG 按该节点的回答范式生成回复
    4. 节点转换时读取菜单节点的 jump_module 属性：
       - 有 jump_module → 跳转到对应子模块（下一轮从子模块首节点开始 FSM 流程）
       - 无 jump_module → 重置回路由根节点，下一轮继续路由（如闲聊）

注册方式：模块顶层调用 ``registry.register(Pattern(...))``，
由 ``discover_builtin_patterns()`` 通过 AST 扫描自动发现导入。
"""

from src.clarify import ClarifyRouteRule, ClarifyStage
from src.dialogue.module import FSMModule, RouteModule
from src.dialogue.recaller.recaller import (
    KeywordRecallPath,
    MultiPathRecaller,
    ScoreThresholdFilter,
    WeightedScoreFusion,
)
from src.dialogue.node import BaseNode
from src.dialogue.pattern import Pattern
from src.dialogue.register import registry


# ============================================================================
# RouteModule —— 顶层路由：根节点 + 意图菜单
#
# 字段消费关系（node 层按 stage 输出不同 facet）：
#   - node_description      → NLG cur_node（回复场景）+ 后续节点列表（根节点 NLU 意图匹配）
#   - node_todo_description → NLU cur_node（理解任务：判断什么/抽什么）
#   - node_slots            → NLU cur_node（槽位抽取模板）
# ============================================================================

route_root = BaseNode(
    node_code="route_root",
    node_name="路由根节点",
    node_description="汽车服务助手总入口，覆盖购车咨询、售后咨询与日常沟通三大场景",
    node_todo_description="识别用户顶层意图，分发到购车咨询/售后咨询/闲聊对应菜单节点",
    sub_nodes=["menu_sales", "menu_after", "menu_chitchat"],
    answer_examples=[
        "您好，我是您的专属汽车服务助手，可以帮您解决购车、试驾、询价以及售后保养等问题，请问您想了解哪方面呢？",
    ],
)

menu_sales = BaseNode(
    node_code="menu_sales",
    node_name="购车咨询",
    node_description="用户想买车、看车、试驾、询价、了解车型配置或贷款方案",
    node_todo_description="命中购车意图，跳转到购车咨询子模块",
    sub_nodes=[],
    jump_module="car_sales_buy",  # 分发目标子模块
    answer_examples=[
        "好的，很高兴为您提供购车服务！请问您想了解哪个品牌或车型呢？",
    ],
)

menu_after = BaseNode(
    node_code="menu_after",
    node_name="售后咨询",
    node_description="用户想咨询售后保养、维修、保险理赔、投诉或道路救援",
    node_todo_description="命中售后意图，跳转到售后咨询子模块",
    sub_nodes=[],
    jump_module="car_sales_after",  # 分发目标子模块
    answer_examples=[
        "好的，已为您转接售后服务。请问您遇到的是什么问题呢？",
    ],
)

menu_chitchat = BaseNode(
    node_code="menu_chitchat",
    node_name="闲聊",
    node_description="用户进行问候、致谢、闲聊或告别，与汽车业务无关",
    node_todo_description="命中闲聊意图，留在路由模块内处理",
    sub_nodes=[],
    answer_examples=[
        "您好呀！很高兴为您服务。请问有什么可以帮您的吗？",
    ],
)

car_sales_root = RouteModule(
    module_code="car_sales_root",
    module_name="汽车销售总路由",
    module_description="顶层路由模块：识别用户意图并分发到购车、售后或闲聊",
    module_todo_description="识别用户顶层意图，分发到对应菜单节点/子模块",
    module_nodes=[route_root, menu_sales, menu_after, menu_chitchat],
)


# ============================================================================
# FSMModule —— 购车咨询：品牌 → 预算 → 城市 → 确认
#
# NLG（cur_node=name+desc）负责「怎么问」，NLU（cur_node=name+todo+slots）负责「抽什么」：
#   - node_description 面向用户回复场景，node_todo_description 面向槽位抽取任务
# ============================================================================

buy_ask_brand = BaseNode(
    node_code="buy_ask_brand",
    node_name="询问品牌",
    node_description="用户正在选购车辆，需要明确品牌/车型偏好后才能进一步推荐",
    node_todo_description="从用户输入中抽取品牌/车型偏好，并流转到询问预算节点",
    node_slots={"brand": "品牌或车型，如 比亚迪汉"},
    sub_nodes=["buy_ask_budget"],
    answer_examples=[
        "请问您心仪的品牌或车型是什么呢？比如比亚迪汉、特斯拉 Model 3 等。",
    ],
)

buy_ask_budget = BaseNode(
    node_code="buy_ask_budget",
    node_name="询问预算",
    node_description="品牌偏好已明确，需要预算范围来筛选可推荐的车款",
    node_todo_description="从用户输入中抽取预算范围，并流转到询问城市节点",
    node_slots={"budget": "预算范围，如 15-20万"},
    sub_nodes=["buy_ask_city"],
    answer_examples=[
        "好的，{brand} 确实是不错的选择。请问您的购车预算大概是多少呢？",
    ],
)

buy_ask_city = BaseNode(
    node_code="buy_ask_city",
    node_name="询问城市",
    node_description="品牌与预算已明确，需要用户所在城市来推荐附近经销商",
    node_todo_description="从用户输入中抽取城市/地区，并流转到确认节点",
    node_slots={"city": "城市或地区，如 北京"},
    sub_nodes=["buy_confirm"],
    answer_examples=[
        "收到，预算 {budget} 左右。请问您在哪个城市呢？方便为您推荐附近的经销商。",
    ],
)

buy_confirm = BaseNode(
    node_code="buy_confirm",
    node_name="确认购车信息",
    node_description="三项购车信息已收集齐全，向用户做最终确认并收尾",
    node_todo_description="汇总校验品牌/预算/城市三要素并结束流程，不再抽取新槽位",
    node_slots={},
    sub_nodes=[],
    is_end=True,
    answer_examples=[
        "好的，已为您记录购车需求：品牌 {brand}，预算 {budget}，城市 {city}。稍后会有销售顾问与您联系，请保持电话畅通。",
    ],
)

_KB_DOCS = [
    {"id": "fee_policy",
     "content": "除车价外仅收取上牌费与服务费，无其他收费",
     "metadata": {"keywords": ["收费", "服务费", "上牌费"]}},
]

car_sales_buy = FSMModule(
    module_code="car_sales_buy",
    module_name="购车咨询",
    module_description="收集品牌、预算、城市等信息，提供购车/试驾咨询服务",
    module_todo_description="按顺序收集购车三要素（品牌、预算、城市）并确认",
    module_nodes=[buy_ask_brand, buy_ask_budget, buy_ask_city, buy_confirm],
    enable_clarify=True,
    clarify_stage=ClarifyStage(
        recaller=MultiPathRecaller(
            recall_paths=[KeywordRecallPath(name="kb", documents=_KB_DOCS)],
            filters=[ScoreThresholdFilter(threshold=0.1)],
            fusion=WeightedScoreFusion(),
        ),
        rule=ClarifyRouteRule(),
    ),
)


# ============================================================================
# FSMModule —— 售后咨询：问题 → 车辆信息 → 确认
# ============================================================================

after_ask_issue = BaseNode(
    node_code="after_ask_issue",
    node_name="询问问题类型",
    node_description="用户带着售后诉求进入咨询，需要先明确问题类型才能分派处理",
    node_todo_description="从用户输入中抽取售后问题类型，并流转到询问车辆信息节点",
    node_slots={"issue_type": "问题类型，如 保养/维修/保险理赔/投诉"},
    sub_nodes=["after_ask_vehicle"],
    answer_examples=[
        "请问您需要什么售后服务呢？比如保养预约、维修、保险理赔或者投诉建议。",
    ],
)

after_ask_vehicle = BaseNode(
    node_code="after_ask_vehicle",
    node_name="询问车辆信息",
    node_description="问题类型已明确，需要核实车辆信息后才能登记处理",
    node_todo_description="从用户输入中抽取车辆信息（品牌车型、车牌号），并流转到确认节点",
    node_slots={"car_info": "车辆信息，如 比亚迪汉 京A12345"},
    sub_nodes=["after_confirm"],
    answer_examples=[
        "好的，关于{issue_type}，麻烦提供一下您的车辆信息（品牌车型、车牌号），方便我们核实处理。",
    ],
)

after_confirm = BaseNode(
    node_code="after_confirm",
    node_name="确认售后信息",
    node_description="售后问题与车辆信息已收集齐全，向用户做最终确认并收尾",
    node_todo_description="汇总校验问题类型与车辆信息并结束流程，不再抽取新槽位",
    node_slots={},
    sub_nodes=[],
    is_end=True,
    answer_examples=[
        "已为您登记售后需求：{issue_type}，车辆信息 {car_info}。售后专员会尽快与您联系，感谢您的耐心等待。",
    ],
)

car_sales_after = FSMModule(
    module_code="car_sales_after",
    module_name="售后咨询",
    module_description="收集售后问题类型与车辆信息，提供售后服务登记",
    module_todo_description="按顺序收集售后问题与车辆信息并确认",
    module_nodes=[after_ask_issue, after_ask_vehicle, after_confirm],
)


# ============================================================================
# Pattern 注册 —— 顶层 registry.register，由 AST 扫描自动发现
# ============================================================================

car_sales_route_pattern = Pattern(
    code="car_sales_route",
    name="汽车销售路由助手",
    description="顶层路由模式：入口 RouteModule 按意图分发到购车/售后 FSM 子模块，闲聊留在路由模块处理",
    entry_module_code="car_sales_root",
    modules=[car_sales_root, car_sales_buy, car_sales_after],
)

registry.register(car_sales_route_pattern)
