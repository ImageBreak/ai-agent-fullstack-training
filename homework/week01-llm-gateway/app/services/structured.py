import json

from jsonschema import Draft202012Validator, SchemaError

from app.domain.errors import GatewayError


def reject_constant(_):
    raise ValueError("Non-standard JSON constant")


def depth(value):
    stack = [(value, 1)]
    maximum = 0
    while stack:
        current, level = stack.pop()
        maximum = max(maximum, level)
        if isinstance(current, dict):
            stack.extend((item, level + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, level + 1) for item in current)
    return maximum


def json_size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


class StructuredService:
    def __init__(self, limits):
        self.limits = limits

    def check_schema(self, response_format):
        if not response_format or response_format["type"] != "json_schema":
            return
        schema = response_format["json_schema"]["schema"]
        if schema.get("type") != "object":
            raise GatewayError("unsupported_schema", 422, "The shared schema subset requires an object root.")
        if json_size(schema) > self.limits.max_schema_bytes or depth(schema) > self.limits.max_schema_depth:
            raise GatewayError("invalid_json_schema", 422, "Schema exceeds size or depth limit.")
        allowed = {"type", "properties", "required", "items", "enum", "additionalProperties"}

        def inspect(node):
            if not isinstance(node, dict):
                raise GatewayError("unsupported_schema", 422, "Schema must use object-form definitions.")
            if set(node) - allowed:
                raise GatewayError("unsupported_schema", 422, "Schema keyword is not supported.")
            for child in node.get("properties", {}).values():
                inspect(child)
            for key in ("items", "additionalProperties"):
                if key in node and isinstance(node[key], dict):
                    inspect(node[key])

        try:
            Draft202012Validator.check_schema(schema)
            inspect(schema)
        except (SchemaError, TypeError, AttributeError):
            raise GatewayError("invalid_json_schema", 422, "Invalid JSON Schema.") from None

    def validate(self, text, response_format, *, stream=False):
        if not response_format:
            return text
        if len(text.encode("utf-8")) > self.limits.max_structured_output_bytes:
            raise GatewayError("upstream_response_too_large", 502, "Structured output exceeds limit.")
        candidate = text.strip()
        if not stream and candidate.startswith("```") and candidate.endswith("```"):
            lines = candidate.splitlines()
            if len(lines) >= 3 and lines[0].lower() in {"```", "```json"} and lines[-1] == "```":
                candidate = "\n".join(lines[1:-1])
        try:
            value = json.loads(candidate, parse_constant=reject_constant)
            if depth(value) > self.limits.max_json_depth:
                raise ValueError("depth")
        except (ValueError, RecursionError):
            raise GatewayError(
                "structured_output_invalid", 422, "Output is not valid bounded JSON."
            ) from None
        if response_format["type"] == "json_object":
            if not isinstance(value, dict):
                raise GatewayError("structured_output_invalid", 422, "Output must be a JSON object.")
        else:
            validator = Draft202012Validator(response_format["json_schema"]["schema"])
            error = next(validator.iter_errors(value), None)
            if error:
                # Only schema keyword/path metadata, never the invalid instance value.
                path = "/".join(str(part) for part in error.absolute_path)[:200]
                raise GatewayError(
                    "structured_output_invalid",
                    422,
                    f"Output failed {error.validator} validation at /{path}.",
                )
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    def instructions(self, response_format):
        text = "Return only valid JSON, without Markdown or explanatory text."
        if response_format and response_format["type"] == "json_schema":
            text += "\nJSON Schema: " + json.dumps(
                response_format["json_schema"]["schema"], ensure_ascii=False
            )
        return text

    def repair_messages(self, messages, previous, error, response_format):
        return [
            *messages,
            {"role": "assistant", "content": previous},
            {
                "role": "user",
                "content": "Correct the previous output. "
                + error.message
                + "\n"
                + self.instructions(response_format),
            },
        ]
