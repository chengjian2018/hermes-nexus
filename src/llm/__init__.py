"""LLM provider management.

Exports:
    - ``BaseLLMProvider``: abstract base class for LLM providers.
    - ``ProviderEntry``: metadata record for a registered provider.
    - ``LLMProviderRegistry``: singleton registry for provider management.
    - ``registry``: module-level singleton instance.
    - ``discover_builtin_providers``: auto-discover and import provider modules.
    - ``build_provider``: unified entry that builds a configured provider
      from an llm config dict (yaml overrides applied).
"""

from src.llm.provider import BaseLLMProvider, ProviderEntry
from src.llm.register import LLMProviderRegistry, discover_builtin_providers, registry
from src.llm.resolve import build_provider