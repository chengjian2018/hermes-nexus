"""cli.py 纯函数单测：斜杠解析 / verbose 渲染 / 菜单渲染 / 短 flag 展开。

不触网络、不触 LLM；REPL 交互与 fire 分发另行手动验收。
"""

import unittest.mock

import pytest

import cli


# ============================================================================
# parse_slash_command
# ============================================================================

class TestParseSlashCommand:
    def test_plain_text_returns_none(self):
        assert cli.parse_slash_command("你好") is None
        assert cli.parse_slash_command("  你好") is None
        assert cli.parse_slash_command("") is None

    def test_known_command_no_arg(self):
        assert cli.parse_slash_command("/help") == {"name": "help", "arg": ""}
        assert cli.parse_slash_command("/exit") == {"name": "exit", "arg": ""}
        assert cli.parse_slash_command("/slots") == {"name": "slots", "arg": ""}

    def test_known_command_with_arg(self):
        assert cli.parse_slash_command("/new car_sales_route") == {
            "name": "new", "arg": "car_sales_route"}
        assert cli.parse_slash_command("/llm openai") == {
            "name": "llm", "arg": "openai"}

    def test_whitespace_normalized(self):
        assert cli.parse_slash_command("  /help  ") == {"name": "help", "arg": ""}

    def test_case_insensitive_command(self):
        assert cli.parse_slash_command("/HELP") == {"name": "help", "arg": ""}

    def test_unknown_command_flagged(self):
        assert cli.parse_slash_command("/foobar") == {
            "name": "unknown", "arg": "foobar"}

    def test_arg_keeps_inner_spaces(self):
        assert cli.parse_slash_command("/new  multi word arg") == {
            "name": "new", "arg": "multi word arg"}

    @pytest.mark.parametrize("cmd", cli._SLASH_COMMANDS)
    def test_all_commands_parse(self, cmd):
        assert cli.parse_slash_command(f"/{cmd}")["name"] == cmd


# ============================================================================
# render_verbose_summary
# ============================================================================

class TestRenderVerboseSummary:
    def _snap(self, node="n1", module="m1", slots=None, intent=None,
              next_node=None):
        return {
            "current_module_code": module,
            "current_node_code": node,
            "filled_slots": slots or {},
            "intent": intent,
            "next_node": next_node,
        }

    def test_no_change_empty(self):
        out = cli.render_verbose_summary(self._snap(), self._snap())
        assert out == ""

    def test_node_transition_rendered(self):
        before, after = self._snap(node="n1"), self._snap(node="n2")
        out = cli.render_verbose_summary(before, after)
        assert "n1" in out and "n2" in out and "→" in out

    def test_module_and_node_transition_shows_both(self):
        before = self._snap(node="n1", module="m1")
        after = self._snap(node="n2", module="m2")
        out = cli.render_verbose_summary(before, after)
        assert "m1" in out and "m2" in out and "n1" in out and "n2" in out

    def test_intent_rendered(self):
        after = self._snap(intent="buy_car")
        out = cli.render_verbose_summary(self._snap(), after)
        assert "buy_car" in out

    def test_slot_change_rendered(self):
        before = self._snap(slots={"budget": "10万"})
        after = self._snap(slots={"budget": "20万", "car_type": "SUV"})
        out = cli.render_verbose_summary(before, after)
        assert "budget" in out and "20万" in out and "car_type" in out

    def test_unchanged_slots_not_rendered(self):
        same = {"budget": "20万"}
        out = cli.render_verbose_summary(self._snap(slots=same),
                                         self._snap(slots=dict(same)))
        assert "budget" not in out

    def test_none_fields_tolerated(self):
        before = {"current_module_code": None, "current_node_code": None,
                  "filled_slots": None, "intent": None, "next_node": None}
        after = self._snap()
        out = cli.render_verbose_summary(before, after)
        assert isinstance(out, str)


# ============================================================================
# render_pattern_menu
# ============================================================================

class _FakePattern:
    def __init__(self, code, name, description=""):
        self.code, self.name, self.description = code, name, description


class TestRenderPatternMenu:
    def test_renders_numbered_entries(self):
        out = cli.render_pattern_menu(
            "选择 pattern",
            [_FakePattern("a", "Pattern A"), _FakePattern("b", "Pattern B", "desc")],
        )
        assert "1. a — Pattern A" in out
        assert "2. b — Pattern B" in out
        assert "desc" in out

    def test_empty_description_omitted(self):
        out = cli.render_pattern_menu(
            "t", [_FakePattern("a", "A", "")])
        lines = [l for l in out.split("\n") if l.strip()]
        assert len(lines) == 2  # 标题 + 一行条目


# ============================================================================
# _expand_short_verbose
# ============================================================================

class TestExpandShortVerbose:
    def test_v_levels(self):
        assert cli._expand_short_verbose(["-v"]) == ["--verbose=1"]
        assert cli._expand_short_verbose(["-vv"]) == ["--verbose=2"]
        assert cli._expand_short_verbose(["-vvv"]) == ["--verbose=3"]

    def test_mixed_args_passthrough(self):
        argv = ["chat", "--pattern", "p1", "-vv", "--session-id", "s"]
        assert cli._expand_short_verbose(argv) == [
            "chat", "--pattern", "p1", "--verbose=2", "--session-id", "s"]

    def test_verbose_long_form_untouched(self):
        assert cli._expand_short_verbose(["--verbose=2"]) == ["--verbose=2"]

    def test_unrelated_dash_flag_untouched(self):
        assert cli._expand_short_verbose(["--pattern", "-v-like"]) == [
            "--pattern", "-v-like"]


# ============================================================================
# render_verbose_full（用桩 context 对象，不依赖引擎）
# ============================================================================

class _FakeCxt:
    def __init__(self, nlu=None, nlg=None, agent=None, recall=None,
                 dispatch_log=None):
        self.nlu_result, self.nlg_result, self.agent_result = nlu, nlg, agent
        self._recall = recall
        self.metadata = {"dispatch_log": dispatch_log} if dispatch_log else {}

    def format_recall_info(self):
        return self._recall


class TestRenderVerboseFull:
    def test_empty_cxt_renders_header_only(self):
        out = cli.render_verbose_full(_FakeCxt())
        assert "context" in out

    def test_nlu_nlg_json(self):
        cxt = _FakeCxt(nlu={"next_node": "n2"}, nlg={"content": "hi"})
        out = cli.render_verbose_full(cxt)
        assert '"next_node": "n2"' in out
        assert '"content": "hi"' in out

    def test_dispatch_log_rendered(self):
        cxt = _FakeCxt(dispatch_log=[{"to": "m2"}])
        out = cli.render_verbose_full(cxt)
        assert "dispatch_log" in out and "m2" in out

    def test_non_serializable_falls_back_to_repr(self):
        class Weird:
            pass
        out = cli.render_verbose_full(_FakeCxt(agent=Weird()))
        assert "agent_result" in out  # 不抛异常即通过


# ============================================================================
# KEEP_CONFIG 菜单 + llm_override 接线
# ============================================================================

class TestKeepConfigMenu:
    def test_provider_menu_includes_keep_config(self):
        """provider 菜单含「维持 config 配置」固定项，置首。"""
        entries = cli._provider_menu_entries()
        assert entries[0]["value"] == cli.KEEP_CONFIG
        assert "维持" in entries[0]["label"]

    def test_pick_keep_config_in_provider_menu_returns_empty(self):
        with cli._patch_select(cli.KEEP_CONFIG):
            assert cli.resolve_llm_choice("", "") == {"code": "", "model": ""}

    def test_pick_keep_config_in_model_menu_keeps_code(self):
        """model 菜单选「维持」：保留已选 code，model 留空（回落全局默认）。"""
        from fake_provider import FAKE_PROVIDER_CODE, register_fake_provider

        register_fake_provider()
        # fake provider 未声明 models 列表 → 走 input() 手输分支，键入「维持」
        with unittest.mock.patch("builtins.input", return_value=cli.KEEP_CONFIG):
            result = cli.resolve_llm_choice(FAKE_PROVIDER_CODE, "")
        assert result == {"code": FAKE_PROVIDER_CODE, "model": ""}


class TestBuildSessionOverride:
    def test_build_session_writes_override_not_llm_config(self, monkeypatch):
        """--llm/--model 预置写 metadata.llm_override，不再直接写 llm_config。"""
        from fake_provider import fake_llm_config, register_fake_provider

        register_fake_provider()
        monkeypatch.setattr(cli, "get_llm_config", lambda *a, **k: fake_llm_config())
        from src.dialogue.register import discover_builtin_patterns

        discover_builtin_patterns()

        session = cli.build_session(
            "t1", "car_sales_route",
            llm_overrides={"code": "fake_test_provider", "model": "fake-model"},
        )
        assert session.cxt.metadata["llm_override"]["model"] == "fake-model"
        assert session.cxt.llm_config is None
