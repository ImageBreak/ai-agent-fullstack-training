import codecs
import json
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from app.domain.errors import GatewayError, UpstreamError
from app.domain.models import Usage
from app.services.structured import reject_constant


def number(value):
    return value if type(value) is int and value >= 0 else None


def details(value):
    return value if isinstance(value, dict) else {}


def parse_usage(data, protocol):
    if not isinstance(data, dict):
        return Usage()
    if protocol == "chat_completions":
        return Usage(
            number(data.get("prompt_tokens")),
            number(data.get("completion_tokens")),
            number(data.get("total_tokens")),
            number(
                data.get(
                    "prompt_cache_hit_tokens", details(data.get("prompt_tokens_details")).get("cached_tokens")
                )
            ),
            number(details(data.get("completion_tokens_details")).get("reasoning_tokens")),
        )
    return Usage(
        number(data.get("input_tokens")),
        number(data.get("output_tokens")),
        number(data.get("total_tokens")),
        number(details(data.get("input_tokens_details")).get("cached_tokens")),
        number(details(data.get("output_tokens_details")).get("reasoning_tokens")),
    )


def retry_after(value):
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if seconds < float("inf") else None
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
            return max(0.0, (moment - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def protocol_error(stream=False):
    return UpstreamError(
        "upstream_stream_error" if stream else "upstream_error",
        502,
        "Upstream response does not match the expected protocol.",
    )


async def sse_events(response, limit):
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending, data, event = "", [], ""
    size = 0

    def parse():
        if not data:
            return None
        text = "\n".join(data)
        if text == "[DONE]":
            return event, text
        try:
            value = json.loads(text, parse_constant=reject_constant)
        except (ValueError, RecursionError):
            raise protocol_error(True) from None
        if not isinstance(value, dict):
            raise protocol_error(True)
        return event or value.get("type", ""), value

    try:
        async for chunk in response.aiter_bytes():
            pending += decoder.decode(chunk)
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                line = line.removesuffix("\r")
                size += len(line.encode("utf-8")) + 1
                if size > limit:
                    raise GatewayError("upstream_response_too_large", 502, "SSE event exceeds limit.")
                if not line:
                    parsed = parse()
                    if parsed is not None:
                        yield parsed
                    data, event, size = [], "", 0
                elif line.startswith(":"):
                    continue
                else:
                    key, _, value = line.partition(":")
                    value = value.removeprefix(" ")
                    if key == "data":
                        data.append(value)
                    elif key == "event":
                        event = value
            if size + len(pending.encode("utf-8")) > limit:
                raise GatewayError("upstream_response_too_large", 502, "SSE event exceeds limit.")
        pending += decoder.decode(b"", final=True)
        # A final event without the SSE terminating blank line is intentionally incomplete.
        if pending or data:
            raise protocol_error(True)
    except UnicodeError:
        raise protocol_error(True) from None


class Adapter(ABC):
    path: str

    def __init__(self, client, model, settings):
        self.client = client
        self.model = model
        self.settings = settings

    @abstractmethod
    def payload(self, request, stream):
        raise NotImplementedError

    @abstractmethod
    async def complete(self, request):
        raise NotImplementedError

    @abstractmethod
    def stream(self, request):
        raise NotImplementedError

    @asynccontextmanager
    async def response(self, request, stream):
        timeouts = self.settings.timeouts
        timeout = httpx.Timeout(
            timeouts.read_idle_timeout_seconds,
            connect=timeouts.connect_timeout_seconds,
            pool=timeouts.connect_timeout_seconds,
        )
        try:
            async with self.client.stream(
                "POST",
                self.model.base_url.rstrip("/") + self.path,
                headers={"Authorization": "Bearer " + self.model.api_key.get_secret_value()},
                json=self.payload(request, stream),
                timeout=timeout,
            ) as response:
                if not 200 <= response.status_code < 300:
                    status = response.status_code
                    limited = status == 429
                    raise UpstreamError(
                        "upstream_rate_limited" if limited else "upstream_error",
                        429 if limited else 502,
                        "Upstream request failed.",
                        retryable=status in {429, 500, 502, 503, 504},
                        retry_after=retry_after(response.headers.get("Retry-After")),
                    )
                yield response
        except httpx.TimeoutException:
            raise UpstreamError(
                "upstream_timeout", 504, "Upstream request timed out.", retryable=True
            ) from None
        except (httpx.NetworkError, httpx.RemoteProtocolError):
            raise UpstreamError(
                "upstream_error", 502, "Upstream connection failed.", retryable=True
            ) from None
        except httpx.HTTPError:
            raise UpstreamError("upstream_error", 502, "Upstream HTTP exchange failed.") from None

    async def read_json(self, response):
        parts, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self.settings.limits.max_upstream_response_bytes:
                raise GatewayError("upstream_response_too_large", 502, "Upstream response exceeds limit.")
            parts.append(chunk)
        try:
            value = json.loads(b"".join(parts), parse_constant=reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise protocol_error() from None
        if not isinstance(value, dict):
            raise protocol_error()
        return value
