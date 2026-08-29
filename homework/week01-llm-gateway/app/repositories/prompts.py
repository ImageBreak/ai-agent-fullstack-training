from datetime import UTC, datetime

from app.domain.errors import GatewayError


class PromptRepository:
    def __init__(self, database):
        self.db = database

    async def publish(self, payload):
        now = datetime.now(UTC).isoformat()
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO prompt_templates VALUES (?, ?, NULL, ?, ?)",
                (payload.id, payload.name, now, now),
            )
            async with conn.execute(
                "SELECT COALESCE(MAX(version), 0)+1 FROM prompt_versions WHERE prompt_id=?",
                (payload.id,),
            ) as cursor:
                version = (await cursor.fetchone())[0]
            await conn.execute(
                "INSERT INTO prompt_versions VALUES (?, ?, ?, ?, ?)",
                (payload.id, version, payload.role, payload.content, now),
            )
            await conn.execute(
                "UPDATE prompt_templates SET name=?, updated_at=? WHERE id=?", (payload.name, now, payload.id)
            )
            if payload.activate:
                await conn.execute(
                    "UPDATE prompt_templates SET active_version=? WHERE id=?", (version, payload.id)
                )
        return {"id": payload.id, "version": version, "activated": payload.activate}

    async def list(self, limit=100, offset=0):
        return await self.db.fetchall(
            "SELECT * FROM prompt_templates ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
        )

    async def detail(self, identifier):
        rows = await self.db.fetchall("SELECT * FROM prompt_templates WHERE id=?", (identifier,))
        if not rows:
            raise GatewayError("prompt_not_found", 404, "Prompt not found.")
        rows[0]["versions"] = await self.db.fetchall(
            "SELECT * FROM prompt_versions WHERE prompt_id=? ORDER BY version", (identifier,)
        )
        return rows[0]

    async def resolve(self, identifier, version):
        # Single query fixes the active pointer and selected immutable content together.
        rows = await self.db.fetchall(
            """SELECT p.active_version, v.* FROM prompt_templates p
               LEFT JOIN prompt_versions v ON v.prompt_id=p.id
                 AND v.version=COALESCE(?, p.active_version)
               WHERE p.id=?""",
            (version, identifier),
        )
        if not rows:
            raise GatewayError("prompt_not_found", 404, "Prompt not found.")
        row = rows[0]
        if row["version"] is None:
            if version is None and row["active_version"] is None:
                raise GatewayError("prompt_version_required", 422, "Select a prompt version.")
            raise GatewayError("prompt_not_found", 404, "Prompt version not found.")
        return row

    async def activate(self, identifier, version):
        async with self.db.transaction() as conn:
            async with conn.execute(
                "SELECT 1 FROM prompt_versions WHERE prompt_id=? AND version=?", (identifier, version)
            ) as cursor:
                if await cursor.fetchone() is None:
                    raise GatewayError("prompt_not_found", 404, "Prompt version not found.")
            await conn.execute(
                "UPDATE prompt_templates SET active_version=?, updated_at=? WHERE id=?",
                (version, datetime.now(UTC).isoformat(), identifier),
            )
        return {"id": identifier, "active_version": version}
