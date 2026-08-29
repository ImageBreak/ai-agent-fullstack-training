import asyncio
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

from app.main import create_app
from tests.conftest import data_event, usage


@asynccontextmanager
async def running_server(app):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Server failed to start")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            async with asyncio.timeout(5):
                await task
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        sock.close()


@pytest.mark.streaming
@pytest.mark.parametrize("cancel", [False, True])
async def test_first_delta_before_upstream_finishes_and_cancel(test_settings, cancel):
    release = asyncio.Event()
    upstream_closed = asyncio.Event()

    async def upstream(request):
        await request.body()

        async def generate():
            try:
                yield data_event({"choices": [{"delta": {"content": "first"}, "finish_reason": None}]})
                await release.wait()
                yield data_event({"choices": [{"delta": {"content": "last"}, "finish_reason": None}]})
                yield data_event(
                    {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage("chat")}
                )
                yield data_event("[DONE]")
            finally:
                upstream_closed.set()

        return StreamingResponse(generate(), media_type="text/event-stream")

    fake = Starlette(routes=[Route("/chat/completions", upstream, methods=["POST"])])
    async with running_server(fake) as upstream_url:
        test_settings.models["deepseek-v4-pro"].base_url = upstream_url
        gateway = create_app(test_settings)
        async with running_server(gateway) as gateway_url:
            async with httpx.AsyncClient(
                base_url=gateway_url, timeout=5, headers={"Authorization": "Bearer test-gateway-key"}
            ) as client:
                async with client.stream(
                    "POST",
                    "/v1/chat/completions",
                    json={
                        "model": "deepseek-v4-pro",
                        "stream": True,
                        "messages": [{"role": "user", "content": "stream"}],
                    },
                ) as response:
                    assert response.status_code == 200
                    lines = response.aiter_lines()
                    async with asyncio.timeout(3):
                        while "first" not in await anext(lines):
                            pass
                    assert not release.is_set() and not upstream_closed.is_set()
                    if not cancel:
                        release.set()
                        rest = "\n".join([line async for line in lines])
                        assert "last" in rest and "[DONE]" in rest
                async with asyncio.timeout(3):
                    await upstream_closed.wait()
                    while True:
                        rows = (await client.get("/admin/usage")).json()["data"]
                        if rows[0]["status"] != "running":
                            break
                        await asyncio.sleep(0.01)
                assert rows[0]["status"] == ("cancelled" if cancel else "success")
