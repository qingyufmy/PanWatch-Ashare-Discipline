import asyncio

import httpx
import pytest

from src.modules.portfolio import lark_transport


@pytest.mark.parametrize("status,body,expected", [
    (200, {"code": 0}, True), (200, {"StatusCode": 0}, True),
    (200, {"code": 19001}, False), (429, {}, False), (200, {}, False),
])
def test_provider_ack_is_required_and_no_retry(monkeypatch, status, body, expected):
    attempts = []
    real_client = httpx.AsyncClient
    def handle(request):
        attempts.append(request)
        return httpx.Response(status, json=body)
    monkeypatch.setattr(lark_transport.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    notifier = lark_transport.LarkNotifier()
    notifier.add_channel("lark", {"webhook_token": "fixture"})
    result = asyncio.run(notifier.notify_with_result("标题", "内容"))
    assert result["success"] is expected
    assert len(attempts) == 1


def test_transport_timeout_propagates_without_resend(monkeypatch):
    attempts = []
    real_client = httpx.AsyncClient
    def handle(request):
        attempts.append(request)
        raise httpx.ReadTimeout("fixture")
    monkeypatch.setattr(lark_transport.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    notifier = lark_transport.LarkNotifier()
    notifier.add_channel("lark", {"webhook_token": "fixture"})
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(notifier.notify_with_result("标题", "内容"))
    assert len(attempts) == 1
