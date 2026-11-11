import asyncio
import importlib.util
import logging
import sys
import types
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "vendor/astrbot_plugin_wechat_native"


def load_adapter_class(*, suppress_runtime_log=True):
    package_name = "native_adapter_supervision_fixture"
    package = types.ModuleType(package_name)
    package.__path__ = [str(PLUGIN)]

    config = types.ModuleType(package_name + ".config")
    config.ADAPTER_ID = "wechat_native"
    config.CONFIG_METADATA = {}
    config.DEFAULT_CONFIG = {}
    config.MAX_CONCURRENT_MEDIA_TASKS = 2
    config.PLUGIN_ID = "astrbot_plugin_wechat_native"
    config.NativeConfig = type("NativeConfig", (), {})

    event = types.ModuleType(package_name + ".event")

    class WeChatNativeEvent:
        def __init__(self, message, platform):
            self.message_obj = types.SimpleNamespace(raw_message=message)
            self.platform = platform
            self.extras = {}

        def set_extra(self, key, value):
            self.extras[key] = value

        def get_extra(self, key):
            return self.extras.get(key)

        def is_stopped(self):
            return False

        def stop_event(self):
            return None

    event.WeChatNativeEvent = WeChatNativeEvent
    event.convert_message = lambda *args, **kwargs: None
    event.message_timestamp = lambda raw: None
    event.serialize_text = lambda *args, **kwargs: ("", None, None)
    event.session_target = lambda *args, **kwargs: ""

    native_client = types.ModuleType(package_name + ".native_client")

    class NativeAPIError(Exception):
        def __init__(self, status, code="request_failed"):
            self.status = status
            self.code = code
            super().__init__(f"Native API HTTP {status}: {code}")

    native_client.NativeAPIError = NativeAPIError
    native_client.NativeClient = type("NativeClient", (), {})
    native_client.GroupMembersUnavailable = type("GroupMembersUnavailable", (Exception,), {})
    native_client.MediaError = type("MediaError", (Exception,), {})
    native_client.SendError = type("SendError", (Exception,), {})

    state = types.ModuleType(package_name + ".state")
    state.StateStore = type("StateStore", (), {})
    state.InboxFullError = type("InboxFullError", (Exception,), {})
    state.StateIdentityError = type("StateIdentityError", (RuntimeError,), {})
    state.cursor_number = lambda value: int(value)

    summary = types.ModuleType(package_name + ".summary")
    summary.GroupSummaryCache = type("GroupSummaryCache", (), {})
    summary.is_summary_command = lambda value: False

    astrbot = types.ModuleType("astrbot")
    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    astrbot_event = types.ModuleType("astrbot.api.event")
    astrbot_event.MessageChain = type("MessageChain", (), {})
    astrbot_components = types.ModuleType("astrbot.api.message_components")
    astrbot_components.Plain = type("Plain", (), {})
    astrbot_platform = types.ModuleType("astrbot.api.platform")

    class Platform:
        def __init__(self, *args, **kwargs):
            pass

    astrbot_platform.Platform = Platform
    astrbot_platform.PlatformMetadata = type("PlatformMetadata", (), {})
    astrbot_platform.register_platform_adapter = lambda *a, **k: lambda cls: cls
    astrbot_star = types.ModuleType("astrbot.api.star")
    astrbot_star.StarTools = types.SimpleNamespace(get_data_dir=lambda _: ROOT)
    modules = {
        package_name: package,
        package_name + ".config": config,
        package_name + ".event": event,
        package_name + ".native_client": native_client,
        package_name + ".state": state,
        package_name + ".summary": summary,
        "astrbot": astrbot,
        "astrbot.api": astrbot_api,
        "astrbot.api.event": astrbot_event,
        "astrbot.api.message_components": astrbot_components,
        "astrbot.api.platform": astrbot_platform,
        "astrbot.api.star": astrbot_star,
    }
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            package_name + ".adapter", PLUGIN / "adapter.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    if suppress_runtime_log:
        module._runtime_log = lambda *args, **kwargs: None
    return module, native_client.NativeAPIError, state.InboxFullError


class FakeState:
    def __init__(self):
        self.cursor = "23418"
        self.bound_servers = []
        self.enqueued = []
        self.full = False
        self.inbox_full_error = RuntimeError
        self.message_statuses = {}
        self.claim_rows = []
        self.retry_calls = []
        self.review_calls = []
        self.recovery_calls = 0

    def bind_server(self, server_id):
        self.bound_servers.append(server_id)

    def enqueue_inbox(self, cursor, message_id, thread, *, max_pending):
        if self.full:
            raise self.inbox_full_error("full")
        self.enqueued.append((cursor, message_id, thread, max_pending))
        self.cursor = cursor
        return True

    def advance(self, cursor):
        self.cursor = cursor

    def claim_inbox(self):
        return self.claim_rows.pop(0) if self.claim_rows else None

    def recover_inflight(self):
        self.recovery_calls += 1
        return {"pending": 0, "needs_review": 0}

    def pending_inbox_count(self):
        return 0

    def begin_inbox_handoff(self, cursor, message_id, thread, server_id):
        self.message_statuses[message_id] = "queued"
        return True

    def finish_inbox_handoff(self, message_id):
        return True

    def return_inbox_for_retry(self, message_id):
        self.retry_calls.append(message_id)
        self.claim_rows.append({
            "cursor": "23419", "message_id": message_id, "thread": "group@chatroom"
        })
        return True

    def fail_inbox_handoff(self, message_id):
        self.review_calls.append(message_id)
        return True


class FakeDiagnostics:
    def __init__(self):
        self.records = []

    def record(self, event, **fields):
        self.records.append((event, fields))


class FakeNativeClient:
    def __init__(self, api_error):
        self.api_error = api_error
        self.events_cursors = []
        self.open_calls = 0
        self.close_calls = 0
        self.message_calls = 0
        self.closed_streams = 0
        self.event_payloads = None

    async def open(self):
        self.open_calls += 1

    async def close(self):
        self.close_calls += 1

    async def account(self):
        return {"id": "account", "authenticated": True, "connection": "online"}

    async def status(self):
        return {"state": "ready", "serverId": "server"}

    async def message(self, message_id):
        self.message_calls += 1
        if hasattr(self, "message_sequence"):
            value = self.message_sequence.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value
        if hasattr(self, "message_result"):
            return self.message_result
        raise AssertionError("the SSE reader must not fetch message bodies")

    def events(self, after, on_connect=None):
        self.events_cursors.append(after)
        attempt = len(self.events_cursors)

        async def stream():
            try:
                if attempt == 1:
                    raise self.api_error
                if on_connect is not None:
                    on_connect()
                payloads = self.event_payloads or [
                    {"kind": "test", "cursor": "23419", "data": {}}
                ]
                for payload in payloads:
                    yield payload
            finally:
                self.closed_streams += 1

        return stream()


class NativeRunSupervisionTests(unittest.TestCase):
    def test_retries_api_error_without_terminating_or_advancing_cursor(self):
        module, NativeAPIError, _ = load_adapter_class()
        Adapter = module.WeChatNativeAdapter
        adapter = Adapter.__new__(Adapter)
        adapter._run_task = None
        adapter.stopped = False
        adapter._state_closed = False
        adapter.native_config = types.SimpleNamespace(account_id="account")
        adapter.state = FakeState()
        adapter.client = FakeNativeClient(NativeAPIError(503, "request_failed"))
        processed = []

        async def process_event(event):
            processed.append(event["kind"])
            adapter.state.cursor = event["cursor"]
            adapter.stopped = True

        adapter.process_native_event = process_event

        async def no_wait(_seconds):
            return None

        async def run():
            with patch("asyncio.sleep", no_wait):
                await adapter.run()

        asyncio.run(run())

        self.assertEqual(adapter.client.events_cursors, ["23418", "23418"])
        self.assertEqual(processed, ["test"])
        self.assertEqual(adapter.state.cursor, "23419")
        self.assertEqual(adapter.client.close_calls, 1)

    def test_committed_message_is_durably_enqueued_without_body_fetch(self):
        module, NativeAPIError, _ = load_adapter_class()
        adapter = module.WeChatNativeAdapter.__new__(module.WeChatNativeAdapter)
        adapter.stopped = False
        adapter.state = FakeState()
        adapter.native_config = types.SimpleNamespace(
            account_id="account", should_fetch_incoming_thread=lambda thread: True
        )
        adapter.client = FakeNativeClient(NativeAPIError(503))
        adapter._inbox_wakeup = asyncio.Event()

        accepted = asyncio.run(adapter.process_native_event({
            "kind": "message.committed",
            "cursor": "23419",
            "data": {
                "messageId": "message-1",
                "threadId": "group@chatroom",
                "origin": "live",
            },
        }))

        self.assertTrue(accepted)
        self.assertEqual(adapter.state.enqueued, [("23419", "message-1", "group@chatroom", 512)])
        self.assertEqual(adapter.client.message_calls, 0)
        self.assertTrue(adapter._inbox_wakeup.is_set())

    def test_all_group_mode_still_requires_real_platform_mention(self):
        module, _, _ = load_adapter_class()
        adapter = module.WeChatNativeAdapter.__new__(module.WeChatNativeAdapter)
        adapter.bot_self_id = "bot"
        adapter.native_config = types.SimpleNamespace(
            group_reply_mode="all",
            allowed_groups={"group@chatroom"},
            enable_media=False,
            enable_private_reply=False,
            allowed_users=set(),
        )
        adapter._check_timestamp = lambda raw: True
        raw = {
            "id": "message-1",
            "threadId": "group@chatroom",
            "senderId": "member",
            "direction": "incoming",
            "type": 1,
            "body": "普通群消息",
            "mentions": {"state": "known", "self": False},
        }

        self.assertEqual(adapter._classify(raw, "live"), "ignore")
        raw["mentions"] = {"state": "known", "self": True}
        self.assertEqual(adapter._classify(raw, "live"), "text")

    def test_full_inbox_does_not_advance_cursor_or_fetch_body(self):
        module, NativeAPIError, InboxFullError = load_adapter_class()
        adapter = module.WeChatNativeAdapter.__new__(module.WeChatNativeAdapter)
        adapter.stopped = False
        adapter.state = FakeState()
        adapter.state.full = True
        adapter.state.inbox_full_error = InboxFullError
        adapter.native_config = types.SimpleNamespace(
            account_id="account", should_fetch_incoming_thread=lambda thread: True
        )
        adapter.client = FakeNativeClient(NativeAPIError(503))
        adapter._inbox_wakeup = asyncio.Event()

        accepted = asyncio.run(adapter.process_native_event({
            "kind": "message.committed",
            "cursor": "23419",
            "data": {
                "messageId": "message-1",
                "threadId": "group@chatroom",
                "origin": "live",
            },
        }))

        self.assertFalse(accepted)
        self.assertEqual(adapter.state.cursor, "23418")
        self.assertEqual(adapter.client.message_calls, 0)

    def test_malformed_committed_event_does_not_advance_cursor(self):
        module, NativeAPIError, _ = load_adapter_class()
        adapter = module.WeChatNativeAdapter.__new__(module.WeChatNativeAdapter)
        adapter.stopped = False
        adapter.state = FakeState()
        adapter.native_config = types.SimpleNamespace(
            account_id="account", should_fetch_incoming_thread=lambda thread: True
        )
        adapter.client = FakeNativeClient(NativeAPIError(503))

        with self.assertRaises(ValueError):
            asyncio.run(adapter.process_native_event({
                "kind": "message.committed", "cursor": "23419", "data": None
            }))

        self.assertEqual(adapter.state.cursor, "23418")
        self.assertEqual(adapter.client.message_calls, 0)

    def test_single_worker_fetches_then_hands_off_without_storing_body(self):
        module, NativeAPIError, _ = load_adapter_class()
        adapter = module.WeChatNativeAdapter.__new__(module.WeChatNativeAdapter)
        adapter.stopped = False
        adapter.native_config = types.SimpleNamespace(
            account_id="account", enable_group_summary=False
        )
        adapter.bot_self_id = "bot"
        adapter.state = FakeState()
        adapter.client = FakeNativeClient(NativeAPIError(503))
        adapter.client.message_result = {
            "id": "message-1",
            "accountId": "account",
            "threadId": "group@chatroom",
            "serverId": "server-1",
            "body": "private body must not be stored",
        }
        adapter._classify = lambda raw, origin: "text"
        adapter._convert_message = lambda raw, **kwargs: asyncio.sleep(
            0, result={"converted": True}
        )
        adapter._events = weakref.WeakValueDictionary()
        adapter._inbox_space = asyncio.Event()
        handed_off = []
        adapter.commit_event = handed_off.append

        asyncio.run(adapter._process_inbox_item({
            "cursor": "23419",
            "message_id": "message-1",
            "thread": "group@chatroom",
        }))

        self.assertEqual(adapter.client.message_calls, 1)
        self.assertEqual(adapter.state.message_statuses, {"message-1": "queued"})
        self.assertEqual(len(handed_off), 1)
        self.assertNotIn("body", vars(adapter.state))

    def test_backpressure_closes_stream_without_advancing_cursor(self):
        module, NativeAPIError, InboxFullError = load_adapter_class()
        adapter = module.WeChatNativeAdapter.__new__(module.WeChatNativeAdapter)
        adapter._run_task = None
        adapter.stopped = False
        adapter._state_closed = False
        adapter.native_config = types.SimpleNamespace(
            account_id="account", should_fetch_incoming_thread=lambda thread: True
        )
        adapter.state = FakeState()
        adapter.state.full = True
        adapter.state.inbox_full_error = InboxFullError
        adapter.client = FakeNativeClient(NativeAPIError(503))
        adapter.client.event_payloads = [{
            "kind": "message.committed",
            "cursor": "23419",
            "data": {
                "messageId": "message-1",
                "threadId": "group@chatroom",
                "origin": "live",
            },
        }]

        async def stop_after_pause():
            adapter.stopped = True

        adapter._wait_for_inbox_space = stop_after_pause

        async def run():
            async def no_wait(_seconds):
                return None

            with patch("asyncio.sleep", no_wait):
                await adapter.run()

        asyncio.run(run())

        self.assertEqual(adapter.state.cursor, "23418")
        self.assertEqual(adapter.client.message_calls, 0)
        self.assertEqual(adapter.client.closed_streams, 2)

    def test_worker_retries_transient_fetch_error_from_same_inbox_row(self):
        module, NativeAPIError, _ = load_adapter_class()
        adapter = module.WeChatNativeAdapter.__new__(module.WeChatNativeAdapter)
        adapter.stopped = False
        adapter.state = FakeState()
        row = {"cursor": "23419", "message_id": "message-1", "thread": "group@chatroom"}
        adapter.state.claim_rows.append(row)
        adapter.client = FakeNativeClient(NativeAPIError(503))
        adapter.client.message_sequence = [
            NativeAPIError(503, "request_failed"), {"retried": True}
        ]
        adapter._inbox_wakeup = asyncio.Event()
        adapter._inbox_space = asyncio.Event()
        adapter._inbox_space.set()
        processed = []

        async def process(item):
            value = await adapter.client.message(item["message_id"])
            processed.append(value)
            adapter.stopped = True

        adapter._process_inbox_item = process

        async def no_wait(_seconds):
            return None

        async def run():
            with patch("asyncio.sleep", no_wait):
                await adapter._run_inbox_worker()

        asyncio.run(run())

        self.assertEqual(adapter.client.message_calls, 2)
        self.assertEqual(adapter.state.retry_calls, ["message-1"])
        self.assertEqual(processed, [{"retried": True}])

    def test_terminal_fetch_error_preserves_row_and_stops_reader(self):
        module, NativeAPIError, _ = load_adapter_class()
        adapter = module.WeChatNativeAdapter.__new__(module.WeChatNativeAdapter)
        adapter.stopped = False
        adapter.state = FakeState()
        adapter.state.claim_rows.append({
            "cursor": "23419", "message_id": "message-1", "thread": "group@chatroom"
        })
        adapter.client = FakeNativeClient(NativeAPIError(401))
        adapter.client.message_sequence = [NativeAPIError(401, "unauthorized")]
        adapter._inbox_wakeup = asyncio.Event()
        adapter._inbox_space = asyncio.Event()
        adapter._inbox_space.set()
        adapter._process_inbox_item = adapter.client.message

        async def run():
            await adapter._run_inbox_worker()

        asyncio.run(run())

        self.assertTrue(adapter.stopped)
        self.assertEqual(adapter.state.review_calls, ["message-1"])

    def test_worker_supervisor_recovers_after_unexpected_exit(self):
        module, _, _ = load_adapter_class()
        adapter = module.WeChatNativeAdapter.__new__(module.WeChatNativeAdapter)
        adapter.stopped = False
        adapter.state = FakeState()
        attempts = []
        delays = []
        records = []
        module._runtime_log = lambda level, message, *args: records.append(
            module._safe_runtime_event(message, args)
        )

        async def worker():
            attempts.append(True)
            if len(attempts) == 1:
                adapter._inbox_stage = "classify"
                raise AttributeError("synthetic-private-value")
            adapter.stopped = True

        async def no_wait(seconds):
            delays.append(seconds)

        adapter._run_inbox_worker = worker

        async def run():
            with patch("asyncio.sleep", no_wait), patch.object(module.random, "uniform", return_value=0):
                adapter._ensure_inbox_worker()
                await asyncio.wait_for(adapter._inbox_task, 1)

        asyncio.run(run())

        self.assertEqual(len(attempts), 2)
        self.assertEqual(adapter.state.recovery_calls, 2)
        self.assertEqual(adapter.state.cursor, "23418")
        self.assertEqual(delays, [1.0])
        self.assertIn("event=inbox_worker_error stage=classify error=attribute", records)
        self.assertNotIn("synthetic-private-value", " ".join(records))

    def test_worker_supervisor_restarts_unexpected_normal_return(self):
        module, _, _ = load_adapter_class()
        adapter = module.WeChatNativeAdapter.__new__(module.WeChatNativeAdapter)
        adapter.stopped = False
        adapter.state = FakeState()
        attempts = []

        async def worker():
            attempts.append(True)
            if len(attempts) == 2:
                adapter.stopped = True

        async def no_wait(seconds):
            return None

        adapter._run_inbox_worker = worker

        async def run():
            with patch("asyncio.sleep", no_wait):
                adapter._ensure_inbox_worker()
                await asyncio.wait_for(adapter._inbox_task, 1)

        asyncio.run(run())

        self.assertEqual(len(attempts), 2)
        self.assertEqual(adapter.state.recovery_calls, 2)

    def test_worker_supervisor_does_not_restart_after_cancellation(self):
        module, _, _ = load_adapter_class()
        adapter = module.WeChatNativeAdapter.__new__(module.WeChatNativeAdapter)
        adapter.stopped = False
        adapter.state = FakeState()
        attempts = []

        async def run():
            started = asyncio.Event()

            async def worker():
                attempts.append(True)
                started.set()
                await asyncio.Event().wait()

            adapter._run_inbox_worker = worker
            adapter._ensure_inbox_worker()
            await asyncio.wait_for(started.wait(), 1)
            adapter._inbox_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await adapter._inbox_task

        asyncio.run(run())

        self.assertEqual(len(attempts), 1)
        self.assertEqual(adapter.state.recovery_calls, 1)

    def test_processing_failure_logs_only_fixed_stage_and_error_class(self):
        module, _, _ = load_adapter_class()
        adapter = module.WeChatNativeAdapter.__new__(module.WeChatNativeAdapter)
        adapter.stopped = False
        adapter.state = FakeState()
        adapter.state.claim_rows = [
            {"cursor": "23419", "message_id": "message-1", "thread": "group@chatroom"},
            {"cursor": "23420", "message_id": "message-2", "thread": "group@chatroom"},
        ]
        records = []
        module._runtime_log = lambda level, message, *args: records.append(
            module._safe_runtime_event(message, args)
        )

        async def process(item):
            adapter._inbox_stage = "classify"
            if item["message_id"] == "message-1":
                raise AttributeError("synthetic-private-value")
            adapter.stopped = True

        adapter._process_inbox_item = process
        asyncio.run(adapter._run_inbox_worker())

        self.assertEqual(adapter.state.review_calls, ["message-1"])
        self.assertIn("event=inbox_item_error stage=classify error=attribute", records)
        self.assertNotIn("synthetic-private-value", " ".join(records))

    def test_worker_diagnostic_schema_rejects_unlisted_values(self):
        module, _, _ = load_adapter_class()
        message = "event=inbox_worker_error stage=%s error=%s"
        self.assertEqual(
            module._safe_runtime_event(message, ("claim", "database")),
            "event=inbox_worker_error stage=claim error=database",
        )
        self.assertEqual(
            module._safe_runtime_event(message, ("synthetic-private-value", "database")),
            "event=diagnostic_invalid",
        )
        self.assertEqual(
            module._safe_runtime_event(message, ("claim", "synthetic-private-value")),
            "event=diagnostic_invalid",
        )

    def test_runtime_log_fallback_cannot_raise_into_worker(self):
        module, _, _ = load_adapter_class(suppress_runtime_log=False)
        with patch.object(module.StarTools, "get_data_dir", side_effect=OSError("unavailable")), patch.object(
            module.logger, "warning", side_effect=RuntimeError("logger unavailable")
        ):
            module._runtime_log(logging.ERROR, "event=inbox_item_review")

    def test_worker_cancellation_survives_recovery_failure(self):
        module, _, _ = load_adapter_class()
        adapter = module.WeChatNativeAdapter.__new__(module.WeChatNativeAdapter)
        adapter.stopped = False
        adapter.state = FakeState()
        adapter.state.claim_rows = [{
            "cursor": "23419", "message_id": "message-1", "thread": "group@chatroom"
        }]

        def unavailable(message_id):
            raise RuntimeError("synthetic-private-value")

        async def cancelled(item):
            raise asyncio.CancelledError

        adapter.state.recover_inbox_item = unavailable
        adapter._process_inbox_item = cancelled
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(adapter._run_inbox_worker())


if __name__ == "__main__":
    unittest.main()
