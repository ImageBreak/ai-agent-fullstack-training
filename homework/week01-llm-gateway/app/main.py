"""Application factory. Importing this module never starts a service or loads API keys."""

import asyncio
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from app.api.errors import (
    error_response,
    gateway_error_handler,
    internal_error_handler,
    validation_error_handler,
)
from app.api.middleware import RequestContextMiddleware
from app.api.routes import router
from app.config import Settings, load_settings
from app.core.rate_limit import RateLimiter
from app.domain.errors import GatewayError
from app.repositories.database import Database
from app.repositories.invocations import InvocationRepository
from app.repositories.prompts import PromptRepository
from app.services.gateway import Gateway
from app.services.prompts import PromptService
from app.services.router import Router
from app.services.structured import StructuredService
from app.services.usage import UsageService


@dataclass
class Dependencies:
    transport: httpx.AsyncBaseTransport | None = None
    clock: object = time.monotonic
    sleep: object = asyncio.sleep
    random: object = random.random


def create_app(settings: Settings | None = None, dependencies: Dependencies | None = None):
    settings = settings or load_settings()
    deps = dependencies or Dependencies()

    @asynccontextmanager
    async def lifespan(application):
        database = Database(settings.database_path)
        capacity = sum(m.max_concurrency for m in settings.models.values() if m.enabled)
        pool = httpx.Limits(max_connections=capacity, max_keepalive_connections=capacity)
        client = httpx.AsyncClient(
            transport=deps.transport or httpx.AsyncHTTPTransport(retries=0, limits=pool, trust_env=False),
            limits=pool,
            follow_redirects=False,
            trust_env=False,
        )
        try:
            await database.open()
            invocations = InvocationRepository(database)
            prompt_repository = PromptRepository(database)
            prompts = PromptService(prompt_repository, settings.limits)
            model_router = Router(settings, client)
            usage = UsageService(invocations)
            limiter = RateLimiter(deps.clock)
            gateway = Gateway(
                settings,
                model_router,
                prompts,
                StructuredService(settings.limits),
                usage,
                limiter,
                deps.clock,
                deps.sleep,
                deps.random,
            )
            application.state.services = SimpleNamespace(
                settings=settings,
                clock=deps.clock,
                database=database,
                client=client,
                invocations=invocations,
                prompt_repository=prompt_repository,
                prompts=prompts,
                router=model_router,
                gateway=gateway,
                limiter=limiter,
            )
            yield
        finally:
            await client.aclose()
            await database.close()

    application = FastAPI(
        title="Week 01 LLM Gateway",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    application.add_exception_handler(GatewayError, gateway_error_handler)
    application.add_exception_handler(RequestValidationError, validation_error_handler)
    application.add_exception_handler(Exception, internal_error_handler)

    async def http_error(request, error):
        return error_response(
            GatewayError(
                "route_not_found" if error.status_code == 404 else "invalid_request",
                error.status_code,
                "HTTP request could not be handled.",
            )
        )

    application.add_exception_handler(HTTPException, http_error)
    application.add_middleware(RequestContextMiddleware, owner=application)
    application.include_router(router)
    return application
