"""Offline tests for relay/telegram_relay.py (PRESERVED SUBSYSTEM #2, §9.2).

Covers the pure parts only: the AES-256-GCM sealed-envelope round trip (with
the exact AAD contract), payload validation, the handoff-token rule, and the
§7.1 nested-request metadata write-back. No Telegram or GitHub network calls.
"""
from __future__ import annotations

import base64
import importlib
import json
import os
import sys
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

RELAY_DIR = Path(__file__).resolve().parents[2] / "relay"
sys.path.insert(0, str(RELAY_DIR))

telegram_relay = importlib.import_module("telegram_relay")

KEY = base64.b64encode(os.urandom(32)).decode()
JOB = "manual-1787692652625"


def seal(payload: dict, job_id: str = JOB, key: str = KEY) -> str:
    raw = base64.b64decode(key)
    iv = os.urandom(12)
    aad = f"clipforge-telegram-relay:v1:{job_id}".encode()
    ciphertext = AESGCM(raw).encrypt(iv, json.dumps(payload).encode(), aad)
    return json.dumps({
        "v": 1,
        "iv": base64.b64encode(iv).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    })


def good_payload() -> dict:
    return {
        "version": 1,
        "job_id": JOB,
        "target_repo": "someone/clone",
        "target_github_pat": "pat-value",
        "stage_a_request": {
            "version": 2,
            "job_id": JOB,
            "source": {"kind": "telegram_relay", "value": "relay:private"},
            "options": {"whisper_model": "base", "language": "auto",
                        "target_duration_seconds": 120, "focus": "",
                        "enable_vision_assist": True},
            "mode": "manual",
            "series": {"enabled": False, "series_id": "", "source_job_id": "",
                       "part": 0, "start_seconds": 0, "context": ""},
            "music": {"ref": "", "source": "none"},
        },
        "telegram": {"group_chat_id": -5405387856, "group_message_id": 777,
                     "declared_size": 1024},
    }


class DecryptPayloadTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        payload = good_payload()
        out = telegram_relay.decrypt_payload(JOB, seal(payload), KEY)
        self.assertEqual(out["target_repo"], "someone/clone")

    def test_rejects_wrong_job(self) -> None:
        with self.assertRaises(RuntimeError):
            telegram_relay.decrypt_payload("manual-other", seal(good_payload()), KEY)

    def test_rejects_wrong_key(self) -> None:
        other = base64.b64encode(os.urandom(32)).decode()
        with self.assertRaises(RuntimeError):
            telegram_relay.decrypt_payload(JOB, seal(good_payload()), other)

    def test_rejects_malformed_envelope(self) -> None:
        with self.assertRaises(RuntimeError):
            telegram_relay.decrypt_payload(JOB, "not json", KEY)
        with self.assertRaises(RuntimeError):
            telegram_relay.decrypt_payload(JOB, json.dumps({"v": 2, "iv": "x", "ciphertext": "y"}), KEY)


class EnsurePayloadTests(unittest.TestCase):
    def test_accepts_complete_payload(self) -> None:
        repo, token, request_doc, telegram = telegram_relay.ensure_payload(good_payload())
        self.assertEqual(repo, "someone/clone")
        self.assertEqual(token, "pat-value")
        self.assertEqual(request_doc["job_id"], JOB)
        self.assertEqual(telegram["group_message_id"], 777)

    def test_rejects_missing_fields(self) -> None:
        for drop in ("target_repo", "target_github_pat", "stage_a_request", "telegram"):
            payload = good_payload()
            payload[drop] = "" if isinstance(payload[drop], str) else None
            with self.assertRaises(RuntimeError, msg=drop):
                telegram_relay.ensure_payload(payload)

    def test_rejects_request_for_another_job(self) -> None:
        payload = good_payload()
        payload["stage_a_request"]["job_id"] = "manual-someone-else"
        with self.assertRaises(RuntimeError):
            telegram_relay.ensure_payload(payload)

    def test_rejects_oversize_and_bad_coordinates(self) -> None:
        payload = good_payload()
        payload["telegram"]["declared_size"] = telegram_relay.MAX_RELAY_BYTES + 1
        with self.assertRaises(RuntimeError):
            telegram_relay.ensure_payload(payload)
        payload = good_payload()
        payload["telegram"]["group_message_id"] = 0
        with self.assertRaises(RuntimeError):
            telegram_relay.ensure_payload(payload)


class HandoffTokenTests(unittest.TestCase):
    def test_central_repo_uses_workflow_token(self) -> None:
        os.environ["RELAY_CENTRAL_REPOSITORY"] = "motionssalt/clipforge"
        os.environ["RELAY_CENTRAL_GITHUB_TOKEN"] = "workflow-token"
        try:
            self.assertEqual(telegram_relay.handoff_token("motionssalt/clipforge", "user-pat"), "workflow-token")
        finally:
            del os.environ["RELAY_CENTRAL_REPOSITORY"]
            del os.environ["RELAY_CENTRAL_GITHUB_TOKEN"]

    def test_clone_keeps_sealed_user_token(self) -> None:
        os.environ["RELAY_CENTRAL_REPOSITORY"] = "motionssalt/clipforge"
        os.environ["RELAY_CENTRAL_GITHUB_TOKEN"] = "workflow-token"
        try:
            self.assertEqual(telegram_relay.handoff_token("someone/clone", "user-pat"), "user-pat")
        finally:
            del os.environ["RELAY_CENTRAL_REPOSITORY"]
            del os.environ["RELAY_CENTRAL_GITHUB_TOKEN"]


class ApplyRelayMetadataTests(unittest.TestCase):
    def test_fills_nested_relay_block(self) -> None:
        doc = json.loads(json.dumps(good_payload()["stage_a_request"]))
        out = telegram_relay.apply_relay_metadata(doc, JOB, "clipforge-relay-input-" + JOB, 1024, "ab" * 32)
        relay = out["source"]["relay"]
        self.assertEqual(relay["release_tag"], "clipforge-relay-input-" + JOB)
        self.assertEqual(relay["expected_size_bytes"], "1024")
        self.assertEqual(relay["sha256"], "ab" * 32)
        self.assertEqual(out["source"]["value"], "relay:private")
        self.assertEqual(out["source"]["kind"], "telegram_relay")
        self.assertEqual(out["version"], 2)

    def test_rejects_wrong_job_or_kind(self) -> None:
        doc = json.loads(json.dumps(good_payload()["stage_a_request"]))
        with self.assertRaises(RuntimeError):
            telegram_relay.apply_relay_metadata(doc, "manual-other", "t", 1, "d")
        doc["source"]["kind"] = "url"
        with self.assertRaises(RuntimeError):
            telegram_relay.apply_relay_metadata(doc, JOB, "t", 1, "d")

    def test_release_tag_contract(self) -> None:
        self.assertEqual(telegram_relay.release_tag(JOB), f"clipforge-relay-input-{JOB}")


if __name__ == "__main__":
    unittest.main()
