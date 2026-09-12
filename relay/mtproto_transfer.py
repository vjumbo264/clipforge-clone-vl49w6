"""Bounded parallel MTProto media transfer — PRESERVED SUBSYSTEM support code.

Ported essentially verbatim from _legacy/scripts/download_drive.py (the
_ParallelTelegramTransferError / _parallel_telegram_client /
_download_telegram_parallel block). Used by relay/telegram_relay.py (§9.2) and
later by pipeline/stage_a/telegram_channel.py (§9.1). Do NOT redesign: at most
four isolated connections, disjoint 512 KiB ranges, direct-offset writes, safe
single-connection fallback.
"""

import asyncio
import os
import time


TELEGRAM_PARALLEL_MIN_BYTES = 64 * 1024 * 1024
TELEGRAM_PARALLEL_WORKERS = 4
TELEGRAM_PARALLEL_PART_BYTES = 512 * 1024


class _ParallelTelegramTransferError(RuntimeError):
    """Signal that the safe sequential Telethon fallback should take over."""


async def _parallel_telegram_client(parent, target_dc: int, primary_dc: int):
    """Create one isolated MTProto connection for a bounded file worker."""
    from telethon import TelegramClient, functions
    from telethon.sessions import StringSession

    kwargs = {
        'connection_retries': 3,
        'request_retries': 1,
        'retry_delay': 1,
        'receive_updates': False,
        'raise_last_call_error': True,
    }
    if target_dc == primary_dc:
        # A copied in-memory session creates an independent connection without
        # placing the account session on disk. This is limited to four workers.
        copied_session = StringSession.save(parent.session)
        child = TelegramClient(StringSession(copied_session), parent.api_id, parent.api_hash, **kwargs)
        await child.connect()
        return child

    # Media may live in a different DC. Use Telethon's own current DC resolver
    # rather than hardcoding Telegram IP addresses, then import a one-use auth.
    dc = await parent._get_dc(target_dc)
    child = TelegramClient(StringSession(), parent.api_id, parent.api_hash, **kwargs)
    child.session.set_dc(dc.id, dc.ip_address, dc.port)
    await child.connect()
    await child(functions.help.GetConfigRequest())
    exported = await parent(functions.auth.ExportAuthorizationRequest(target_dc))
    await child(functions.auth.ImportAuthorizationRequest(exported.id, exported.bytes))
    return child


async def _download_telegram_parallel(client, message, output_path: str, media_size: int) -> str:
    """Download a large document through a conservative isolated MTProto pool.

    Telethon's public ``download_media`` uses one connection. This implementation
    creates at most four isolated connections, fetches disjoint 512 KiB ranges,
    writes them directly at their offsets, and falls back to the stock path if a
    protocol edge case cannot be handled safely.
    """
    from telethon import errors, functions, types, utils

    dc_id, location = utils.get_input_location(message.media)
    target_dc = int(dc_id or client.session.dc_id)
    primary_dc = int(client.session.dc_id)
    workers = min(TELEGRAM_PARALLEL_WORKERS, max(1, (media_size + TELEGRAM_PARALLEL_MIN_BYTES - 1) // TELEGRAM_PARALLEL_MIN_BYTES))
    if workers < 2:
        return await client.download_media(message, file=output_path)

    queue: asyncio.Queue[tuple[int, int]] = asyncio.Queue()
    for offset in range(0, media_size, TELEGRAM_PARALLEL_PART_BYTES):
        queue.put_nowait((offset, min(TELEGRAM_PARALLEL_PART_BYTES, media_size - offset)))

    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or '.', exist_ok=True)
    file_descriptor = os.open(output_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    children = []
    completed_bytes = 0
    progress_lock = asyncio.Lock()
    last_report = time.monotonic()

    async def worker(index: int, child) -> None:
        nonlocal completed_bytes, last_report
        while True:
            try:
                offset, length = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            attempts = 0
            while True:
                try:
                    result = await asyncio.wait_for(
                        # Telegram requires request limits to be a valid MTProto
                        # part size. Keep the final request at 512 KiB and accept
                        # its naturally shorter response.
                        child(functions.upload.GetFileRequest(location, offset=offset, limit=TELEGRAM_PARALLEL_PART_BYTES)),
                        timeout=25,
                    )
                    if isinstance(result, types.upload.FileCdnRedirect):
                        raise _ParallelTelegramTransferError('Telegram redirected this media through its CDN.')
                    data = result.bytes
                    if not data or len(data) > length:
                        raise _ParallelTelegramTransferError('Telegram returned an invalid media part.')
                    os.pwrite(file_descriptor, data, offset)
                    async with progress_lock:
                        completed_bytes += len(data)
                        now = time.monotonic()
                        if now - last_report >= 10:
                            print(f'  ...parallel Telegram transfer {completed_bytes / 1e6:.1f} MB / {media_size / 1e6:.1f} MB ({completed_bytes * 100.0 / media_size:.1f}%) with {workers} workers', flush=True)
                            last_report = now
                    break
                except errors.FloodWaitError as error:
                    attempts += 1
                    if attempts > 2:
                        raise _ParallelTelegramTransferError('Telegram rate-limited the parallel media transfer.') from error
                    await asyncio.sleep(max(1, int(error.seconds)))
                except (errors.TimedOutError, errors.ServerError, asyncio.TimeoutError, ConnectionError, OSError) as error:
                    # Telegram frequently returns a recoverable server timeout for
                    # an individual range. Retrying that range preserves all other
                    # direct-offset writes instead of restarting the whole file.
                    attempts += 1
                    if attempts > 8:
                        raise _ParallelTelegramTransferError('Parallel Telegram media transfer could not recover from repeated network timeouts.') from error
                    await asyncio.sleep(min(12, attempts * 2))
            queue.task_done()

    try:
        print(f'Downloading the public Telegram channel video through {workers} authenticated MTProto connections.', flush=True)
        children = [await _parallel_telegram_client(client, target_dc, primary_dc) for _ in range(workers)]
        await asyncio.gather(*(worker(index, child) for index, child in enumerate(children)))
        if completed_bytes != media_size or os.path.getsize(output_path) != media_size:
            raise _ParallelTelegramTransferError('Parallel Telegram transfer finished with an incomplete file.')
        print(f'Done. Wrote parallel authenticated public Telegram post video to {output_path}.', flush=True)
        return output_path
    except _ParallelTelegramTransferError:
        raise
    except Exception as error:
        print(f'Parallel Telegram transfer diagnostic: {type(error).__name__}: {error}', flush=True)
        raise _ParallelTelegramTransferError('Parallel Telegram media transfer could not initialize safely.') from error
    finally:
        os.close(file_descriptor)
        await asyncio.gather(*(child.disconnect() for child in children), return_exceptions=True)
