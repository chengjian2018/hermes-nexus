"""模版编译器 / 落盘存储 / 启动重放单测。

真实 PatternRegistry 用 tmp_ 前缀 code 并在测试后 deregister 清理。
"""

import pytest

from dialogue.module import AgentModule, FSMModule, ModuleLink, RouteModule
from dialogue.register import registry as pattern_registry
from templates.compiler import compile_template
from templates.store import TemplateStore, replay_templates, template_hash


def make_template(code="tmp_compile_demo"):
    return {
        "code": code,
        "name": "编译演示",
        "description": "mixed 形态：FSM 主流程 + Agent 兜底 + Route 菜单",
        "entry_module_code": "fsm_mod",
        "max_hops": 3,
        "counterpart_hint": {"role_prompt": "你是店家"},
        "modules": [
            {
                "module_code": "fsm_mod", "type": "fsm",
                "module_name": "主流程", "module_description": "d",
                "module_todo_description": "t",
                "sub_modules": [
                    {"target": "agent_mod", "lend_tools": []},
                    "route_mod",
                ],
                "nodes": [
                    {"node_code": "ask", "node_name": "询问",
                     "sub_nodes": ["confirm"], "node_slots": {"time": "时间"}},
                    {"node_code": "confirm", "node_name": "确认",
                     "jump_module": "agent_mod", "is_end": False},
                ],
            },
            {
                "module_code": "agent_mod", "type": "agent",
                "module_name": "兜底", "base_prompt": "你是兜底助手",
                "is_end": True,
            },
            {
                "module_code": "route_mod", "type": "route",
                "module_name": "菜单", "module_description": "菜单分发",
                "nodes": [{"node_code": "menu", "node_name": "菜单根"}],
            },
        ],
    }


@pytest.fixture(autouse=True)
def _cleanup_registry():
    yield
    for code in list(pattern_registry.list_codes()):
        if code.startswith("tmp_"):
            pattern_registry.deregister(code)


def test_compile_mixed_template():
    pattern = compile_template(make_template())

    assert pattern.code == "tmp_compile_demo"
    assert pattern.entry_module_code == "fsm_mod"
    assert pattern.max_hops == 3
    assert set(pattern.module_map) == {"fsm_mod", "agent_mod", "route_mod"}
    # 模块形态
    assert isinstance(pattern.module_map["fsm_mod"], FSMModule)
    assert isinstance(pattern.module_map["agent_mod"], AgentModule)
    assert isinstance(pattern.module_map["route_mod"], RouteModule)
    assert pattern.module_map["agent_mod"].is_end is True
    # node_map 扁平：模块 code + 全部节点 code
    assert {"fsm_mod", "agent_mod", "route_mod", "ask", "confirm", "menu"} <= set(pattern.node_map)
    # 转移边归一化为 ModuleLink（str 旧式也被 BaseModule 归一）
    links = pattern.module_map["fsm_mod"].sub_modules
    assert [l.target for l in links] == ["agent_mod", "route_mod"]
    assert all(isinstance(l, ModuleLink) for l in links)
    # 节点细节：槽位 / jump_module（kwargs 透传属性）/ is_end
    ask = pattern.node_map["ask"]
    assert ask.node_slots == {"time": "时间"} and ask.sub_nodes == ["confirm"]
    assert getattr(pattern.node_map["confirm"], "jump_module", None) == "agent_mod"
    # 元数据挂载（供任务引擎取默认对端配置）
    assert pattern.counterpart_hint == {"role_prompt": "你是店家"}


def test_register_overwrites_same_code():
    tpl = make_template()
    pattern_registry.register(compile_template(tpl))
    assert pattern_registry.get("tmp_compile_demo").name == "编译演示"

    tpl["name"] = "编译演示 v2"
    pattern_registry.register(compile_template(tpl))  # 原生覆盖
    assert pattern_registry.get("tmp_compile_demo").name == "编译演示 v2"


def test_store_roundtrip_and_hash_stability(tmp_path):
    store = TemplateStore(str(tmp_path / "templates"))
    tpl = make_template("tmp_store_demo")

    h1 = store.save(tpl)
    loaded = store.load("tmp_store_demo")
    assert loaded == tpl  # JSON 往返等价
    assert store.hash_of("tmp_store_demo") == h1
    assert h1 == template_hash(tpl)

    tpl2 = dict(tpl)
    tpl2["name"] = "改名后"
    h2 = store.save(tpl2)
    assert h2 != h1  # 内容变化 → hash 变化（Q13 对齐依据）
    assert store.list_codes() == ["tmp_store_demo"]
    assert store.summarize("tmp_store_demo")["hash"] == h2


def test_replay_registers_all(tmp_path):
    store = TemplateStore(str(tmp_path / "templates"))
    store.save(make_template("tmp_replay_a"))
    store.save(make_template("tmp_replay_b"))

    restored = replay_templates(store, pattern_registry)
    assert {"tmp_replay_a", "tmp_replay_b"} <= set(restored)
    assert pattern_registry.is_registered("tmp_replay_a")
    assert pattern_registry.get("tmp_replay_b").entry_module_code == "fsm_mod"


def test_replay_skips_broken_file(tmp_path):
    store = TemplateStore(str(tmp_path / "templates"))
    store.save(make_template("tmp_replay_good"))
    (tmp_path / "templates" / "tmp_replay_bad.json").write_text(
        "{not json", encoding="utf-8")

    restored = replay_templates(store, pattern_registry)
    assert "tmp_replay_good" in restored
    assert "tmp_replay_bad" not in restored
    assert not pattern_registry.is_registered("tmp_replay_bad")
