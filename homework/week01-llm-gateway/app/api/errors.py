import logging

from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from app.domain.errors import GatewayError

logger = logging.getLogger("gateway.api")


def error_response(error):
    return JSONResponse(error.payload(), status_code=error.status, headers=error.headers)


async def gateway_error_handler(request, error):
    return error_response(error)


async def validation_error_handler(request, error: RequestValidationError):
    unsupported = any(item["type"] == "extra_forbidden" for item in error.errors())
    return error_response(
        GatewayError(
            "unsupported_feature" if unsupported else "invalid_request",
            422,
            "Unsupported request field." if unsupported else "Invalid request parameters.",
        )
    )


async def internal_error_handler(request, error):
    logger.error(
        "request_failed request_id=%s category=%s",
        getattr(request.state, "request_id", "unknown"),
        type(error).__name__,
    )
    return error_response(GatewayError("internal_error", 500, "Internal gateway error."))
