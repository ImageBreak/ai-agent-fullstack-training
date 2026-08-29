from fastapi import Request

from app.core.security import authenticate


def fingerprint(request: Request):
    settings = request.app.state.services.settings
    return authenticate(request.headers.get("authorization"), settings.gateway_api_key.get_secret_value())


def services(request: Request):
    return request.app.state.services
