import asyncio
import logging
import time
from dataclasses import dataclass, field, replace

import anyio

from app.core.retry import backoff
from app.domain.errors import GatewayError, UpstreamError, request_timeout
from app.domain.models import Finished, NormalizedRequest, TextDelta, Usage, aggregate_usage

logger = logging.getLogger("gateway")


@dataclass
class Call:
    identifier: str
    client_identifier: str | None
    alias: str
    adapter: object
    request: object
    clock: object
    started: float
    timeout: float
    normalized: NormalizedRequest | None = None
    prompt_id: str | None = None
    prompt_version: int | None = None
    attempts: list = field(default_factory=list)
    transport_retry_count: int = 0
    structured_repair_count: int = 0
    committed: bool = False
    finalized: bool = False
    lease: tuple | None = None
    ttft_ms: float | None = None
    status: str = "running"
    http_status: int = 200
    error_code: str | None = None
    created: int = field(default_factory=lambda: int(time.time()))

    @property
    def count(self):
        return len(self.attempts)

    def remaining(self):
        return max(0, self.started + self.timeout - self.clock())

    def attempt(self, stage):
        item = {"number": self.count + 1, "stage": stage, "status": "running", "usage": None}
        self.attempts.append(item)
        return item

    def usage(self):
        return aggregate_usage(self.attempts)


class Gateway:
    def __init__(self, settings, router, prompts, structured, usage, limiter, clock, sleep, random):
        self.settings, self.router, self.prompts = settings, router, prompts
        self.structured, self.usage, self.limiter = structured, usage, limiter
        self.clock, self.sleep, self.random = clock, sleep, random

    async def prepare(self, request, identifier, client_identifier, fingerprint, started):
        adapter = self.router.resolve(request.model)
        call = Call(
            identifier,
            client_identifier,
            request.model,
            adapter,
            request,
            self.clock,
            started,
            self.settings.timeouts.request_timeout_seconds,
        )
        await self.usage.start(call, fingerprint)
        try:
            messages, call.prompt_id, call.prompt_version = await self.prompts.apply(
                [message.model_dump() for message in request.messages], request.gateway_prompt
            )
            seen_non_system = False
            for message in messages:
                if message["role"] == "system" and seen_non_system:
                    raise GatewayError("invalid_request", 422, "System messages must come first.", "messages")
                seen_non_system |= message["role"] != "system"
            if not any(m["role"] == "user" and m["content"].strip() for m in messages):
                raise GatewayError("invalid_request", 422, "A user task is required.", "messages")
            tokens = request.max_tokens or self.settings.limits.default_max_tokens
            if tokens > self.settings.limits.max_output_tokens:
                raise GatewayError("invalid_request", 422, "Output token limit exceeded.", "max_tokens")
            if adapter.model.thinking_enabled and (
                request.temperature is not None or request.top_p is not None
            ):
                raise GatewayError("unsupported_feature", 422, "Sampling is unavailable in thinking mode.")
            fmt = (
                request.response_format.model_dump(by_alias=True, exclude_none=True)
                if request.response_format
                else None
            )
            self.structured.check_schema(fmt)
            call.normalized = NormalizedRequest(messages, tokens, request.temperature, request.top_p, fmt)
            call.lease = await self.limiter.acquire(fingerprint, request.model, adapter.model)
            return call
        except BaseException as error:
            await self.finalize(call, error)
            raise

    async def _complete_attempts(self, call, stage):
        while True:
            if call.remaining() <= 0:
                raise request_timeout()
            attempt = call.attempt(stage)
            try:
                result = await call.adapter.complete(call.normalized)
                attempt["usage"] = result.usage.to_dict()
                attempt["status"] = "success" if result.finish_reason == "stop" else "incomplete"
                return result
            except UpstreamError as error:
                attempt.update(status="error", error_code=error.code)
                await backoff(error, call, self.settings.retry, self.sleep, self.random)
            except BaseException:
                attempt["status"] = "cancelled" if asyncio.current_task().cancelling() else "error"
                raise

    async def complete(self, call):
        stage = "generate"
        while True:
            result = await self._complete_attempts(call, stage)
            fmt = call.normalized.response_format
            if fmt and result.finish_reason != "stop":
                raise GatewayError("structured_output_invalid", 422, "Structured response was incomplete.")
            try:
                text = self.structured.validate(result.text, fmt)
            except GatewayError as error:
                can_repair = (
                    error.code == "structured_output_invalid"
                    and call.structured_repair_count < self.settings.retry.max_structured_repairs
                    and call.count < self.settings.retry.max_upstream_calls
                    and call.remaining() > 0
                )
                if not can_repair:
                    raise
                call.structured_repair_count += 1
                call.normalized = replace(
                    call.normalized,
                    messages=self.structured.repair_messages(
                        call.normalized.messages, result.text, error, fmt
                    ),
                )
                stage = "repair"
                continue
            call.status = "success" if result.finish_reason == "stop" else "incomplete"
            usage, complete = call.usage()
            return {
                "id": call.identifier,
                "object": "chat.completion",
                "created": call.created,
                "model": call.alias,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": result.finish_reason,
                    }
                ],
                "usage": usage.public(),
                "gateway_usage_complete": complete,
            }

    async def stream(self, call):
        delivered = False
        while True:
            if call.remaining() <= 0:
                raise request_timeout()
            attempt = call.attempt("generate")
            parts, size = [], 0
            upstream = call.adapter.stream(call.normalized)
            try:
                async for event in upstream:
                    if isinstance(event, Usage):
                        # Each usage event is a snapshot, not a token increment.
                        attempt["usage"] = event.to_dict()
                    elif isinstance(event, TextDelta):
                        if call.normalized.response_format:
                            size += len(event.text.encode("utf-8"))
                            if size > self.settings.limits.max_structured_output_bytes:
                                raise GatewayError(
                                    "upstream_response_too_large",
                                    502,
                                    "Structured stream exceeds buffer limit.",
                                )
                            parts.append(event.text)
                        delivered = True
                        yield event
                    elif isinstance(event, Finished):
                        attempt["status"] = "success" if event.reason == "stop" else "incomplete"
                        if call.normalized.response_format:
                            if event.reason != "stop":
                                raise GatewayError(
                                    "structured_output_invalid", 422, "Structured response was incomplete."
                                )
                            self.structured.validate(
                                "".join(parts), call.normalized.response_format, stream=True
                            )
                        call.status = "success" if event.reason == "stop" else "incomplete"
                        yield event
                        return
            except UpstreamError as error:
                attempt.update(status="error", error_code=error.code)
                if delivered or call.committed:
                    raise
                await backoff(error, call, self.settings.retry, self.sleep, self.random)
            except BaseException as error:
                if attempt["status"] == "running":
                    attempt["status"] = (
                        "cancelled" if isinstance(error, (asyncio.CancelledError, GeneratorExit)) else "error"
                    )
                raise
            finally:
                await upstream.aclose()

    async def finalize(self, call, error=None):
        if call.finalized:
            return
        call.finalized = True
        if isinstance(error, asyncio.CancelledError) and call.remaining() <= 0:
            error = request_timeout()
        if isinstance(error, GatewayError):
            call.status = "rejected" if error.code == "rate_limit_exceeded" else "error"
            call.error_code = error.code
            call.http_status = 200 if call.committed else error.status
        elif isinstance(error, (asyncio.CancelledError, GeneratorExit, OSError)):
            call.status, call.error_code = "cancelled", "client_cancelled"
        elif error is not None:
            call.status, call.error_code = "error", "internal_error"
            call.http_status = 200 if call.committed else 500
        if call.status == "running":
            call.status, call.error_code = "error", "incomplete_lifecycle"
        with anyio.move_on_after(self.settings.timeouts.cleanup_timeout_seconds, shield=True) as scope:
            await self.limiter.release(call.lease)
            call.lease = None
            await self.usage.finish(call)
        if scope.cancel_called:
            logger.error("call_cleanup_timeout request_id=%s", call.identifier)
