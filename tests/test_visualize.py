"""visualize 模块测试 -- 纯离线，只做结构渲染断言，不依赖 LLM。"""

import pytest

from src.dialogue import visualize
from src.dialogue.register import discover_builtin_patterns, registry

# car_sales_route 的全部节点 code（见 src/dialogue/car_sales_route.py）
ALL_NODE_CODES = [
    "route_root", "menu_sales", "menu_after", "menu_chitchat",
    "buy_ask_brand", "buy_ask_budget", "buy_ask_city", "buy_confirm",
    "after_ask_issue", "after_ask_vehicle", "after_confirm",
]


@pytest.fixture(scope="module")
def car_sales():
    discover_builtin_patterns()
    pattern = registry.get("car_sales_route")
    assert pattern is not None, "car_sales_route 未注册"
    return pattern


@pytest.fixture(scope="module")
def mermaid(car_sales):
    return visualize.pattern_to_mermaid(car_sales)


# ============================================================================
# Mermaid 结构断言
# ============================================================================

class TestMermaid:
    def test_flowchart_header_and_start(self, mermaid):
        assert mermaid.startswith("flowchart TB")
        assert 'START(("⏵ 开始"))' in mermaid

    def test_modules_rendered_as_subgraphs(self, mermaid):
        for code in ("car_sales_root", "car_sales_buy", "car_sales_after"):
            assert f"subgraph m_{code} [" in mermaid
        # 入口模块排在最前
        assert mermaid.index("m_car_sales_root") < mermaid.index("m_car_sales_buy")

    def test_module_type_in_title(self, mermaid):
        assert "(ROUTE)" in mermaid
        assert "(FSM)" in mermaid

    def test_all_nodes_rendered(self, mermaid):
        for code in ALL_NODE_CODES:
            assert f"n_{code}[" in mermaid

    def test_entry_edge_from_start(self, mermaid):
        assert "START --> n_route_root" in mermaid

    def test_fsm_edges(self, mermaid):
        expected = [
            "n_route_root --> n_menu_sales",
            "n_route_root --> n_menu_after",
            "n_route_root --> n_menu_chitchat",
            "n_buy_ask_brand --> n_buy_ask_budget",
            "n_buy_ask_budget --> n_buy_ask_city",
            "n_buy_ask_city --> n_buy_confirm",
            "n_after_ask_issue --> n_after_ask_vehicle",
            "n_after_ask_vehicle --> n_after_confirm",
        ]
        for edge in expected:
            assert edge in mermaid

    def test_jump_module_edges(self, mermaid):
        assert "n_menu_sales -.->|jump_module| n_buy_ask_brand" in mermaid
        assert "n_menu_after -.->|jump_module| n_after_ask_issue" in mermaid

    def test_route_reset_edge(self, mermaid):
        # menu_chitchat 无 jump_module，重置回路由根节点
        assert "n_menu_chitchat -.->|重置回根| n_route_root" in mermaid

    def test_end_node_class(self, mermaid):
        assert "class n_buy_confirm nodeEnd" in mermaid
        assert "class n_after_confirm nodeEnd" in mermaid
        assert "classDef nodeEnd" in mermaid

    def test_slots_in_label(self, mermaid):
        assert "slots: brand" in mermaid
        assert "slots: budget" in mermaid

    def test_module_styles_by_type(self, mermaid):
        assert "style m_car_sales_root fill:#eff6ff,stroke:#3b82f6,stroke-width:3px" in mermaid
        assert "style m_car_sales_buy fill:#f0fdf4,stroke:#16a34a" in mermaid

    def test_label_escaping(self, mermaid):
        # 标签行成对引号闭合，不会破坏 mermaid 语法
        assert mermaid.count('["') == mermaid.count('"]')

    def test_escape_label_helper(self):
        assert visualize._escape_label('含"引号"') == "含#quot;引号#quot;"
        assert visualize._escape_label("a\nb") == "a<br/>b"
        assert visualize._escape_label(None) == ""


# ============================================================================
# HTML / Markdown 渲染断言
# ============================================================================

class TestRenderers:
    def test_html_basic(self, car_sales):
        out = visualize.render_pattern_html(car_sales)
        assert out.startswith("<!DOCTYPE html>")
        assert "汽车销售路由助手" in out
        assert "car_sales_route" in out
        assert 'class="mermaid"' in out
        # mermaid 源码经 HTML 转义后嵌入
        assert "flowchart TB" in out
        # CDN 多源回退
        assert "cdn.jsdelivr.net" in out
        assert "registry.npmmirror.com" in out
        # 渲染失败降级区块
        assert 'id="fallback"' in out

    def test_html_module_details(self, car_sales):
        out = visualize.render_pattern_html(car_sales)
        for code in ALL_NODE_CODES:
            assert code in out
        # 模块卡片与类型徽标
        assert '<span class="badge route">ROUTE</span>' in out
        assert '<span class="badge fsm">FSM</span>' in out
        # 节点表格含槽位与跳转信息
        assert "brand" in out
        assert "car_sales_buy" in out

    def test_markdown_basic(self, car_sales):
        out = visualize.render_pattern_markdown(car_sales)
        assert out.startswith("# Pattern: 汽车销售路由助手 (`car_sales_route`)")
        assert "```mermaid" in out
        assert "## 模块与节点详情" in out
        # 详情表包含全部节点
        for code in ALL_NODE_CODES:
            assert code in out

    def test_render_pattern_dispatch(self, car_sales):
        assert visualize.render_pattern(car_sales, "mermaid").startswith("flowchart TB")
        assert visualize.render_pattern(car_sales, "md").startswith("# Pattern:")
        assert visualize.render_pattern(car_sales, "html").startswith("<!DOCTYPE html>")
        with pytest.raises(ValueError):
            visualize.render_pattern(car_sales, "nope")


# ============================================================================
# AGENT 模块（无节点）渲染
# ============================================================================

class TestAgentModule:
    def test_agent_module_renders_representative_node(self):
        from src.dialogue.module import AgentModule
        from src.dialogue.pattern import Pattern

        pattern = Pattern(
            code="t_agent",
            name="agent 测试",
            description="agent only",
            entry_module_code="t_chat",
            modules=[
                AgentModule(module_code="t_chat", module_name="闲聊模块", base_prompt="你是客服"),
            ],
        )
        m = visualize.pattern_to_mermaid(pattern)
        # 无节点模块渲染 Agent 代表节点，入口边指向它
        assert "n_t_chat__agent[" in m
        assert "Agent 对话" in m
        assert "START --> n_t_chat__agent" in m
        assert "class n_t_chat__agent nodeAgent" in m
        assert "(AGENT)" in m


# ============================================================================
# CLI 断言
# ============================================================================

class TestCli:
    def test_list(self, capsys):
        assert visualize.main(["--list"]) == 0
        assert "car_sales_route" in capsys.readouterr().out

    def test_write_html_to_custom_path(self, car_sales, tmp_path):
        out_file = tmp_path / "diagram.html"
        assert visualize.main(["car_sales_route", "-o", str(out_file)]) == 0
        content = out_file.read_text(encoding="utf-8")
        assert content.startswith("<!DOCTYPE html>")
        assert "汽车销售路由助手" in content

    def test_write_mermaid_format(self, car_sales, tmp_path):
        out_file = tmp_path / "diagram.mmd"
        assert visualize.main(["car_sales_route", "--format", "mermaid", "-o", str(out_file)]) == 0
        assert out_file.read_text(encoding="utf-8").startswith("flowchart TB")

    def test_default_output_path(self, car_sales, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert visualize.main(["car_sales_route"]) == 0
        assert (tmp_path / "diagrams" / "car_sales_route.html").exists()

    def test_unknown_pattern_returns_error(self, capsys):
        assert visualize.main(["no_such_pattern"]) == 1
        assert "未注册" in capsys.readouterr().out

    def test_all_generates_every_pattern(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert visualize.main(["--all", "--format", "md"]) == 0
        assert (tmp_path / "diagrams" / "car_sales_route.md").exists()
