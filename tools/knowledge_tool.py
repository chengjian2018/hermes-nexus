"""知识库工具组 —— 商品/客服知识检索、商品目录、商品链接文本卡片。

移植自 Customer-Agent 的 Agent/CustomerAgent/tools/（get_product_knowledge /
search_customer_service_knowledge / get_shop_products / send_goods_link），
注册机制换成 hermes-nexus 的 ``registry.register()``（AST 自动发现）。

与原版的差异（降级说明）：
- 无 trusted dependencies 注入：``account_id`` 是工具参数，由 LLM 从系统提示
  「## 任务信息」抄写（mock 服务可接受，演进路径见 ARCHITECTURE.md）
- ``list_products`` 读知识库而非拼多多 API（认证来源不存在）
- ``send_goods_link`` 返回链接文本而非副作用发送（webhook 模型只有一段回复
  文本）；保留归属校验：goods_id 必须在当前 scope 知识库存在，防 LLM 编造

权限：``allowed_patterns={"customer_agent": True}`` —— domain 工具精确授权
（customer_agent 为 Customer-Agent 整装迁移 pattern，见
dialogue/customer_agent_route.py；原 knowledge_agent 演示 pattern 已删除）。
"""

from typing import Any, Dict

from database.knowledge_store import (
    _clean_untrusted,
    get_knowledge_store,
)
from tools.register import registry, tool_error, tool_result

_SCOPE_PREFIX = "xianyu"  # 当前唯一渠道；多渠道接入时再参数化

_ACCOUNT_ID_DESC = (
    "卖家账号 ID（account_id）。必须使用系统提示「## 任务信息」中列出的值，"
    "不要编造。"
)


def _scope(account_id: str) -> str:
    return f"{_SCOPE_PREFIX}:{account_id}"


# ---------------------------------------------------------------------------
# search_product_knowledge
# ---------------------------------------------------------------------------

def _handle_search_product_knowledge(args: Dict[str, Any]) -> str:
    account_id = str(args.get("account_id") or "").strip()
    if not account_id:
        return tool_error("缺少 account_id，无法检索商品知识")
    goods_id = args.get("goods_id")
    query = args.get("query")
    if goods_id is None and not (query and str(query).strip()):
        return tool_error("请提供 goods_id 或 query 至少其一")

    if goods_id is not None:
        try:
            goods_id = int(goods_id)
        except (TypeError, ValueError):
            return tool_error("goods_id 必须是整数", goods_id=goods_id)

    store = get_knowledge_store()
    products = store.search_products(
        _scope(account_id), query=query, goods_id=goods_id)
    return store.format_result(products, [])


SEARCH_PRODUCT_SCHEMA = {
    "name": "search_product_knowledge",
    "description": (
        "检索商品知识：按 goods_id 精确查某件商品，或按关键词分词匹配商品名与"
        "知识正文。买家询问具体商品的成色/配置/电池/配件等细节时使用。"
        "account_id 取系统提示「## 任务信息」中的值。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "description": _ACCOUNT_ID_DESC},
            "goods_id": {
                "type": "integer",
                "description": "商品 ID（精确查询，与 query 二选一）",
            },
            "query": {
                "type": "string",
                "description": "搜索关键词（与 goods_id 二选一，如'阅读器 墨水屏'）",
            },
        },
        "required": ["account_id"],
    },
}


# ---------------------------------------------------------------------------
# search_customer_service_knowledge
# ---------------------------------------------------------------------------

def _handle_search_cs_knowledge(args: Dict[str, Any]) -> str:
    account_id = str(args.get("account_id") or "").strip()
    query = args.get("query")
    if not account_id:
        return tool_error("缺少 account_id，无法检索客服知识")
    if not (query and str(query).strip()):
        return tool_error("缺少 query，无法检索客服知识")

    store = get_knowledge_store()
    cs_entries = store.search_cs(_scope(account_id), query=query)
    return store.format_result([], cs_entries)


SEARCH_CS_SCHEMA = {
    "name": "search_customer_service_knowledge",
    "description": (
        "检索客服知识库：售后政策、退货换货、发货物流、验货说明、议价规则等"
        "非商品特定问题。买家问'能退货吗''什么时候发货'这类问题时使用。"
        "account_id 取系统提示「## 任务信息」中的值。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "description": _ACCOUNT_ID_DESC},
            "query": {
                "type": "string",
                "description": "搜索关键词，如'退货 政策'、'发货 时效'",
            },
        },
        "required": ["account_id", "query"],
    },
}


# ---------------------------------------------------------------------------
# list_products（降级版 get_shop_products：读知识库，无拼多多 API）
# ---------------------------------------------------------------------------

def _handle_list_products(args: Dict[str, Any]) -> str:
    account_id = str(args.get("account_id") or "").strip()
    if not account_id:
        return tool_error("缺少 account_id，无法获取商品列表")
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10

    store = get_knowledge_store()
    products = store.search_products(_scope(account_id), limit=limit)
    return store.format_catalog(products)


LIST_PRODUCTS_SCHEMA = {
    "name": "list_products",
    "description": (
        "获取店铺在售商品目录（最新 N 条）：名称、商品 ID、价格、已售。用于"
        "买家没指明具体商品、需要浏览或推荐时。商品详情请改用 "
        "search_product_knowledge。account_id 取系统提示「## 任务信息」中的值。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "description": _ACCOUNT_ID_DESC},
            "limit": {
                "type": "integer",
                "description": "返回条数，默认 10，上限 50",
            },
        },
        "required": ["account_id"],
    },
}


# ---------------------------------------------------------------------------
# send_goods_link（文本版降级：返回链接文本，由 LLM 织入回复）
# ---------------------------------------------------------------------------

def _handle_send_goods_link(args: Dict[str, Any]) -> str:
    account_id = str(args.get("account_id") or "").strip()
    if not account_id:
        return tool_error("缺少 account_id，无法生成商品链接")
    goods_id = args.get("goods_id")
    try:
        goods_id = int(goods_id)
    except (TypeError, ValueError):
        return tool_error("goods_id 必须是整数", goods_id=goods_id)

    store = get_knowledge_store()
    # 归属校验（对齐 Customer-Agent send_goods_link 的服务端校验思想）：
    # goods_id 必须存在于当前 scope 的知识库，防 LLM 编造/串号
    rows = store.search_products(_scope(account_id), goods_id=goods_id)
    if not rows:
        return tool_error(
            f"商品 ID {goods_id} 不属于当前账号的在售商品，无法生成链接。"
            "请先通过 list_products 或 search_product_knowledge 获取正确 ID")

    p = rows[0]
    name = _clean_untrusted(p.get("goods_name"), 200)
    price = _clean_untrusted(p.get("price"), 100)
    link = f"https://goofish.com/item?id={p['goods_id']}"
    card = f"【{name}】"
    if price:
        card += f" ¥{price}"
    card += f"\n{link}"
    return tool_result({
        "goods_card": card,
        "note": "这是文本卡片，请将 goods_card 内容（含链接）自然织入你的回复中发给买家",
    })


SEND_GOODS_LINK_SCHEMA = {
    "name": "send_goods_link",
    "description": (
        "为指定商品生成文本卡片（名称+价格+闲鱼商品链接），返回内容需由你"
        "织入回复发给买家。goods_id 必须是商品目录中的真实 ID，严禁使用列表"
        "序号。account_id 取系统提示「## 任务信息」中的值。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "description": _ACCOUNT_ID_DESC},
            "goods_id": {
                "type": "integer",
                "description": "商品 ID（必须来自商品目录/检索结果，不是列表序号）",
            },
        },
        "required": ["account_id", "goods_id"],
    },
}


# ---------------------------------------------------------------------------
# 自注册（模块导入即注册，AST 扫描自动发现）
# ---------------------------------------------------------------------------

_KNOWLEDGE_TOOLS = [
    (SEARCH_PRODUCT_SCHEMA, _handle_search_product_knowledge,
     "检索商品知识（goods_id 精确/关键词分词）", "🔍"),
    (SEARCH_CS_SCHEMA, _handle_search_cs_knowledge,
     "检索客服知识（售后/物流/退换货）", "📋"),
    (LIST_PRODUCTS_SCHEMA, _handle_list_products,
     "获取商品目录（最新 N 条）", "📦"),
    (SEND_GOODS_LINK_SCHEMA, _handle_send_goods_link,
     "生成商品文本卡片链接（织入回复）", "🔗"),
]

for _schema, _handler, _desc, _emoji in _KNOWLEDGE_TOOLS:
    registry.register(
        name=_schema["name"],
        toolset="knowledge",
        schema=_schema,
        handler=_handler,
        description=_desc,
        emoji=_emoji,
        allowed_patterns={"customer_agent": True},
    )
