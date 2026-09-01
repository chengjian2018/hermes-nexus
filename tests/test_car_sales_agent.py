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
