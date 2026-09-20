"""模版校验器单测——collect-all 三层校验（schema / 结构 / 引用）。

registry 用打桩对象注入（validate_template 的可注入参数），不污染真实单例。
"""

import pytest

from templates.validator import validate_template


class FakeToolRegistry:
    def __init__(self, tools=(), acl=()):
        self._tools = set(tools)
        self._acl = set(acl)

    def get_entry(self, name):
        return object() if name in self._tools else None

    def get_allowed_tools_for_pattern(self, pattern_code, module_code=""):
        return set(self._acl)


class FakePatternRegistry:
    def __init__(self, codes=()):
        self._codes = set(codes)

    def is_registered(self, code):
        return code in self._codes


def make_template(**over):
    tpl = {
        "code": "tmp_demo",
        "name": "演示模版",
        "description": "测试用话术模版",
        "entry_module_code": "main_mod",
        "recommended_form": "mixed",
        "counterpart_hint": {"role_prompt": "你是忙碌的店家"},
        "modules": [
            {
                "module_code": "main_mod", "type": "fsm",
                "module_name": "主流程", "module_description": "d",
                "module_todo_description": "t",
                "sub_modules": ["end_mod"],
                "nodes": [
                    {"node_code": "ask", "node_name": "询问", "sub_nodes": ["done"]},
                    {"node_code": "done", "node_name": "完成", "is_end": True},
                ],
            },
            {
                "module_code": "end_mod", "type": "agent",
                "module_name": "收尾", "base_prompt": "你是收尾助手",
                "is_end": True,
            },
        ],
    }
    tpl.update(over)
    return tpl


def codes_of(result):
    return {i.code for i in result.errors} | {i.code for i in result.warnings}


def test_valid_template_ok():
    result = validate_template(make_template(),
                               pattern_registry=FakePatternRegistry(),
                               tool_registry=FakeToolRegistry())
    assert result.ok, [i.to_dict() for i in result.errors]
    assert not codes_of(result) & {"UNREACHABLE", "NO_TERMINAL"}


def test_collect_all_returns_every_error():
    """一次注入多个错误：全部收集（非 fail-fast）且带路径定位。"""
    tpl = make_template()
    del tpl["name"]                       # MISSING_FIELD
    tpl["code"] = "Tmp-Bad"               # CODE_FORMAT
    tpl["entry_module_code"] = "nope"     # ENTRY_MISSING
    tpl["modules"][0]["sub_modules"] = [{"target": "ghost", "lend_tools": ["x"]}]  # DANGLING_EDGE
    tpl["modules"][1]["use_tools"] = ["no_such_tool"]  # TOOL_UNKNOWN

    result = validate_template(tpl,
                               pattern_registry=FakePatternRegistry(),
                               tool_registry=FakeToolRegistry())
    assert not result.ok
    got = {i.code for i in result.errors}
    assert {"MISSING_FIELD", "CODE_FORMAT", "ENTRY_MISSING",
            "DANGLING_EDGE", "TOOL_UNKNOWN"} <= got
    # 路径定位
    paths = {i.path for i in result.errors}
    assert "name" in paths
    assert any(p.startswith("modules[0].sub_modules[0]") for p in paths)
    assert any(p.startswith("modules[1].use_tools[0]") for p in paths)


def test_builtin_conflict_error_and_template_exempt():
    reg = FakePatternRegistry(["customer_agent"])
    conflict = validate_template(make_template(code="customer_agent"),
                                 pattern_registry=reg,
                                 tool_registry=FakeToolRegistry())
    assert "BUILTIN_CONFLICT" in {i.code for i in conflict.errors}

    exempt = validate_template(make_template(code="customer_agent"),
                               pattern_registry=reg,
                               tool_registry=FakeToolRegistry(),
                               template_codes={"customer_agent"})
    assert "BUILTIN_CONFLICT" not in {i.code for i in exempt.errors}


def test_tool_acl_only_warns():
    tpl = make_template()
    tpl["modules"][1]["use_tools"] = ["calculator"]
    result = validate_template(tpl,
                               pattern_registry=FakePatternRegistry(),
                               tool_registry=FakeToolRegistry(tools=["calculator"]))
    assert result.ok  # 工具存在：不阻断
    assert "TOOL_ACL" in {i.code for i in result.warnings}
    assert "TOOL_UNKNOWN" not in {i.code for i in result.errors}


def test_node_edges_and_jump_module():
    tpl = make_template()
    tpl["modules"][0]["nodes"][0]["sub_nodes"] = ["ask", "ghost"]
    tpl["modules"][0]["nodes"][0]["jump_module"] = "main_mod"  # 模块自环
    tpl["modules"][0]["nodes"][1]["jump_module"] = "ghost_mod"
    result = validate_template(tpl,
                               pattern_registry=FakePatternRegistry(),
                               tool_registry=FakeToolRegistry())
    got = {(i.code, i.path) for i in result.errors}
    assert any(c == "DANGLING_EDGE" and "nodes[0].sub_nodes[1]" in p for c, p in got)
    assert any(c == "SELF_LOOP" and "jump_module" in p for c, p in got)
    assert any(c == "DANGLING_EDGE" and "nodes[1].jump_module" in p for c, p in got)


def test_unauthorized_lend():
    tpl = make_template()
    tpl["modules"][0]["sub_modules"] = [{"target": "end_mod", "lend_tools": ["calc"]}]
    result = validate_template(tpl,
                               pattern_registry=FakePatternRegistry(),
                               tool_registry=FakeToolRegistry(tools=["calc"]))
    assert "UNAUTHORIZED_LEND" in {i.code for i in result.errors}


def test_unreachable_and_no_terminal_warn():
    tpl = make_template()
    tpl["modules"][0]["sub_modules"] = []
    tpl["modules"][0]["nodes"][1]["is_end"] = False
    tpl["modules"][1]["is_end"] = False
    result = validate_template(tpl,
                               pattern_registry=FakePatternRegistry(),
                               tool_registry=FakeToolRegistry())
    assert result.ok  # 均为 warning 级
    got = {i.code for i in result.warnings}
    assert "UNREACHABLE" in got and "NO_TERMINAL" in got


def test_normalize_drops_unsupported_fields():
    tpl = make_template()
    tpl["generate"] = {"nlu": "oops"}
    tpl["modules"][1]["messages_builder"] = "some_function"
    tpl["mystery"] = 1
    result = validate_template(tpl,
                               pattern_registry=FakePatternRegistry(),
                               tool_registry=FakeToolRegistry())
    assert result.ok
    warnings = " ".join(i.message for i in result.warnings)
    assert "generate" in warnings and "messages_builder" in warnings
    assert "mystery" in warnings
    # 剔除后编译输入不再携带这些字段（normalize 已在 validate 内先行执行）
    from templates.schema import normalize_template
    norm, _ = normalize_template(tpl)
    assert "generate" not in norm and "mystery" not in norm
    assert "messages_builder" not in norm["modules"][1]


def test_duplicate_node_code_against_module_code():
    tpl = make_template()
    tpl["modules"][0]["nodes"][0]["node_code"] = "end_mod"  # 撞模块 code（node_map 扁平）
    result = validate_template(tpl,
                               pattern_registry=FakePatternRegistry(),
                               tool_registry=FakeToolRegistry())
    assert "DUPLICATE_CODE" in {i.code for i in result.errors}


def test_pattern_llm_cross_check_warns():
    tpl = make_template()
    config = {"pattern_llm": {"tmp_demo": {
        "modules": {"ghost_mod": {"temperature": 0.3}},
        "nodes": {"ask": {"temperature": 0.1}},
    }}}
    result = validate_template(tpl,
                               pattern_registry=FakePatternRegistry(),
                               tool_registry=FakeToolRegistry(),
                               config=config)
    assert result.ok
    msgs = [i.message for i in result.warnings if i.code == "PATTERN_LLM_MISMATCH"]
    assert len(msgs) == 1 and "ghost_mod" in msgs[0]
