from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None

    @property
    def complete(self) -> bool:
        return all(v is not None for v in (self.input_tokens, self.output_tokens, self.total_tokens))

    def public(self):
        return {
            "prompt_tokens": self.input_tokens,
            "completion_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "prompt_tokens_details": {"cached_tokens": self.cached_input_tokens},
            "completion_tokens_details": {"reasoning_tokens": self.reasoning_tokens},
        }

    def to_dict(self):
        return asdict(self)


@dataclass
class TextDelta:
    text: str


@dataclass
class Finished:
    reason: str = "stop"


StreamEvent = TextDelta | Usage | Finished


@dataclass
class NormalizedRequest:
    messages: list[dict[str, str]]
    max_tokens: int
    temperature: float | None = None
    top_p: float | None = None
    response_format: dict[str, Any] | None = None


@dataclass
class NormalizedResponse:
    text: str
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)


def aggregate_usage(attempts: list[dict]) -> tuple[Usage, bool]:
    values = [a.get("usage") or {} for a in attempts]
    totals = {}
    for key in Usage.__dataclass_fields__:
        known = [v[key] for v in values if v.get(key) is not None]
        totals[key] = sum(known) if known else None
    complete = bool(values) and all(
        all(v.get(k) is not None for k in ("input_tokens", "output_tokens", "total_tokens")) for v in values
    )
    return Usage(**totals), complete
