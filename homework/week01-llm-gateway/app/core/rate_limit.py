import asyncio
import math
from dataclasses import dataclass

from app.config import ModelSettings
from app.domain.errors import GatewayError


@dataclass
class Bucket:
    tokens: float
    updated: float
    active: int = 0


class RateLimiter:
    def __init__(self, clock):
        self.clock = clock
        self.buckets: dict[tuple[str, str], Bucket] = {}
        self.lock = asyncio.Lock()

    async def acquire(self, key: str, alias: str, config: ModelSettings):
        bucket_key = (key, alias)
        async with self.lock:
            now = self.clock()
            bucket = self.buckets.setdefault(bucket_key, Bucket(float(config.burst), now))
            rate = config.requests_per_minute / 60
            bucket.tokens = min(config.burst, bucket.tokens + max(0, now - bucket.updated) * rate)
            bucket.updated = now
            if bucket.active >= config.max_concurrency:
                raise GatewayError(
                    "rate_limit_exceeded",
                    429,
                    "Model concurrency limit reached.",
                    "concurrency",
                    {"Retry-After": "1"},
                )
            if bucket.tokens < 1:
                retry = max(1, math.ceil((1 - bucket.tokens) / rate))
                raise GatewayError(
                    "rate_limit_exceeded",
                    429,
                    "Model request rate limit reached.",
                    "rate_limit",
                    {"Retry-After": str(retry)},
                )
            bucket.tokens -= 1
            bucket.active += 1
        return bucket_key

    async def release(self, lease):
        if lease is not None:
            async with self.lock:
                self.buckets[lease].active -= 1
