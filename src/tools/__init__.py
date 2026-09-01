"""Tools — pluggable tool system.

Each tool file auto-registers with ``ToolRegistry`` at module import;
``discover_builtin_tools()`` discovers and imports all self-registering tools via AST scanning.

Exports:
    - ``registry``: module-level singleton :class:`ToolRegistry`
    - ``discover_builtin_tools``: auto-discover and import tool modules
    - ``tool_error`` / ``tool_result``: convenience response builders
"""

from src.tools.register import (
    ToolRegistry,
    ToolEntry,
    registry,
    discover_builtin_tools,
    tool_error,
    tool_result,
    invalidate_check_fn_cache,
)