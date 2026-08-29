import pytest

from app.config import Limits, ModelSettings, RetrySettings, Settings
from app.core.rate_limit import RateLimiter
from app.core.retry import backoff
from app.domain.errors import GatewayError, UpstreamError
from app.domain.models import aggregate_usage
from app.services.prompts import PromptService
from app.services.structured import StructuredService


@pytest.mark.limits
async def test_atomic_rate_and_concurrency():
    now = [0.0]
    limiter = RateLimiter(lambda: now[0])
    config = ModelSettings(
        adapter="responses",
        base_url="http://localhost",
        api_key="test",
        upstream_model="test",
        requests_per_minute=60,
        burst=2,
        max_concurrency=1,
    )
    lease = await limiter.acquire("key", "pro", config)
    with pytest.raises(GatewayError) as error:
        await limiter.acquire("key", "pro", config)
    assert error.value.param == "concurrency"
    assert limiter.buckets[("key", "pro")].tokens == 1
    await limiter.release(lease)
    await limiter.acquire("key", "pro", config)
    await limiter.acquire("key", "flash", config)
    await limiter.release(lease)
    with pytest.raises(GatewayError) as error:
        await limiter.acquire("key", "pro", config)
    assert error.value.headers["Retry-After"] == "1"
    now[0] = 1
    await limiter.acquire("key", "pro", config)


@pytest.mark.resilience
async def test_backoff_budget_and_retry_after():
    from types import SimpleNamespace

    call = SimpleNamespace(count=1, committed=False, transport_retry_count=0, remaining=lambda: 10)
    waits = []

    async def sleep(delay):
        waits.append(delay)

    error = UpstreamError("upstream_rate_limited", 429, retryable=True, retry_after=2)
    await backoff(error, call, RetrySettings(), sleep, lambda: 0.5)
    assert waits == [2]
    assert call.transport_retry_count == 1
    call.count = 3
    with pytest.raises(UpstreamError):
        await backoff(error, call, RetrySettings(), sleep, lambda: 0.5)
    assert waits == [2]


@pytest.mark.prompts
def test_render_strict_and_sandbox():
    service = PromptService(None, Limits())
    assert service.render("Task: {{ text }}", {"text": "{{ secret }}"}) == "Task: {{ secret }}"
    for content in ["{{ missing }}", "{{ text.__class__ }}", "{{ 'x' * 100000 }}"]:
        with pytest.raises(GatewayError):
            service.render(content, {"text": "hello"})
    assert service.render("{% for x in items %}{{ x }}{% endfor %}", {"items": ["a", "b"]}) == "ab"


@pytest.mark.structured
def test_schema_and_json_rules():
    service = StructuredService(Limits())
    fmt = {
        "type": "json_schema",
        "json_schema": {
            "name": "person",
            "schema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    }
    service.check_schema(fmt)
    assert service.validate('```json\n{"name":"Ada"}\n```', fmt) == '{"name":"Ada"}'
    for text in ['{"name":3}', '{"name":NaN}', 'Here is {"name":"Ada"}']:
        with pytest.raises(GatewayError):
            service.validate(text, fmt)
    with pytest.raises(GatewayError):
        service.validate("[]", {"type": "json_object"})
    fmt["json_schema"]["schema"] = {"$ref": "https://example.test/schema"}
    with pytest.raises(GatewayError) as error:
        service.check_schema(fmt)
    assert error.value.code == "unsupported_schema"


@pytest.mark.observability
def test_usage_partial_and_subsets():
    totals, complete = aggregate_usage(
        [
            {
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 7,
                    "total_tokens": 19,
                    "cached_input_tokens": 4,
                    "reasoning_tokens": 2,
                }
            },
            {"usage": None},
        ]
    )
    assert (totals.total_tokens, totals.cached_input_tokens, totals.reasoning_tokens) == (19, 4, 2)
    assert not complete


@pytest.mark.security
def test_missing_key_rejected(test_settings):
    data = test_settings.model_dump()
    data["gateway_api_key"] = ""
    with pytest.raises(ValueError):
        Settings.model_validate(data)
