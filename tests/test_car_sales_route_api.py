"""car_sales_route（ROUTE 模式）HTTP API 集成测试。

通过 main.py 的 FastAPI 应用走 launch → chat 完整链路（TestClient 进程内调用），
LLM 使用脚本化 FakeProvider，不访问真实 API。
"""

import logging

import pytest

from fake_provider import fake_llm_config, register_fake_provider

logging.basicConfig(level=logging.WARNING)


@pytest.fixture(scope="module")
def client():
    """导入 main.py（触发工具/pattern 自动发现）并返回 TestClient。"""
    from fastapi.testclient import TestClient

    import main  # noqa: F401 —— 导入即完成 discover_builtin_tools/patterns

    return TestClient(main.app)


@pytest.fixture()
def session_id(client):
    """发起对话任务并返回 session_id。"""
    resp = client.post(
        "/api/v1/launch",
        json={
            "request_id": "req-launch-1",
            "session_id": "api-s1",
            "pattern_code": "car_sales_route",
            "task_info": {"caller": "pytest"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] is True, body["message"]
    # 注入脚本化 provider，避免走真实 LLM
    import main

    main.all_sessions["api-s1"].cxt.llm_config = fake_llm_config()
    return "api-s1"


def chat(client, session_id, query, request_id="req-chat"):
    resp = client.post(
        "/api/v1/chat",
        json={
            "request_id": request_id,
            "session_id": session_id,
            "query": query,
        },
    )
    assert resp.status_code == 200
    return resp.json()


def test_launch_unknown_pattern(client):
    """未注册的 pattern_code → 返回 404 业务码。"""
    resp = client.post(
        "/api/v1/launch",
        json={
            "request_id": "req-launch-2",
            "session_id": "api-s-bad",
            "pattern_code": "not_registered_pattern",
            "task_info": {},
        },
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["status"] is False
    assert body["code"] == "404"


def test_chat_before_launch(client):
    """未发起任务直接 chat → 返回 404 业务码。"""
    body = chat(client, "no-such-session", "你好")
    assert body["status"] is False
    assert body["code"] == "404"


def test_route_pattern_api_flow(client, session_id):
    """launch → 路由分发 → FSM 多轮，走完整 HTTP 链路。"""
    import main

    # 第 1 轮：顶层路由命中购车意图 → 静默分发，FSM 首节点同轮消化该句
    body = chat(client, session_id, "我想买车，帮忙看看车型")
    assert body["status"] is True, body["message"]
    assert "询问品牌" in body["data"]["response"]
    session = main.all_sessions[session_id]
    assert session.cxt.current_module_code == "car_sales_buy"
    assert session.cxt.current_node_code == "buy_ask_budget"
    assert session.cxt.filled_slots["brand"] == "我想买车，帮忙看看车型"

    # 第 2 轮：buy_ask_budget 消化，推进到 buy_ask_city
    body = chat(client, session_id, "比亚迪")
    assert "询问预算" in body["data"]["response"]
    assert session.cxt.current_node_code == "buy_ask_city"
    assert session.cxt.filled_slots["budget"] == "比亚迪"

    # 第 3 轮：buy_ask_city 消化，推进到 buy_confirm
    body = chat(client, session_id, "预算20万左右")
    assert "询问城市" in body["data"]["response"]
    assert session.cxt.current_node_code == "buy_confirm"
    assert session.cxt.filled_slots["city"] == "预算20万左右"
