"""OpenAICompatibleProvider 重试分类测试——4xx 快速失败 / 429、5xx、网络错误照常重试。

打桩 requests.post（离线，无真实请求）。
"""

from unittest.mock import patch

import pytest
import requests

from llm.openai_provider import OpenAICompatibleProvider


def _provider(max_retries=2):
    return OpenAICompatibleProvider(
        code="t", api_base="http://x/v1", api_key="k",
        default_model="m", max_retries=max_retries)


def _http_error(status, body="err"):
    resp = requests.Response()
    resp.status_code = status
    resp._content = body.encode("utf-8")
    return requests.exceptions.HTTPError(f"{status}", response=resp)


def _call(provider):
    return provider.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        model="m", temperature=0.7, max_tokens=8)


def test_permanent_4xx_fails_fast_without_retry():
    p = _provider(max_retries=2)
    with patch("llm.openai_provider.requests.post",
               side_effect=_http_error(401)) as post:
        with pytest.raises(RuntimeError, match="不可重试"):
            _call(p)
    assert post.call_count == 1  # 无重试


def test_400_with_body_in_error():
    p = _provider()
    with patch("llm.openai_provider.requests.post",
               side_effect=_http_error(400, body="invalid payload detail")):
        with pytest.raises(RuntimeError, match="invalid payload detail"):
            _call(p)


def test_429_and_5xx_retry_then_raise():
    p = _provider(max_retries=2)
    with patch("llm.openai_provider.requests.post",
               side_effect=_http_error(429)) as post, \
         patch("llm.openai_provider.time.sleep") as sleep:
        with pytest.raises(RuntimeError, match="failed after 3 attempts"):
            _call(p)
    assert post.call_count == 3
    assert sleep.call_count == 2

    with patch("llm.openai_provider.requests.post",
               side_effect=_http_error(503)) as post, \
         patch("llm.openai_provider.time.sleep"):
        with pytest.raises(RuntimeError):
            _call(p)
    assert post.call_count == 3


def test_connection_error_retries():
    p = _provider(max_retries=1)
    with patch("llm.openai_provider.requests.post",
               side_effect=requests.exceptions.ConnectionError("down")) as post, \
         patch("llm.openai_provider.time.sleep"):
        with pytest.raises(RuntimeError, match="failed after 2 attempts"):
            _call(p)
    assert post.call_count == 2


def test_success_after_transient_500():
    p = _provider(max_retries=1)
    ok = _Resp200()
    with patch("llm.openai_provider.requests.post",
               side_effect=[_http_error(500), ok]), \
         patch("llm.openai_provider.time.sleep"):
        result = _call(p)
    assert result["content"] == "答案"


class _Resp200(requests.Response):
    def __init__(self):
        super().__init__()
        self.status_code = 200
        self._content = (
            '{"choices": [{"message": {"content": "答案"}, "finish_reason": "stop"}]}'
        ).encode("utf-8")

    def json(self):
        import json
        return json.loads(self._content.decode("utf-8"))
