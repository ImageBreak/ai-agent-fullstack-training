import json

from app.adapters.base import Adapter, parse_usage, protocol_error, sse_events
from app.domain.errors import UpstreamError
from app.domain.models import Finished, NormalizedResponse, TextDelta


class ChatCompletionsAdapter(Adapter):
    path = "/chat/completions"

    def payload(self, request, stream):
        messages = list(request.messages)
        body = {
            "model": self.model.upstream_model,
            "messages": messages,
            "stream": stream,
            "max_tokens": request.max_tokens,
            "thinking": {"type": "enabled" if self.model.thinking_enabled else "disabled"},
        }
        if self.model.thinking_enabled:
            body["reasoning_effort"] = self.model.reasoning_effort
        for key in ("temperature", "top_p"):
            value = getattr(request, key)
            if value is not None:
                body[key] = value
        if request.response_format:
            body["response_format"] = {"type": "json_object"}
            instruction = "Return only valid JSON without Markdown."
            if request.response_format["type"] == "json_schema":
                instruction += " Follow this JSON Schema: " + json.dumps(
                    request.response_format["json_schema"]["schema"], ensure_ascii=False
                )
            body["messages"] = [{"role": "system", "content": instruction}, *messages]
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    async def complete(self, request):
        async with self.response(request, False) as response:
            data = await self.read_json(response)
        try:
            choice = data["choices"][0]
            message = choice["message"]
            text = message.get("content") or ""
            reason = "content_filter" if message.get("refusal") else choice["finish_reason"]
            if not isinstance(text, str) or reason not in {"stop", "length", "content_filter"}:
                raise ValueError()
            return NormalizedResponse(text, reason, parse_usage(data.get("usage"), "chat_completions"))
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise protocol_error() from None

    async def stream(self, request):
        reason = None
        async with self.response(request, True) as response:
            async for _, data in sse_events(response, self.settings.limits.max_sse_event_bytes):
                if data == "[DONE]":
                    if reason is None:
                        raise protocol_error(True)
                    yield Finished(reason)
                    return
                if data.get("error"):
                    raise protocol_error(True)
                if data.get("usage") is not None:
                    yield parse_usage(data["usage"], "chat_completions")
                try:
                    choices = data.get("choices", [])
                    if choices:
                        choice = choices[0]
                        delta = choice.get("delta", {})
                        text = delta.get("content")
                        if text is not None and not isinstance(text, str):
                            raise TypeError()
                        if text:
                            yield TextDelta(text)
                        if delta.get("refusal"):
                            reason = "content_filter"
                        if choice.get("finish_reason") is not None:
                            reason = choice["finish_reason"]
                            if reason not in {"stop", "length", "content_filter"}:
                                raise ValueError()
                except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                    raise protocol_error(True) from None
        raise UpstreamError(
            "upstream_stream_error", 502, "Upstream stream ended without a terminal event.", retryable=True
        )
