"""enable_clarify 模块开关 + FSM NLU prompt 澄清意图指令测试。"""


def test_default_disabled():
    from src.dialogue.module import FSMModule

    m = FSMModule(module_code="m1")
    assert m.enable_clarify is False


def test_explicit_enabled():
    from src.dialogue.module import FSMModule

    m = FSMModule(module_code="m1", enable_clarify=True)
    assert m.enable_clarify is True


def test_kwargs_style_enabled():
    """car_sales_route 等声明式 pattern 用 kwargs 传参，需同样生效。"""
    from src.dialogue.module import FSMModule

    m = FSMModule(module_code="m1", **{"enable_clarify": True})
    assert m.enable_clarify is True


def test_fsm_nlu_prompt_contains_clarify_protocol():
    from src.prompt import FSM_NLU_DEFAULT_PROMPT

    assert '"clarify"' in FSM_NLU_DEFAULT_PROMPT
    assert "topic" in FSM_NLU_DEFAULT_PROMPT
    assert "keywords" in FSM_NLU_DEFAULT_PROMPT
