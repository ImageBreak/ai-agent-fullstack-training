from app.domain.errors import UpstreamError


async def backoff(error: UpstreamError, call, settings, sleep, random):
    if not error.retryable or call.count >= settings.max_upstream_calls or call.committed:
        raise error
    delay = settings.base_delay_seconds * (2**call.transport_retry_count)
    delay += settings.jitter_seconds * random()
    delay = max(delay, error.retry_after or 0)
    if delay >= call.remaining():
        raise error
    await sleep(delay)
    call.transport_retry_count += 1
