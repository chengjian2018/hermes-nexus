"""模版 API 集成测试——validate / register / get / list 全链路（TestClient，不触发 startup）。

template_store 注入 tmp 目录版本；真实 PatternRegistry 用 tmp_ 前缀 code
并在测试后 deregister 清理。
"""

import pytest
from fastapi.testclient import TestClient

from dialogue.register import registry as pattern_registry
from templates.store import TemplateStore, template_hash


@pytest.fixture(scope="module")
def client():
    import main  # noqa: F401 -- importing it completes discovery + router mounting
    return TestClient(main.app)


@pytest.fixture()
def tpl_store(tmp_path):
    import main

    s = TemplateStore(str(tmp_path / "tpl"))
    prev = main.template_store
    main.template_store = s
    yield s
    main.template_store = prev


@pytest.fixture(autouse=True)
def _registry_cleanup():
    yield
    for code in list(pattern_registry.list_codes()):
        if code.startswith("tmp_"):
            pattern_registry.deregister(code)


def make_template(code="tmp_api_demo"):
    return {
        "code": code,
        "name": "API 演示",
        "description": "接口测试模版",
        "entry_module_code": "talk",
        "counterpart_hint": {"role_prompt": "你是店家", "scripted_replies": ["在的"]},
        "modules": [
            {"module_code": "talk", "type": "agent",
             "module_name": "洽谈", "base_prompt": "你是代聊助手",
             "sub_modules": ["bye"]},
            {"module_code": "bye", "type": "agent",
             "module_name": "收尾", "base_prompt": "道谢收尾", "is_end": True},
        ],
    }


def validate(client, tpl):
    return client.post("/api/v1/templates/validate", json={"template": tpl}).json()


def register(client, tpl):
    return client.post("/api/v1/templates", json={"template": tpl}).json()


def test_validate_ok_and_fail_collect_all(client, tpl_store):
    ok = validate(client, make_template())
    assert ok["code"] == "0" and ok["data"]["ok"] is True

    broken = make_template()
    broken["entry_module_code"] = "ghost"      # ENTRY_MISSING
    broken["modules"][0]["sub_modules"] = ["ghost2"]  # DANGLING_EDGE
    body = validate(client, broken)
    assert body["code"] == "400" and body["data"]["ok"] is False
    got = {e["code"] for e in body["data"]["errors"]}
    assert {"ENTRY_MISSING", "DANGLING_EDGE"} <= got


def test_register_get_hash_and_overwrite(client, tpl_store):
    tpl = make_template()
    body = register(client, tpl)
    assert body["code"] == "0", body["message"]
    assert body["data"]["hash"] == template_hash(tpl)
    assert pattern_registry.is_registered("tmp_api_demo")

    # GET：模版来源 + hash 对齐（Q13）
    got = client.get("/api/v1/templates/tmp_api_demo").json()
    assert got["code"] == "0"
    assert got["data"]["exists"] is True and got["data"]["source"] == "template"
    assert got["data"]["hash"] == template_hash(tpl)

    # 覆盖重注册（Q9）：内容变化 → hash 变化，registry 换新对象
    tpl["name"] = "API 演示 v2"
    body2 = register(client, tpl)
    assert body2["code"] == "0"
    assert body2["data"]["hash"] != body["data"]["hash"]
    assert pattern_registry.get("tmp_api_demo").name == "API 演示 v2"


def test_register_invalid_rejected_not_persisted(client, tpl_store):
    broken = make_template()
    broken["modules"][1]["is_end"] = "yes"  # TYPE_ERROR
    body = register(client, broken)
    assert body["code"] == "400"
    assert not pattern_registry.is_registered("tmp_api_demo")
    assert client.get("/api/v1/templates/tmp_api_demo").json()["code"] == "404"


def test_builtin_conflict_and_lookup(client, tpl_store):
    # 内置 pattern 查询：exists + builtin（子 skill 应报冲突而非覆盖）
    got = client.get("/api/v1/templates/customer_agent").json()
    assert got["code"] == "0"
    assert got["data"]["source"] == "builtin" and got["data"]["hash"] is None

    # 注册撞内置 code：拒绝
    body = register(client, make_template(code="customer_agent"))
    assert body["code"] == "400"
    assert "BUILTIN_CONFLICT" in {e["code"] for e in body["data"]["errors"]}


def test_list_templates(client, tpl_store):
    register(client, make_template())
    body = client.get("/api/v1/templates").json()
    assert body["code"] == "0"
    codes = [t["code"] for t in body["data"]["templates"]]
    assert "tmp_api_demo" in codes


def test_save_leaves_no_tmp_residue(tmp_path):
    """落盘原子写：uuid 后缀 tmp 不残留、list_codes 不误收 tmp 文件。"""
    s = TemplateStore(str(tmp_path / "tpl"))
    s.save(make_template())
    s.save(make_template())  # 二次覆盖同 code

    files = sorted(p.name for p in (tmp_path / "tpl").iterdir())
    assert files == ["tmp_api_demo.json"]
    assert s.list_codes() == ["tmp_api_demo"]
