"""ClipForge Stage A — ingest + analyze.

Stages (per ARCHITECTURE.md §7.1–§7.2):

  1. ingest      — resolve the user's source into a local original file.
  2. transcribe  — faster-whisper CPU transcript.
  3. scenes      — shot boundaries, key moments, composites.
  4. bundle      — analysis bundle + 00_READ_THIS_FIRST.txt + manifest.

Only the four non-preserved source kinds are implemented here (url, drive,
magnet, torrent_file). The two PRESERVED subsystems (§9.1 telegram_channel
and §9.2 telegram_relay) are built in their own later phases; ingest accepts
their source kinds in the schema but routes them to a fail-closed gate until
those phases land.
"""
