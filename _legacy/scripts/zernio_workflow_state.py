#!/usr/bin/env python3
"""Small, dependency-free safety helpers shared by Zernio Actions steps."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def publishing_error_state(prior: dict[str, Any] | None = None) -> dict[str, Any]:
    """Preserve safe prior details while recording the original publisher failure."""
    import time
    publishing = dict(prior) if isinstance(prior, dict) else {}
    publishing.update({
        "provider": "zernio",
        "status": "error",
        "updated_at_epoch": int(time.time()),
        "error": "Zernio publishing workflow failed; inspect the earlier publishing step for the original sanitized error.",
        "result_available": False,
    })
    return publishing


def read_json_object_safely(path: str | Path) -> dict[str, Any] | None:
    """Return a JSON object only when a workflow result file is usable.

    A publisher command can fail before writing stdout, leaving the redirected
    result file absent or empty. Treat every unreadable, blank, malformed, or
    non-object result as unavailable so the workflow can record the original
    publishing failure rather than raise a secondary JSONDecodeError.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
