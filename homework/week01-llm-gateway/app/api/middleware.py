import asyncio
import json
import uuid

from app.api.errors import error_response
from app.core.security import client_request_id
from app.domain.errors import GatewayError, request_timeout
from app.services.structured import depth, reject_constant


class RequestContextMiddleware:
    """Bound body consumption and attach correlation metadata without BaseHTTPMiddleware buffering SSE."""

    def __init__(self, app, owner):
        self.app, self.owner = app, owner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        services = self.owner.state.services
        state = scope.setdefault("state", {})
        state["request_id"] = "req_" + uuid.uuid4().hex
        state["started"] = services.clock()
        headers = dict(scope.get("headers", []))
        state["client_request_id"] = client_request_id(headers.get(b"x-request-id", b"").decode("latin-1"))

        async def tagged_send(message):
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [*message.get("headers", []), (b"x-request-id", state["request_id"].encode())],
                }
            await send(message)

        replay = None
        if scope["method"] in {"POST", "PUT", "PATCH"}:
            parts, size = [], 0
            try:
                async with asyncio.timeout(services.settings.timeouts.request_timeout_seconds):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        data = message.get("body", b"")
                        size += len(data)
                        if size > services.settings.limits.max_request_bytes:
                            raise GatewayError("request_too_large", 413, "Request body exceeds limit.")
                        parts.append(data)
                        if not message.get("more_body", False):
                            break
                    body = b"".join(parts)
                    if body:
                        parsed = json.loads(body, parse_constant=reject_constant)
                        if depth(parsed) > services.settings.limits.max_json_depth:
                            raise ValueError("depth")
                    replay = {"type": "http.request", "body": body, "more_body": False}
            except (ValueError, UnicodeError, RecursionError):
                await error_response(GatewayError("invalid_request", 422, "Invalid JSON request."))(
                    scope, receive, tagged_send
                )
                return
            except GatewayError as error:
                await error_response(error)(scope, receive, tagged_send)
                return
            except TimeoutError:
                await error_response(request_timeout())(scope, receive, tagged_send)
                return

        async def replay_receive():
            nonlocal replay
            if replay is not None:
                message, replay = replay, None
                return message
            return await receive()

        await self.app(scope, replay_receive, tagged_send)
