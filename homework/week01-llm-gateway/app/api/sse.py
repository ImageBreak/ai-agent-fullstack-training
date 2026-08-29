import asyncio
import json

import anyio
from starlette.responses import JSONResponse, Response

from app.domain.errors import GatewayError, request_timeout
from app.domain.models import Finished, TextDelta


def encode(data):
    return (
        "data: " + (data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)) + "\n\n"
    ).encode("utf-8")


def chunk(call, *, delta=None, reason=None, usage=False):
    body = {
        "id": call.identifier,
        "object": "chat.completion.chunk",
        "created": call.created,
        "model": call.alias,
        "choices": [] if usage else [{"index": 0, "delta": delta or {}, "finish_reason": reason}],
    }
    if usage:
        value, complete = call.usage()
        body.update(usage=value.public(), gateway_usage_complete=complete)
    return body


class RecordedJSONResponse(JSONResponse):
    def __init__(self, content, gateway, call):
        super().__init__(content)
        self.gateway, self.call = gateway, call

    async def __call__(self, scope, receive, send):
        error = None
        try:
            self.call.committed = True
            async with asyncio.timeout(self.call.remaining()):
                await super().__call__(scope, receive, send)
        except BaseException as exc:
            error = request_timeout() if isinstance(exc, TimeoutError) else exc
            raise
        finally:
            await self.gateway.finalize(self.call, error)


class GatewayStreamResponse(Response):
    media_type = "text/event-stream"

    def __init__(self, gateway, call, iterator, first):
        super().__init__(
            content=b"",
            media_type=self.media_type,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        self.raw_headers = [(key, value) for key, value in self.raw_headers if key != b"content-length"]
        self.gateway, self.call = gateway, call
        self.iterator, self.first = iterator, first

    async def __call__(self, scope, receive, send):
        error = None
        finished = anyio.Event()
        stream_completed = False
        internal_cancel = False

        async def emit(event):
            if isinstance(event, TextDelta):
                if self.call.ttft_ms is None:
                    self.call.ttft_ms = max(0, (self.call.clock() - self.call.started) * 1000)
                await send(
                    {
                        "type": "http.response.body",
                        "body": encode(chunk(self.call, delta={"content": event.text})),
                        "more_body": True,
                    }
                )
            elif isinstance(event, Finished):
                await send(
                    {
                        "type": "http.response.body",
                        "body": encode(chunk(self.call, reason=event.reason)),
                        "more_body": True,
                    }
                )
                if self.call.request.stream_options and self.call.request.stream_options.include_usage:
                    await send(
                        {
                            "type": "http.response.body",
                            "body": encode(chunk(self.call, usage=True)),
                            "more_body": True,
                        }
                    )

        async def run_stream(group):
            nonlocal error, stream_completed
            try:
                self.call.committed = True
                await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
                async with asyncio.timeout(self.call.remaining()):
                    await emit(self.first)
                    async for event in self.iterator:
                        await emit(event)
                    await send({"type": "http.response.body", "body": encode("[DONE]"), "more_body": False})
                    stream_completed = True
            except asyncio.CancelledError as exc:
                error = error or exc
                raise
            except Exception as exc:
                error = request_timeout() if isinstance(exc, TimeoutError) else exc
                if isinstance(exc, OSError):
                    return
                public = (
                    error
                    if isinstance(error, GatewayError)
                    else GatewayError("internal_error", 500, "Internal gateway error.")
                )
                # An expired business deadline does not permit unbounded error delivery.
                with anyio.move_on_after(self.gateway.settings.timeouts.cleanup_timeout_seconds, shield=True):
                    try:
                        await send(
                            {
                                "type": "http.response.body",
                                "body": encode(public.payload()) + encode("[DONE]"),
                                "more_body": False,
                            }
                        )
                    except OSError:
                        pass
            finally:
                finished.set()

        async def disconnected(group):
            nonlocal error
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    # A server can report disconnect immediately after the
                    # terminal body was accepted. That is a normal completion.
                    if not stream_completed and error is None:
                        error = asyncio.CancelledError()
                    finished.set()
                    return

        try:
            async with anyio.create_task_group() as group:
                group.start_soon(run_stream, group)
                group.start_soon(disconnected, group)
                await finished.wait()
                internal_cancel = True
                group.cancel_scope.cancel()
        except asyncio.CancelledError as exc:
            if not internal_cancel:
                error = error or exc
                raise
        finally:
            with anyio.move_on_after(self.gateway.settings.timeouts.cleanup_timeout_seconds, shield=True):
                await self.iterator.aclose()
            await self.gateway.finalize(self.call, error)
