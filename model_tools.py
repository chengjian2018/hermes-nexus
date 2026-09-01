"""Minimal stubs for ``model_tools`` dependencies required by ``tools/register.py``.

The original hermes-agent codebase defines ``_run_async`` and
``_sanitize_tool_error`` in a top-level ``model_tools`` module.  This file
provides the same API surface so the tool registry can dispatch and sanitize
tool results without importing the full agent runtime.

Import path: ``from model_tools import _run_async, _sanitize_tool_error``
"""

import asyncio
import concurrent.futures
import re


def _run_async(coro):
    """Run an async coroutine and return its result synchronously.

    Handles the case where an event loop is already running (e.g. inside a
    FastAPI request handler) by delegating to a separate thread.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(asyncio.run, coro)
                return future.result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# =========================================================================
# Tool error sanitization
# =========================================================================
#
# Tool exceptions can carry arbitrary text into the model's context as the
# `tool` message content. json.dumps() handles quote/backslash escaping so a
# raw injection of `</tool_call>` won't break message framing, but the model
# still *reads* those tokens and they can confuse downstream tool-call
# parsing or, in adversarial cases, nudge it toward role-confusion framing.
#
# This helper strips structural framing tokens (XML role tags, CDATA,
# markdown code fences) and caps the message at a sane upper bound before it
# becomes part of the conversation. It's defense-in-depth — the json layer
# already prevents framing escape — but cheap and worth having.
#
# Ported from ironclaw#1639.
_TOOL_ERROR_ROLE_TAG_RE = re.compile(
    r'</?(?:tool_call|function_call|result|response|output|input|system|assistant|user)>',
    re.IGNORECASE,
)
_TOOL_ERROR_FENCE_OPEN_RE = re.compile(r'^\s*```(?:json|xml|html|markdown)?\s*', re.MULTILINE)
_TOOL_ERROR_FENCE_CLOSE_RE = re.compile(r'\s*```\s*$', re.MULTILINE)
_TOOL_ERROR_CDATA_RE = re.compile(r'<!\[CDATA\[.*?\]\]>', re.DOTALL)
_TOOL_ERROR_MAX_LEN = 2000


def _sanitize_tool_error(error_msg: str) -> str:
    """Strip structural framing tokens from a tool error before showing it to the model.

    See _TOOL_ERROR_ROLE_TAG_RE docstring above for rationale.
    """
    if not error_msg:
        return "[TOOL_ERROR] "
    sanitized = _TOOL_ERROR_ROLE_TAG_RE.sub("", error_msg)
    sanitized = _TOOL_ERROR_FENCE_OPEN_RE.sub("", sanitized)
    sanitized = _TOOL_ERROR_FENCE_CLOSE_RE.sub("", sanitized)
    sanitized = _TOOL_ERROR_CDATA_RE.sub("", sanitized)
    if len(sanitized) > _TOOL_ERROR_MAX_LEN:
        sanitized = sanitized[:_TOOL_ERROR_MAX_LEN - 3] + "..."
    return f"[TOOL_ERROR] {sanitized}"