import sqlite3
import tempfile
import unittest
from pathlib import Path

from vendor.astrbot_plugin_wechat_native.state import (
    InboxFullError,
    StateIdentityError,
    StateStore,
)


class NativeInboxStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = StateStore(self.path)
        self.store.advance("23418")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def enqueue(self, cursor, message_id, thread="group@chatroom", limit=8):
        return self.store.enqueue_inbox(
            cursor, message_id, thread, max_pending=limit
        )

    def test_enqueue_and_checkpoint_are_atomic_and_inbox_is_id_only(self):
        self.assertTrue(self.enqueue("23419", "message-1"))
        self.assertEqual(self.store.cursor, "23419")
        columns = [row[1] for row in self.store.db.execute("PRAGMA table_info(inbox)")]
        self.assertEqual(columns, ["cursor", "message_id", "thread", "status"])
        self.assertEqual(self.store.pending_inbox_count(), 1)

    def test_full_inbox_does_not_advance_checkpoint(self):
        self.enqueue("23419", "message-1", limit=1)
        with self.assertRaises(InboxFullError):
            self.enqueue("23420", "message-2", limit=1)
        self.assertEqual(self.store.cursor, "23419")
        self.assertEqual(self.store.pending_inbox_count(), 1)
        item = self.store.claim_inbox()
        self.assertTrue(
            self.store.begin_inbox_handoff(
                item["cursor"], item["message_id"], item["thread"], "server-1"
            )
        )
        self.assertTrue(self.store.finish_inbox_handoff("message-1"))
        self.assertTrue(self.enqueue("23420", "message-2", limit=1))
        self.assertEqual(self.store.cursor, "23420")

    def test_enqueue_failure_rolls_back_inbox_and_checkpoint_together(self):
        self.store.db.execute(
            "CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON checkpoint "
            "BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.enqueue("23419", "message-1")
        self.assertEqual(self.store.cursor, "23418")
        self.assertEqual(self.store.pending_inbox_count(), 0)

    def test_duplicate_message_is_idempotent_but_advances_checkpoint(self):
        self.assertTrue(self.enqueue("23419", "message-1"))
        self.assertFalse(self.enqueue("23420", "message-1"))
        self.assertEqual(self.store.cursor, "23420")
        self.assertEqual(self.store.pending_inbox_count(), 1)

    def test_unknown_event_behind_checkpoint_is_not_added(self):
        self.store.advance("23420")
        with self.assertRaises(StateIdentityError):
            self.enqueue("23419", "message-1")
        self.assertEqual(self.store.cursor, "23420")
        self.assertEqual(self.store.pending_inbox_count(), 0)

    def test_identity_conflict_is_retained_for_review_without_advancing(self):
        self.enqueue("23419", "message-1")
        with self.assertRaises(StateIdentityError):
            self.enqueue("23420", "message-1", thread="other@chatroom")
        self.assertEqual(self.store.cursor, "23419")
        self.assertEqual(self.store.inbox_status("message-1"), "needs_review")

    def test_processing_recovers_but_ambiguous_handoff_requires_review(self):
        self.enqueue("23419", "message-1")
        self.enqueue("23420", "message-2")
        first = self.store.claim_inbox()
        self.assertEqual(first["message_id"], "message-1")
        self.store.close()
        self.store = StateStore(self.path)
        self.assertEqual(self.store.inbox_status("message-1"), "pending")
        first = self.store.claim_inbox()
        self.assertTrue(
            self.store.begin_inbox_handoff(
                first["cursor"], first["message_id"], first["thread"], "server-1"
            )
        )
        self.store.close()
        self.store = StateStore(self.path)
        self.assertEqual(self.store.inbox_status("message-1"), "needs_review")
        self.assertEqual(self.store.message_status("message-1"), "needs_review")
        self.assertEqual(self.store.cursor, "23420")

    def test_recall_cancels_pending_message_before_handoff(self):
        self.enqueue("23419", "message-1")
        self.store.recall("group@chatroom", "server-1", "message-1")
        self.assertIsNone(self.store.inbox_status("message-1"))
        self.assertEqual(self.store.message_status("message-1"), "recalled")

    def test_recall_seen_before_message_fetch_prevents_handoff(self):
        self.enqueue("23419", "message-1")
        item = self.store.claim_inbox()
        self.store.recall("group@chatroom", "server-1", None)
        self.assertFalse(
            self.store.begin_inbox_handoff(
                item["cursor"], item["message_id"], item["thread"], "server-1"
            )
        )
        self.assertEqual(self.store.message_status("message-1"), "recalled")
        self.assertIsNone(self.store.inbox_status("message-1"))

    def test_media_fetch_state_is_not_replayed_after_interrupted_processing(self):
        self.enqueue("23419", "message-1")
        item = self.store.claim_inbox()
        self.assertTrue(
            self.store.prepare_inbox_media(
                item["cursor"], item["message_id"], item["thread"], "server-1"
            )
        )
        self.assertEqual(self.store.message_status("message-1"), "queued")
        self.assertEqual(self.store.recover_inbox_item("message-1"), "needs_review")
        self.assertEqual(self.store.message_status("message-1"), "needs_review")

    def test_unexpected_worker_exit_recovers_pending_and_ambiguous_rows(self):
        self.enqueue("23419", "message-1")
        self.enqueue("23420", "message-2")
        pending = self.store.claim_inbox()
        ambiguous = self.store.claim_inbox()
        self.assertEqual(pending["message_id"], "message-1")
        self.assertEqual(ambiguous["message_id"], "message-2")
        self.assertTrue(
            self.store.prepare_inbox_media(
                ambiguous["cursor"], ambiguous["message_id"], ambiguous["thread"], "server-2"
            )
        )
        self.assertTrue(
            self.store.begin_inbox_handoff(
                ambiguous["cursor"], ambiguous["message_id"], ambiguous["thread"], "server-2"
            )
        )

        recovered = self.store.recover_inflight()

        self.assertEqual(recovered, {"pending": 1, "needs_review": 1})
        self.assertEqual(self.store.inbox_status("message-1"), "pending")
        self.assertEqual(self.store.inbox_status("message-2"), "needs_review")
        self.assertEqual(self.store.message_status("message-2"), "needs_review")

    def test_handoff_completion_removes_inbox_and_duplicate_does_not_requeue(self):
        self.enqueue("23419", "message-1")
        item = self.store.claim_inbox()
        self.assertTrue(
            self.store.begin_inbox_handoff(
                item["cursor"], item["message_id"], item["thread"], "server-1"
            )
        )
        self.assertTrue(self.store.finish_inbox_handoff("message-1"))
        self.assertFalse(self.enqueue("23420", "message-1"))
        self.assertEqual(self.store.message_status("message-1"), "queued")
        self.assertEqual(self.store.pending_inbox_count(), 0)

    def test_worker_recovery_does_not_change_already_handed_off_messages(self):
        self.enqueue("23419", "message-1")
        first = self.store.claim_inbox()
        self.assertTrue(self.store.begin_inbox_handoff(
            first["cursor"], first["message_id"], first["thread"], "server-1"
        ))
        self.assertTrue(self.store.finish_inbox_handoff("message-1"))
        self.enqueue("23420", "message-2")
        self.store.claim_inbox()

        self.assertEqual(self.store.recover_inflight(), {"pending": 1, "needs_review": 0})
        self.assertEqual(self.store.message_status("message-1"), "queued")
        self.assertIsNone(self.store.inbox_status("message-1"))
        self.assertEqual(self.store.inbox_status("message-2"), "pending")
        self.assertEqual(self.store.cursor, "23420")


if __name__ == "__main__":
    unittest.main()
