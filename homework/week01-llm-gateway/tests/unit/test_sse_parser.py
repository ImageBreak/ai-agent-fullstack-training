import httpx
import pytest

from app.adapters.base import sse_events
from app.domain.errors import GatewayError
from tests.conftest import ByteStream


@pytest.mark.streaming
async def test_utf8_multiline_and_network_fragments():
    source = ': heartbeat\r\n\r\nevent: delta\r\ndata: {"text":\r\ndata: "你好"}\r\n\r\n'.encode()
    response = httpx.Response(200, stream=ByteStream([bytes([byte]) for byte in source]))
    events = [event async for event in sse_events(response, 256)]
    assert events == [("delta", {"text": "你好"})]


@pytest.mark.streaming
@pytest.mark.limits
async def test_bounded_event_and_unterminated_json():
    for source, limit in [(b"data: " + b"x" * 50, 20), (b'data: {"x":1}', 100)]:
        with pytest.raises(GatewayError):
            _ = [event async for event in sse_events(httpx.Response(200, stream=ByteStream([source])), limit)]
