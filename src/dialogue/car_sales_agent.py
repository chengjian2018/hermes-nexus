"""汽车销售纯 Agent 模式 —— inject/transfer 双原语演示。

结构：
    car_sales_agent (Pattern, entry: reception)
    ├── reception        前台接待（分诊 + 邻接投影代答）
    │     ├── → after_sales    全投影边（知识 + query_workorder 工具）
    │     ├── → sales_consult  纯知识边
    │     └── → complaint      纯 transfer 边（敏感域，不投影）
    ├── sales_consult    购车顾问（可转回 reception）
    ├── after_sales      售后维保（query_workorder；可转回 reception）
    └── complaint        投诉处理（终态，无出边）
"""

from src.dialogue.module import AgentModule, ModuleLink
from src.dialogue.pattern import Pattern
from src.dialogue.register import registry

sales_consult = AgentModule(
    module_code="sales_consult",
    module_name="购车顾问",
    module_description="车型推荐、报价比较、贷款方案咨询",
    module_todo_description="理解购车需求并给出推荐与报价",
    answer_examples=["推荐您看看{model}，目前优惠后 {price} 万，很适合您的需求。"],
    use_tools=[],
    sub_modules=["reception"],
    base_prompt="你是 4S 店购车顾问，热情专业，只谈购车相关话题。",
)

after_sales = AgentModule(
    module_code="after_sales",
    module_name="售后维保",
    module_description="保养预约、维修工单、保险理赔的查询与办理",
    module_todo_description="查改保养预约、跟踪维修工单进度",
    answer_examples=["已为您把保养预约改到{时间}，请按时到店。"],
    use_tools=["query_workorder"],
    sub_modules=["reception"],
    base_prompt="你是 4S 店售后顾问，耐心细致，负责保养预约与维修工单。",
)

complaint = AgentModule(
    module_code="complaint",
    module_name="投诉处理",
    module_description="客户不满受理、安抚与人工升级",
    module_todo_description="记录投诉内容并升级人工专员",
    answer_examples=["非常抱歉给您带来不便，已为您记录并升级专员处理。"],
    use_tools=[],
    sub_modules=[],
    base_prompt="你是投诉处理专员，先安抚情绪，再记录投诉并告知将升级人工。",
)

reception = AgentModule(
    module_code="reception",
    module_name="前台接待",
    module_description="接待客户，解答通用问题，按需分诊到专家",
    module_todo_description="识别客户诉求：能直接答的直接答，需要专家的转交",
    answer_examples=["您好，请问有什么可以帮您？"],
    use_tools=[],
    sub_modules=[
        ModuleLink(target="after_sales",
                   lend_knowledge=True,
                   lend_tools=["query_workorder"]),
        ModuleLink(target="sales_consult",
                   lend_knowledge=True, lend_tools=[]),
        ModuleLink(target="complaint",
                   lend_knowledge=False, lend_tools=[]),
    ],
    base_prompt="你是 4S 店前台接待，友好高效，覆盖购车与售后的通用咨询。",
)

car_sales_agent_pattern = Pattern(
    code="car_sales_agent",
    name="汽车销售 Agent 助手",
    description="纯 agent 模式：前台分诊 + inject 代答 + transfer 深入移交",
    entry_module_code="reception",
    modules=[reception, sales_consult, after_sales, complaint],
)

registry.register(car_sales_agent_pattern)
