from app.adapters.chat_completions import ChatCompletionsAdapter
from app.adapters.responses import ResponsesAdapter
from app.domain.errors import GatewayError


class Router:
    def __init__(self, settings, client):
        classes = {"chat_completions": ChatCompletionsAdapter, "responses": ResponsesAdapter}
        self.adapters = {
            alias: classes[model.adapter](client, model, settings)
            for alias, model in settings.models.items()
            if model.enabled
        }

    def resolve(self, alias):
        if alias not in self.adapters:
            raise GatewayError("model_not_found", 404, "Unknown model alias.", "model")
        return self.adapters[alias]

    def models(self):
        return {
            "object": "list",
            "data": [
                {
                    "id": alias,
                    "object": "model",
                    "capabilities": {
                        "stream": True,
                        "structured_output": True,
                        "upstream_api": adapter.model.adapter,
                    },
                }
                for alias, adapter in self.adapters.items()
            ],
        }
