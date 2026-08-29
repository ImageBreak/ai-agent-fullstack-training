"""Load configuration without importing it or reading secrets at module import."""

import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

ROOT = Path(__file__).resolve().parents[1]


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetrySettings(ConfigModel):
    max_upstream_calls: int = Field(3, ge=1, le=3)
    max_structured_repairs: int = Field(1, ge=0, le=1)
    base_delay_seconds: float = Field(0.25, ge=0)
    jitter_seconds: float = Field(0.1, ge=0)


class Timeouts(ConfigModel):
    connect_timeout_seconds: float = Field(10, gt=0)
    read_idle_timeout_seconds: float = Field(60, gt=0)
    request_timeout_seconds: float = Field(120, gt=0)
    cleanup_timeout_seconds: float = Field(2, gt=0)


class Limits(ConfigModel):
    max_request_bytes: int = Field(1048576, gt=0)
    max_template_bytes: int = Field(65536, gt=0)
    max_variables_bytes: int = Field(262144, gt=0)
    max_rendered_prompt_bytes: int = Field(524288, gt=0)
    max_schema_bytes: int = Field(32768, gt=0)
    max_schema_depth: int = Field(16, ge=1)
    max_json_depth: int = Field(32, ge=1)
    max_structured_output_bytes: int = Field(1048576, gt=0)
    max_upstream_response_bytes: int = Field(2097152, gt=0)
    max_sse_event_bytes: int = Field(262144, gt=0)
    default_max_tokens: int = Field(1024, ge=1)
    max_output_tokens: int = Field(4096, ge=1)

    @model_validator(mode="after")
    def token_order(self):
        if self.default_max_tokens > self.max_output_tokens:
            raise ValueError("default_max_tokens exceeds max_output_tokens")
        return self


class ModelSettings(ConfigModel):
    adapter: Literal["chat_completions", "responses"]
    base_url: str
    api_key: SecretStr = SecretStr("")
    upstream_model: str = Field(min_length=1)
    enabled: bool = True
    thinking_enabled: bool = False
    reasoning_effort: Literal["low", "high", "max"] = "high"
    requests_per_minute: float = Field(30, gt=0)
    burst: int = Field(5, ge=1)
    max_concurrency: int = Field(5, ge=1)


class Settings(ConfigModel):
    gateway_api_key: SecretStr
    database_path: Path = Path("data/gateway.db")
    docs_enabled: bool = True
    retry: RetrySettings = Field(default_factory=RetrySettings)
    timeouts: Timeouts = Field(default_factory=Timeouts)
    limits: Limits = Field(default_factory=Limits)
    models: dict[str, ModelSettings]

    @model_validator(mode="after")
    def validate_secrets(self):
        def valid(key):
            return bool(key) and key not in {"replace-me", "changeme", "..."}

        if not valid(self.gateway_api_key.get_secret_value()):
            raise ValueError("GATEWAY_API_KEY must be configured")
        active = [model for model in self.models.values() if model.enabled]
        if not active:
            raise ValueError("At least one model must be enabled")
        for model in active:
            parsed = urlsplit(model.base_url)
            local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("Invalid upstream URL")
            if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
                raise ValueError("Use HTTPS, except for local test servers")
            if not valid(model.api_key.get_secret_value()):
                raise ValueError("Enabled models require an API key")
        return self


def load_settings(config_path: str | Path | None = None, env_path: Path | None = None) -> Settings:
    env = {**dotenv_values(env_path or ROOT / ".env"), **os.environ}
    path = Path(config_path or env.get("GATEWAY_CONFIG") or ROOT / "gateway.yaml")
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists() and config_path is None and not env.get("GATEWAY_CONFIG"):
        path = ROOT / "gateway.example.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        def expand(value):
            if isinstance(value, str):
                return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}", lambda m: env.get(m[1]) or "", value)
            if isinstance(value, dict):
                return {k: expand(v) for k, v in value.items()}
            if isinstance(value, list):
                return [expand(v) for v in value]
            return value

        raw = expand(raw)
        raw["gateway_api_key"] = env.get("GATEWAY_API_KEY", "")
        settings = Settings.model_validate(raw)
        if not settings.database_path.is_absolute():
            settings.database_path = path.parent / settings.database_path
        return settings
    except (OSError, ValueError, TypeError, yaml.YAMLError, ValidationError):
        # Never print ValidationError input_value: it can contain credentials.
        raise RuntimeError("Invalid gateway configuration. Check YAML and local .env values.") from None
