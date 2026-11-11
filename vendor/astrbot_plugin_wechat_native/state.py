"""Small transactional journal: IDs/receipts only, never tokens or message bodies.

The AstrBot queue cannot participate in a database transaction. A crash after
handoff is therefore retained for review rather than automatically handed off twice.
"""

import sqlite3
from pathlib import Path


def cursor_number(value: str) -> int:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or len(value) > 19
        or int(value) > 9223372036854775807
    ):
        raise ValueError("invalid_cursor")
    return int(value)


class StateIdentityError(RuntimeError):
    """Stored identity changed; preserve evidence and require operator inspection."""


class InboxFullError(RuntimeError):
    """The durable inbox is full; the event cursor must not advance."""


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY, cursor TEXT NOT NULL);
            INSERT OR IGNORE INTO checkpoint VALUES (1, '0');
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recalls (
                thread TEXT NOT NULL, server_id TEXT NOT NULL, PRIMARY KEY(thread, server_id));
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, thread TEXT NOT NULL, server_id TEXT, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sends (
                key TEXT PRIMARY KEY, message_id TEXT, thread TEXT NOT NULL,
                state TEXT NOT NULL, send_id TEXT);
            CREATE TABLE IF NOT EXISTS inbox (
                cursor TEXT PRIMARY KEY, message_id TEXT NOT NULL UNIQUE,
                thread TEXT NOT NULL, status TEXT NOT NULL
                    CHECK (status IN ('pending', 'processing', 'handoff_started', 'needs_review')));
            CREATE INDEX IF NOT EXISTS sends_message_state ON sends(message_id, state);
            CREATE INDEX IF NOT EXISTS inbox_status_cursor ON inbox(status, cursor);
        """)
        with self.db:
            self.db.execute("UPDATE messages SET status='needs_review' WHERE status='queued'")
            self.db.execute("UPDATE sends SET state='needs_review' WHERE state='submitting'")
            self.db.execute(
                "UPDATE messages SET status=CASE WHEN status='replied' THEN 'partial' "
                "ELSE 'needs_review' END WHERE status IN ('replied','no_reply') "
                "AND EXISTS (SELECT 1 FROM sends WHERE sends.message_id=messages.id "
                "AND sends.state!='accepted')"
            )
            self.db.execute(
                "UPDATE inbox SET status='needs_review' WHERE status='handoff_started' "
                "OR (status='processing' AND EXISTS (SELECT 1 FROM messages "
                "WHERE messages.id=inbox.message_id AND messages.status='needs_review'))"
            )
            self.db.execute("UPDATE inbox SET status='pending' WHERE status='processing'")

    def bind_server(self, server_id: str):
        if not isinstance(server_id, str) or not server_id:
            raise StateIdentityError("Native server identity unavailable; state retained")
        row = self.db.execute("SELECT value FROM settings WHERE key='server_id'").fetchone()
        if row and row[0] != server_id:
            raise StateIdentityError("Native server identity changed; state retained for review")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('server_id', ?)", (server_id,))

    @property
    def cursor(self) -> str:
        return self.db.execute("SELECT cursor FROM checkpoint WHERE id=1").fetchone()[0]

    def _advance(self, cursor: str):
        if cursor_number(cursor) > cursor_number(self.cursor):
            self.db.execute("UPDATE checkpoint SET cursor=? WHERE id=1", (cursor,))

    def advance(self, cursor: str):
        with self.db:
            self._advance(cursor)

    def enqueue_inbox(
        self, cursor: str, message_id: str, thread: str, *, max_pending: int
    ) -> bool:
        cursor_number(cursor)
        if (
            not isinstance(message_id, str)
            or not message_id
            or not isinstance(thread, str)
            or not thread
        ):
            raise ValueError("invalid_inbox_identity")
        if type(max_pending) is not int or max_pending < 1:
            raise ValueError("invalid_inbox_limit")

        conflict = False
        inserted = False
        with self.db:
            message = self.db.execute(
                "SELECT thread FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            queued = self.db.execute(
                "SELECT cursor, thread FROM inbox WHERE message_id=?", (message_id,)
            ).fetchone()
            same_cursor = self.db.execute(
                "SELECT message_id, thread FROM inbox WHERE cursor=?", (cursor,)
            ).fetchone()

            if (message and message[0] != thread) or (queued and queued[1] != thread):
                self.db.execute(
                    "UPDATE messages SET status='needs_review' WHERE id=? AND status!='recalled'",
                    (message_id,),
                )
                self.db.execute(
                    "UPDATE inbox SET status='needs_review' WHERE message_id=?", (message_id,)
                )
                conflict = True
            elif same_cursor and (same_cursor[0] != message_id or same_cursor[1] != thread):
                self.db.execute(
                    "UPDATE inbox SET status='needs_review' WHERE cursor=?", (cursor,)
                )
                self.db.execute(
                    "UPDATE messages SET status='needs_review' WHERE id=? AND status!='recalled'",
                    (same_cursor[0],),
                )
                conflict = True
            elif message or queued:
                self._advance(cursor)
            elif cursor_number(cursor) <= cursor_number(self.cursor):
                conflict = True
            else:
                active = self.db.execute(
                    "SELECT COUNT(*) FROM inbox WHERE status IN "
                    "('pending', 'processing', 'handoff_started')"
                ).fetchone()[0]
                if active >= max_pending:
                    raise InboxFullError("durable inbox at capacity")
                self.db.execute(
                    "INSERT INTO inbox(cursor, message_id, thread, status) "
                    "VALUES (?, ?, ?, 'pending')",
                    (cursor, message_id, thread),
                )
                self._advance(cursor)
                inserted = True
        if conflict:
            raise StateIdentityError("Native inbox identity conflict; cursor retained for review")
        return inserted

    def claim_inbox(self) -> dict | None:
        with self.db:
            row = self.db.execute(
                "SELECT cursor, message_id, thread FROM inbox WHERE status='pending' "
                "ORDER BY CAST(cursor AS INTEGER), rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            result = self.db.execute(
                "UPDATE inbox SET status='processing' WHERE cursor=? AND status='pending'",
                (row[0],),
            )
            if result.rowcount != 1:
                return None
            return {"cursor": row[0], "message_id": row[1], "thread": row[2]}

    def inbox_status(self, message_id: str) -> str | None:
        row = self.db.execute(
            "SELECT status FROM inbox WHERE message_id=?", (message_id,)
        ).fetchone()
        return row[0] if row else None

    def prepare_inbox_media(
        self, cursor: str, message_id: str, thread: str, server_id: str | None
    ) -> bool:
        with self.db:
            row = self._inbox_row(cursor, message_id, thread)
            if row is None or row[0] != "processing":
                return False
            existing = self.db.execute(
                "SELECT thread, server_id, status FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if existing and (existing[0] != thread or existing[1] != server_id):
                self.db.execute(
                    "UPDATE messages SET status='needs_review' WHERE id=? AND status!='recalled'",
                    (message_id,),
                )
                self.db.execute(
                    "UPDATE inbox SET status='needs_review' WHERE message_id=?", (message_id,)
                )
                return False
            recalled = self.db.execute(
                "SELECT 1 FROM recalls WHERE thread=? AND server_id=?",
                (thread, server_id),
            ).fetchone()
            if recalled or (existing and existing[2] == "recalled"):
                self.db.execute(
                    "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, 'recalled')",
                    (message_id, thread, server_id),
                )
                self.db.execute("DELETE FROM inbox WHERE message_id=?", (message_id,))
                return False
            if existing and existing[2] != "queued":
                self.db.execute(
                    "UPDATE inbox SET status='needs_review' WHERE message_id=?", (message_id,)
                )
                return False
            self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, 'queued')",
                (message_id, thread, server_id),
            )
            return True

    def pending_inbox_count(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM inbox WHERE status IN "
            "('pending', 'processing', 'handoff_started')"
        ).fetchone()[0]

    def return_inbox_for_retry(self, message_id: str) -> bool:
        with self.db:
            result = self.db.execute(
                "UPDATE inbox SET status='pending' WHERE message_id=? AND status='processing'",
                (message_id,),
            )
            return result.rowcount == 1

    def recover_inbox_item(self, message_id: str) -> str | None:
        with self.db:
            row = self.db.execute(
                "SELECT cursor, thread, status FROM inbox WHERE message_id=?", (message_id,)
            ).fetchone()
            if row is None:
                return None
            message = self.db.execute(
                "SELECT status FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if row[2] == "handoff_started" or (
                row[2] == "processing"
                and message
                and message[0] in {"queued", "needs_review"}
            ):
                status = "needs_review"
            elif row[2] == "processing":
                status = "pending"
            else:
                return row[2]
            self.db.execute(
                "UPDATE inbox SET status=? WHERE message_id=?", (status, message_id)
            )
            if status == "needs_review":
                self.db.execute(
                    "UPDATE messages SET status='needs_review' "
                    "WHERE id=? AND status NOT IN ('recalled', 'replied', 'partial')",
                    (message_id,),
                )
            return status

    def recover_inflight(self) -> dict[str, int]:
        """Recover a stopped worker without changing work already handed to AstrBot."""
        with self.db:
            ambiguous = self.db.execute(
                "SELECT message_id FROM inbox WHERE status='handoff_started' "
                "OR (status='processing' AND EXISTS (SELECT 1 FROM messages "
                "WHERE messages.id=inbox.message_id "
                "AND messages.status IN ('queued', 'needs_review')))"
            ).fetchall()
            for row in ambiguous:
                self.db.execute(
                    "UPDATE inbox SET status='needs_review' WHERE message_id=?", (row[0],)
                )
                self.db.execute(
                    "UPDATE messages SET status='needs_review' WHERE id=? "
                    "AND status NOT IN ('recalled', 'replied', 'partial')", (row[0],)
                )
            pending = self.db.execute(
                "UPDATE inbox SET status='pending' WHERE status='processing'"
            ).rowcount
            return {"pending": pending, "needs_review": len(ambiguous)}

    def _inbox_row(self, cursor: str, message_id: str, thread: str):
        row = self.db.execute(
            "SELECT status, thread FROM inbox WHERE cursor=? AND message_id=?",
            (cursor, message_id),
        ).fetchone()
        if row and row[1] != thread:
            self.db.execute(
                "UPDATE inbox SET status='needs_review' WHERE cursor=?", (cursor,)
            )
            return ("needs_review", row[1])
        return row

    def finish_inbox_ignored(
        self, cursor: str, message_id: str, thread: str, server_id: str | None
    ) -> bool:
        with self.db:
            row = self._inbox_row(cursor, message_id, thread)
            if row is None or row[0] != "processing":
                return False
            existing = self.db.execute(
                "SELECT thread, server_id, status FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if existing and (existing[0] != thread or existing[1] != server_id):
                self.db.execute(
                    "UPDATE messages SET status='needs_review' WHERE id=? AND status!='recalled'",
                    (message_id,),
                )
                self.db.execute(
                    "UPDATE inbox SET status='needs_review' WHERE message_id=?", (message_id,)
                )
                return False
            recalled = self.db.execute(
                "SELECT 1 FROM recalls WHERE thread=? AND server_id=?",
                (thread, server_id),
            ).fetchone()
            self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?)",
                (message_id, thread, server_id, "recalled" if recalled else "ignored"),
            )
            self.db.execute("DELETE FROM inbox WHERE message_id=?", (message_id,))
            return not recalled

    def begin_inbox_handoff(
        self, cursor: str, message_id: str, thread: str, server_id: str | None
    ) -> bool:
        with self.db:
            row = self._inbox_row(cursor, message_id, thread)
            if row is None or row[0] not in {"processing", "handoff_started"}:
                return False
            existing = self.db.execute(
                "SELECT thread, server_id, status FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if existing and (existing[0] != thread or existing[1] != server_id):
                self.db.execute(
                    "UPDATE messages SET status='needs_review' WHERE id=? AND status!='recalled'",
                    (message_id,),
                )
                self.db.execute(
                    "UPDATE inbox SET status='needs_review' WHERE message_id=?", (message_id,)
                )
                return False
            recalled = self.db.execute(
                "SELECT 1 FROM recalls WHERE thread=? AND server_id=?",
                (thread, server_id),
            ).fetchone()
            if recalled or (existing and existing[2] == "recalled"):
                self.db.execute(
                    "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, 'recalled')",
                    (message_id, thread, server_id),
                )
                self.db.execute("DELETE FROM inbox WHERE message_id=?", (message_id,))
                return False
            if existing and existing[2] != "queued":
                self.db.execute(
                    "UPDATE inbox SET status='needs_review' WHERE message_id=?", (message_id,)
                )
                return False
            self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, 'queued')",
                (message_id, thread, server_id),
            )
            self.db.execute(
                "UPDATE inbox SET status='handoff_started' WHERE message_id=?", (message_id,)
            )
            return True

    def finish_inbox_handoff(self, message_id: str) -> bool:
        with self.db:
            result = self.db.execute(
                "DELETE FROM inbox WHERE message_id=? AND status='handoff_started'",
                (message_id,),
            )
            return result.rowcount == 1

    def fail_inbox_handoff(self, message_id: str) -> bool:
        with self.db:
            self.db.execute(
                "UPDATE messages SET status='needs_review' "
                "WHERE id=? AND status NOT IN ('recalled', 'replied', 'partial')",
                (message_id,),
            )
            result = self.db.execute(
                "UPDATE inbox SET status='needs_review' WHERE message_id=?",
                (message_id,),
            )
            return result.rowcount == 1

    def finish_inbox_after_media(self, message_id: str) -> bool:
        with self.db:
            row = self.db.execute(
                "SELECT status FROM inbox WHERE message_id=?", (message_id,)
            ).fetchone()
            if row is None:
                return True
            message = self.db.execute(
                "SELECT status FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            status = message[0] if message else None
            if status in {"recalled", "replied", "no_reply", "failed", "partial", "ignored"}:
                self.db.execute("DELETE FROM inbox WHERE message_id=?", (message_id,))
                return True
            if row[0] == "handoff_started" and status == "queued":
                self.db.execute("DELETE FROM inbox WHERE message_id=?", (message_id,))
                return True
            self.db.execute(
                "UPDATE inbox SET status='needs_review' WHERE message_id=?", (message_id,)
            )
            self.db.execute(
                "UPDATE messages SET status='needs_review' WHERE id=? AND status='queued'",
                (message_id,),
            )
            return False

    def accept(
        self, cursor: str, message_id: str, thread: str, server_id: str | None, status: str
    ) -> bool:
        existing = self.db.execute(
            "SELECT thread, server_id FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        if existing and (existing[0] != thread or existing[1] != server_id):
            self.mark_message(message_id, "needs_review")
            raise StateIdentityError("Native message identity reused; cursor retained for review")
        with self.db:
            recalled = self.db.execute(
                "SELECT 1 FROM recalls WHERE thread=? AND server_id=?", (thread, server_id)
            ).fetchone()
            result = self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?)",
                (message_id, thread, server_id, "recalled" if recalled else status),
            )
            self._advance(cursor)
            return result.rowcount == 1 and not recalled

    def message_status(self, message_id: str) -> str | None:
        row = self.db.execute("SELECT status FROM messages WHERE id=?", (message_id,)).fetchone()
        return row[0] if row else None

    def mark_message(self, message_id: str, status: str):
        with self.db:
            self.db.execute(
                "UPDATE messages SET status=CASE "
                "WHEN status='partial' THEN 'partial' "
                "WHEN status='replied' AND ? IN ('failed','needs_review') THEN 'partial' "
                "WHEN status IN ('failed','needs_review') AND ?='replied' THEN 'partial' "
                "ELSE ? END WHERE id=? AND status!='recalled'",
                (status, status, status, message_id),
            )

    def finish_queued_no_reply(self, message_id: str) -> bool:
        with self.db:
            result = self.db.execute(
                "UPDATE messages SET status='no_reply' WHERE id=? AND status='queued'",
                (message_id,),
            )
            return result.rowcount == 1

    def finish_queued_needs_review(self, message_id: str) -> bool:
        with self.db:
            result = self.db.execute(
                "UPDATE messages SET status='needs_review' WHERE id=? AND status='queued'",
                (message_id,),
            )
            return result.rowcount == 1

    def recall(self, thread: str, server_id: str | None, message_id: str | None):
        with self.db:
            if server_id:
                self.db.execute("INSERT OR IGNORE INTO recalls VALUES (?, ?)", (thread, server_id))
            self.db.execute(
                "UPDATE messages SET status='recalled' WHERE thread=? AND "
                "((? IS NOT NULL AND server_id=?) OR (? IS NULL AND id=?))",
                (thread, server_id, server_id, server_id, message_id),
            )
            if message_id:
                self.db.execute(
                    "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, 'recalled')",
                    (message_id, thread, server_id),
                )
                self.db.execute("DELETE FROM inbox WHERE message_id=?", (message_id,))

    def begin_send(self, key: str, message_id: str | None, thread: str):
        with self.db:
            self.db.execute(
                "UPDATE messages SET status='queued' WHERE id=? AND status='no_reply'",
                (message_id,),
            )
            self.db.execute(
                "INSERT INTO sends VALUES (?, ?, ?, 'submitting', NULL)", (key, message_id, thread)
            )

    def finish_send(self, key: str, state: str, send_id: str | None):
        with self.db:
            self.db.execute(
                "UPDATE sends SET state=?, send_id=? WHERE key=?", (state, send_id, key)
            )

    def send_record(self, key: str) -> dict | None:
        row = self.db.execute("SELECT * FROM sends WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def close(self):
        self.db.close()
