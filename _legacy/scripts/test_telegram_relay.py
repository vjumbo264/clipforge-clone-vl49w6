#!/usr/bin/env python3
"""Offline contract tests for scripts/telegram_relay.py."""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, str(Path(__file__).resolve().parent))
import telegram_relay  # noqa: E402
from telegram_relay import (  # noqa: E402
    decrypt_payload,
    ensure_payload,
    handoff_token,
    known_basic_group_message_request,
    release_tag,
    stage_a_request_document,
    verify_bot_group_access,
)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def sealed(job_id: str, key: bytes, payload: dict) -> str:
    nonce = bytes(range(12))
    ciphertext = AESGCM(key).encrypt(nonce, json.dumps(payload).encode(), f'clipforge-telegram-relay:v1:{job_id}'.encode())
    return json.dumps({'v': 1, 'iv': b64(nonce), 'ciphertext': b64(ciphertext)})


class FakeTelegramResponse:
    def __init__(self, ok: bool, payload: dict):
        self.ok = ok
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def successful_group_requester(url: str, *, params: dict, timeout: int) -> FakeTelegramResponse:
    assert url.endswith('/getChat')
    assert params == {'chat_id': -5405387856}
    assert timeout > 0
    return FakeTelegramResponse(True, {'ok': True, 'result': {'id': -5405387856, 'type': 'group'}})


def missing_group_requester(url: str, *, params: dict, timeout: int) -> FakeTelegramResponse:
    return FakeTelegramResponse(False, {'ok': False, 'description': 'chat not found'})


class FakeInputMessageID:
    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeGetMessagesRequest:
    def __init__(self, *, id: list[FakeInputMessageID]):
        self.id = id


def main() -> None:
    job_id = 'manual-relay-test'
    key = bytes(range(32))
    payload = {
        'version': 1, 'job_id': job_id, 'target_repo': 'owner/clone', 'target_github_pat': 'test-only-pat',
        'stage_a_inputs': {'job_id': job_id, 'source_type': 'telegram_bot_forward'},
        'telegram': {'group_chat_id': -100123, 'group_message_id': 44, 'declared_size': 4096}
    }
    encrypted = sealed(job_id, key, payload)
    assert decrypt_payload(job_id, encrypted, b64(key)) == payload
    try:
        decrypt_payload('other-job', encrypted, b64(key))
    except RuntimeError:
        pass
    else:
        raise AssertionError('AAD/job binding must reject a replay to another job')
    repo, token, inputs, telegram = ensure_payload(payload)
    assert repo == 'owner/clone' and token == 'test-only-pat'
    assert inputs['source_type'] == 'telegram_bot_forward'
    assert telegram['group_message_id'] == 44
    assert release_tag(job_id) == 'clipforge-relay-input-manual-relay-test'

    with patch.dict(os.environ, {
        'RELAY_CENTRAL_REPOSITORY': 'motionssalt/clipforge',
        'RELAY_CENTRAL_GITHUB_TOKEN': 'workflow-token',
    }, clear=False):
        assert handoff_token('motionssalt/clipforge', 'sealed-token') == 'workflow-token'
        assert handoff_token('owner/external-clone', 'sealed-token') == 'sealed-token'

    recovered_request = stage_a_request_document(job_id, {
        'video_url': 'relay:private', 'source_type': 'telegram_bot_forward',
        'target_duration_seconds': '45', 'automatic_mode': 'true',
        'series_mode': 'false', 'whisper_model': 'small', 'language': 'en',
    })
    assert recovered_request['job_id'] == job_id
    assert recovered_request['source_type'] == 'telegram_bot_forward'
    assert recovered_request['target_duration_seconds'] == '45'
    assert recovered_request['automatic_mode'] == 'true'
    assert recovered_request['relay_release_tag'] == ''

    # The local Bot API image initializes its mounted data directory as root.
    # The relay streams only the exact already validated media path through the
    # container, creating the destination file as the runner rather than
    # changing permissions on any storage directory.
    remote_media = Path('/var/lib/telegram-bot-api/test-bot/videos/file_0')
    output_media = Path('/tmp/clipforge-relay-test/source_input.bin')
    output_media.parent.mkdir(parents=True, exist_ok=True)
    with patch.object(telegram_relay.subprocess, 'run', return_value=telegram_relay.subprocess.CompletedProcess([], 0)) as run_command, \
         patch.dict(os.environ, {'LOCAL_BOT_API_CONTAINER': 'clipforge-local-bot-api'}, clear=False):
        telegram_relay.stream_local_bot_api_media_to_runner(remote_media, output_media)
    output_media.unlink(missing_ok=True)
    assert run_command.call_args.args[0] == [
        'docker', 'exec', '--user', '0:0', 'clipforge-local-bot-api', 'cat', '--', str(remote_media)
    ]

    # Bot B must be a member of the configured basic group. The preflight uses
    # Bot API getChat and never exposes Telegram's response body in workflow logs.
    verify_bot_group_access('test-token', -5405387856, successful_group_requester)
    try:
        verify_bot_group_access('test-token', -5405387856, missing_group_requester)
    except RuntimeError as error:
        assert 'Add Bot B' in str(error)
    else:
        raise AssertionError('Missing Bot B membership must fail before MTProto transfer.')

    # Bot accounts cannot call messages.getDialogs. The supplied private relay
    # group is a basic group, so the exact authenticated message is requested
    # directly through messages.getMessages instead of enumerating dialogs.
    direct_request = known_basic_group_message_request(
        -5405387856,
        91,
        FakeGetMessagesRequest,
        FakeInputMessageID,
    )
    assert isinstance(direct_request, FakeGetMessagesRequest)
    assert [item.message_id for item in direct_request.id] == [91]
    for invalid_group_id in (-1004377458972, 0, 123):
        try:
            known_basic_group_message_request(
                invalid_group_id,
                91,
                FakeGetMessagesRequest,
                FakeInputMessageID,
            )
        except RuntimeError as error:
            assert 'basic Telegram group ID' in str(error)
        else:
            raise AssertionError('Supergroup, channel, and user IDs must not trigger a bot dialogs lookup.')

    print('telegram_relay offline contracts passed')


if __name__ == '__main__':
    main()
