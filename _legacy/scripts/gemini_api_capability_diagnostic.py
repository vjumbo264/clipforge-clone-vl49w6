#!/usr/bin/env python3
"""Safely verify whether the repository's existing Gemini keys support Automatic Mode.

This one-off diagnostic uses the official ``google-genai`` SDK and tests a
minimal native function-call request that also contains an inline PNG.  It never
prints or writes raw keys, provider request bodies, response text, cookies, or
Google-project identifiers.  The JSON report contains only key fingerprints,
selected model IDs, boolean capabilities, HTTP-like error codes, and bounded
redacted error labels.

It cannot determine whether two API keys share a Google Cloud project: Gemini
Developer API responses do not expose project ownership.  That association must
be checked in the key owner's AI Studio Projects/API Keys views.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types


MODEL_CANDIDATES = (
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-2.5-flash",
)
# A valid 1×1 PNG. It is deliberately content-free and only establishes that
# the selected model/key accepts an inline image part alongside a function tool.
PROBE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL7WAAAAABJRU5ErkJggg=="
)


@dataclass(frozen=True)
class ApiKey:
    raw: str
    fingerprint: str


def fingerprint(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return "key-" + digest


def load_keys(raw: str | None) -> list[ApiKey]:
    seen: set[str] = set()
    keys: list[ApiKey] = []
    for value in re.split(r"[\r\n,]+", raw or ""):
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            keys.append(ApiKey(raw=cleaned, fingerprint=fingerprint(cleaned)))
    if not keys:
        raise RuntimeError("GEMINI_API_KEYS is empty or has no usable values.")
    return keys


def error_metadata(error: Exception) -> dict[str, Any]:
    status = getattr(error, "code", None) or getattr(error, "status_code", None)
    status = status if isinstance(status, int) else None
    message = str(error)[:400]
    message = re.sub(r"(?i)(AIza)[A-Za-z0-9_-]+", r"\1[REDACTED]", message)
    message = re.sub(
        r"(?i)((?:api[_-]?key|authorization|token|secret)\s*[=:]\s*)[^,\s}]+",
        r"\1[REDACTED]",
        message,
    )
    return {
        "status": status,
        "type": type(error).__name__,
        "message": message or "Gemini SDK request failed without an error message.",
    }


def model_ids(client: genai.Client) -> set[str]:
    available: set[str] = set()
    for model in client.models.list():
        name = str(getattr(model, "name", ""))
        if name:
            available.add(name.removeprefix("models/"))
    return available


def capability_probe(client: genai.Client, model: str) -> bool:
    declaration = types.FunctionDeclaration(
        name="echo_evidence",
        description="Return the supplied diagnostic label exactly once after inspecting the attached image.",
        parameters_json_schema={
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
            "additionalProperties": False,
        },
    )
    config = types.GenerateContentConfig(
        tools=[types.Tool(function_declarations=[declaration])],
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="ANY")
        ),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        max_output_tokens=128,
    )
    contents = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text=(
                    "This is a ClipForge capability probe. Inspect the attached 1×1 PNG and call "
                    "echo_evidence with label `direct_api_vision_tool_probe`."
                )
            ),
            types.Part.from_bytes(data=PROBE_PNG, mime_type="image/png"),
        ],
    )
    response = client.models.generate_content(model=model, contents=contents, config=config)
    calls = response.function_calls or []
    return any(
        getattr(call, "name", None) == "echo_evidence"
        and isinstance(getattr(call, "args", None), dict)
        and call.args.get("label") == "direct_api_vision_tool_probe"
        for call in calls
    )


def probe_key(key: ApiKey) -> dict[str, Any]:
    result: dict[str, Any] = {
        "fingerprint": key.fingerprint,
        "model_list_retrieved": False,
        "available_configured_models": [],
        "selected_model": None,
        "native_function_calling": False,
        "inline_image_input": False,
        "success": False,
    }
    try:
        client = genai.Client(api_key=key.raw)
        available = model_ids(client)
        result["model_list_retrieved"] = True
        result["available_configured_models"] = [
            model for model in MODEL_CANDIDATES if model in available
        ]
        for model in MODEL_CANDIDATES:
            if model not in available:
                continue
            result["selected_model"] = model
            try:
                result["native_function_calling"] = capability_probe(client, model)
                result["inline_image_input"] = result["native_function_calling"]
                result["success"] = result["native_function_calling"]
                if result["success"]:
                    result.pop("error", None)
                    return result
                if not result["success"]:
                    result["error"] = {
                        "status": None,
                        "type": "CapabilityMismatch",
                        "message": "The model returned no matching native function call for the inline-image probe.",
                    }
                return result
            except Exception as error:  # probe alternate current Flash candidate only
                result["error"] = error_metadata(error)
        if not result["available_configured_models"]:
            result["error"] = {
                "status": None,
                "type": "NoConfiguredModel",
                "message": "None of the current Flash model candidates was visible to this key.",
            }
    except Exception as error:
        result["error"] = error_metadata(error)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe configured Gemini keys without exposing secrets.")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--key-env", default="GEMINI_API_KEYS")
    args = parser.parse_args()

    try:
        keys = load_keys(os.environ.get(args.key_env))
        report = {
            "version": 1,
            "provider": "gemini-developer-api",
            "sdk": "google-genai",
            "tested_capabilities": ["model_listing", "native_function_calling", "inline_image_input"],
            "project_quota_scope": "per_project_not_per_key",
            "project_topology": "not_exposed_by_api_key_requests; verify in AI Studio",
            "keys": [probe_key(key) for key in keys],
        }
        report["successful_keys"] = sum(1 for entry in report["keys"] if entry["success"])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(
            "Gemini direct capability diagnostic completed: "
            + str(report["successful_keys"])
            + "/"
            + str(len(keys))
            + " keys support the selected native image/function probe.",
            flush=True,
        )
        return 0 if report["successful_keys"] else 1
    except Exception as error:
        print("Gemini capability diagnostic failed safely: " + type(error).__name__, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
