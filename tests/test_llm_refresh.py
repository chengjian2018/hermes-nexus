"""R1-R4 注入刷新：逐轮按当前位置解析 + override 优先（spec §4）。"""

from unittest.mock import patch

from src.chat.session import Session
from src.dialogue.module import FSMModule, RouteModule
from src.dialogue.node import BaseNode
from src.dialogue.pattern import Pattern


def _fsm_pattern():
    n1 = BaseNode(node_code="f1", node_name="节点一")
    n2 = BaseNode(node_code="f2", node_name="节点二")
    m = FSMModule(module_code="m1", module_name="m1", module_description="d",
                  module_todo_description="t", sub_modules=[],
                  module_nodes=[n1, n2])
    return Pattern(code="pf", name="t", description="t",
                   entry_module_code="m1", modules=[m])


def _launch(pattern, sessions, sid="s1"):
    session = Session(session_id=sid, pattern_code=pattern.code)
    session.pattern = pattern
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["dispatch_graph"] = pattern.dispatch_graph
    session.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    sessions[sid] = session
    return session


def _chat(sessions, sid, query):
    from src.chat.chat import chat as chat_fn
    return chat_fn(query=query, session_id=sid, all_sessions=sessions)


def _record_calls(calls):
    import src.chat.chat as chat_mod
    real = chat_mod.get_llm_config

    def spy(pattern_code="", module_code="", node_code="", override=None, config_path=""):
        calls.append(dict(pattern_code=pattern_code, module_code=module_code,
                          node_code=node_code, override=override))
        return real(pattern_code=pattern_code, module_code=module_code,
                    node_code=node_code, override=override,
                    config_path=config_path)
    return spy


def test_r1_passes_position_and_override():
    """R1：pattern/module/node + override 全部透传，且写 metadata pattern_code。"""
    sessions = {}
    _launch(_fsm_pattern(), sessions)
    calls = []
    with patch("src.chat.loop.build_provider"), \
         patch("src.chat.chat.get_llm_config", side_effect=_record_calls(calls)):
        _chat(sessions, "s1", "你好")
    assert calls, "R1 应调用 get_llm_config"
    first = calls[0]
    assert first["pattern_code"] == "pf"
    assert first["override"] == {"code": "x", "model": "m"}
    assert sessions["s1"].cxt.metadata["pattern_code"] == "pf"
    # 会话内已定位 module/node 时 R1 就带上（首轮为空）
    assert first["module_code"] in ("", "m1")


def test_r2_agent_module_chat_path_uses_module_code():
    """R2：AGENT 模块经 chat() 路径触发 _handle_agent_module，
    get_llm_config 以 module_code=<agent模块code>、node_code="" 调用。"""
    from src.dialogue.module import AgentModule
    agent_m = AgentModule(module_code="reception", module_name=" reception",
                          module_description="d", module_todo_description="t",
                          sub_modules=[])
    pattern = Pattern(code="pa", name="t", description="t",
                      entry_module_code="reception", modules=[agent_m])
    sessions = {}
    _launch(pattern, sessions, sid="s3")
    calls = []

    class _Scripted:
        def chat_completion(self, messages, model, temperature, max_tokens,
                            tools=None, tool_choice=None, **kw):
            return {"content": "ok", "tool_calls": []}

    with patch("src.chat.loop.build_provider", return_value=_Scripted()), \
         patch("src.chat.chat.get_llm_config",
               side_effect=_record_calls(calls)):
        _chat(sessions, "s3", "你好")
    r2 = [c for c in calls if c["module_code"] == "reception"
          and c["node_code"] == ""]
    assert r2, f"R2 应以 module_code=reception、node_code='' 解析，实际: {calls}"
    assert r2[0]["override"] == {"code": "x", "model": "m"}


def test_r3_refresh_after_node_resolution():
    """R3：_run_pipeline 节点解析后按 module+node 刷新。"""
    sessions = {}
    _launch(_fsm_pattern(), sessions)
    calls = []
    with patch("src.chat.loop.build_provider"), \
         patch("src.chat.chat.get_llm_config", side_effect=_record_calls(calls)):
        _chat(sessions, "s1", "你好")
    r3 = [c for c in calls if c["module_code"] == "m1" and c["node_code"] == "f1"]
    assert r3, f"R3 应按 module=m1 node=f1 解析，实际调用: {calls}"


def test_r4_route_menu_node_takes_effect_same_turn():
    """R4：ROUTE 菜单命中切节点后当轮刷新（菜单节点配置驱动当轮 NLG）。"""
    menu = BaseNode(node_code="menu_a", node_name="菜单A",
                    jump_module="m1", base_nlg_prompt="回答A")
    root = BaseNode(node_code="root", node_name="根")
    route = RouteModule(module_code="r1", module_name="r", module_description="d",
                        module_todo_description="t", sub_modules=[],
                        module_nodes=[root, menu])
    agent_m = _fsm_pattern().module_map["m1"]
    pattern = Pattern(code="pr", name="t", description="t",
                      entry_module_code="r1", modules=[route, agent_m])
    sessions = {}
    _launch(pattern, sessions, sid="s2")
    calls = []
    # RouteNLU/FSMNLU 打桩返回意图命中菜单（绕开真实 LLM 协议）
    class _StubNLU:
        stage_name = "nlu"
        def execute(self, ctx):
            ctx.nlu_result = {"next_node": "menu_a", "slots": {}}
            return ctx
    class _StubNLG:
        stage_name = "nlg"
        def execute(self, ctx):
            ctx.nlg_result = {"content": "ok"}
            return ctx
    import src.chat.chat as chat_mod
    pattern.stages = [_StubNLU(), chat_mod._RouteNodeAdvance(), _StubNLG()]
    with patch("src.chat.loop.build_provider"), \
         patch("src.chat.chat.get_llm_config", side_effect=_record_calls(calls)):
        _chat(sessions, "s2", "选A")
    r4 = [c for c in calls if c["node_code"] == "menu_a"]
    assert r4, f"R4 应在菜单命中后按 node=menu_a 刷新，实际调用: {calls}"


def test_override_wins_and_survives_turns():
    """override 写入 cxt.llm_config 且逐轮不被冲掉。"""
    sessions = {}
    _launch(_fsm_pattern(), sessions)
    with patch("src.chat.loop.build_provider"):
        _chat(sessions, "s1", "你好")
        _chat(sessions, "s1", "继续")
    assert sessions["s1"].cxt.llm_config["model"] == "m"
