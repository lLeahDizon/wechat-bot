"""Registered in-process AstrBot platform adapter."""

import asyncio
import hashlib
import logging
import os
import random
import re
import sqlite3
import stat
import sys
import threading
import time
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.platform import Platform, PlatformMetadata, register_platform_adapter
from astrbot.api.star import StarTools

from .config import (
    ADAPTER_ID,
    CONFIG_METADATA,
    DEFAULT_CONFIG,
    MAX_CONCURRENT_MEDIA_TASKS,
    PLUGIN_ID,
    NativeConfig,
)
from .event import (
    WeChatNativeEvent,
    convert_message,
    message_timestamp,
    serialize_text,
    session_target,
)
from .native_client import (
    GroupMembersUnavailable,
    MediaError,
    NativeAPIError,
    NativeClient,
    SendError,
)
from .state import InboxFullError, StateIdentityError, StateStore, cursor_number
from .summary import GroupSummaryCache, is_summary_command

_RUNTIME_LOGGER_NAME = f"{PLUGIN_ID}.adapter_runtime"
_RUNTIME_LOGGER_LOCK = threading.Lock()
_RUNTIME_LOG_MAX_BYTES = 256 * 1024
_RUNTIME_LOG_BACKUP_COUNT = 2
_INBOX_MAX_PENDING = 512
_INBOX_STAGES = {
    "unknown", "recovery", "claim", "idle_wait", "fetch", "identity", "classify",
    "cache_policy", "ignore_commit", "summary_cache", "convert", "media_prepare",
    "wrap", "media", "media_finish", "handoff_prepare", "queue_handoff",
    "handoff_finish", "event_cache", "error_recovery", "report_error", "retry_wait", "notify",
}
_INBOX_ERROR_KINDS = {
    "attribute", "key", "type", "value", "database", "identity", "timeout", "network", "unexpected",
}

_RUNTIME_EVENT_FIELDS = {
    "supervisor_started": {},
    "account_offline": {},
    "sse_connect_attempt": {},
    "sse_first_event_received": {},
    "sse_stream_ended": {"action": {"reconnect"}},
    "native_terminal_http": {"http_status": "http"},
    "native_http_retry": {"http_status": "http"},
    "validation_retry": {},
    "server_identity_changed": {"terminal": {"true"}},
    "unexpected_retry": {"category": {"timeout", "network", "unexpected"}},
    "reconnect_scheduled": {"delay_seconds": "delay"},
    "client_close_error": {"category": {"timeout", "network", "unexpected"}},
    "native_event_received": {"kind": {"message_committed", "message_recalled", "other"}},
    "duplicate_cursor": {},
    "message_identity_invalid": {},
    "message_filtered": {"reason": {"not_live", "thread_policy"}},
    "cursor_advanced": {},
    "recall_processed": {},
    "native_message_fetch_started": {},
    "native_message_fetch_completed": {},
    "message_identity_mismatch": {},
    "message_conversion_started": {},
    "message_conversion_completed": {},
    "checkpoint_commit_started": {},
    "checkpoint_committed": {"fresh": {"true", "false"}},
    "queue_handoff_started": {},
    "queue_handoff_completed": {},
    "queue_handoff_failed": {},
    "inbox_worker_started": {},
    "inbox_worker_error": {"stage": _INBOX_STAGES, "error": _INBOX_ERROR_KINDS},
    "inbox_worker_ended": {},
    "inbox_worker_restart": {"delay_seconds": "delay"},
    "inbox_item_error": {"stage": _INBOX_STAGES, "error": _INBOX_ERROR_KINDS},
    "inbox_enqueue_completed": {"fresh": {"true", "false"}},
    "inbox_backpressure_started": {},
    "inbox_backpressure_resumed": {},
    "inbox_item_started": {},
    "inbox_item_completed": {},
    "inbox_item_review": {},
    "inbox_retry_scheduled": {
        "category": {"timeout", "network", "unexpected"},
        "delay_seconds": "delay",
    },
}


def _safe_runtime_event(message: str, args: tuple) -> str:
    try:
        rendered = message % args if args else message
    except (TypeError, ValueError):
        return "event=diagnostic_invalid"
    parts = rendered.split()
    if not parts or not parts[0].startswith("event="):
        return "event=diagnostic_invalid"
    event = parts[0][6:]
    schema = _RUNTIME_EVENT_FIELDS.get(event)
    if schema is None or len(parts) != len(schema) + 1:
        return "event=diagnostic_invalid"
    seen = set()
    for token in parts[1:]:
        key, separator, value = token.partition("=")
        if not separator or key not in schema or key in seen:
            return "event=diagnostic_invalid"
        seen.add(key)
        rule = schema[key]
        if rule == "http":
            if (
                not value.isascii()
                or not value.isdecimal()
                or len(value) > 3
                or not 0 <= int(value) <= 599
            ):
                return "event=diagnostic_invalid"
        elif rule == "delay":
            if (
                not value.isascii()
                or not value.isdecimal()
                or len(value) > 2
                or not 1 <= int(value) <= 30
            ):
                return "event=diagnostic_invalid"
        elif value not in rule:
            return "event=diagnostic_invalid"
    if seen != set(schema):
        return "event=diagnostic_invalid"
    return rendered


def _rotate_runtime_log(path: Path, incoming_bytes: int) -> None:
    try:
        current = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(current.st_mode):
        raise OSError("runtime_log_not_regular")
    os.chmod(path, 0o600, follow_symlinks=False)
    if current.st_size + incoming_bytes <= _RUNTIME_LOG_MAX_BYTES:
        return

    oldest = Path(f"{path}.{_RUNTIME_LOG_BACKUP_COUNT}")
    try:
        old_stat = os.stat(oldest, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(old_stat.st_mode):
            raise OSError("runtime_log_backup_not_regular")
        os.unlink(oldest)

    for index in range(_RUNTIME_LOG_BACKUP_COUNT - 1, 0, -1):
        source = Path(f"{path}.{index}")
        try:
            source_stat = os.stat(source, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(source_stat.st_mode):
            raise OSError("runtime_log_backup_not_regular")
        target = Path(f"{path}.{index + 1}")
        os.replace(source, target)
        os.chmod(target, 0o600, follow_symlinks=False)

    first_backup = Path(f"{path}.1")
    os.replace(path, first_backup)
    os.chmod(first_backup, 0o600, follow_symlinks=False)


def _runtime_log(level: int, message: str, *args) -> None:
    try:
        levels = {
            logging.INFO: "INFO",
            logging.WARNING: "WARNING",
            logging.ERROR: "ERROR",
        }
        level_name = levels.get(level, "INFO")
        line = (
            f"{datetime.now(timezone.utc).isoformat(timespec='milliseconds')} "
            f"{level_name} {_safe_runtime_event(message, args)}\n"
        ).encode("ascii")
        root = Path(StarTools.get_data_dir(PLUGIN_ID))
        root.mkdir(parents=True, exist_ok=True)
        path = root / "adapter-runtime.log"
        with _RUNTIME_LOGGER_LOCK:
            _rotate_runtime_log(path, len(line))
            flags = os.O_CREAT | os.O_WRONLY | os.O_APPEND
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
                offset = 0
                while offset < len(line):
                    written = os.write(fd, line[offset:])
                    if written <= 0:
                        raise OSError("runtime_log_write_failed")
                    offset += written
            finally:
                os.close(fd)
    except Exception:
        try:
            logger.warning("WeChat Native 专用诊断日志不可写")
        except Exception:
            pass


def _retry_category(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, (ConnectionError, OSError)):
        return "network"
    return "unexpected"


def _inbox_error_kind(error: Exception) -> str:
    for error_type, kind in (
        (StateIdentityError, "identity"), (sqlite3.Error, "database"),
        (AttributeError, "attribute"), (KeyError, "key"), (TypeError, "type"),
        (ValueError, "value"), (TimeoutError, "timeout"),
        ((ConnectionError, OSError), "network"),
    ):
        if isinstance(error, error_type):
            return kind
    return "unexpected"


@register_platform_adapter(
    ADAPTER_ID,
    "直接连接 agent-wechat-native 的个人微信平台适配器",
    default_config_tmpl=DEFAULT_CONFIG.copy(),
    adapter_display_name="WeChat Native",
    logo_path="logo.svg",
    support_streaming_message=False,
    config_metadata=CONFIG_METADATA,
)
class WeChatNativeAdapter(Platform):
    reconnect_delay = 1.0
    max_reconnect_delay = 30.0
    stable_connection_interval = 60.0
    time_warning_interval = 60.0

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
        *,
        state_dir: Path | None = None,
    ):
        super().__init__(platform_config, event_queue)
        self.native_config = NativeConfig.from_dict(platform_config)
        cfg = self.native_config
        self.client = NativeClient(cfg.api_url, cfg.account_id, cfg.load_token())
        # PlatformManager owns client_self_id (its instance/task UUID).
        self.bot_self_id = cfg.account_id
        root = state_dir if state_dir is not None else StarTools.get_data_dir(PLUGIN_ID)
        identity = f"{cfg.api_url}\n{cfg.account_id}\n{platform_config.get('id', ADAPTER_ID)}"
        self.state = StateStore(
            Path(root) / (hashlib.sha256(identity.encode()).hexdigest() + ".sqlite3")
        )
        self.stopped = False
        self._state_closed = False
        self._run_task = None
        self._inbox_task = None
        self._inbox_wakeup = asyncio.Event()
        self._inbox_space = asyncio.Event()
        self._inbox_space.set()
        self._conversion_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="wechat-native-convert"
        )
        self._send_tasks = set()
        self._media_tasks = set()
        self._media_semaphore = asyncio.Semaphore(MAX_CONCURRENT_MEDIA_TASKS)
        self._events = weakref.WeakValueDictionary()
        self._send_lock = asyncio.Lock()
        self._terminate_lock = asyncio.Lock()
        self._last_time_warning = float("-inf")
        self.summary_cache = GroupSummaryCache(cfg.summary_max_messages, cfg.summary_window_minutes)

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name=ADAPTER_ID,
            description="WeChat Native",
            id=self.config.get("id", ADAPTER_ID),
            adapter_display_name="WeChat Native",
            support_streaming_message=False,
        )

    def get_client(self):
        return self.client

    def _ensure_inbox_worker(self):
        inbox_task = getattr(self, "_inbox_task", None)
        if inbox_task is not None and not inbox_task.done():
            return
        if not hasattr(self, "_inbox_wakeup"):
            self._inbox_wakeup = asyncio.Event()
        if not hasattr(self, "_inbox_space"):
            self._inbox_space = asyncio.Event()
            self._inbox_space.set()
        self._inbox_task = asyncio.create_task(self._supervise_inbox_worker())

    def _record_inbox_error(self, event: str, error: Exception):
        stage = getattr(self, "_inbox_stage", "unknown")
        if stage not in _INBOX_STAGES:
            stage = "unknown"
        _runtime_log(
            logging.ERROR, "event=%s stage=%s error=%s", event, stage, _inbox_error_kind(error)
        )

    async def _supervise_inbox_worker(self):
        delay = self.reconnect_delay
        while not self.stopped:
            started_at = time.monotonic()
            try:
                self._inbox_stage = "recovery"
                self.state.recover_inflight()
                self._notify_inbox_space()
                await self._run_inbox_worker()
                if not self.stopped:
                    _runtime_log(logging.ERROR, "event=inbox_worker_ended")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._record_inbox_error("inbox_worker_error", error)
            if self.stopped:
                return
            if time.monotonic() - started_at >= self.stable_connection_interval:
                delay = self.reconnect_delay
            _runtime_log(
                logging.WARNING, "event=inbox_worker_restart delay_seconds=%.0f", delay
            )
            await asyncio.sleep(delay + random.uniform(0, delay * 0.2))
            delay = min(self.max_reconnect_delay, delay * 2)

    async def _convert_message(self, raw: dict, *, image_bytes: bytes | None = None):
        if not hasattr(self, "_conversion_executor"):
            self._conversion_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="wechat-native-convert"
            )
        loop = asyncio.get_running_loop()
        convert = partial(
            convert_message,
            raw,
            self.native_config,
            self.bot_self_id,
            image_bytes=image_bytes,
        )
        return await loop.run_in_executor(self._conversion_executor, convert)

    async def _wait_for_inbox_space(self):
        if not hasattr(self, "_inbox_space"):
            self._inbox_space = asyncio.Event()
        while not self.stopped:
            self._inbox_space.clear()
            if self.state.pending_inbox_count() < _INBOX_MAX_PENDING:
                return
            await self._inbox_space.wait()

    def _notify_inbox_space(self):
        try:
            if (
                hasattr(self, "_inbox_space")
                and self.state.pending_inbox_count() < _INBOX_MAX_PENDING
            ):
                self._inbox_space.set()
        except Exception:
            self._inbox_stage = "notify"
            raise

    async def _run_inbox_worker(self):
        _runtime_log(logging.INFO, "event=inbox_worker_started")
        delay = self.reconnect_delay
        while not self.stopped:
            self._inbox_stage = "claim"
            row = self.state.claim_inbox()
            if row is None:
                self._inbox_stage = "idle_wait"
                self._inbox_wakeup.clear()
                if self.state.pending_inbox_count():
                    self._inbox_wakeup.set()
                    continue
                await self._inbox_wakeup.wait()
                continue

            message_id = row["message_id"]
            _runtime_log(logging.INFO, "event=inbox_item_started")
            try:
                await self._process_inbox_item(row)
                delay = self.reconnect_delay
                _runtime_log(logging.INFO, "event=inbox_item_completed")
            except asyncio.CancelledError:
                self._inbox_stage = "recovery"
                try:
                    self.state.recover_inbox_item(message_id)
                except Exception as error:
                    self._record_inbox_error("inbox_item_error", error)
                raise
            except NativeAPIError as error:
                status = error.status if type(error.status) is int else 0
                if status in {401, 403, 410}:
                    self._inbox_stage = "error_recovery"
                    self.state.fail_inbox_handoff(message_id)
                    _runtime_log(
                        logging.ERROR,
                        "event=native_terminal_http http_status=%s",
                        status,
                    )
                    self.stopped = True
                    if hasattr(self, "_inbox_wakeup"):
                        self._inbox_wakeup.set()
                    if hasattr(self, "_inbox_space"):
                        self._inbox_space.set()
                    run_task = getattr(self, "_run_task", None)
                    if run_task is not None and run_task is not asyncio.current_task():
                        run_task.cancel()
                    self._inbox_stage = "report_error"
                    logger.error("WeChat Native 消息读取需重新认证或人工检查；队列已保留")
                    self._notify_inbox_space()
                    return
                if status in {0, 408, 429} or 500 <= status < 600:
                    category = "timeout" if status in {0, 408} else "network"
                else:
                    self._inbox_stage = "error_recovery"
                    self.state.fail_inbox_handoff(message_id)
                    _runtime_log(logging.ERROR, "event=inbox_item_review")
                    self._inbox_stage = "report_error"
                    logger.error("WeChat Native 消息无法读取；标识已保留，需人工检查")
                    self._notify_inbox_space()
                    continue
                self._inbox_stage = "error_recovery"
                self.state.return_inbox_for_retry(message_id)
                _runtime_log(
                    logging.WARNING,
                    "event=inbox_retry_scheduled category=%s delay_seconds=%.0f",
                    category,
                    delay,
                )
                self._inbox_stage = "retry_wait"
                await asyncio.sleep(delay + random.uniform(0, delay * 0.2))
                delay = min(self.max_reconnect_delay, delay * 2)
            except Exception as error:
                self._record_inbox_error("inbox_item_error", error)
                category = _retry_category(error)
                self._inbox_stage = "error_recovery"
                if category in {"timeout", "network"}:
                    self.state.return_inbox_for_retry(message_id)
                    _runtime_log(
                        logging.WARNING,
                        "event=inbox_retry_scheduled category=%s delay_seconds=%.0f",
                        category,
                        delay,
                    )
                    self._inbox_stage = "retry_wait"
                    await asyncio.sleep(delay + random.uniform(0, delay * 0.2))
                    delay = min(self.max_reconnect_delay, delay * 2)
                else:
                    self.state.fail_inbox_handoff(message_id)
                    _runtime_log(logging.ERROR, "event=inbox_item_review")
                    self._inbox_stage = "report_error"
                    logger.error("WeChat Native 消息处理失败；标识已保留，需人工检查")
            finally:
                cancelling = isinstance(sys.exc_info()[1], asyncio.CancelledError)
                try:
                    self._notify_inbox_space()
                except Exception as error:
                    if not cancelling:
                        raise
                    self._record_inbox_error("inbox_item_error", error)

    async def _process_inbox_item(self, row: dict):
        cursor, message_id, thread = row["cursor"], row["message_id"], row["thread"]
        self._inbox_stage = "fetch"
        _runtime_log(logging.INFO, "event=native_message_fetch_started")
        raw = await self.client.message(message_id)
        _runtime_log(logging.INFO, "event=native_message_fetch_completed")
        self._inbox_stage = "identity"
        if (
            not isinstance(raw, dict)
            or raw.get("id") != message_id
            or raw.get("accountId") != self.native_config.account_id
            or raw.get("threadId") != thread
        ):
            self.state.fail_inbox_handoff(message_id)
            _runtime_log(logging.ERROR, "event=message_identity_mismatch")
            return

        _runtime_log(logging.INFO, "event=message_conversion_started")
        self._inbox_stage = "classify"
        kind = self._classify(raw, "live")
        self._inbox_stage = "cache_policy"
        cacheable = self.native_config.enable_group_summary and self._eligible(
            raw, "live", summary=True
        )
        if kind == "ignore":
            self._inbox_stage = "ignore_commit"
            fresh = self.state.finish_inbox_ignored(
                cursor, message_id, thread, raw.get("serverId")
            )
            if fresh and cacheable:
                self._inbox_stage = "summary_cache"
                self.summary_cache.add(raw)
            _runtime_log(logging.INFO, "event=message_conversion_completed")
            self._notify_inbox_space()
            return

        self._inbox_stage = "convert"
        converted = await self._convert_message(raw)
        _runtime_log(logging.INFO, "event=message_conversion_completed")
        server_id = raw.get("serverId")
        if kind == "media":
            self._inbox_stage = "media_prepare"
            fresh = self.state.prepare_inbox_media(cursor, message_id, thread, server_id)
            if not fresh:
                self._notify_inbox_space()
                return
            self._inbox_stage = "wrap"
            wrapped = WeChatNativeEvent(converted, self)
            self._inbox_stage = "summary_cache"
            context_sequence = self.summary_cache.add(raw) if cacheable else None
            if context_sequence is not None:
                wrapped.set_extra("wechat_native_context_sequence", context_sequence)
            self._events[message_id] = wrapped
            task = asyncio.create_task(
                self._process_media_message(raw, wrapped, inbox_cursor=cursor)
            )
            wrapped.set_extra("wechat_native_media_task", task)
            self._media_tasks.add(task)
            task.add_done_callback(self._media_tasks.discard)
            self._inbox_stage = "media"
            await task
            self._inbox_stage = "media_finish"
            if not self.state.finish_inbox_after_media(message_id):
                _runtime_log(logging.ERROR, "event=inbox_item_review")
            self._notify_inbox_space()
            return

        if kind != "text":
            self._inbox_stage = "error_recovery"
            self.state.fail_inbox_handoff(message_id)
            _runtime_log(logging.ERROR, "event=inbox_item_review")
            self._notify_inbox_space()
            return
        self._inbox_stage = "handoff_prepare"
        if not self.state.begin_inbox_handoff(cursor, message_id, thread, server_id):
            self._notify_inbox_space()
            return
        self._inbox_stage = "wrap"
        wrapped = WeChatNativeEvent(converted, self)
        self._inbox_stage = "summary_cache"
        context_sequence = self.summary_cache.add(raw) if cacheable else None
        if context_sequence is not None:
            wrapped.set_extra("wechat_native_context_sequence", context_sequence)
        try:
            self._inbox_stage = "queue_handoff"
            _runtime_log(logging.INFO, "event=queue_handoff_started")
            self.commit_event(wrapped)
        except Exception:
            self._inbox_stage = "error_recovery"
            self.state.fail_inbox_handoff(message_id)
            _runtime_log(logging.ERROR, "event=queue_handoff_failed")
            self._inbox_stage = "report_error"
            logger.error("WeChat Native 队列交付失败；消息标识已保留，需人工检查")
            raise RuntimeError("Native event handoff requires inspection") from None
        self._inbox_stage = "handoff_finish"
        self.state.finish_inbox_handoff(message_id)
        _runtime_log(logging.INFO, "event=queue_handoff_completed")
        self._inbox_stage = "event_cache"
        self._events[message_id] = wrapped
        self._notify_inbox_space()

    async def group_history(self, thread_id: str):
        cfg = self.native_config
        if (
            not isinstance(thread_id, str)
            or not thread_id.endswith("@chatroom")
            or thread_id not in cfg.allowed_groups
            or not cfg.should_fetch_incoming_thread(thread_id)
        ):
            return None
        try:
            result = await self.client.thread_messages(thread_id, limit=200)
        except NativeAPIError as error:
            logger.warning(
                "WeChat Native 持久群历史不可用：HTTP %s，错误码 %s",
                error.status,
                error.code,
            )
            return None
        items = result.get("items")
        if not isinstance(items, list) or len(items) > 200 or any(
            not isinstance(item, dict) for item in items
        ):
            logger.warning("WeChat Native 持久群历史响应结构无效")
            return None
        return items, bool(result.get("nextCursor"))

    def _check_timestamp(self, raw: dict) -> bool:
        stamp = message_timestamp(raw)
        now = time.time()
        if stamp is None or not -60 <= now - stamp <= 300:
            monotonic = time.monotonic()
            if monotonic - self._last_time_warning >= self.time_warning_interval:
                self._last_time_warning = monotonic
                delta = f"{now - stamp:.0f}" if stamp is not None else "未知"
                logger.warning(
                    "WeChat Native 实时消息因时间异常被忽略：时间差 %s 秒；"
                    "允许范围 -60 至 300 秒，请检查时钟或消息积压",
                    delta,
                )
            return False
        return True

    def _classify(self, raw: dict, origin: str) -> str:
        if (
            origin != "live"
            or raw.get("direction") != "incoming"
            or raw.get("recall") is not None
            or raw.get("senderId") == self.bot_self_id
            or not isinstance(raw.get("senderId"), str)
            or not raw["senderId"]
        ):
            return "ignore"
        cfg = self.native_config
        msg_type = raw.get("type")
        is_direct_image = msg_type == 3
        is_quote = msg_type == 49 and raw.get("appMessageType") == 57
        quote = raw.get("quote")
        is_quoted_image = is_quote and isinstance(quote, dict) and quote.get("type") == 3
        body = raw.get("body")
        has_text_body = isinstance(body, str) and bool(body.strip())
        if is_direct_image:
            if not cfg.enable_media or (body is not None and not isinstance(body, str)):
                return "ignore"
        elif is_quote or msg_type == 1:
            if not has_text_body:
                return "ignore"
        else:
            return "ignore"
        if not self._check_timestamp(raw):
            return "ignore"
        thread = raw["threadId"]
        if not thread.endswith("@chatroom"):
            if not cfg.enable_private_reply or (
                cfg.allowed_users and raw["senderId"] not in cfg.allowed_users
            ):
                return "ignore"
            if cfg.enable_media and (is_direct_image or is_quoted_image):
                return "media"
            return "text" if has_text_body else "ignore"
        if cfg.group_reply_mode == "disabled" or (
            cfg.allowed_groups and thread not in cfg.allowed_groups
        ):
            return "ignore"
        if is_direct_image:
            return "ignore"
        mentions = raw.get("mentions")
        mentioned = (
            isinstance(mentions, dict)
            and mentions.get("state") == "known"
            and mentions.get("self") is True
        )
        if mentioned and isinstance(body, str):
            stripped = re.sub(r"^(?:@[^\u2005\r\n]+\u2005[ \t]*)+", "", body)
            if is_summary_command(stripped):
                return "text"
        if cfg.enable_media and is_quoted_image:
            return "media" if mentioned else "ignore"
        if mentioned:
            return "text"
        return "ignore"

    def _eligible(self, raw: dict, origin: str, *, summary: bool = False) -> bool:
        if not summary:
            return self._classify(raw, origin) != "ignore"
        cfg = self.native_config
        if (
            origin != "live"
            or raw.get("direction") != "incoming"
            or not (
                raw.get("type") == 1 or (raw.get("type") == 49 and raw.get("appMessageType") == 57)
            )
            or raw.get("recall") is not None
            or raw.get("senderId") == self.bot_self_id
            or not isinstance(raw.get("senderId"), str)
            or not raw["senderId"]
            or not isinstance(raw.get("body"), str)
            or not raw["body"].strip()
        ):
            return False
        if not self._check_timestamp(raw):
            return False
        thread = raw["threadId"]
        if not thread.endswith("@chatroom"):
            return False
        if cfg.group_reply_mode == "disabled" or (
            cfg.allowed_groups and thread not in cfg.allowed_groups
        ):
            return False
        return cfg.enable_group_summary

    async def _process_media_message(
        self, raw: dict, wrapped: WeChatNativeEvent, *, inbox_cursor: str | None = None
    ) -> None:
        task = asyncio.current_task()
        message_id = raw["id"]
        thread = raw["threadId"]
        is_quote = raw.get("type") == 49 and raw.get("appMessageType") == 57

        def guard() -> None:
            if (
                self.stopped
                or self._state_closed
                or wrapped.is_stopped()
                or wrapped.get_extra("wechat_native_quote_invalidated")
                or self.state.message_status(message_id) != "queued"
            ):
                raise MediaError("recalled")

        try:
            async with self._media_semaphore:
                guard()
                if is_quote:
                    quote = raw.get("quote")
                    if not isinstance(quote, dict) or quote.get("type") != 3:
                        raise MediaError("unavailable")
                    state = quote.get("state")
                    if state == "recalled":
                        wrapped.set_extra("wechat_native_quote_invalidated", True)
                        wrapped.stop_event()
                        if (
                            not self._state_closed
                            and self.state.message_status(message_id) == "queued"
                        ):
                            self.state.mark_message(message_id, "no_reply")
                        return
                    if state == "unsupported":
                        raise MediaError("unsupported_format")
                    if state != "embedded" or quote.get("embeddedImage") is not True:
                        raise MediaError("expired")
                image_bytes = await self.client.fetch_image(
                    message_id, quoted=is_quote, guard=guard
                )
                guard()
                enriched = await self._convert_message(raw, image_bytes=image_bytes)
                event = WeChatNativeEvent(enriched, self)
                if inbox_cursor is not None and not self.state.begin_inbox_handoff(
                    inbox_cursor, message_id, thread, raw.get("serverId")
                ):
                    return
                try:
                    self.commit_event(event)
                except Exception:
                    if inbox_cursor is None:
                        self.state.mark_message(message_id, "needs_review")
                    else:
                        self.state.fail_inbox_handoff(message_id)
                    logger.error("WeChat Native 队列交付失败；消息标识已保留，需人工检查")
                    raise RuntimeError("Native event handoff requires inspection") from None
                if inbox_cursor is not None:
                    self.state.finish_inbox_handoff(message_id)
                self._events[message_id] = event
        except MediaError as error:
            if error.reason == "recalled":
                if not self._state_closed and self.state.message_status(message_id) == "queued":
                    if wrapped.get_extra("wechat_native_quote_invalidated"):
                        self.state.mark_message(message_id, "no_reply")
                    else:
                        self.state.recall(thread, raw.get("serverId"), message_id)
                wrapped.stop_event()
                return
            if (
                self.stopped
                or self._state_closed
                or wrapped.is_stopped()
                or wrapped.get_extra("wechat_native_quote_invalidated")
                or self.state.message_status(message_id) != "queued"
            ):
                if (
                    not self._state_closed
                    and wrapped.get_extra("wechat_native_quote_invalidated")
                    and self.state.message_status(message_id) == "queued"
                ):
                    self.state.mark_message(message_id, "no_reply")
                return
            try:
                sent = await self.send_chain(
                    thread,
                    MessageChain([Plain(error.user_message)]),
                    source=raw,
                    event=wrapped,
                )
                if (
                    not sent
                    and not self._state_closed
                    and self.state.message_status(message_id) == "queued"
                ):
                    self.state.mark_message(message_id, "no_reply")
            except (Exception, asyncio.CancelledError) as send_err:
                if not self._state_closed:
                    current = self.state.message_status(message_id)
                    if current not in {"failed", "needs_review", "partial", "recalled"}:
                        self.state.mark_message(
                            message_id,
                            "failed" if isinstance(send_err, ValueError) else "needs_review",
                        )
                if isinstance(send_err, asyncio.CancelledError):
                    raise
        except asyncio.CancelledError:
            if not self._state_closed and self.state.message_status(message_id) == "queued":
                self.state.mark_message(message_id, "needs_review")
            if self.stopped:
                raise
        except Exception:
            if not self._state_closed and self.state.message_status(message_id) == "queued":
                self.state.mark_message(message_id, "needs_review")
            logger.error("WeChat Native 图片处理失败；消息标识已保留，需人工检查")
        finally:
            self._media_tasks.discard(task)

    async def process_native_event(self, event: dict):
        if self.stopped:
            return
        if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
            raise ValueError("invalid_native_event")
        cursor = event.get("cursor")
        cursor_number(cursor)
        if cursor_number(cursor) <= cursor_number(self.state.cursor):
            _runtime_log(logging.INFO, "event=duplicate_cursor")
            return True
        event_kind = event["kind"]
        data = event.get("data")
        if event_kind in {"message.committed", "message.recalled"} and not isinstance(data, dict):
            _runtime_log(logging.ERROR, "event=message_identity_invalid")
            raise ValueError("invalid_native_event_data")
        if not isinstance(data, dict):
            data = {}
        kind_label = {
            "message.committed": "message_committed",
            "message.recalled": "message_recalled",
        }.get(event_kind, "other")
        _runtime_log(logging.INFO, "event=native_event_received kind=%s", kind_label)
        if event_kind == "message.recalled":
            thread, server_id, message_id = (
                data.get("threadId"),
                data.get("serverId"),
                data.get("messageId"),
            )
            if not isinstance(thread, str) or not thread:
                _runtime_log(logging.ERROR, "event=message_identity_invalid")
                raise ValueError("invalid_recall_identity")
            if (
                not isinstance(server_id, str)
                or not server_id
                or (message_id is not None and not isinstance(message_id, str))
            ):
                _runtime_log(logging.ERROR, "event=message_identity_invalid")
                raise ValueError("invalid_recall_identity")
            if not self.native_config.should_fetch_incoming_thread(thread):
                self.state.advance(cursor)
                _runtime_log(logging.INFO, "event=message_filtered reason=thread_policy")
                _runtime_log(logging.INFO, "event=cursor_advanced")
                return True
            self.state.recall(thread, server_id, message_id)
            self.summary_cache.recall(thread, server_id)
            for active in list(self._events.values()):
                raw = active.message_obj.raw_message
                if raw["threadId"] == thread and (raw.get("serverId") == server_id):
                    active.stop_event()
                    media_task = active.get_extra("wechat_native_media_task")
                    if media_task and not media_task.done():
                        media_task.cancel()
                elif (
                    raw["threadId"] == thread
                    and isinstance(raw.get("quote"), dict)
                    and raw["quote"].get("serverId") == server_id
                ):
                    active.set_extra("wechat_native_quote_invalidated", True)
                    active.stop_event()
                    if self.state.message_status(raw["id"]) == "queued":
                        self.state.mark_message(raw["id"], "no_reply")
                    media_task = active.get_extra("wechat_native_media_task")
                    if media_task and not media_task.done():
                        media_task.cancel()
            self.state.advance(cursor)
            _runtime_log(logging.INFO, "event=recall_processed")
            _runtime_log(logging.INFO, "event=cursor_advanced")
            self._notify_inbox_space()
            return True
        if event_kind != "message.committed":
            self.state.advance(cursor)
            _runtime_log(logging.INFO, "event=cursor_advanced")
            return True
        message_id, thread = data.get("messageId"), data.get("threadId")
        if (
            not isinstance(message_id, str)
            or not message_id
            or not isinstance(thread, str)
            or not thread
        ):
            _runtime_log(logging.ERROR, "event=message_identity_invalid")
            raise ValueError("invalid_message_identity")
        origin = data.get("origin", "unknown")
        if origin != "live":
            self.state.advance(cursor)
            _runtime_log(logging.INFO, "event=message_filtered reason=not_live")
            _runtime_log(logging.INFO, "event=cursor_advanced")
            return True
        if not self.native_config.should_fetch_incoming_thread(thread):
            self.state.advance(cursor)
            _runtime_log(logging.INFO, "event=message_filtered reason=thread_policy")
            _runtime_log(logging.INFO, "event=cursor_advanced")
            return True
        try:
            fresh = self.state.enqueue_inbox(
                cursor, message_id, thread, max_pending=_INBOX_MAX_PENDING
            )
        except InboxFullError:
            _runtime_log(logging.WARNING, "event=inbox_backpressure_started")
            return False
        _runtime_log(
            logging.INFO,
            "event=inbox_enqueue_completed fresh=%s",
            str(fresh).lower(),
        )
        if fresh:
            if not hasattr(self, "_inbox_wakeup"):
                self._inbox_wakeup = asyncio.Event()
            self._inbox_wakeup.set()
        return True

    async def run(self):
        self._run_task = asyncio.current_task()
        delay = self.reconnect_delay
        logger.info("WeChat Native 事件订阅监督器已启动")
        _runtime_log(logging.INFO, "event=supervisor_started")
        try:
            while not self.stopped:
                connected_at = None
                try:
                    await self.client.open()
                    # Recheck on every connection, including initial transient failures.
                    account = await self.client.account()
                    if account.get("id") != self.native_config.account_id:
                        raise ValueError("account_identity_mismatch")
                    status = await self.client.status()
                    self.state.bind_server(status.get("serverId"))
                    self.bot_self_id = account.get("ownUsername") or self.native_config.account_id
                    if not account.get("authenticated") or account.get("connection") != "online":
                        logger.warning("WeChat Native 账号尚未在线；等待 Native 恢复连接")
                        _runtime_log(logging.WARNING, "event=account_offline")
                    self._ensure_inbox_worker()
                    connected_at = time.monotonic()
                    _runtime_log(logging.INFO, "event=sse_connect_attempt")
                    first_event = False
                    paused_for_capacity = False
                    stream = self.client.events(self.state.cursor)
                    try:
                        async for event in stream:
                            if not first_event:
                                _runtime_log(logging.INFO, "event=sse_first_event_received")
                                first_event = True
                            accepted = await self.process_native_event(event)
                            if accepted is False:
                                paused_for_capacity = True
                                close_stream = getattr(stream, "aclose", None)
                                if close_stream is not None:
                                    await close_stream()
                                break
                    finally:
                        close_stream = getattr(stream, "aclose", None)
                        if close_stream is not None:
                            await close_stream()
                    if paused_for_capacity and not self.stopped:
                        await self._wait_for_inbox_space()
                        _runtime_log(logging.INFO, "event=inbox_backpressure_resumed")
                    elif not self.stopped:
                        logger.warning("WeChat Native SSE 已结束；保留当前游标并重连")
                        _runtime_log(logging.WARNING, "event=sse_stream_ended action=reconnect")
                except NativeAPIError as error:
                    status_code = (
                        error.status
                        if type(error.status) is int and 0 <= error.status <= 599
                        else 0
                    )
                    if error.status in {401, 403, 410}:
                        logger.error(
                            "WeChat Native 停止接收：HTTP %s (%s)；请检查认证或游标",
                            error.status,
                            error.code,
                        )
                        _runtime_log(
                            logging.ERROR,
                            "event=native_terminal_http http_status=%s",
                            status_code,
                        )
                        return
                    logger.warning(
                        "WeChat Native 暂时不可达；HTTP %s (%s)，保留当前游标并重连",
                        error.status,
                        error.code,
                    )
                    _runtime_log(
                        logging.WARNING,
                        "event=native_http_retry http_status=%s",
                        status_code,
                    )
                except ValueError:
                    logger.error("WeChat Native 校验失败；保留当前游标并重试")
                    _runtime_log(logging.ERROR, "event=validation_retry")
                except StateIdentityError:
                    logger.error("WeChat Native 服务身份改变；保留状态并停止，需人工检查")
                    _runtime_log(logging.ERROR, "event=server_identity_changed terminal=true")
                    return
                except Exception as error:
                    logger.warning(
                        "WeChat Native 适配器遇到瞬时异常；类别 %s，保留当前游标并重连",
                        _retry_category(error),
                    )
                    _runtime_log(
                        logging.WARNING,
                        "event=unexpected_retry category=%s",
                        _retry_category(error),
                    )
                if not self.stopped:
                    if (
                        connected_at is not None
                        and time.monotonic() - connected_at >= self.stable_connection_interval
                    ):
                        delay = self.reconnect_delay
                    retry_delay = delay
                    _runtime_log(
                        logging.INFO,
                        "event=reconnect_scheduled delay_seconds=%.0f",
                        retry_delay,
                    )
                    await asyncio.sleep(retry_delay + random.uniform(0, retry_delay * 0.2))
                    delay = min(self.max_reconnect_delay, retry_delay * 2)
        except asyncio.CancelledError:
            if not self.stopped:
                raise
        finally:
            inbox_task = getattr(self, "_inbox_task", None)
            if inbox_task is not None and inbox_task is not asyncio.current_task():
                inbox_task.cancel()
                await asyncio.gather(inbox_task, return_exceptions=True)
                self._inbox_task = None
            try:
                await self.client.close()
            except Exception as error:
                logger.warning(
                    "WeChat Native 关闭连接时出现异常；类别 %s",
                    _retry_category(error),
                )
                _runtime_log(
                    logging.WARNING,
                    "event=client_close_error category=%s",
                    _retry_category(error),
                )
            finally:
                if self._run_task is asyncio.current_task():
                    self._run_task = None

    async def send_chain(self, thread: str, chain, *, source=None, event=None) -> bool:
        if self.stopped:
            raise RuntimeError("WeChat Native 已停止")
        task = asyncio.current_task()
        self._send_tasks.add(task)
        try:
            async with self._send_lock:
                if self.stopped:
                    raise RuntimeError("WeChat Native 已停止")
                if event and event.get_extra("wechat_native_quote_invalidated"):
                    event.stop_event()
                    return False
                text, mentions, reply_id = serialize_text(chain, group=thread.endswith("@chatroom"))
                if not text.strip():
                    return False
                if len(text.encode()) > 65536:
                    raise ValueError("Native 文本超过 64 KiB，未发送")
                if reply_id is not None and len(text.encode()) > 16384:
                    raise ValueError("Native 引用回复文本超过 16 KiB，未发送")
                if self.client.closed:
                    raise ValueError("Native 客户端未连接，未提交发送")
                message_id = source["id"] if source else None
                auto_mention = False
                if source:
                    source_status = self.state.message_status(message_id)
                    if source_status in {"needs_review", "failed", "partial"}:
                        raise RuntimeError("此消息的回复需人工检查，禁止自动重发")
                    if source_status == "recalled":
                        if event:
                            event.stop_event()
                        return False
                    if (
                        thread.endswith("@chatroom")
                        and mentions is None
                        and source_status != "replied"
                    ):
                        mentions = {"userIds": [source["senderId"]]}
                        auto_mention = True
                if mentions:
                    try:
                        await self.client.prepare_group_mentions(
                            thread, [self.bot_self_id, *mentions.get("userIds", [])]
                        )
                    except GroupMembersUnavailable as error:
                        if not auto_mention or error.code in {
                            "auth_required",
                            "account_identity_mismatch",
                        }:
                            raise
                        logger.warning("WeChat Native 省略自动 @，继续发送回复正文；%s", error)
                        mentions = None
                if source:
                    current = await self.client.message(message_id)
                    if (
                        current.get("id") != message_id
                        or current.get("accountId") != self.native_config.account_id
                        or current.get("threadId") != thread
                        or current.get("serverId") != source.get("serverId")
                    ):
                        raise ValueError("reply_source_identity_mismatch")
                    if self.state.message_status(message_id) in {
                        "needs_review",
                        "failed",
                        "partial",
                    }:
                        raise RuntimeError("此消息的回复需人工检查，禁止自动重发")
                    if (
                        current.get("recall") is not None
                        or self.state.message_status(message_id) == "recalled"
                    ):
                        self.state.recall(thread, source.get("serverId"), message_id)
                        if event:
                            event.stop_event()
                        return False
                    original_quote = source.get("quote")
                    current_quote = current.get("quote")
                    if isinstance(original_quote, dict) and (
                        not isinstance(current_quote, dict)
                        or current_quote.get("serverId") != original_quote.get("serverId")
                        or (
                            original_quote.get("state") != "recalled"
                            and current_quote.get("state") == "recalled"
                        )
                        or (event and event.get_extra("wechat_native_quote_invalidated"))
                    ):
                        if source_status == "queued":
                            self.state.mark_message(message_id, "no_reply")
                        if event:
                            event.stop_event()
                        return False
                quote = None
                if reply_id is not None:
                    target = (
                        current
                        if source and reply_id == message_id
                        else await self.client.message(reply_id)
                    )
                    if (
                        target.get("id") != reply_id
                        or target.get("accountId") != self.native_config.account_id
                        or target.get("threadId") != thread
                        or not isinstance(target.get("serverId"), str)
                        or not target["serverId"]
                        or target.get("recall") is not None
                        or self.state.message_status(reply_id) == "recalled"
                    ):
                        raise ValueError("Native 引用来源不可用、已撤回或不属于当前账号和聊天")
                    quote = {"messageId": reply_id, "serverId": target["serverId"]}
                    # Another target read may overlap source recall or send failure.
                    if source and self.state.message_status(message_id) == "recalled":
                        if event:
                            event.stop_event()
                        return False
                    if source and self.state.message_status(message_id) in {
                        "needs_review",
                        "failed",
                        "partial",
                    }:
                        raise RuntimeError("此消息的回复需人工检查，禁止自动重发")
                records = event.get_extra("wechat_native_summary_records") if event else None
                if event and event.get_extra("wechat_native_quote_invalidated"):
                    event.stop_event()
                    return False
                if records is not None and not self.summary_cache.contains(records):
                    self.state.mark_message(message_id, "no_reply")
                    event.stop_event()
                    return False
                key = "astrbot-" + uuid.uuid4().hex
                self.state.begin_send(key, message_id, thread)
                try:
                    receipt = await self.client.send_text(thread, text, key, mentions, quote=quote)
                except BaseException as error:
                    state = "needs_review"
                    if isinstance(error, SendError) and error.state in {"failed", "cancelled"}:
                        state = error.state
                    elif isinstance(error, NativeAPIError) and 400 <= error.status < 500:
                        state = "failed"
                    send_id = error.send_id if isinstance(error, SendError) else None
                    self.state.finish_send(key, state, send_id)
                    if message_id:
                        self.state.mark_message(
                            message_id, "failed" if state == "cancelled" else state
                        )
                    logger.error(
                        "WeChat Native 回复未确认；状态 %s，回执键 %s。请检查状态库，勿盲目重发",
                        state,
                        key,
                    )
                    raise
                self.state.finish_send(key, "accepted", receipt["id"])
                if message_id:
                    self.state.mark_message(message_id, "replied")
                return True
        finally:
            self._send_tasks.discard(task)

    async def send_by_session(self, session, message_chain):
        thread = session_target(session.session_id, self.native_config.account_id)
        if session.platform_id != self.meta().id:
            raise ValueError("会话不属于当前机器人实例")
        if await self.send_chain(thread, message_chain):
            await super().send_by_session(session, message_chain)

    async def terminate(self):
        async with self._terminate_lock:
            await self._terminate()

    async def _terminate(self):
        if self._state_closed:
            return
        self.stopped = True
        current = asyncio.current_task()
        tasks = {task for task in (self._send_tasks | self._media_tasks) if task is not current}
        if self._run_task and self._run_task is not current:
            tasks.add(self._run_task)
        if self._inbox_task and self._inbox_task is not current:
            tasks.add(self._inbox_task)
        if hasattr(self, "_inbox_wakeup"):
            self._inbox_wakeup.set()
        if hasattr(self, "_inbox_space"):
            self._inbox_space.set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for event in list(self._events.values()):
            event.stop_event()
        self.summary_cache.clear()
        if hasattr(self, "_conversion_executor"):
            self._conversion_executor.shutdown(wait=False, cancel_futures=True)
        await self.client.close()
        self.state.close()
        self._state_closed = True
