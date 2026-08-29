import asyncio
import json

import httpx
import pytest

from tests.conftest import ByteStream, completion, data_event

MODELS = ["deepseek-v4-pro", "deepseek-v4-flash"]


@pytest.mark.routing
@pytest.mark.observability
@pytest.mark.parametrize("model", MODELS)
async def test_models_route_and_record(api_client, mock_upstream, chat_body, model):
    chat_body["model"] = model
    response = await api_client.post("/v1/chat/completions", json=chat_body)
    assert response.status_code == 200
    result = response.json()
    assert result["model"] == model and result["choices"][0]["message"]["content"] == "Hello"
    request, payload = mock_upstream.requests[-1]
    assert request.url.path == ("/responses" if "flash" in model else "/chat/completions")
    assert request.headers["Authorization"] == "Bearer test-upstream-key"
    assert payload.get("reasoning", {"effort": "none"})["effort"] == "none"
    assert payload.get("thinking", {"type": "disabled"})["type"] == "disabled"
    record = (await api_client.get("/admin/usage")).json()["data"][0]
    assert record["request_id"] == response.headers["x-request-id"]
    assert record["total_tokens"] == 19 and record["cached_input_tokens"] == 4
    assert record["ttft_ms"] is None and record["total_latency_ms"] >= 0
    summary = (await api_client.get("/admin/usage/summary")).json()
    assert summary["total"]["requests"] == 1


@pytest.mark.streaming
@pytest.mark.parametrize("model", MODELS)
async def test_stream_events_and_usage(api_client, mock_upstream, chat_body, model):
    chat_body.update(model=model, stream=True, stream_options={"include_usage": True})
    response = await api_client.post("/v1/chat/completions", json=chat_body)
    assert response.status_code == 200
    data = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert data[-1] == "[DONE]"
    chunks = [json.loads(item) for item in data[:-1]]
    text = "".join(choice["delta"].get("content", "") for item in chunks for choice in item["choices"])
    assert text == "Hello" and "private reasoning" not in response.text
    assert chunks[-1]["usage"]["total_tokens"] == 19
    record = (await api_client.get("/admin/usage")).json()["data"][0]
    assert record["ttft_ms"] is not None and record["status"] == "success"
    assert mock_upstream.streams[-1].closed


@pytest.mark.resilience
async def test_retries_then_success(api_client, mock_upstream, chat_body):
    mock_upstream.responses.extend([httpx.Response(503), httpx.Response(503)])
    response = await api_client.post("/v1/chat/completions", json=chat_body)
    assert response.status_code == 200
    assert len(mock_upstream.requests) == 3
    record = (await api_client.get("/admin/usage")).json()["data"][0]
    assert record["upstream_call_count"] == 3 and record["transport_retry_count"] == 2
    assert record["usage_complete"] is False


@pytest.mark.resilience
async def test_auth_failure_not_retried(api_client, mock_upstream, chat_body):
    mock_upstream.responses.append(httpx.Response(401, text="secret upstream error"))
    response = await api_client.post("/v1/chat/completions", json=chat_body)
    assert response.status_code == 502 and len(mock_upstream.requests) == 1
    assert "secret" not in response.text


@pytest.mark.structured
@pytest.mark.parametrize("model", MODELS)
async def test_structured_native_mapping(api_client, mock_upstream, chat_body, model):
    chat_body.update(
        model=model,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "summary",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                    "additionalProperties": False,
                },
            },
        },
    )
    response = await api_client.post("/v1/chat/completions", json=chat_body)
    assert response.status_code == 200
    assert json.loads(response.json()["choices"][0]["message"]["content"]) == {"summary": "done"}
    payload = mock_upstream.requests[-1][1]
    if "pro" in model:
        assert payload["response_format"] == {"type": "json_object"}
    else:
        assert payload["text"]["format"]["type"] == "json_schema"


@pytest.mark.structured
@pytest.mark.resilience
async def test_repair_shared_budget(api_client, mock_upstream, chat_body):
    chat_body["response_format"] = {"type": "json_object"}
    mock_upstream.responses.extend(
        [
            httpx.Response(200, json=completion("chat", "invalid")),
            httpx.ReadTimeout("test timeout"),
            httpx.Response(200, json=completion("chat", '{"ok":true}')),
        ]
    )
    response = await api_client.post("/v1/chat/completions", json=chat_body)
    assert response.status_code == 200
    assert len(mock_upstream.requests) == 3
    record = (await api_client.get("/admin/usage")).json()["data"][0]
    assert record["structured_repair_count"] == 1
    assert record["transport_retry_count"] == 1
    assert record["total_tokens"] == 38


@pytest.mark.structured
@pytest.mark.resilience
async def test_no_fourth_call(api_client, mock_upstream, chat_body):
    chat_body["response_format"] = {"type": "json_object"}
    mock_upstream.responses.extend(
        [httpx.Response(503), httpx.Response(503), httpx.Response(200, json=completion("chat", "invalid"))]
    )
    response = await api_client.post("/v1/chat/completions", json=chat_body)
    assert response.status_code == 422
    assert len(mock_upstream.requests) == 3


@pytest.mark.streaming
@pytest.mark.resilience
async def test_after_delta_failure_no_retry(api_client, mock_upstream, chat_body):
    stream = ByteStream(
        [
            data_event({"choices": [{"delta": {"content": "first"}, "finish_reason": None}]}),
            httpx.ReadError("connection lost"),
        ]
    )
    mock_upstream.responses.append(httpx.Response(200, stream=stream))
    response = await api_client.post("/v1/chat/completions", json={**chat_body, "stream": True})
    assert response.status_code == 200 and "first" in response.text
    assert '"error"' in response.text and "[DONE]" in response.text
    assert len(mock_upstream.requests) == 1 and stream.closed
    record = (await api_client.get("/admin/usage")).json()["data"][0]
    assert record["status"] == "error" and record["ttft_ms"] is not None


@pytest.mark.limits
async def test_model_buckets_independent(api_client, test_settings, mock_upstream, chat_body):
    test_settings.models["deepseek-v4-pro"].burst = 1
    test_settings.models["deepseek-v4-pro"].requests_per_minute = 1
    assert (await api_client.post("/v1/chat/completions", json=chat_body)).status_code == 200
    assert (await api_client.post("/v1/chat/completions", json=chat_body)).status_code == 429
    chat_body["model"] = "deepseek-v4-flash"
    assert (await api_client.post("/v1/chat/completions", json=chat_body)).status_code == 200
    assert len(mock_upstream.requests) == 2


@pytest.mark.prompts
async def test_prompt_versions_roles_and_concurrent_publish(api_client, mock_upstream, chat_body):
    template = {"id": "task", "name": "Task", "role": "user", "content": "v1 {{ text }}", "activate": True}
    assert (await api_client.post("/v1/prompts", json=template)).json()["version"] == 1
    template.update(content="v2 {{ text }}")
    assert (await api_client.post("/v1/prompts", json=template)).json()["version"] == 2
    for version, expected in [(None, "v2 data"), (1, "v1 data")]:
        ref = {"id": "task", "variables": {"text": "data"}}
        if version is not None:
            ref["version"] = version
        body = {"model": "deepseek-v4-pro", "messages": [], "gateway_prompt": ref}
        response = await api_client.post("/v1/chat/completions", json=body)
        assert response.status_code == 200
        assert mock_upstream.requests[-1][1]["messages"][-1] == {"role": "user", "content": expected}
    responses = await asyncio.gather(*[api_client.post("/v1/prompts", json=template) for _ in range(3)])
    assert sorted(r.json()["version"] for r in responses) == [3, 4, 5]
    assert (await api_client.post("/v1/prompts/task/activate", json={"version": 1})).status_code == 200
    detail = (await api_client.get("/v1/prompts/task")).json()
    assert detail["active_version"] == 1 and detail["versions"][0]["content"] == "v1 {{ text }}"


@pytest.mark.security
async def test_auth_validation_and_request_ids(api_client, mock_upstream, chat_body):
    for path in ["/v1/models", "/v1/prompts", "/admin/usage"]:
        response = await api_client.get(path, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    unsupported = await api_client.post("/v1/chat/completions", json={**chat_body, "tools": []})
    assert unsupported.status_code == 422
    first = await api_client.post("/v1/chat/completions", json=chat_body, headers={"X-Request-ID": "same"})
    second = await api_client.post("/v1/chat/completions", json=chat_body, headers={"X-Request-ID": "same"})
    assert first.headers["x-request-id"] != second.headers["x-request-id"]
    assert len(mock_upstream.requests) == 2


@pytest.mark.observability
async def test_recording_failures_do_not_call_twice(
    api_client, test_app, mock_upstream, chat_body, monkeypatch
):
    repo = test_app.state.services.invocations

    async def fail(*args, **kwargs):
        raise RuntimeError("private value")

    monkeypatch.setattr(repo, "finish", fail)
    response = await api_client.post("/v1/chat/completions", json=chat_body)
    assert response.status_code == 200 and len(mock_upstream.requests) == 1
    monkeypatch.setattr(repo, "create", fail)
    response = await api_client.post("/v1/chat/completions", json=chat_body)
    assert response.status_code == 503 and len(mock_upstream.requests) == 1
    assert "private value" not in response.text


@pytest.mark.streaming
@pytest.mark.resilience
async def test_stream_before_delta_can_retry(api_client, mock_upstream, chat_body):
    mock_upstream.responses.extend([httpx.Response(503), httpx.Response(503)])
    response = await api_client.post("/v1/chat/completions", json={**chat_body, "stream": True})
    assert response.status_code == 200 and "[DONE]" in response.text
    assert len(mock_upstream.requests) == 3


@pytest.mark.streaming
@pytest.mark.structured
async def test_invalid_structured_stream_is_error_without_repair(api_client, mock_upstream, chat_body):
    stream = ByteStream(
        [
            data_event({"choices": [{"delta": {"content": "not JSON"}, "finish_reason": None}]}),
            data_event({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            data_event("[DONE]"),
        ]
    )
    mock_upstream.responses.append(httpx.Response(200, stream=stream))
    response = await api_client.post(
        "/v1/chat/completions", json={**chat_body, "stream": True, "response_format": {"type": "json_object"}}
    )
    assert response.status_code == 200 and "structured_output_invalid" in response.text
    assert len(mock_upstream.requests) == 1
    record = (await api_client.get("/admin/usage")).json()["data"][0]
    assert record["status"] == "error" and record["total_tokens"] is None


@pytest.mark.limits
async def test_body_and_output_limits(api_client, test_settings, mock_upstream, chat_body):
    test_settings.limits.max_upstream_response_bytes = 10
    response = await api_client.post("/v1/chat/completions", json=chat_body)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_response_too_large"
    test_settings.limits.max_request_bytes = 10
    response = await api_client.post("/v1/chat/completions", json=chat_body)
    assert response.status_code == 413 and len(mock_upstream.requests) == 1


@pytest.mark.prompts
async def test_missing_active_version_and_variables(api_client, mock_upstream, chat_body):
    await api_client.post(
        "/v1/prompts",
        json={
            "id": "inactive",
            "name": "Inactive",
            "role": "user",
            "content": "{{ missing }}",
            "activate": False,
        },
    )
    body = {**chat_body, "gateway_prompt": {"id": "inactive", "variables": {}}}
    response = await api_client.post("/v1/chat/completions", json=body)
    assert response.json()["error"]["code"] == "prompt_version_required"
    body["gateway_prompt"]["version"] = 1
    response = await api_client.post("/v1/chat/completions", json=body)
    assert response.json()["error"]["code"] == "prompt_render_error"
    assert not mock_upstream.requests
