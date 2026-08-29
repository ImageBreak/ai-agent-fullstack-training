import json
import logging
from datetime import UTC, datetime

from app.domain.errors import GatewayError
from app.domain.models import aggregate_usage

logger = logging.getLogger("gateway.usage")


class UsageService:
    def __init__(self, repository):
        self.repository = repository

    async def start(self, call, fingerprint):
        try:
            await self.repository.create(
                {
                    "request_id": call.identifier,
                    "client_request_id": call.client_identifier,
                    "key_fingerprint": fingerprint,
                    "model_alias": call.alias,
                    "provider_model": call.adapter.model.upstream_model,
                    "adapter_type": call.adapter.model.adapter,
                    "stream": int(call.request.stream),
                    "status": "running",
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
        except Exception:
            logger.error("usage_start_failed request_id=%s", call.identifier)
            raise GatewayError(
                "observability_unavailable",
                503,
                "Call recording is unavailable; no upstream request was sent.",
            ) from None

    async def finish(self, call):
        usage, complete = aggregate_usage(call.attempts)
        data = {
            "status": call.status,
            "http_status": call.http_status,
            "error_code": call.error_code,
            "prompt_id": call.prompt_id,
            "prompt_version": call.prompt_version,
            "finished_at": datetime.now(UTC).isoformat(),
            "total_latency_ms": max(0, (call.clock() - call.started) * 1000),
            "ttft_ms": call.ttft_ms,
            "upstream_call_count": call.count,
            "transport_retry_count": call.transport_retry_count,
            "structured_repair_count": call.structured_repair_count,
            "attempt_details": json.dumps(call.attempts),
            "usage_complete": int(complete),
            **usage.to_dict(),
        }
        try:
            await self.repository.finish(call.identifier, data)
        except Exception:
            logger.error("usage_finish_failed request_id=%s", call.identifier)
