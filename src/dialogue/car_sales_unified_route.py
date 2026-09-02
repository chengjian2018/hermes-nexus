"""汽车销售统一阶段示例 —— 单次调用 + structured output 演示。

与 car_sales_route（默认 NLU → NLG 两阶段）的差异：
ROUTE 与 FSM 模块均注入统一阶段（unified.py），每轮对话只发起一次 LLM 调用，
回复话术与节点决策（next_node/slots）由同一次推理产出，下游跳转逻辑零改动。

Pattern 结构（Pattern → Module → Node）：

    car_sales_unified (Pattern, entry: unified_root)
    ├── unified_root (RouteModule, 统一路由)
    │   ├── u_route_root     路由根节点（sub_nodes = 意图菜单）
    │   ├── u_menu_sales     购车菜单 → jump_module: unified_buy
    │   └── u_menu_chitchat  闲聊菜单（无 jump_module，留在路由模块）
    └── unified_buy (FSMModule, 统一阶段)
        └── u_ask_brand → u_ask_budget → u_confirm(is_end)

接入方式（module 级 generate 注入，经默认骨架 GenerateSlot 槽位解析命中）：
    RouteModule(generate=RouteUnifiedNLU())
    FSMModule(generate=FSMUnifiedNLU())

候选节点的 answer_examples 在统一阶段会被拼进 prompt（next_node_pattern 槽位），
模型据此在选定节点的话术风格内直接生成回复 —— 因此本 pattern 的每个节点
都显式声明了回答范式。

注册方式：模块顶层调用 ``registry.register(Pattern(...))``，
由 ``discover_builtin_patterns()`` 通过 AST 扫描自动发现导入。
"""

from src.dialogue.module import FSMModule, RouteModule
from src.dialogue.node import BaseNode
from src.dialogue.pattern import Pattern
from src.dialogue.register import registry
from src.dialogue.unified import (
    FSMUnifiedNLU,
    RouteUnifiedNLU,
)


# ============================================================================
# RouteModule —— 顶层路由：根节点 + 意图菜单（统一阶段）
# ============================================================================

unified_root = RouteModule(
    module_code="unified_root",
    module_name="统一路由模块",
    module_description="汽车销售顶层路由：意图分类并分发到购车子流程或闲聊",
    module_todo_description="判断用户是购车咨询还是闲聊，分发到对应菜单",
    module_nodes=[
        BaseNode(
            node_code="u_route_root",
            node_name="统一路由根节点",
            node_description="汽车销售助手入口，负责顶层意图分类",
            node_todo_description="理解用户输入，匹配到购车或闲聊意图菜单；无法判断时保持根节点",
            sub_nodes=["u_menu_sales", "u_menu_chitchat"],
            answer_examples=[
                "您好，这里是汽车销售助手，请问您是想看车还是有其他问题呢？",
            ],
        ),
        BaseNode(
            node_code="u_menu_sales",
            node_name="购车菜单",
            node_description="购车咨询入口菜单",
            node_todo_description="用户有购车/看车/询价意图时选中本菜单",
            jump_module="unified_buy",
            answer_examples=[
                "您好，购车咨询为您服务！想先聊聊预算还是心仪的车型呢？",
            ],
        ),
        BaseNode(
            node_code="u_menu_chitchat",
            node_name="闲聊菜单",
            node_description="寒暄与闲聊承接",
            node_todo_description="用户打招呼或闲聊时选中本菜单",
            answer_examples=[
                "您好呀～有什么能帮到您的，随时告诉我！",
            ],
        ),
    ],
    # 统一阶段注入：一次调用完成意图分类与菜单回复生成
    generate=RouteUnifiedNLU(),
)


# ============================================================================
# FSMModule —— 购车子流程（统一阶段）：品牌 → 预算 → 确认
# ============================================================================

unified_buy = FSMModule(
    module_code="unified_buy",
    module_name="统一购车流程模块",
    module_description="购车信息收集流程：品牌 → 预算 → 确认",
    module_todo_description="按节点链收集品牌与预算，最终确认购车信息",
    module_nodes=[
        BaseNode(
            node_code="u_ask_brand",
            node_name="询问品牌",
            node_description="收集用户心仪的汽车品牌",
            node_todo_description="理解用户提到的汽车品牌并抽取 brand 槽位",
            sub_nodes=["u_ask_budget"],
            node_slots={"brand": "汽车品牌，如比亚迪、特斯拉"},
            answer_examples=[
                "好的，您对{brand}感兴趣呀！方便说下大概的预算区间吗？",
            ],
        ),
        BaseNode(
            node_code="u_ask_budget",
            node_name="询问预算",
            node_description="收集用户的购车预算区间",
            node_todo_description="理解用户提到的预算并抽取 budget 槽位",
            sub_nodes=["u_confirm"],
            node_slots={"budget": "预算区间，如20万左右、15万以内"},
            answer_examples=[
                "预算{budget}很清晰！下面帮您确认一下信息，没问题吧？",
            ],
        ),
        BaseNode(
            node_code="u_confirm",
            node_name="确认购车信息",
            node_description="向用户确认已收集的品牌与预算信息",
            node_todo_description="确认信息无误；流程到此结束",
            sub_nodes=[],
            node_slots={},
            answer_examples=[
                "为您确认：品牌{brand}，预算{budget}。信息无误的话，稍后会有顾问与您联系～",
            ],
            is_end=True,
        ),
    ],
    # 统一阶段注入：一次调用完成意图/槽位抽取与回复生成
    generate=FSMUnifiedNLU(),
)


# ============================================================================
# Pattern 注册
# ============================================================================

registry.register(
    Pattern(
        code="car_sales_unified",
        name="汽车销售统一阶段示例",
        description="ROUTE + FSM 全统一阶段接入示例：每轮单次 LLM 调用",
        entry_module_code="unified_root",
        modules=[unified_root, unified_buy],
    )
)
