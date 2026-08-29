import ipaddress
import json
import socket
from collections import deque

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.config import Settings
from app.main import Dependencies, create_app


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="Allow billable real upstream calls.")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--live"):
        for item in items:
            if "live" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="Real model calls require explicit --live."))


@pytest.fixture(autouse=True)
def block_external_network(request, monkeypatch):
    if request.config.getoption("--live") and request.node.get_closest_marker("live"):
        return
    original = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple):
            try:
                local = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                local = address[0] == "localhost"
            if not local:
                raise AssertionError("External networking is disabled for this test.")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts, self.closed = parts, False

    async def __aiter__(self):
        for part in self.parts:
            if isinstance(part, Exception):
                raise part
            yield part

    async def aclose(self):
        self.closed = True


def data_event(value, event=None):
    prefix = f"event: {event}\n" if event else ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return (prefix + "data: " + text + "\n\n").encode()


def usage(protocol):
    if protocol == "chat":
        return {
            "prompt_tokens": 12,
            "completion_tokens": 7,
            "total_tokens": 19,
            "prompt_cache_hit_tokens": 4,
            "completion_tokens_details": {"reasoning_tokens": 2},
        }
    return {
        "input_tokens": 12,
        "output_tokens": 7,
        "total_tokens": 19,
        "input_tokens_details": {"cached_tokens": 4},
        "output_tokens_details": {"reasoning_tokens": 2},
    }


def completion(protocol, text="Hello", reason="stop"):
    if protocol == "chat":
        return {
            "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": reason}],
            "usage": usage(protocol),
        }
    return {
        "status": "completed" if reason == "stop" else "incomplete",
        "incomplete_details": None if reason == "stop" else {"reason": "max_output_tokens"},
        "output": [
            {"type": "reasoning", "content": [{"text": "private reasoning"}]},
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
        ],
        "usage": usage(protocol),
    }


class FakeUpstream:
    def __init__(self):
        self.requests = []
        self.responses = deque()
        self.streams = []

    def __call__(self, request):
        payload = json.loads(request.content)
        self.requests.append((request, payload))
        if self.responses:
            response = self.responses.popleft()
            if isinstance(response, Exception):
                raise response
            return response
        protocol = "chat" if request.url.path.endswith("/chat/completions") else "responses"
        fmt = payload.get("response_format") or payload.get("text", {}).get("format")
        answer = '{"summary":"done"}' if fmt else "Hello"
        if not payload.get("stream"):
            return httpx.Response(200, json=completion(protocol, answer))
        if protocol == "chat":
            parts = [
                b": keepalive\n\n",
                data_event({"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]}),
                data_event({"choices": [{"delta": {"content": answer[:2]}, "finish_reason": None}]}),
                data_event({"choices": [{"delta": {"content": answer[2:]}, "finish_reason": None}]}),
                data_event({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage(protocol)}),
                data_event("[DONE]"),
            ]
        else:
            parts = [
                data_event({"type": "response.reasoning_text.delta", "delta": "private reasoning"}),
                data_event({"type": "response.output_text.delta", "delta": answer[:2]}),
                data_event({"type": "response.output_text.delta", "delta": answer[2:]}),
                data_event({"type": "response.output_text.done", "text": answer}),
                data_event({"type": "response.completed", "response": completion(protocol, answer)}),
            ]
        stream = ByteStream(parts)
        self.streams.append(stream)
        return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})


@pytest.fixture
def test_settings(tmp_path):
    models = {}
    for name, adapter in [("deepseek-v4-pro", "chat_completions"), ("deepseek-v4-flash", "responses")]:
        models[name] = {
            "adapter": adapter,
            "base_url": "https://upstream.test",
            "api_key": "test-upstream-key",
            "upstream_model": name,
            "requests_per_minute": 6000,
            "burst": 100,
            "max_concurrency": 20,
        }
    return Settings(
        gateway_api_key="test-gateway-key",
        database_path=tmp_path / "gateway.db",
        models=models,
        retry={"base_delay_seconds": 0, "jitter_seconds": 0},
    )


@pytest.fixture
def mock_upstream():
    return FakeUpstream()


@pytest.fixture
async def test_app(test_settings, mock_upstream):
    app = create_app(test_settings, Dependencies(transport=httpx.MockTransport(mock_upstream)))
    async with LifespanManager(app):
        yield app


@pytest.fixture
async def api_client(test_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_app),
        base_url="http://gateway.test",
        headers={"Authorization": "Bearer test-gateway-key"},
    ) as client:
        yield client


@pytest.fixture
def chat_body():
    return {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "Say hello"}]}
