#!/usr/bin/env python3
"""Trusted, temporary Bot B MTProto relay for one ClipForge task.

PRESERVED SUBSYSTEM #2 (ARCHITECTURE.md §9.2). Ported essentially verbatim
from _legacy/scripts/telegram_relay.py with ONE deliberate adaptation to the
new contracts (mirroring the github.js adaptation): the sealed payload carries
the §7.1 NESTED stage-a-request document (``stage_a_request``) instead of the
legacy flat ``stage_a_inputs`` wall, and the request write-back fills
``source.relay.{release_tag,expected_size_bytes,sha256}`` per
schemas/stage_a_request.schema.json.

This runs only in the central repository. The target clone receives a
temporary, prerelease, checksummed release asset; it never receives Bot B or
the MTProto application secrets.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Reuse the production-tested bounded four-connection MTProto transfer rather
# than falling back to a new single-connection implementation.
try:  # script mode: python relay/telegram_relay.py
    from mtproto_transfer import _ParallelTelegramTransferError, _download_telegram_parallel
except ImportError:  # package/test mode: repo root on sys.path
    from relay.mtproto_transfer import _ParallelTelegramTransferError, _download_telegram_parallel

MAX_RELAY_BYTES = 1800 * 1024 * 1024
REQUEST_TIMEOUT = 60
API_VERSION = '2026-03-10'


def fail(message: str) -> None:
    raise RuntimeError(message)


class GitHubHandoffError(RuntimeError):
    """A safe GitHub handoff failure that retains only status and operation."""

    def __init__(self, operation: str, status: int) -> None:
        self.operation = operation
        self.status = status
        super().__init__(f'GitHub {operation} failed with HTTP {status}.')


def decode_b64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except Exception as error:  # pragma: no cover - message is the contract
        raise RuntimeError('The relay envelope is malformed.') from error


def decrypt_payload(job_id: str, serialized: str, secret: str) -> dict[str, Any]:
    try:
        envelope = json.loads(serialized)
        iv = decode_b64(str(envelope['iv']))
        ciphertext = decode_b64(str(envelope['ciphertext']))
        key = decode_b64(secret)
        if envelope.get('v') != 1 or len(iv) != 12 or len(key) != 32:
            raise ValueError('unsupported envelope')
        plaintext = AESGCM(key).decrypt(iv, ciphertext, f'clipforge-telegram-relay:v1:{job_id}'.encode())
        payload = json.loads(plaintext.decode())
    except Exception as error:
        raise RuntimeError('The relay payload could not be authenticated.') from error
    if not isinstance(payload, dict) or payload.get('version') != 1 or payload.get('job_id') != job_id:
        raise RuntimeError('The relay payload does not belong to this job.')
    return payload


def ensure_payload(payload: dict[str, Any]) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    repo = str(payload.get('target_repo') or '').strip()
    token = str(payload.get('target_github_pat') or '').strip()
    request_doc = payload.get('stage_a_request')
    telegram = payload.get('telegram')
    if not repo or '/' not in repo or not token or not isinstance(request_doc, dict) or not isinstance(telegram, dict):
        fail('The relay payload is incomplete.')
    if str(request_doc.get('job_id') or '') != str(payload.get('job_id') or ''):
        fail('The relay payload Stage A request does not belong to this job.')
    group_chat = int(telegram.get('group_chat_id') or 0)
    message_id = int(telegram.get('group_message_id') or 0)
    declared_size = int(telegram.get('declared_size') or 0)
    if not group_chat or message_id <= 0 or declared_size <= 0 or declared_size > MAX_RELAY_BYTES:
        fail('The relay media metadata is unsafe or exceeds the direct-forward limit.')
    return repo, token, request_doc, telegram


def preflight_space(expected_bytes: int, work_dir: Path) -> None:
    free = shutil.disk_usage(work_dir).free
    required = expected_bytes * 2 + 1024 * 1024 * 1024
    if free < required:
        fail(f'Insufficient central-runner disk space for this relay source (need at least {required // (1024 * 1024)} MiB free).')


def verify_bot_group_access(
    bot_token: str,
    marked_chat_id: int,
    requester: Any = requests.get,
) -> None:
    """Confirm Bot B can access the configured basic relay group.

    A bot that has not been added to the group cannot resolve its messages.
    Check this with the supported Bot API before opening the MTProto transfer,
    while deliberately keeping Telegram's response body out of workflow logs.
    """
    try:
        response = requester(
            f'https://api.telegram.org/bot{bot_token}/getChat',
            params={'chat_id': marked_chat_id},
            timeout=REQUEST_TIMEOUT,
        )
        payload = response.json()
    except Exception as error:
        raise RuntimeError('Bot B group-access preflight could not contact Telegram.') from error
    result = payload.get('result') if isinstance(payload, dict) else None
    if not getattr(response, 'ok', False) or not isinstance(result, dict):
        fail(
            'Bot B cannot access the configured private relay group. Add '
            'Bot B to that group, then retry the relay.'
        )
    if int(result.get('id') or 0) != marked_chat_id or result.get('type') != 'group':
        fail('The configured private relay group is not an accessible basic Telegram group.')


def known_basic_group_message_request(
    marked_chat_id: int,
    message_id: int,
    request_factory: Any,
    input_message_factory: Any,
) -> Any:
    """Build a direct request for one known message in a basic group.

    Bot accounts cannot enumerate dialogs, so relay media must be addressed by
    the exact message ID already authenticated in Bot A's sealed payload. A
    normal Telegram group uses a negative marked ID without the ``-100``
    supergroup/channel prefix and is retrieved through ``messages.getMessages``.
    """
    if not (-1_000_000_000_000 < marked_chat_id < 0):
        fail(
            'The private relay must use a basic Telegram group ID (for example '
            '-5345479732), not a supergroup or channel ID.'
        )
    if message_id <= 0:
        fail('The private relay message ID is invalid.')
    return request_factory(id=[input_message_factory(message_id)])


def stream_local_bot_api_media_to_runner(remote_path: Path, output_path: Path) -> None:
    """Stream one validated local Bot API media file into a runner-owned file.

    The Bot API container initializes its data directory as root and may use
    non-traversable directories. Rather than changing broad host permissions,
    stream only the already prefix-validated media file through the named
    container; the output file is therefore created by the GitHub runner.
    """
    container = str(os.environ.get('LOCAL_BOT_API_CONTAINER') or '').strip()
    if not container:
        fail('Local Telegram Bot API container is unavailable for relay media transfer.')
    try:
        with output_path.open('wb') as target:
            result = subprocess.run(
                ['docker', 'exec', '--user', '0:0', container, 'cat', '--', str(remote_path)],
                check=False,
                stdout=target,
                stderr=subprocess.DEVNULL,
                timeout=30 * 60,
            )
    except (OSError, subprocess.SubprocessError) as error:
        output_path.unlink(missing_ok=True)
        raise RuntimeError('Local Telegram Bot API could not stream relay media to the runner.') from error
    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        fail('Local Telegram Bot API could not stream the validated relay media file.')


def download_local_bot_api_media(bot_token: str, file_id: str, expected_size: int, output_path: Path) -> bool:
    """Download one Bot B-visible file through a trusted local Bot API server.

    Telegram's public Bot API rejects large files, while the official local Bot
    API mode supports unrestricted downloads. The file ID is accepted only from
    Bot B's authenticated group update and is never logged.
    """
    base = str(os.environ.get('LOCAL_BOT_API_BASE') or '').rstrip('/')
    if not base or not file_id:
        return False
    try:
        lookup = requests.post(
            f'{base}/bot{bot_token}/getFile',
            data={'file_id': file_id},
            timeout=(REQUEST_TIMEOUT, 15 * 60),
        )
        result = lookup.json()
    except Exception as error:
        raise RuntimeError('Local Telegram Bot API media lookup could not be completed.') from error
    file_meta = result.get('result') if isinstance(result, dict) and result.get('ok') else None
    if not isinstance(file_meta, dict):
        fail('Local Telegram Bot API rejected Bot B’s relay media handle.')
    file_path = str(file_meta.get('file_path') or '')
    reported_size = int(file_meta.get('file_size') or 0)
    if not file_path:
        fail(f'Local Telegram Bot API accepted the relay media handle but returned no file path (size {reported_size}).')
    if reported_size and reported_size != expected_size:
        fail('Local Telegram Bot API relay media size does not match the staged source.')
    files_root = Path(str(os.environ.get('LOCAL_BOT_API_FILES_DIR') or '')).expanduser()
    if files_root.is_dir():
        remote_path = Path(file_path)
        try:
            relative_path = remote_path.relative_to('/var/lib/telegram-bot-api')
        except ValueError:
            fail('Local Telegram Bot API returned an unsafe relay file path.')
        local_path = (files_root / relative_path).resolve()
        try:
            local_path.relative_to(files_root.resolve())
        except ValueError:
            fail('Local Telegram Bot API returned a relay file outside its shared storage.')
        stream_local_bot_api_media_to_runner(remote_path, output_path)
        if not output_path.is_file() or output_path.stat().st_size != expected_size:
            output_path.unlink(missing_ok=True)
            fail('Local Telegram Bot API streamed relay media with an unexpected size.')
        return True
    written = 0
    try:
        with requests.get(
            f'{base}/file/bot{bot_token}/{file_path}',
            stream=True,
            timeout=(REQUEST_TIMEOUT, 15 * 60),
        ) as response:
            if response.status_code >= 400:
                fail(f'Local Telegram Bot API file download failed with HTTP {response.status_code}.')
            with output_path.open('wb') as target:
                for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > expected_size:
                        fail('Local Telegram Bot API returned media larger than the staged source.')
                    target.write(chunk)
    except requests.RequestException as error:
        raise RuntimeError('Local Telegram Bot API media download could not be completed.') from error
    if written != expected_size:
        fail('Local Telegram Bot API download did not match the staged media size.')
    return True


async def download_group_media(telegram: dict[str, Any], output_path: Path, relay_file_id: str = '') -> None:
    """Download one Bot A-copied basic-group video through an authorized client.

    Telegram permits Bot B to receive the authenticated marker but can return an
    empty ``messages.getMessages`` response for bot sessions in basic groups.
    In that documented limitation, the existing dedicated user-authorized media
    session is used only for the exact group/message pair sealed by Bot A.
    """
    try:
        from telethon import TelegramClient, functions, types
        from telethon.sessions import MemorySession, StringSession
    except ImportError as error:
        raise RuntimeError('Telegram MTProto dependencies are unavailable.') from error
    try:
        bot_api_id = int(os.environ['BOTB_MTPROTO_API_ID'])
    except (KeyError, ValueError) as error:
        raise RuntimeError('Bot B MTProto API ID is invalid.') from error
    bot_api_hash = str(os.environ.get('BOTB_MTPROTO_API_HASH') or '')
    bot_token = str(os.environ.get('BOTB_MTPROTO_BOT_TOKEN') or '')
    if not bot_api_hash or not bot_token:
        fail('Bot B MTProto credentials are not configured.')
    group_chat_id = int(telegram['group_chat_id'])
    group_message_id = int(telegram['group_message_id'])
    verify_bot_group_access(bot_token, group_chat_id)
    expected_size = int(telegram['declared_size'])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    preflight_space(expected_size, output_path.parent)
    if download_local_bot_api_media(bot_token, relay_file_id, expected_size, output_path):
        return
    client: Any | None = None
    try:
        client = TelegramClient(MemorySession(), bot_api_id, bot_api_hash, connection_retries=3, request_retries=3, retry_delay=1, receive_updates=False)
        await client.connect()
        await client(functions.auth.ImportBotAuthorizationRequest(
            flags=0, api_id=bot_api_id, api_hash=bot_api_hash, bot_auth_token=bot_token
        ))
        request = known_basic_group_message_request(
            group_chat_id,
            group_message_id,
            functions.messages.GetMessagesRequest,
            types.InputMessageID,
        )
        result = await client(request)
        messages = list(getattr(result, 'messages', []) or [])
        message = next(
            (candidate for candidate in messages
             if int(getattr(candidate, 'id', 0) or 0) == group_message_id),
            None,
        )
        if not message or not getattr(message, 'media', None):
            await client.disconnect()
            client = None
            try:
                user_api_id = int(os.environ['CLIPFORGE_TELEGRAM_API_ID'])
            except (KeyError, ValueError) as error:
                raise RuntimeError('Dedicated Telegram media-session API ID is invalid.') from error
            user_api_hash = str(os.environ.get('CLIPFORGE_TELEGRAM_API_HASH') or '')
            user_session = str(os.environ.get('CLIPFORGE_TELEGRAM_SESSION') or '')
            if not user_api_hash or not user_session:
                fail('Dedicated Telegram media-session credentials are not configured.')
            client = TelegramClient(StringSession(user_session), user_api_id, user_api_hash, connection_retries=3, request_retries=3, retry_delay=1, receive_updates=False)
            await client.connect()
            if not await client.is_user_authorized():
                fail('The dedicated Telegram media session is no longer authorized.')
            # A basic-group peer cannot be reconstructed from its numeric ID in
            # a fresh Telethon session. Resolve it from the authorized user's
            # dialog list, which carries the required in-session peer metadata.
            dialogs = await client.get_dialogs(limit=200)
            entity = next(
                (dialog.entity for dialog in dialogs if int(getattr(dialog, 'id', 0) or 0) == group_chat_id),
                None,
            )
            if entity is None:
                fail('The dedicated Telegram media session cannot access the private relay group.')
            message = await client.get_messages(entity, ids=group_message_id)
        if not message or not getattr(message, 'media', None):
            fail('The internal relay message is unavailable or has no media.')
        mime = str(getattr(getattr(message, 'file', None), 'mime_type', '') or '').lower()
        actual_size = int(getattr(getattr(message, 'file', None), 'size', 0) or 0)
        if not getattr(message, 'video', None) and not mime.startswith('video/'):
            fail('The internal relay message is not a video attachment.')
        if actual_size <= 0 or actual_size > MAX_RELAY_BYTES:
            fail('The actual Telegram media size is unsupported for direct forwarding.')
        if actual_size != expected_size:
            fail('The internal relay media size does not match Bot A’s staged source metadata.')
        try:
            await asyncio.wait_for(_download_telegram_parallel(client, message, str(output_path), actual_size), timeout=45 * 60)
        except _ParallelTelegramTransferError as error:
            if output_path.exists():
                output_path.unlink()
            print(f'Parallel Telegram transfer fell back to one connection: {error}', flush=True)
            await asyncio.wait_for(client.download_media(message, file=str(output_path)), timeout=45 * 60)
        if not output_path.is_file() or output_path.stat().st_size != actual_size:
            fail('Authenticated Telegram media download completed with an incomplete file.')
    except asyncio.TimeoutError as error:
        if output_path.exists():
            output_path.unlink()
        raise RuntimeError('Telegram media download exceeded the 45-minute safety limit.') from error
    finally:
        if client is not None:
            await client.disconnect()


def handoff_token(repo: str, sealed_token: str) -> str:
    """Use the workflow-scoped token only for its own configured repository.

    Relay jobs can target a user clone and retain their sealed, user-provided
    token in that case. For the central repository itself, GitHub supplies a
    fresh workflow token with the exact write scopes declared by the workflow,
    avoiding an expired token that may have been stored when a task was made.
    """
    central_repo = str(os.environ.get('RELAY_CENTRAL_REPOSITORY') or '').strip()
    central_token = str(os.environ.get('RELAY_CENTRAL_GITHUB_TOKEN') or '').strip()
    if central_repo and central_token and repo == central_repo:
        return central_token
    return sealed_token


def api_headers(token: str) -> dict[str, str]:
    return {
        'Accept': 'application/vnd.github+json',
        'Authorization': f'Bearer {token}',
        'X-GitHub-Api-Version': API_VERSION,
    }


def github_request(token: str, method: str, url: str, *, operation: str, **kwargs: Any) -> requests.Response:
    response = requests.request(method, url, headers=api_headers(token), timeout=REQUEST_TIMEOUT, **kwargs)
    if response.status_code >= 400:
        # Do not interpolate URLs or response content: either can reflect a
        # credential or untrusted input. The static operation label is safe.
        raise GitHubHandoffError(operation, response.status_code)
    return response


def release_tag(job_id: str) -> str:
    return f'clipforge-relay-input-{job_id}'


def create_release(repo: str, token: str, job_id: str) -> dict[str, Any]:
    url = f'https://api.github.com/repos/{repo}/releases'
    response = github_request(token, 'POST', url, operation='temporary release creation', json={
        'tag_name': release_tag(job_id),
        'target_commitish': 'main',
        'name': f'ClipForge temporary relay input — {job_id}',
        'body': 'Temporary private source handoff. ClipForge deletes this asset after task expiry.',
        'draft': False,
        'prerelease': True,
        'generate_release_notes': False,
    })
    result = response.json()
    if not isinstance(result, dict) or not result.get('upload_url'):
        fail('GitHub did not return a release upload URL.')
    return result


def upload_release_asset(release: dict[str, Any], token: str, source_path: Path) -> None:
    upload_url = str(release['upload_url']).split('{', 1)[0]
    with source_path.open('rb') as source:
        response = requests.post(
            f'{upload_url}?name={quote("source_input.bin")}',
            headers={**api_headers(token), 'Content-Type': 'application/octet-stream'},
            data=source,
            timeout=60 * 60,
        )
    if response.status_code >= 400:
        raise RuntimeError(f'GitHub temporary-release upload failed with HTTP {response.status_code}.')


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def apply_relay_metadata(request_doc: dict[str, Any], job_id: str, tag: str, size: int, digest: str) -> dict[str, Any]:
    """Fill the §7.1 nested request's ``source.relay`` block (validated against
    schemas/stage_a_request.schema.json) so the clone's Stage A can fetch and
    checksum the temporary asset. Fails closed if the committed request does
    not belong to this relay job."""
    if str(request_doc.get('job_id')) != job_id:
        fail('Target clone Stage A request does not belong to this relay job.')
    source = request_doc.get('source')
    if not isinstance(source, dict) or str(source.get('kind')) != 'telegram_relay':
        fail('Target clone Stage A request does not match this relay job.')
    source['value'] = 'relay:private'
    source['relay'] = {
        'release_tag': tag,
        'expected_size_bytes': str(size),
        'sha256': digest,
    }
    request_doc['version'] = 2
    request_doc['source'] = source
    request_doc['saved_at_epoch'] = int(time.time())
    return request_doc


def write_stage_a_request_and_dispatch(repo: str, token: str, job_id: str, payload_request: dict[str, Any], size: int, digest: str) -> None:
    content_url = f'https://api.github.com/repos/{repo}/contents/jobs/{quote(job_id, safe="")}/stage-a-request.json?ref=main'
    request_sha = ''
    try:
        current = github_request(token, 'GET', content_url, operation='Stage A request lookup').json()
        request_doc = json.loads(base64.b64decode(str(current['content']).replace('\n', '')).decode())
        request_sha = str(current['sha'])
    except GitHubHandoffError as error:
        if error.status != 404:
            raise
        request_doc = copy.deepcopy(payload_request)
    except Exception as error:
        raise RuntimeError('Target clone Stage A request is unreadable.') from error
    tag = release_tag(job_id)
    request_doc = apply_relay_metadata(request_doc, job_id, tag, size, digest)
    encoded = base64.b64encode((json.dumps(request_doc, indent=2, sort_keys=True) + '\n').encode()).decode()
    write_url = f'https://api.github.com/repos/{repo}/contents/jobs/{quote(job_id, safe="")}/stage-a-request.json'
    write_body: dict[str, Any] = {
        'message': f'clipforge: attach private relay source for job {job_id}',
        'content': encoded,
        'branch': 'main',
    }
    if request_sha:
        write_body['sha'] = request_sha
    github_request(token, 'PUT', write_url, operation='Stage A request update', json=write_body)
    dispatch_url = f'https://api.github.com/repos/{repo}/actions/workflows/stage-a.yml/dispatches'
    github_request(token, 'POST', dispatch_url, operation='Stage A dispatch', json={
        'ref': 'main',
        'inputs': {'job_id': job_id, 'code_ref': ''},
    })


def run(job_id: str, serialized_payload: str, relay_file_id: str = '') -> None:
    payload = decrypt_payload(job_id, serialized_payload, os.environ.get('RELAY_ENCRYPTION_KEY', ''))
    repo, token, request_doc, telegram = ensure_payload(payload)
    token = handoff_token(repo, token)
    work = Path('work/telegram-relay')
    source = work / 'source_input.bin'
    asyncio.run(download_group_media(telegram, source, relay_file_id))
    actual_size = source.stat().st_size
    if actual_size != int(telegram['declared_size']):
        fail('Downloaded source size changed before handoff.')
    digest = sha256(source)
    release = create_release(repo, token, job_id)
    try:
        upload_release_asset(release, token, source)
        write_stage_a_request_and_dispatch(repo, token, job_id, request_doc, actual_size, digest)
    except Exception:
        # Best-effort cleanup. No error details are logged because release API
        # errors can include untrusted reflected content.
        try:
            github_request(token, 'DELETE', f'https://api.github.com/repos/{repo}/releases/{int(release["id"])}', operation='temporary release cleanup')
        except Exception:
            pass
        raise
    finally:
        source.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description='Relay one Bot B Telegram media message into a target ClipForge clone.')
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--relay-payload', required=True)
    parser.add_argument('--relay-file-id', default='')
    args = parser.parse_args()
    run(str(args.job_id), str(args.relay_payload), str(args.relay_file_id))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Private Telegram relay failed: {error}', file=sys.stderr)
        raise SystemExit(1)
