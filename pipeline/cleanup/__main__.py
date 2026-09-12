"""CLI entry point: ``python -m pipeline.cleanup`` runs the TTL cleanup."""
from __future__ import annotations

from urllib.error import HTTPError, URLError

from pipeline.cleanup.expired import main

if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except (HTTPError, URLError, RuntimeError) as exc:
        import sys
        print(f"cleanup failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
