"""Operator-only read-only view of bounded investment failure metadata."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3

ROOT = Path("/opt/sub2api-wechat-bot")
STATE = ROOT / "astrbot-data/sub2api_guard/state.sqlite"
RETENTION_SECONDS = 7 * 24 * 60 * 60
SHANGHAI = timezone(timedelta(hours=8))
SAFE_FAILURE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


def _safe_failure_code(value):
    if not isinstance(value, str):
        return "other_error"
    value = value.lower()
    return value if SAFE_FAILURE_CODE.fullmatch(value) else "other_error"


def recent_model_failures(path=STATE, *, limit=20, now=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return {"available": False, "retention_days": 7, "items": []}
    now = datetime.now().timestamp() if now is None else float(now)
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        connection.execute("PRAGMA query_only=ON")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(deliveries)")}
        if "failure_code" not in columns:
            return {"available": False, "retention_days": 7, "items": []}
        rows = connection.execute(
            "SELECT at,failure_code FROM deliveries "
            "WHERE failure_code IS NOT NULL AND at >= ? ORDER BY at DESC LIMIT ?",
            (now - RETENTION_SECONDS, limit),
        ).fetchall()
    finally:
        connection.close()
    items = [{
        "at": datetime.fromtimestamp(float(at), SHANGHAI).isoformat(timespec="seconds"),
        "intent": "investment",
        "code": _safe_failure_code(code),
    } for at, code in rows]
    return {"available": True, "retention_days": 7, "items": items}


def main():
    parser = argparse.ArgumentParser(description="List recent sanitized investment model failure codes")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    try:
        result = recent_model_failures(limit=args.limit)
    except (OSError, sqlite3.Error, ValueError):
        result = {"available": False, "retention_days": 7, "items": []}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["available"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
