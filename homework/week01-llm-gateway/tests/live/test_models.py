import json

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.config import load_settings
from app.main import create_app

pytestmark = pytest.mark.live


@pytest.mark.parametrize("model", ["deepseek-v4-pro", "deepseek-v4-flash"])
@pytest.mark.parametrize("mode", ["text", "stream", "json"])
async def test_real_model(model, mode, tmp_path, record_property):
    settings = load_settings()
    settings.database_path = tmp_path / "live.db"
    app = create_app(settings)
    body = {
        "model": model,
        "max_tokens": 128,
        "messages": [{"role": "user", "content": 'Return JSON {"ok":true} without Markdown.'}],
    }
    if mode == "stream":
        body.update(stream=True, stream_options={"include_usage": True})
    elif mode == "json":
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "answer",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            },
        }
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway.test",
            headers={"Authorization": "Bearer " + settings.gateway_api_key.get_secret_value()},
        ) as client:
            response = await client.post("/v1/chat/completions", json=body)
            assert response.status_code == 200
            if mode == "stream":
                events = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
                assert events[-1] == "[DONE]"
                assert all("error" not in json.loads(item) for item in events[:-1])
            else:
                text = response.json()["choices"][0]["message"]["content"]
                assert text.strip()
                if mode == "json":
                    assert isinstance(json.loads(text)["ok"], bool)
            record = (await client.get("/admin/usage")).json()["data"][0]
            assert record["status"] == "success" and record["usage_complete"]
            for key in [
                "request_id",
                "model_alias",
                "adapter_type",
                "status",
                "total_tokens",
                "total_latency_ms",
                "ttft_ms",
                "created_at",
            ]:
                record_property(key, record[key])
