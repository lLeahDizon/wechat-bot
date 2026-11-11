import sqlite3
import tempfile
import unittest
from pathlib import Path

from admin.model_failures import recent_model_failures


class ModelFailureQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite"
        self.now = 1_800_000_000
        connection = sqlite3.connect(self.path)
        connection.execute("""
            CREATE TABLE deliveries(
                id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                at REAL NOT NULL,
                failure_code TEXT
            )
        """)
        connection.executemany("INSERT INTO deliveries VALUES (?,?,?,?)", [
            ("private-digest-a", "replied", self.now - 60, "http_503"),
            ("private-digest-b", "replied", self.now - 120, "raw error text"),
            ("private-digest-old", "replied", self.now - 8 * 86400, "transport"),
        ])
        connection.commit()
        connection.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_returns_only_recent_normalized_codes_and_times(self):
        result = recent_model_failures(self.path, now=self.now, limit=10)
        self.assertTrue(result["available"])
        self.assertEqual(result["retention_days"], 7)
        self.assertEqual([item["code"] for item in result["items"]], ["http_503", "other_error"])
        self.assertTrue(all(item["intent"] == "investment" for item in result["items"]))
        self.assertNotIn("private-digest", repr(result))
        self.assertNotIn("raw error text", repr(result))

    def test_enforces_operator_query_limit(self):
        with self.assertRaises(ValueError):
            recent_model_failures(self.path, now=self.now, limit=101)

    def test_legacy_delivery_table_is_reported_unavailable(self):
        legacy = Path(self.tmp.name) / "legacy.sqlite"
        connection = sqlite3.connect(legacy)
        connection.execute("CREATE TABLE deliveries(id TEXT PRIMARY KEY,state TEXT NOT NULL,at REAL NOT NULL)")
        connection.commit()
        connection.close()
        result = recent_model_failures(legacy, now=self.now)
        self.assertEqual(result, {"available": False, "retention_days": 7, "items": []})
