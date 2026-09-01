"""Workorder query tool — mock 维修工单查询。

Tool name: ``query_workorder``
Permission: 仅 car_sales_agent pattern 的 after_sales 模块（借出经 ModuleLink.lend_tools 授权）。
"""

from typing import Any, Dict

from src.tools.register import registry, tool_result

_MOCK_ORDERS: Dict[str, Dict[str, Any]] = {
    "京A12345": {"status": "维修中", "item": "更换刹车片", "eta": "明天 17:00"},
}


def _handle_query_workorder(args: Dict[str, Any]) -> str:
    plate = args.get("plate", "")
    order = _MOCK_ORDERS.get(plate)
    if not order:
        return tool_result({"plate": plate, "status": "未找到工单"})
    return tool_result({"plate": plate, **order})


WORKORDER_SCHEMA = {
    "name": "query_workorder",
    "description": "按车牌号查询维修工单状态、项目与预计完工时间。",
    "parameters": {
        "type": "object",
        "properties": {"plate": {"type": "string", "description": "车牌号，如 京A12345"}},
        "required": ["plate"],
    },
}

registry.register(
    name="query_workorder",
    toolset="aftersales",
    schema=WORKORDER_SCHEMA,
    handler=_handle_query_workorder,
    description="查询维修工单（模拟数据）",
    emoji="🔧",
    allowed_patterns={"car_sales_agent": ["after_sales"]},
)
