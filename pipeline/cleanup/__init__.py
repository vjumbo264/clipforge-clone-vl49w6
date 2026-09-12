"""ClipForge background cleanup subsystem (ARCHITECTURE.md §12).

Ported from _legacy/scripts/cleanup.py onto the package layout. The cleanup
workflow (`.github/workflows/cleanup.yml`) invokes ``python -m
pipeline.cleanup`` hourly to delete jobs (and their releases, tags, and
per-job branches) whose TTL has passed.
"""
