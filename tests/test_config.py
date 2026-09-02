"""config 加载测试（显式传配置路径，不依赖本地 yaml）。"""

import pytest

from config.config import DEFAULT_SESSION_DB_PATH, get_session_db_path
from config.config import load_config

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
