"""knowledge_tool 单测：注册/ACL/dispatch 路径/scope 派生/归属校验。

工具读全局 get_knowledge_store()（默认 data/knowledge.db），测试用
monkeypatch 换成 tmp_path 实例，避免污染真实数据文件。
"""

import json

import pytest

from src.tools import knowledge_tool  # noqa: F401 -- import 即注册
from src.tools.knowledge_store import KnowledgeStore
from src.tools.register import registry


@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = KnowledgeStore(str(tmp_path / "kb.db"))
    s.seed("xianyu:acct_001")
    monkeypatch.setattr(knowledge_tool, "get_knowledge_store", lambda: s)
    yield s
    s.close()


def _dispatch(name: str, **args) -> str:
    return registry.dispatch(name, args)


# ---------------------------------------------------------------------------
# 注册与 pattern ACL（方向都要对）
# ---------------------------------------------------------------------------

def test_tools_registered():
    for name in ("search_product_knowledge",
                 "search_customer_service_knowledge",
                 "list_products", "send_goods_link"):
        assert registry.get_entry(name) is not None, name
        assert registry.get_toolset_for_tool(name) == "knowledge"


def test_pattern_acl_grant_and_deny():
    allowed = registry.get_allowed_tools_for_pattern("knowledge_agent", "kb_agent")
    for name in ("search_product_knowledge",
                 "search_customer_service_knowledge",
                 "list_products", "send_goods_link"):
        assert name in allowed

    # deny-by-default：其他 pattern 拿不到
    assert registry.get_allowed_tools_for_pattern("xianyu_agent", "xianyu_root") \
        & {"search_product_knowledge", "send_goods_link"} == set()


# ---------------------------------------------------------------------------
# dispatch 正常路径
# ---------------------------------------------------------------------------

def test_search_product_by_goods_id(store):
    out = _dispatch("search_product_knowledge",
                    account_id="acct_001", goods_id=1001)
    assert "iPhone 13" in out
    assert "＜untrusted_knowledge＞" in out


def test_search_product_by_query(store):
    out = _dispatch("search_product_knowledge",
                    account_id="acct_001", query="阅读器 墨水屏")
    assert "Kindle" in out


def test_search_cs(store):
    out = _dispatch("search_customer_service_knowledge",
                    account_id="acct_001", query="退货")
    assert "7 天" in out or "7天" in out
    assert "【客服知识】" in out


def test_list_products_catalog(store):
    out = _dispatch("list_products", account_id="acct_001", limit=2)
    assert "[untrusted_product_catalog]" in out
    assert "商品ID:" in out
    # 目录不带知识正文
    assert "电池健康" not in out


def test_send_goods_link_happy_path(store):
    out = _dispatch("send_goods_link", account_id="acct_001", goods_id=1002)
    payload = json.loads(out)
    assert "goofish.com/item?id=1002" in payload["goods_card"]
    assert "AirPods" in payload["goods_card"]


# ---------------------------------------------------------------------------
# dispatch 异常/边界路径
# ---------------------------------------------------------------------------

def test_missing_account_id(store):
    for name in ("search_product_knowledge", "search_customer_service_knowledge",
                 "list_products", "send_goods_link"):
        out = _dispatch(name)
        assert "error" in json.loads(out), name


def test_search_requires_goods_or_query(store):
    out = _dispatch("search_product_knowledge", account_id="acct_001")
    assert "error" in json.loads(out)


def test_send_goods_link_rejects_unknown_goods(store):
    """归属校验：不属于当前 scope 的 goods_id 拒绝（防 LLM 编造）。"""
    out = _dispatch("send_goods_link", account_id="acct_001", goods_id=999999)
    payload = json.loads(out)
    assert "error" in payload
    assert "不属于当前账号" in payload["error"]


def test_scope_derived_from_account_id(store):
    """账号隔离：acct_002 无种子数据，检索为空但不串 acct_001 的数据。"""
    out = _dispatch("search_product_knowledge",
                    account_id="acct_002", goods_id=1001)
    assert out == "未找到相关知识。"
    catalog = _dispatch("list_products", account_id="acct_002")
    assert "未找到商品" in catalog


def test_empty_result_is_not_error(store):
    out = _dispatch("search_customer_service_knowledge",
                    account_id="acct_001", query="不存在的词xyz")
    assert "error" not in out
    assert out == "未找到相关知识。"
