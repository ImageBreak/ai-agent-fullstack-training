import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import fingerprint
from app.api.sse import GatewayStreamResponse, RecordedJSONResponse
from app.domain.errors import GatewayError, request_timeout
from app.schemas import ActivatePrompt, ChatRequest, PublishPrompt

router = APIRouter()


async def until_disconnect(request, operation):
    async def watch():
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    work = asyncio.create_task(operation())
    watcher = asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait({work, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            work.cancel()
            result = (await asyncio.gather(work, return_exceptions=True))[0]
            if isinstance(result, (RecordedJSONResponse, GatewayStreamResponse)):
                if isinstance(result, GatewayStreamResponse):
                    await result.iterator.aclose()
                await result.gateway.finalize(result.call, asyncio.CancelledError())
            raise asyncio.CancelledError()
        return await work
    finally:
        work.cancel()
        watcher.cancel()
        await asyncio.gather(work, watcher, return_exceptions=True)


@router.post("/v1/chat/completions")
async def chat(payload: ChatRequest, request: Request, key: str = Depends(fingerprint)):
    gateway = request.app.state.services.gateway
    call = None
    iterator = None

    async def generate():
        nonlocal call, iterator
        try:
            remaining = max(
                0, request.state.started + gateway.settings.timeouts.request_timeout_seconds - gateway.clock()
            )
            async with asyncio.timeout(remaining):
                call = await gateway.prepare(
                    payload,
                    request.state.request_id,
                    request.state.client_request_id,
                    key,
                    request.state.started,
                )
                if payload.stream:
                    iterator = gateway.stream(call)
                    first = await anext(iterator)
                    return GatewayStreamResponse(gateway, call, iterator, first)
                body = await gateway.complete(call)
                return RecordedJSONResponse(body, gateway, call)
        except BaseException as exc:
            error = request_timeout() if isinstance(exc, TimeoutError) else exc
            if iterator is not None:
                await iterator.aclose()
            if call is not None:
                await gateway.finalize(call, error)
            if isinstance(exc, TimeoutError):
                raise error from None
            raise

    return await until_disconnect(request, generate)


@router.get("/v1/models", dependencies=[Depends(fingerprint)])
async def models(request: Request):
    return request.app.state.services.router.models()


@router.post("/v1/prompts", status_code=201, dependencies=[Depends(fingerprint)])
async def publish(payload: PublishPrompt, request: Request):
    return await request.app.state.services.prompts.publish(payload)


@router.get("/v1/prompts", dependencies=[Depends(fingerprint)])
async def prompts(request: Request, limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0)):
    return {"data": await request.app.state.services.prompt_repository.list(limit, offset)}


@router.get("/v1/prompts/{identifier}", dependencies=[Depends(fingerprint)])
async def prompt(identifier: str, request: Request):
    return await request.app.state.services.prompt_repository.detail(identifier)


@router.post("/v1/prompts/{identifier}/activate", dependencies=[Depends(fingerprint)])
async def activate(identifier: str, payload: ActivatePrompt, request: Request):
    return await request.app.state.services.prompt_repository.activate(identifier, payload.version)


def usage_filters(
    model: str | None = None,
    status: str | None = None,
    stream: bool | None = None,
    from_time: datetime | None = Query(None, alias="from"),
    to_time: datetime | None = Query(None, alias="to"),
):
    if status not in {None, "running", "success", "incomplete", "error", "cancelled", "rejected"}:
        raise GatewayError("invalid_request", 422, "Unknown status filter.")

    def iso(value):
        return (
            value.replace(tzinfo=UTC).isoformat()
            if value.tzinfo is None
            else value.astimezone(UTC).isoformat()
        )

    if from_time and to_time and iso(from_time) > iso(to_time):
        raise GatewayError("invalid_request", 422, "Invalid time range.")
    return {
        "model": model,
        "status": status,
        "stream": stream,
        "from_time": iso(from_time) if from_time else None,
        "to_time": iso(to_time) if to_time else None,
    }


@router.get("/admin/usage", dependencies=[Depends(fingerprint)])
async def usage(
    request: Request,
    filters: dict = Depends(usage_filters),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return {"data": await request.app.state.services.invocations.list(limit=limit, offset=offset, **filters)}


@router.get("/admin/usage/summary", dependencies=[Depends(fingerprint)])
async def summary(request: Request, filters: dict = Depends(usage_filters)):
    return await request.app.state.services.invocations.summary(**filters)


@router.get("/healthz")
async def health():
    return {"status": "ok"}


@router.get("/readyz")
async def ready(request: Request):
    if not await request.app.state.services.database.ready():
        raise GatewayError("observability_unavailable", 503, "Database unavailable.")
    return {"status": "ready"}
