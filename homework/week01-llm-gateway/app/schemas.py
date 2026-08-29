from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)


class Message(SchemaModel):
    role: Literal["system", "user", "assistant"]
    content: str


class JsonSchemaSpec(SchemaModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    strict: bool = True
    schema_value: dict[str, Any] = Field(alias="schema")


class ResponseFormat(SchemaModel):
    type: Literal["json_object", "json_schema"]
    json_schema: JsonSchemaSpec | None = None

    @model_validator(mode="after")
    def valid_format(self):
        if (self.type == "json_schema") != (self.json_schema is not None):
            raise ValueError("json_schema is required only for json_schema format")
        return self


class PromptReference(SchemaModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    version: int | None = Field(None, ge=1)
    variables: dict[str, Any] = Field(default_factory=dict)


class StreamOptions(SchemaModel):
    include_usage: bool = False


class ChatRequest(SchemaModel):
    model: str = Field(min_length=1, max_length=100)
    messages: list[Message] = Field(default_factory=list)
    stream: bool = False
    stream_options: StreamOptions | None = None
    max_tokens: int | None = Field(None, ge=1)
    temperature: float | None = Field(None, ge=0, le=2, allow_inf_nan=False)
    top_p: float | None = Field(None, gt=0, le=1, allow_inf_nan=False)
    response_format: ResponseFormat | None = None
    gateway_prompt: PromptReference | None = None

    @model_validator(mode="after")
    def validate_options(self):
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options requires stream=true")
        return self


class PublishPrompt(SchemaModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=200)
    role: Literal["system", "user"]
    content: str = Field(min_length=1)
    activate: bool = True


class ActivatePrompt(SchemaModel):
    version: int = Field(ge=1)
