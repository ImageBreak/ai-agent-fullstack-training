from jinja2 import StrictUndefined, TemplateError, nodes
from jinja2.sandbox import SandboxedEnvironment

from app.domain.errors import GatewayError
from app.services.structured import depth, json_size


class PromptService:
    def __init__(self, repository, limits):
        self.repository = repository
        self.limits = limits
        self.environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
        self.environment.globals.clear()
        self.environment.filters = {
            key: value
            for key, value in self.environment.filters.items()
            if key in {"default", "length", "tojson"}
        }

    def compile(self, content):
        if len(content.encode("utf-8")) > self.limits.max_template_bytes:
            raise GatewayError("prompt_render_error", 422, "Template exceeds size limit.")
        try:
            tree = self.environment.parse(content)
            blocked = (
                nodes.Call,
                nodes.Getattr,
                nodes.Import,
                nodes.FromImport,
                nodes.Include,
                nodes.Extends,
                nodes.Macro,
                nodes.Assign,
                nodes.BinExpr,
                nodes.Concat,
                nodes.FilterBlock,
            )
            if any(True for _ in tree.find_all(blocked)):
                raise ValueError("Unsupported template expression")
            for loop in tree.find_all(nodes.For):
                if loop.recursive or any(True for _ in loop.find_all(nodes.For)):
                    raise ValueError("Nested or recursive loops are not supported")
            return self.environment.from_string(content)
        except (TemplateError, ValueError, RecursionError):
            raise GatewayError(
                "prompt_render_error", 422, "Template syntax is unsupported or invalid."
            ) from None

    def render(self, content, variables):
        try:
            if json_size(variables) > self.limits.max_variables_bytes:
                raise ValueError("variables too large")
            if depth(variables) > self.limits.max_json_depth:
                raise ValueError("variables too deep")
            template = self.compile(content)
            parts, size = [], 0
            for part in template.generate(**variables):
                size += len(part.encode("utf-8"))
                if size > self.limits.max_rendered_prompt_bytes:
                    raise ValueError("rendered output too large")
                parts.append(part)
            return "".join(parts)
        except (TemplateError, ValueError, TypeError, RecursionError):
            raise GatewayError(
                "prompt_render_error", 422, "Prompt rendering failed or exceeded limits."
            ) from None

    async def apply(self, messages, reference):
        if reference is None:
            return messages, None, None
        version = await self.repository.resolve(reference.id, reference.version)
        rendered = self.render(version["content"], reference.variables)
        message = {"role": version["role"], "content": rendered}
        combined = [message, *messages] if message["role"] == "system" else [*messages, message]
        return combined, reference.id, version["version"]

    async def publish(self, payload):
        self.compile(payload.content)
        return await self.repository.publish(payload)
