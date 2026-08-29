from app.adapters.base import Adapter, parse_usage, protocol_error, sse_events
from app.domain.errors import UpstreamError
from app.domain.models import Finished, NormalizedResponse, TextDelta


def finish_reason(data):
    if data.get("status") == "completed":
        return "stop"
    if data.get("status") == "incomplete":
        reason = (data.get("incomplete_details") or {}).get("reason")
        if reason in {"max_output_tokens", "content_filter"}:
            return "length" if reason == "max_output_tokens" else "content_filter"
    raise protocol_error()


class ResponsesAdapter(Adapter):
    path = "/responses"

    def payload(self, request, stream):
        system = [m["content"] for m in request.messages if m["role"] == "system"]
        messages = [m for m in request.messages if m["role"] != "system"]
        body = {
            "model": self.model.upstream_model,
            "input": messages,
            "max_output_tokens": request.max_tokens,
            "stream": stream,
            "reasoning": {"effort": self.model.reasoning_effort if self.model.thinking_enabled else "none"},
        }
        if system:
            body["instructions"] = "\n\n".join(system)
        for key in ("temperature", "top_p"):
            if getattr(request, key) is not None:
                body[key] = getattr(request, key)
        if request.response_format:
            spec = request.response_format
            fmt = {"type": spec["type"]}
            if spec["type"] == "json_schema":
                fmt.update(spec["json_schema"])
            body["text"] = {"format": fmt}
            body["instructions"] = body.get("instructions", "") + "\nReturn only valid JSON without Markdown."
        return body

    async def complete(self, request):
        async with self.response(request, False) as response:
            data = await self.read_json(response)
        reason = finish_reason(data)
        try:
            parts = []
            for item in data["output"]:
                if item.get("type") == "message":
                    for part in item.get("content", []):
                        if part.get("type") == "refusal":
                            reason = "content_filter"
                        if part.get("type") == "output_text":
                            if not isinstance(part.get("text"), str):
                                raise TypeError()
                            parts.append(part["text"])
            return NormalizedResponse("".join(parts), reason, parse_usage(data.get("usage"), "responses"))
        except (KeyError, TypeError, AttributeError):
            raise protocol_error() from None

    async def stream(self, request):
        refusal = False
        async with self.response(request, True) as response:
            async for kind, data in sse_events(response, self.settings.limits.max_sse_event_bytes):
                if data == "[DONE]":
                    raise protocol_error(True)
                if kind == "response.output_text.delta":
                    text = data.get("delta")
                    if not isinstance(text, str):
                        raise protocol_error(True)
                    if text:
                        yield TextDelta(text)
                elif kind == "response.refusal.delta":
                    refusal = True
                elif kind in {"response.completed", "response.incomplete"}:
                    final = data.get("response")
                    if not isinstance(final, dict):
                        raise protocol_error(True)
                    if final.get("usage") is not None:
                        yield parse_usage(final["usage"], "responses")
                    yield Finished("content_filter" if refusal else finish_reason(final))
                    return
                elif kind in {"response.failed", "error"}:
                    raise protocol_error(True)
                # Reasoning, metadata, and output_text.done do not produce answer deltas.
        raise UpstreamError(
            "upstream_stream_error", 502, "Upstream stream ended without a terminal event.", retryable=True
        )
