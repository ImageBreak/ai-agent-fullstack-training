import asyncio
from contextlib import asynccontextmanager

import aiosqlite

DDL = """
CREATE TABLE IF NOT EXISTS prompt_templates(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, active_version INTEGER,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prompt_versions(
 prompt_id TEXT NOT NULL REFERENCES prompt_templates(id),
 version INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('system','user')),
 content TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(prompt_id, version)
);
CREATE TABLE IF NOT EXISTS invocations(
 request_id TEXT PRIMARY KEY, client_request_id TEXT, key_fingerprint TEXT NOT NULL,
 model_alias TEXT NOT NULL, provider_model TEXT NOT NULL, adapter_type TEXT NOT NULL,
 stream INTEGER NOT NULL, status TEXT NOT NULL, http_status INTEGER,
 error_code TEXT, prompt_id TEXT, prompt_version INTEGER, created_at TEXT NOT NULL,
 finished_at TEXT, total_latency_ms REAL, ttft_ms REAL,
 input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
 cached_input_tokens INTEGER, reasoning_tokens INTEGER, usage_complete INTEGER NOT NULL DEFAULT 0,
 upstream_call_count INTEGER NOT NULL DEFAULT 0, transport_retry_count INTEGER NOT NULL DEFAULT 0,
 structured_repair_count INTEGER NOT NULL DEFAULT 0, attempt_details TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS invocations_created ON invocations(created_at);
CREATE INDEX IF NOT EXISTS invocations_model_status ON invocations(model_alias, status);
"""


class Database:
    def __init__(self, path):
        self.path = path
        self.connection = None
        self.lock = asyncio.Lock()

    async def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(str(self.path), isolation_level=None)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.execute("PRAGMA foreign_keys=ON")
        await self.connection.execute("PRAGMA busy_timeout=1000")
        await self.connection.executescript(DDL)
        await self.connection.execute(
            "UPDATE invocations SET status='error', error_code='process_interrupted' WHERE status='running'"
        )

    async def close(self):
        if self.connection is not None:
            await self.connection.close()

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            try:
                await self.connection.execute("BEGIN IMMEDIATE")
                yield self.connection
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise

    async def fetchall(self, query, values=()):
        async with self.lock:
            async with self.connection.execute(query, values) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def ready(self):
        try:
            return bool(await self.fetchall("SELECT 1"))
        except Exception:
            return False
