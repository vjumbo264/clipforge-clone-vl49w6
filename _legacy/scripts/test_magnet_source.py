#!/usr/bin/env python3
"""Offline regression coverage for Stage A magnet-link ingestion."""
from __future__ import annotations

import base64
import importlib.util
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "scripts" / "magnet_source.py"
spec = importlib.util.spec_from_file_location("magnet_source", HELPER_PATH)
assert spec and spec.loader
magnet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(magnet)

SUPPLIED_MAGNET = (
    "magnet:?xt=urn:btih:5B164256AA9E1A4EF90C9367837C7AE9ABF2CDF0"
    "&dn=Rick.and.Morty.S07E10.2160p.Upscaled.WEB.H264.sombince"
    "&tr=udp://tracker.coppersurfer.tk:6969/announce"
    "&tr=udp://9.rarbg.me:2850/announce"
    "&tr=udp://9.rarbg.to:2920/announce"
    "&tr=udp://tracker.opentrackr.org:1337"
    "&tr=udp://tracker.leechers-paradise.org:6969/announce"
)

parsed = magnet.inspect_magnet(SUPPLIED_MAGNET)
assert parsed["infohash_v1"] == "5B164256AA9E1A4EF90C9367837C7AE9ABF2CDF0"
assert parsed["display_name"] == "Rick.and.Morty.S07E10.2160p.Upscaled.WEB.H264.sombince"
assert parsed["tracker_count"] == 5
assert all(item.startswith("udp://") for item in parsed["trackers"])

raw_hash = bytes.fromhex(parsed["infohash_v1"])
base32_hash = base64.b32encode(raw_hash).decode("ascii")
base32_magnet = f"magnet:?xt=urn:btih:{base32_hash}&dn=fixture"
assert magnet.inspect_magnet(base32_magnet)["infohash_v1"] == parsed["infohash_v1"]

exact_source = (
    "magnet:?xt=urn:btih:0E876CE2A1A504F849CA72A5E2BC07347B3BC957"
    "&dn=Blender_Foundation_-_Big_Buck_Bunny_720p"
    "&xs=https%3A%2F%2Farchive.org%2Fdownload%2FBigBuckBunny_124%2F"
    "blender_foundation_-_big_buck_bunny_720p.torrent"
)
exact_parsed = magnet.inspect_magnet(exact_source)
assert exact_parsed["infohash_v1"] == "0E876CE2A1A504F849CA72A5E2BC07347B3BC957"
assert exact_parsed["metadata_sources"] == [
    "https://archive.org/download/BigBuckBunny_124/"
    "blender_foundation_-_big_buck_bunny_720p.torrent"
]

for bad_uri in (
    "https://example.com/video.mp4",
    "magnet:?dn=missing-infohash",
    "magnet:?xt=urn:btmh:1220deadbeef",
    "magnet:?xt=urn:btih:5B164256AA9E1A4EF90C9367837C7AE9ABF2CDF0&tr=file:///etc/passwd",
    "magnet:?xt=urn:btih:5B164256AA9E1A4EF90C9367837C7AE9ABF2CDF0&tr=https://user:pass@example.com/announce",
    "magnet:?xt=urn:btih:5B164256AA9E1A4EF90C9367837C7AE9ABF2CDF0&xs=file:///etc/passwd",
    "magnet:?xt=urn:btih:5B164256AA9E1A4EF90C9367837C7AE9ABF2CDF0&xs=https://user:pass@example.com/source.torrent",
    "magnet:?xt=urn:btih:5B164256AA9E1A4EF90C9367837C7AE9ABF2CDF0&dn=unsafe%0Aname",
):
    try:
        magnet.inspect_magnet(bad_uri)
        raise AssertionError(f"invalid magnet accepted: {bad_uri}")
    except magnet.MagnetError:
        pass

with tempfile.TemporaryDirectory(prefix="clipforge_magnet_metadata_") as temp_dir:
    root = Path(temp_dir)
    expected = root / f"{parsed['infohash_v1'].lower()}.torrent"
    expected.write_bytes(b"d4:infod4:name4:teste")
    assert magnet.find_saved_metadata(root, parsed["infohash_v1"]) == expected
    try:
        magnet.find_saved_metadata(root, "0" * 40)
        raise AssertionError("wrong-infohash metadata file was accepted")
    except FileNotFoundError:
        pass

workflow = (ROOT / ".github" / "workflows" / "stage-a.yml").read_text(encoding="utf-8")
app = (ROOT / "app.js").read_text(encoding="utf-8")
html = (ROOT / "index.html").read_text(encoding="utf-8")
assert "scripts/magnet_source.py validate" in workflow
assert "--bt-metadata-only=true" in workflow
assert "--bt-save-metadata=true" in workflow
assert "metadata-source" in workflow
assert "Exact metadata source does not match the magnet infohash" in workflow
assert "scripts/torrent_source.py infohash" in workflow
assert "--seed-time=0" in workflow
assert "--enable-dht=true" in workflow
assert "--enable-peer-exchange=true" in workflow
assert "--bt-enable-lpd=true" in workflow
assert "--bt-max-peers=100" in workflow
assert "--bt-tracker=\"udp://tracker.opentrackr.org:1337/announce" in workflow
assert "--bt-enable-peer-exchange" not in workflow
assert "--enable-lpd=true" not in workflow
assert "source_ref=\"path:$expected_path\"" in workflow
assert 'git add "$torrent_path" "$selection_path"' in workflow
assert 'SOURCE_REF: ${{ github.event.inputs.video_url }}' in workflow
assert "isMagnetLink" in app and "Retrieving magnet metadata" in app
assert "magnet:?xt=urn:btih" in html

print("PASS: magnet links are validated offline and route through the explicit torrent-video selection handoff")
