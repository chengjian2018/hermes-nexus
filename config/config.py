"""
配置加载模块 —— 从 local_config.yaml 读取本地配置。

LLM 配置项来源：``src/llm/provider.py`` 中的 ``ProviderEntry`` 和
``BaseLLMProvider``，以及 ``src/llm/openai_provider.py`` 中的
``OpenAICompatibleProvider``。
"""

import os
from pathlib import Path
from typing import Any, Dict

import yaml

# ============================================================================
# LLM 配置的必填字段与可选字段（来自 src/llm/provider.py 的 ProviderEntry）
# ============================================================================

_LLM_REQUIRED_FIELDS = {
    "code",   # Provider 唯一编码，对应 registry 中注册的 provider
    "model",  # 使用的模型名称
}

_LLM_OPTIONAL_FIELDS = {
    "api_base",     # API 地址（覆盖 provider 默认值）
    "api_key",      # 直接设置 API key（优先级高于 api_key_env）
    "api_key_env",  # API key 环境变量名，如 "DASHSCOPE_API_KEY"
    "temperature",  # 生成温度，默认 0.7
    "max_tokens",   # 最大输出 token 数，默认 2048
    "timeout",      # 请求超时秒数，默认 60（来自 OpenAICompatibleProvider）
    "max_retries",  # 失败重试次数，默认 2（来自 OpenAICompatibleProvider）
    "enable_thinking",  # Qwen3 思考模式开关，默认 False（来自 OpenAICompatibleProvider）
}

# 所有合法的 LLM 配置字段
_LLM_ALL_FIELDS = _LLM_REQUIRED_FIELDS | _LLM_OPTIONAL_FIELDS


# ============================================================================
# Pattern 级 LLM 配置（spec 2026-09-02）：provider 连接 / 模型编排分离
# ============================================================================

# 编排字段：llm_default 与 pattern_llm 各层条目允许的字段
_ORCHESTRATION_FIELDS = {
    "code", "model", "temperature", "max_tokens",
    "timeout", "max_retries", "enable_thinking",
}

# 连接字段：llm_providers 各段允许的字段（legacy llm: 节点按此拆分）
_CONNECTION_FIELDS = {
    "api_base", "api_key", "api_key_env", "timeout", "max_retries",
}

_PATTERN_LLM_SUBKEYS = {"modules", "nodes"}


# ============================================================================
# 会话持久化配置
# ============================================================================

# 会话审计 SQLite 文件缺省路径（相对服务启动目录）
DEFAULT_SESSION_DB_PATH = "data/dialogue.db"


# ============================================================================
# 配置加载
# ============================================================================

def _get_config_path() -> Path:
    """获取 local_config.yaml 的路径。

    优先从项目根目录下的 config/ 目录查找。
    """
    # 当前文件位于 config/config.py，config 目录即为项目配置目录
    config_dir = Path(__file__).resolve().parent
    config_path = config_dir / "local_config.yaml"

    if not config_path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {config_path}\n"
            f"请在 config/ 目录下创建 local_config.yaml"
        )

    return config_path


def _validate_llm_config(llm_config: Dict[str, Any]) -> None:
    """校验 LLM 配置的完整性与合法性。

    Args:
        llm_config: 从 yaml 中解析出的 llm 配置字典。

    Raises:
        ValueError: 缺少必填字段或包含未知字段时。
    """
    if not isinstance(llm_config, dict):
        raise ValueError(
            f"llm 配置应为字典类型，实际为: {type(llm_config).__name__}"
        )

    # 检查必填字段
    missing = _LLM_REQUIRED_FIELDS - set(llm_config.keys())
    if missing:
        raise ValueError(
            f"llm 配置缺少必填字段: {sorted(missing)}\n"
            f"必填字段: {sorted(_LLM_REQUIRED_FIELDS)}"
        )

    # 检查未知字段（警告，不阻止运行）
    unknown = set(llm_config.keys()) - _LLM_ALL_FIELDS
    if unknown:
        import logging
        logging.getLogger(__name__).warning(
            "llm 配置包含未知字段将被忽略: %s", sorted(unknown)
        )


def _convert_legacy_llm(llm_cfg: Dict[str, Any]):
    """legacy 顶层 llm: 节点 → (llm_providers, llm_default)（spec §6）。"""
    conn = {
        k: llm_cfg[k] for k in _CONNECTION_FIELDS
        if llm_cfg.get(k) not in (None, "")
    }
    orch = {k: v for k, v in llm_cfg.items() if k not in _CONNECTION_FIELDS}
    providers = {llm_cfg["code"]: conn} if conn else {}
    return providers, orch


def _validate_pattern_llm(pattern_llm: Dict[str, Any]) -> None:
    """pattern_llm 词表与嵌套校验：未知字段 warning 后剔除，modules/nodes 内
    再嵌套 modules/nodes 视为非法嵌套，warning 后置空。"""
    for pcode, pcfg in pattern_llm.items():
        if not isinstance(pcfg, dict):
            raise ValueError(f"pattern_llm.{pcode} 应为字典，实际为: {type(pcfg).__name__}")
        for key in list(pcfg.keys()):
            if key in _PATTERN_LLM_SUBKEYS:
                for sub_code, sub_cfg in (pcfg[key] or {}).items():
                    if not isinstance(sub_cfg, dict):
                        raise ValueError(
                            f"pattern_llm.{pcode}.{key}.{sub_code} 应为字典")
                    bad = set(sub_cfg.keys()) & _PATTERN_LLM_SUBKEYS
                    unknown = set(sub_cfg.keys()) - _ORCHESTRATION_FIELDS
                    if bad or unknown:
                        import logging
                        logging.getLogger(__name__).warning(
                            "pattern_llm.%s.%s.%s 含非法嵌套/未知字段 %s，已忽略",
                            pcode, key, sub_code, sorted(bad | unknown))
                        pattern_llm[pcode][key][sub_code] = {
                            k: v for k, v in sub_cfg.items()
                            if k in _ORCHESTRATION_FIELDS
                        }
            elif key not in _ORCHESTRATION_FIELDS:
                import logging
                logging.getLogger(__name__).warning(
                    "pattern_llm.%s 含未知字段 '%s'，已忽略", pcode, key)
                pattern_llm[pcode].pop(key)


def load_config(config_path: str = "") -> Dict[str, Any]:
    """从 local_config.yaml 读取本地配置并返回。

    Args:
        config_path: 可选，指定配置文件路径。为空时自动查找
                     ``config/local_config.yaml``。

    Returns:
        配置字典，包含 ``llm_providers`` / ``llm_default`` / ``pattern_llm``
        / ``session_db_path`` 键。legacy ``llm:`` 节点在加载期自动转换为
        ``llm_providers`` + ``llm_default``。

    Raises:
        FileNotFoundError: 配置文件不存在时。
        ValueError: 配置格式不合法时（缺少必填字段等）。

    Example:
        >>> config = load_config()
        >>> llm_cfg = config["llm_default"]
        >>> print(llm_cfg["code"])   # "openai"
        >>> print(llm_cfg["model"])  # "qwen3.8-max"
    """
    # 确定配置文件路径
    if config_path:
        path = Path(config_path)
    else:
        path = _get_config_path()

    # 读取 yaml
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if raw is None:
        raise ValueError(
            f"配置文件 {path} 内容为空，请按格式填写 llm 配置"
        )

    if not isinstance(raw, dict):
        raise ValueError(
            f"配置文件顶层应为字典，实际为: {type(raw).__name__}"
        )

    # 提取 LLM 配置：新结构（llm_providers/llm_default/pattern_llm）或 legacy llm 节点
    has_legacy = raw.get("llm") is not None
    has_new = raw.get("llm_providers") is not None or raw.get("llm_default") is not None
    if has_legacy and has_new:
        raise ValueError(
            f"配置文件 {path} 中旧 'llm:' 节点与 'llm_providers:'/'llm_default:' "
            f"并存，请改写为新结构（spec 2026-09-02 §6）"
        )
    if has_legacy:
        llm_cfg = raw["llm"]
        _validate_llm_config(llm_cfg)  # 旧节点沿用旧校验（code/model 必填）
        llm_providers, llm_default = _convert_legacy_llm(llm_cfg)
    elif raw.get("llm_default") is not None:
        llm_default = raw["llm_default"]
        if not isinstance(llm_default, dict):
            raise ValueError("llm_default 应为字典")
        _validate_llm_config(llm_default)  # code/model 必填
        llm_providers = raw.get("llm_providers") or {}
    else:
        raise ValueError(
            f"配置文件 {path} 缺少 'llm_default'（或旧式 'llm'）节点"
        )
    if not isinstance(llm_providers, dict):
        raise ValueError("llm_providers 应为字典")
    pattern_llm = raw.get("pattern_llm") or {}
    if not isinstance(pattern_llm, dict):
        raise ValueError("pattern_llm 应为字典")
    _validate_pattern_llm(pattern_llm)

    return {
        "llm_providers": llm_providers,
        "llm_default": llm_default,
        "pattern_llm": pattern_llm,
        # 会话持久化 SQLite 路径（可选，缺省 data/dialogue.db）
        "session_db_path": raw.get("session_db_path", DEFAULT_SESSION_DB_PATH),
        # 后续可扩展其他节点，如: "dialogue", "logging", "storage" 等
    }


def get_llm_config(config_path: str = "") -> Dict[str, Any]:
    """便捷方法：返回全局默认 llm 配置（连接字段 ⊕ llm_default）。"""
    cfg = load_config(config_path)
    providers = cfg["llm_providers"]
    merged = dict(cfg["llm_default"])
    return {**providers.get(merged.get("code", ""), {}), **merged}


def get_session_db_path(config_path: str = "") -> str:
    """便捷方法：返回会话持久化 SQLite 文件路径。

    Equivalent to ``load_config(config_path)["session_db_path"]``.
    """
    return load_config(config_path)["session_db_path"]