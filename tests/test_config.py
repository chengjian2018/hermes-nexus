"""config 加载测试（显式传配置路径，不依赖本地 yaml）。"""

import pytest

from config.config import DEFAULT_SESSION_DB_PATH, get_session_db_path
from config.config import get_llm_config, load_config

_LLM_MIN = """\
llm:
  code: openai
  model: qwen3.8-max
"""


def _write_config(tmp_path, extra=""):
    path = tmp_path / "local_config.yaml"
    path.write_text(_LLM_MIN + extra, encoding="utf-8")
    return str(path)


def test_session_db_path_default(tmp_path):
    """配置未写 session_db_path 时返回缺省路径。"""
    assert get_session_db_path(_write_config(tmp_path)) == DEFAULT_SESSION_DB_PATH


def test_session_db_path_override(tmp_path):
    """配置显式写 session_db_path 时返回覆盖值。"""
    config_path = _write_config(tmp_path, "\nsession_db_path: /tmp/audit.db\n")
    assert get_session_db_path(config_path) == "/tmp/audit.db"


# ============================================================================
# Pattern 级 LLM 配置新结构（spec 2026-09-02）：llm_providers / llm_default / pattern_llm
# ============================================================================

_NEW_STRUCT = """\
llm_providers:
  openai:
    api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: DASHSCOPE_API_KEY
llm_default:
  code: openai
  model: qwen3.8-max
  temperature: 0.7
pattern_llm:
  car_sales_route:
    model: qwen-flash
    modules:
      car_sales_buy: {model: qwen3.8-max}
    nodes:
      buy_confirm: {code: deepseek, model: deepseek-chat}
"""

_LEGACY = """\
llm:
  code: openai
  model: qwen3.8-max
  api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key_env: DASHSCOPE_API_KEY
  temperature: 0.7
"""


def _write(tmp_path, text):
    p = tmp_path / "local_config.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_new_structure_parsed(tmp_path):
    cfg = load_config(_write(tmp_path, _NEW_STRUCT))
    assert cfg["llm_default"]["code"] == "openai"
    assert cfg["llm_providers"]["openai"]["api_key_env"] == "DASHSCOPE_API_KEY"
    assert cfg["pattern_llm"]["car_sales_route"]["modules"]["car_sales_buy"]["model"] == "qwen3.8-max"
    assert cfg["pattern_llm"]["car_sales_route"]["nodes"]["buy_confirm"]["code"] == "deepseek"
    assert "llm" not in cfg


def test_legacy_llm_converted(tmp_path):
    cfg = load_config(_write(tmp_path, _LEGACY))
    # 连接字段进 llm_providers.<code>，编排字段进 llm_default
    assert cfg["llm_providers"] == {
        "openai": {
            "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key_env": "DASHSCOPE_API_KEY",
        }
    }
    assert cfg["llm_default"] == {"code": "openai", "model": "qwen3.8-max", "temperature": 0.7}
    assert cfg.get("pattern_llm") == {}


def test_legacy_and_new_coexist_rejected(tmp_path):
    with pytest.raises(ValueError, match="并存"):
        load_config(_write(tmp_path, _LEGACY + "llm_default:\n  code: openai\n  model: m\n"))


def test_llm_default_missing_required_fields(tmp_path):
    with pytest.raises(ValueError, match="必填字段"):
        load_config(_write(tmp_path, "llm_default:\n  code: openai\n"))


def test_no_llm_section_at_all_rejected(tmp_path):
    with pytest.raises(ValueError, match="llm"):
        load_config(_write(tmp_path, "session_db_path: /tmp/x.db\n"))


def test_unknown_orchestration_field_warns(tmp_path, caplog):
    text = _NEW_STRUCT.replace(
        "car_sales_route:\n    model: qwen-flash",
        "car_sales_route:\n    model: qwen-flash\n    bogus_field: 1",
    )
    with caplog.at_level("WARNING"):
        cfg = load_config(_write(tmp_path, text))
    assert cfg["pattern_llm"]["car_sales_route"].get("bogus_field") is None
    assert any("bogus_field" in r.message for r in caplog.records)


def test_nested_modules_rejected_with_warning(tmp_path, caplog):
    text = _NEW_STRUCT.replace(
        "car_sales_buy: {model: qwen3.8-max}",
        "car_sales_buy:\n        modules: {inner: {model: m}}",
    )
    with caplog.at_level("WARNING"):
        cfg = load_config(_write(tmp_path, text))
    assert cfg["pattern_llm"]["car_sales_route"]["modules"]["car_sales_buy"] == {}
    assert any("嵌套" in r.message for r in caplog.records)


# ============================================================================
# get_llm_config 三层合并（spec 2026-09-02 §3.2/§3.3）
# ============================================================================

def test_layered_merge_priority(tmp_path):
    """node > module > pattern > 全局，逐层浅合并。"""
    path = _write(tmp_path, _NEW_STRUCT + """\
  car_sales_route2:
    model: qwen3.8-max
    modules:
      m1: {model: m-flash}
      m2: {temperature: 0.2}
    nodes:
      n1: {code: deepseek, model: deepseek-chat}
""")
    cfg = get_llm_config(pattern_code="car_sales_route2",
                         module_code="m2", node_code="n1", config_path=path)
    # n1 换 code → 连接层切到 deepseek 段（无该段则空）；temperature 继承 m2
    assert cfg["code"] == "deepseek"
    assert cfg["model"] == "deepseek-chat"
    assert cfg["temperature"] == 0.2
    assert cfg.get("api_base", "") == ""  # deepseek 未配 provider 段 → 空


def test_cross_provider_connection_switch(tmp_path):
    """node 换 code 时连接字段来自新 code 的 provider 段，不串台。"""
    text = """\
llm_providers:
  openai:
    api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: DASHSCOPE_API_KEY
  deepseek:
    api_base: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY
llm_default:
  code: openai
  model: qwen3.8-max
pattern_llm:
  p:
    nodes:
      n: {code: deepseek, model: deepseek-chat}
"""
    cfg = get_llm_config(pattern_code="p", node_code="n",
                         config_path=_write(tmp_path, text))
    assert cfg["api_base"] == "https://api.deepseek.com/v1"
    assert cfg["api_key_env"] == "DEEPSEEK_API_KEY"


def test_unknown_codes_fallback_to_shallow_layer(tmp_path, caplog):
    cfg = get_llm_config(pattern_code="no_such_pattern", config_path=_write(tmp_path, _NEW_STRUCT))
    assert cfg["model"] == "qwen3.8-max"  # 回退全局
    cfg2 = get_llm_config(pattern_code="car_sales_route", module_code="no_such_module",
                          config_path=_write(tmp_path, _NEW_STRUCT))
    assert cfg2["model"] == "qwen-flash"  # 回退 pattern 层
    # pattern 未配置为常态，降为 debug；module 未命中仍是 warning
    with caplog.at_level("DEBUG"):
        get_llm_config(pattern_code="no_such_pattern", config_path=_write(tmp_path, _NEW_STRUCT))
    assert any("no_such_pattern" in r.message for r in caplog.records)
    with caplog.at_level("WARNING"):
        get_llm_config(pattern_code="car_sales_route", module_code="no_such_module",
                       config_path=_write(tmp_path, _NEW_STRUCT))
    assert any("no_such_module" in r.message for r in caplog.records)


def test_no_args_returns_global(tmp_path):
    cfg = get_llm_config(config_path=_write(tmp_path, _NEW_STRUCT))
    assert cfg["code"] == "openai"
    assert cfg["model"] == "qwen3.8-max"
    assert cfg["api_key_env"] == "DASHSCOPE_API_KEY"  # 连接层并入


def test_override_skips_layers(tmp_path):
    ov = {"code": "fake_test_provider", "model": "fake-model", "temperature": 0.1}
    cfg = get_llm_config(pattern_code="car_sales_route", node_code="buy_confirm",
                         override=ov, config_path=_write(tmp_path, _NEW_STRUCT))
    assert cfg["model"] == "fake-model" and cfg["temperature"] == 0.1


# ============================================================================
# main startup 交叉校验（spec 2026-09-02 §5）：未知 code 仅 warning 不阻断
# ============================================================================

def test_cross_check_warns_unknown_codes(tmp_path, caplog):
    """pattern_llm 的未知 pattern/module/node code 仅 warning 不抛。"""
    import main
    text = _NEW_STRUCT + """\
  no_such_pattern:
    model: m
"""
    # 未注册 pattern 的 modules/nodes 分支不可达（continue），故 module/node
    # 未知分支挂在已注册的 car_sales_route 下单独验证
    text = text.replace("buy_confirm: {code: deepseek, model: deepseek-chat}",
                        "no_such_node: {code: deepseek, model: deepseek-chat}")
    text = text.replace("car_sales_buy: {model: qwen3.8-max}",
                        "no_such_module: {model: qwen3.8-max}")
    with caplog.at_level("WARNING"):
        main._cross_check_pattern_llm(config_path=_write(tmp_path, text))
    msgs = " ".join(r.message for r in caplog.records)
    assert "no_such_pattern" in msgs
    assert "no_such_module" in msgs
    assert "no_such_node" in msgs


def test_cross_check_skips_on_load_failure(tmp_path, caplog):
    """load_config 失败时仅 exception 日志，不抛。"""
    import main
    with caplog.at_level("WARNING"):
        main._cross_check_pattern_llm(config_path="/no/such/file.yaml")
    assert not any("未注册" in r.message for r in caplog.records)


def test_cross_check_registered_codes_no_warning(tmp_path, caplog):
    """已注册的 pattern/module/node 不产生 warning。"""
    import main
    pattern = main.pattern_registry.list_codes()
    assert pattern  # discover 在 import main 时已跑
    with caplog.at_level("WARNING"):
        main._cross_check_pattern_llm(config_path=_write(tmp_path, _NEW_STRUCT))
    assert not any("未注册" in r.message for r in caplog.records)


def test_override_survives_missing_yaml(tmp_path, caplog):
    """yaml 不存在时 override 路径静默降级（离线测试封闭性）。"""
    ov = {"code": "x", "model": "m"}
    with caplog.at_level("WARNING"):
        cfg = get_llm_config(override=ov,
                             config_path=str(tmp_path / "nope.yaml"))
    assert cfg == {"code": "x", "model": "m"}


# ============================================================================
# 终审修复回归（Final review I1 / I3）
# ============================================================================

def test_llm_providers_unknown_field_warns_and_stripped(tmp_path, caplog):
    """llm_providers 段未知字段（如手误 api_key_evn）warning 后剔除。"""
    text = _NEW_STRUCT.replace(
        "  openai:\n    api_base:",
        "  openai:\n    api_key_evn: WRONG_ENV\n    api_base:")
    with caplog.at_level("WARNING"):
        cfg = load_config(_write(tmp_path, text))
    assert "api_key_evn" not in cfg["llm_providers"]["openai"]
    assert cfg["llm_providers"]["openai"]["api_key_env"] == "DASHSCOPE_API_KEY"
    assert any("api_key_evn" in r.message for r in caplog.records)


def test_override_without_code_falls_back_to_llm_default(tmp_path):
    """override 只写 model（无 code）→ 解析结果补 llm_default 的 code。"""
    ov = {"model": "override-model"}
    cfg = get_llm_config(override=ov, config_path=_write(tmp_path, _NEW_STRUCT))
    assert cfg["code"] == "openai"
    assert cfg["model"] == "override-model"
