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


def load_config(config_path: str = "") -> Dict[str, Any]:
    """从 local_config.yaml 读取本地配置并返回。

    Args:
        config_path: 可选，指定配置文件路径。为空时自动查找
                     ``config/local_config.yaml``。

    Returns:
        配置字典，至少包含 ``llm`` 键，其值为 LLM 配置子字典。

    Raises:
        FileNotFoundError: 配置文件不存在时。
        ValueError: 配置格式不合法时（缺少必填字段等）。

    Example:
        >>> config = load_config()
        >>> llm_cfg = config["llm"]
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

    # 提取 llm 配置
    llm_config = raw.get("llm")
    if llm_config is None:
        raise ValueError(
            f"配置文件中缺少 'llm' 节点，请在 {path} 中添加 llm 配置"
        )

    _validate_llm_config(llm_config)

    return {
        "llm": llm_config,
        # 后续可扩展其他节点，如: "dialogue", "logging", "storage" 等
    }


def get_llm_config(config_path: str = "") -> Dict[str, Any]:
    """便捷方法：直接返回 llm 配置子字典。

    Equivalent to ``load_config(config_path)["llm"]``.
    """
    return load_config(config_path)["llm"]