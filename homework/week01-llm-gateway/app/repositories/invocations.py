import json


class InvocationRepository:
    def __init__(self, database):
        self.db = database

    async def create(self, data):
        columns = ", ".join(data)
        async with self.db.transaction() as conn:
            await conn.execute(
                f"INSERT INTO invocations ({columns}) VALUES ({', '.join('?' for _ in data)})",
                tuple(data.values()),
            )

    async def finish(self, identifier, data):
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE invocations SET " + ", ".join(f"{key}=?" for key in data) + " WHERE request_id=?",
                (*data.values(), identifier),
            )

    def filters(self, model=None, status=None, stream=None, from_time=None, to_time=None, **_):
        clauses, values = [], []
        for column, operator, value in [
            ("model_alias", "=", model),
            ("status", "=", status),
            ("stream", "=", stream),
            ("created_at", ">=", from_time),
            ("created_at", "<=", to_time),
        ]:
            if value is not None:
                clauses.append(f"{column} {operator} ?")
                values.append(value)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), values

    async def list(self, limit=20, offset=0, **filters):
        where, values = self.filters(**filters)
        rows = await self.db.fetchall(
            "SELECT * FROM invocations" + where + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*values, limit, offset),
        )
        for row in rows:
            row["attempt_details"] = json.loads(row["attempt_details"])
            row["stream"] = bool(row["stream"])
            row["usage_complete"] = bool(row["usage_complete"])
        return rows

    async def summary(self, **filters):
        where, values = self.filters(**filters)
        measures = """COUNT(*) AS requests,
            SUM(status='success') AS success_requests,
            SUM(status='error') AS error_requests,
            SUM(usage_complete=1) AS complete_usage_requests,
            SUM(usage_complete=0) AS incomplete_usage_requests,
            SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
            SUM(total_tokens) AS total_tokens, SUM(cached_input_tokens) AS cached_input_tokens,
            SUM(reasoning_tokens) AS reasoning_tokens,
            AVG(total_latency_ms) AS avg_latency_ms, AVG(ttft_ms) AS avg_ttft_ms"""
        total = (await self.db.fetchall(f"SELECT {measures} FROM invocations" + where, values))[0]
        groups = {}
        for key, column in [("by_model", "model_alias"), ("by_status", "status"), ("by_stream", "stream")]:
            groups[key] = await self.db.fetchall(
                f"SELECT {column}, {measures} FROM invocations" + where + f" GROUP BY {column}", values
            )
        return {"total": total, **groups}
