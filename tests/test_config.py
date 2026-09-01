"""config 加载测试 —— session_db_path 缺省与覆盖（显式传配置路径，不依赖本地 yaml）。"""

from config.config import DEFAULT_SESSION_DB_PATH, get_session_db_path

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
