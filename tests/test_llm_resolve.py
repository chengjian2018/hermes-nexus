"""build_provider 统一入口测试 -- 验证 local_config.yaml 的 provider 字段真正生效。

全部离线：不访问真实 API，仅断言配置覆盖是否传到 provider 实例。
"""

import pytest

import config.config
from fake_provider import FAKE_PROVIDER_CODE, register_fake_provider
from src.llm import registry as llm_registry
from src.llm.openai_provider import OpenAICompatibleProvider
from src.llm.resolve import build_provider


# ============================================================================
# 覆盖字段生效
# ============================================================================

def test_yaml_overrides_reach_provider():
    """yaml 中非空的 api_base/api_key/api_key_env/timeout/max_retries 覆盖注册默认值。"""
    provider = build_provider({
        "code": "openai",
        "model": "qwen3.8-max",
        "api_base": "https://example.com/compatible-mode/v1",
        "api_key": "sk-from-yaml",
        "api_key_env": "MY_KEY_ENV",
        "timeout": 11,
        "max_retries": 3,
    })

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.api_base == "https://example.com/compatible-mode/v1"
    assert provider.resolve_api_key() == "sk-from-yaml"
    assert provider.api_key_env == "MY_KEY_ENV"
    assert provider.timeout == 11
    assert provider.max_retries == 3


def test_blank_fields_fall_back_to_registered_defaults():
    """空字符串 / None / 缺失字段不覆盖，落回 provider 注册时声明的默认值。"""
    provider = build_provider({
        "code": "openai",
        "model": "qwen3.8-max",
        "api_base": "",
        "api_key": None,
    })

    entry = llm_registry.get("openai")
    assert provider.api_base == entry.api_base
    assert provider.api_key_env == entry.api_key_env
    assert provider.timeout == 60   # OpenAICompatibleProvider 构造默认
    assert provider.max_retries == 2


def test_zero_max_retries_is_kept():
    """max_retries=0 是合法值（不重试），不应被当作"未设置"丢弃。"""
    provider = build_provider({"code": "openai", "model": "m", "max_retries": 0})
    assert provider.max_retries == 0


def test_unknown_code_raises():
    with pytest.raises(ValueError, match="未注册"):
        build_provider({"code": "no-such-provider", "model": "m"})


def test_openai_provider_discovered_on_first_use():
    """provider 未注册时，build_provider 内部触发自动发现并完成注册。"""
    import importlib
    import sys

    llm_registry.deregister("openai")
    # 模块已在 sys.modules 缓存时 import_module 不会重新执行注册代码，
    # 弹出缓存以模拟全新进程的首次导入
    sys.modules.pop("src.llm.openai_provider", None)
    try:
        provider = build_provider({"code": "openai", "model": "m"})
        assert provider.code == "openai"
        assert llm_registry.is_registered("openai")
    finally:
        # 还原现场：重新执行模块注册代码，保持 registry 与模块缓存一致
        llm_registry.deregister("openai")
        sys.modules.pop("src.llm.openai_provider", None)
        importlib.import_module("src.llm.openai_provider")


# ============================================================================
# _call_llm(llm_config=None) 回退路径
# ============================================================================

def test_call_llm_with_none_config_uses_loaded_config(monkeypatch):
    """llm_config=None 时回退加载 local_config.yaml，model 取自加载后的配置而非入参。

    回归：旧实现此处抛 ``TypeError: 'NoneType' object is not subscriptable``。
    """
    from src.dialogue.nlu import FSMNLU

    register_fake_provider()
    monkeypatch.setattr(
        config.config,
        "get_llm_config",
        lambda: {"code": FAKE_PROVIDER_CODE, "model": "fake-model"},
    )

    out = FSMNLU()._call_llm("ping", None)  # 不应抛 TypeError
    assert isinstance(out, str)


def test_call_llm_with_none_config_loads_real_yaml(monkeypatch):
    """回退路径读取真实 local_config.yaml 时能构建出配置生效的 provider。"""
    from src.dialogue.nlg import FSMNLG
    import src.dialogue.nlg as nlg_module

    built = {}

    class _StubProvider:
        def chat_completion(self, **kwargs):
            return {"content": "stub"}

    def spy(cfg):
        built.update(cfg)
        return _StubProvider()

    monkeypatch.setattr(nlg_module, "build_provider", spy)
    FSMNLG()._call_llm("ping", None)

    assert built.get("code") == "openai"  # local_config.yaml 中的 code
    assert built.get("api_base")  # yaml 中的 api_base 随配置进入 build_provider
