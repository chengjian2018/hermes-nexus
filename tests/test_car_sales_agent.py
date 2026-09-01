"""car_sales_agent 演示 pattern：结构 + 三类边配置。"""


def test_pattern_discovered_and_edges():
    from src.dialogue.register import discover_builtin_patterns, registry
    imported = discover_builtin_patterns()
    assert "src.dialogue.car_sales_agent" in imported
    p = registry.get("car_sales_agent")
    assert p.entry_module_code == "reception"

    rec = p.module_map["reception"]
    links = {l.target: l for l in rec.sub_modules}
    # 全投影边（知识+工具）
    assert links["after_sales"].lend_knowledge is True
    assert "query_workorder" in links["after_sales"].lend_tools
    # 纯知识边
    assert links["sales_consult"].lend_knowledge is True
    assert links["sales_consult"].lend_tools == []
    # 纯 transfer 边
    assert links["complaint"].lend_knowledge is False

    # 转移图：专家可转回 reception
    assert "reception" in p.dispatch_graph["after_sales"]


def test_workorder_tool_lend_chain():
    """工具按 after_sales 名义注册，经 lend_tools 借给 reception。"""
    import src.tools.workorder_tool  # noqa: F401 触发注册
    from src.chat.loop import _resolve_lent_tools
    from src.dialogue.register import discover_builtin_patterns, registry

    discover_builtin_patterns()
    p = registry.get("car_sales_agent")
    rec = p.module_map["reception"]
    schemas, lent_by = _resolve_lent_tools(rec, p)
    assert "query_workorder" in lent_by
    assert lent_by["query_workorder"] == "after_sales"
    assert any(s["function"]["name"] == "query_workorder" for s in schemas)
