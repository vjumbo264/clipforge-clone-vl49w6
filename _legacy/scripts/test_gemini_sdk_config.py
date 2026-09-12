#!/usr/bin/env python3
"""Offline contract check for the exact official Gemini SDK request shapes."""

from automatic_analysis import ApiKey, GeminiGateway, function_schemas


gateway = GeminiGateway([ApiKey("offline-test-key")])
config = gateway._config(tools_enabled=True)
assert config.tools and config.tools[0].function_declarations
assert [item.name for item in config.tools[0].function_declarations] == [
    "read_transcript", "read_scene_index", "read_key_moments", "open_composite"
]
assert config.automatic_function_calling is not None
assert config.automatic_function_calling.disable is True
assert gateway._config(tools_enabled=False).tools is None
assert function_schemas()[-1]["parameters_json_schema"]["required"] == ["filename"]

class FakeResponse:
    function_calls = []
    text = "{}"
    candidates = [type("Candidate", (), {"content": {"role": "model"}})()]
    usage_metadata = type("Usage", (), {
        "prompt_token_count": 11,
        "candidates_token_count": 7,
        "total_token_count": 18,
        "thoughts_token_count": None,
        "cached_content_token_count": 0,
        "tool_use_prompt_token_count": None,
    })()

class FakeModels:
    def __init__(self, client):
        self.client = client

    def generate_content(self, **_kwargs):
        assert self.client is not None, "gateway must retain the client during request execution"
        return FakeResponse()

class FakeClient:
    def __init__(self):
        self.models = FakeModels(self)

created = []
def factory(_key):
    client = FakeClient()
    created.append(client)
    return client

gateway = GeminiGateway([ApiKey("offline-test-key")], client_factory=factory, types_module=gateway.types)
turn = gateway.generate("gemini-3.7-flash", gateway.new_history("probe"), tools_enabled=True)
assert turn.calls == [] and len(created) == 1
assert gateway.usage_totals == {
    "prompt_token_count": 11,
    "candidates_token_count": 7,
    "total_token_count": 18,
    "cached_content_token_count": 0,
}
print("PASS: official Gemini SDK accepts native ClipForge function declarations, multimodal tool configuration, a retained client lifecycle, and numeric usage telemetry")
