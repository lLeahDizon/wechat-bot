"""Supervise the Native event stream and retain content-free diagnostics."""

import asyncio
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import random
import re
import threading

_SAFE_CODES = {
    "event_cursor_expired",
    "invalid_cursor",
    "unauthorized",
    "forbidden",
    "not_found",
    "timeout",
    "account_not_found",
    "insufficient_scope",
    "request_failed",
}
_EVENTS = {
    "run_started",
    "account_offline",
    "stream_connecting",
    "stream_open",
    "stream_closed",
    "api_error",
    "cursor_expired",
    "run_error",
    "close_error",
    "retry_wait",
    "run_stopped",
}
_SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class ConnectionDiagnostics:
    """Append bounded JSONL records containing only fixed diagnostic fields."""

    def __init__(self, path: Path, *, max_bytes: int = 256 * 1024, backups: int = 2):
        if type(max_bytes) is not int or max_bytes < 256:
            raise ValueError("max_bytes must be at least 256")
        if type(backups) is not int or not 0 <= backups <= 5:
            raise ValueError("backups must be between 0 and 5")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()

    def record(
        self,
        event: str,
        *,
        cursor: str | None = None,
        status: int | None = None,
        code: str | None = None,
        error_type: str | None = None,
        retry_seconds: float | None = None,
    ) -> bool:
        if event not in _EVENTS:
            return False
        record = {
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
        }
        if isinstance(cursor, str) and cursor.isascii() and cursor.isdecimal() and len(cursor) <= 19:
            record["cursor"] = cursor
        if type(status) is int and 0 <= status <= 599:
            record["status"] = status
        if isinstance(code, str):
            record["code"] = code if code in _SAFE_CODES else "other"
        if isinstance(error_type, str):
            record["error_type"] = error_type if _SAFE_NAME.fullmatch(error_type) else "OtherError"
        if isinstance(retry_seconds, (int, float)) and not isinstance(retry_seconds, bool):
            if math.isfinite(retry_seconds) and 0 <= retry_seconds <= 3600:
                record["retry_seconds"] = round(float(retry_seconds), 2)
        payload = (json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
        try:
            with self._lock:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if self.path.parent.is_symlink() or self.path.is_symlink():
                    return False
                self._rotate(len(payload))
                flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(self.path, flags, 0o600)
                try:
                    os.fchmod(descriptor, 0o600)
                    view = memoryview(payload)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise OSError("short diagnostics write")
                        view = view[written:]
                finally:
                    os.close(descriptor)
            return True
        except (OSError, ValueError, TypeError):
            return False

    def _rotate(self, incoming_bytes: int) -> None:
        if not self.path.exists():
            return
        if self.path.is_symlink() or self.path.stat().st_size + incoming_bytes <= self.max_bytes:
            return
        if self.backups == 0:
            self.path.unlink()
            return
        for index in range(self.backups, 0, -1):
            destination = self.path.with_name(self.path.name + f".{index}")
            source = self.path if index == 1 else self.path.with_name(self.path.name + f".{index - 1}")
            if destination.is_symlink() or source.is_symlink():
                raise OSError("diagnostics rotation path is a symlink")
            if destination.exists():
                destination.unlink()
            if source.exists():
                os.replace(source, destination)


async def supervise_native_events(
    client,
    account_id: str,
    state,
    process_event,
    is_stopped,
    diagnostics: ConnectionDiagnostics,
    *,
    sleep=asyncio.sleep,
    jitter=random.uniform,
    initial_delay: float = 1.0,
    maximum_delay: float = 30.0,
    cursor_error_delay: float = 60.0,
) -> None:
    delay = initial_delay
    diagnostics.record("run_started", cursor=state.cursor)
    while not is_stopped():
        retry_delay = delay
        try:
            await client.open()
            account = await client.account()
            if account.get("id") != account_id:
                raise ValueError("account_identity_mismatch")
            status = await client.status()
            state.bind_server(status.get("serverId"))
            if account.get("authenticated") is not True or account.get("connection") != "online":
                diagnostics.record("account_offline", cursor=state.cursor)
            else:
                diagnostics.record("stream_connecting", cursor=state.cursor)

                def stream_opened():
                    nonlocal delay, retry_delay
                    delay = initial_delay
                    retry_delay = initial_delay
                    diagnostics.record("stream_open", cursor=state.cursor)

                async for event in client.events(state.cursor, on_connect=stream_opened):
                    await process_event(event)
                    delay = initial_delay
                    retry_delay = initial_delay
                if not is_stopped():
                    diagnostics.record("stream_closed", cursor=state.cursor)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            status = getattr(error, "status", None)
            code = getattr(error, "code", None)
            cursor_expired = status == 410 or code == "event_cursor_expired"
            event = "cursor_expired" if cursor_expired else "api_error" if type(status) is int else "run_error"
            diagnostics.record(
                event,
                cursor=state.cursor,
                status=status if type(status) is int else None,
                code=code if isinstance(code, str) else None,
                error_type=type(error).__name__,
            )
            if cursor_expired:
                retry_delay = cursor_error_delay
                delay = initial_delay
        finally:
            try:
                await client.close()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                diagnostics.record("close_error", cursor=state.cursor, error_type=type(error).__name__)

        if is_stopped():
            break
        wait = retry_delay + jitter(0, retry_delay * 0.2)
        diagnostics.record("retry_wait", cursor=state.cursor, retry_seconds=wait)
        await sleep(wait)
        if retry_delay != cursor_error_delay:
            delay = min(maximum_delay, max(initial_delay, delay * 2))
    diagnostics.record("run_stopped", cursor=state.cursor)
