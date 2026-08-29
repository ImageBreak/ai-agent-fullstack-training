from dataclasses import dataclass, field


@dataclass
class GatewayError(Exception):
    code: str
    status: int = 422
    message: str = "Request could not be completed."
    param: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def payload(self) -> dict:
        kind = (
            "authentication_error"
            if self.status == 401
            else "rate_limit_error"
            if self.status == 429
            else "invalid_request_error"
            if self.status < 500
            else "api_error"
        )
        return {"error": {"message": self.message, "type": kind, "param": self.param, "code": self.code}}


@dataclass
class UpstreamError(GatewayError):
    retryable: bool = False
    retry_after: float | None = None


def request_timeout() -> GatewayError:
    return GatewayError("request_timeout", 504, "Request time limit exceeded.")
