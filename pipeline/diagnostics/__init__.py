"""ClipForge diagnostics subsystem (ARCHITECTURE.md §12).

Ported from ``_legacy/scripts/gemini_api_capability_diagnostic.py`` onto the
package layout. The diagnostics workflow (``.github/workflows/diagnostics.yml``)
invokes ``python -m pipeline.diagnostics.gemini`` for the manual Gemini
capability check and drives the §9.1 Telegram intake positive/negative checks
through ``pipeline.stage_a.ingest``.
"""
