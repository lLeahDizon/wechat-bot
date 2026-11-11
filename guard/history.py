"""Bounded, privacy-filtered context from the target group's stored history."""
from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace


def _field(record, name, default=None):
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _timestamp(record):
    value = _field(record, "timestamp")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    value = _field(record, "createdAt")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(parsed.timestamp()) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def merge_group_history(persisted_records, live_records, thread: str, current_message_id: str,
                        current_timestamp: int, bot_sender_id: str = "", *,
                        max_records: int = 200):
    """Merge one newest-first Native page with cache records before this event."""
    if type(max_records) is not int or not 1 <= max_records <= 200:
        raise ValueError("history merge limit must be between 1 and 200")
    persisted = list(persisted_records or ())
    boundary = next((index for index, row in enumerate(persisted)
                     if _field(row, "id") == current_message_id), None)
    if boundary is not None:
        persisted = persisted[boundary + 1:]
    else:
        persisted = [row for row in persisted
                     if (_timestamp(row) or 0) < current_timestamp]

    merged = {}
    for record in (*persisted, *(live_records or ())):
        message_id = _field(record, "id")
        record_thread = _field(record, "threadId", _field(record, "thread", ""))
        text = _field(record, "body", _field(record, "text", ""))
        stamp = _timestamp(record)
        if (not isinstance(message_id, str) or not message_id
                or message_id == current_message_id or record_thread != thread
                or not isinstance(text, str) or not text.strip() or stamp is None):
            continue
        if isinstance(record, dict):
            if record.get("direction") not in {"incoming", "outgoing"}:
                continue
            message_type = record.get("type")
            if message_type != 1 and not (
                message_type == 49 and record.get("appMessageType") == 57
            ):
                continue
            if record.get("recall") is not None or text.strip() == "[Message recalled]":
                continue
        sender = _field(record, "senderId", _field(record, "sender", "未知发言者"))
        if not isinstance(sender, str) or not sender:
            sender = "未知发言者"
        if bot_sender_id and sender == bot_sender_id:
            sender = "机器人"
        normalized = SimpleNamespace(
            id=message_id,
            thread=thread,
            sender=sender,
            timestamp=stamp,
            text=text,
            truncated=bool(_field(record, "truncated", False)),
        )
        previous = merged.get(message_id)
        if previous is None or len(normalized.text) > len(previous.text):
            merged[message_id] = normalized

    ordered = sorted(merged.values(), key=lambda row: row.timestamp)
    return tuple(ordered[-max_records:])


def build_group_context(records, current_message_id: str, hidden_label_rules=(), *,
                        max_messages: int = 20, max_chars: int = 6000) -> list[dict]:
    """Return bounded, redacted messages with ephemeral speaker labels only."""
    if type(max_messages) is not int or not 1 <= max_messages <= 20:
        raise ValueError("context message limit must be between 1 and 20")
    if type(max_chars) is not int or not 1 <= max_chars <= 6000:
        raise ValueError("context character limit must be between 1 and 6000")
    from .core import mask_labels, redact_question

    safe = []
    for record in records:
        if getattr(record, "id", None) == current_message_id:
            continue
        text = getattr(record, "text", "")
        text = redact_question(text) if isinstance(text, str) else None
        if text is None:
            continue
        text = mask_labels(text, hidden_label_rules).strip()
        if not text:
            continue
        safe.append((getattr(record, "sender", ""), text,
                     bool(getattr(record, "truncated", False))))

    safe = safe[-max_messages:]
    selected = []
    used = 0
    for sender, text, truncated in reversed(safe):
        room = max_chars - used
        if room <= 0:
            break
        overhead = 16
        if len(text) + overhead > room:
            text = text[:max(0, room - overhead)].rstrip()
            truncated = True
        if not text:
            break
        selected.append((sender, text, truncated))
        used += len(text) + overhead
    selected.reverse()

    speakers = {}
    member_count = 0
    result = []
    for sender, text, truncated in selected:
        if sender not in speakers:
            if sender == "机器人":
                speakers[sender] = sender
            else:
                member_count += 1
                speakers[sender] = f"群成员{member_count}"
        result.append({"speaker": speakers[sender], "text": text, "truncated": truncated})
    return result


def build_investment_context(records, current_message_id: str, sender_id: str, hidden_label_rules=(), *,
                             excluded_aliases=(), max_messages: int = 20, max_chars: int = 6000) -> list[dict]:
    """Filter by raw sender identity before redaction and speaker anonymization."""
    if not isinstance(sender_id, str) or not sender_id:
        return []
    from .investment import references_other_personal_data
    relevant = re.compile(r"股票|股价|股市|持仓|持有|买入|买了|成本|预算|资金|风险偏好|仓位|投资|分红|股息|估值|证券|组合|A股|港股|美股|台股", re.IGNORECASE)
    self_disclosure = re.compile(r"我|本人|我的")
    own = []
    for record in records:
        text = _field(record, "text", "")
        if (_field(record, "sender") != sender_id or not isinstance(text, str)
                or not relevant.search(text) or not self_disclosure.search(text)
                or references_other_personal_data(text, excluded_aliases)):
            continue
        own.append(record)
    context = build_group_context(own, current_message_id, hidden_label_rules,
        max_messages=max_messages, max_chars=max_chars)
    return [{**item, "speaker": "提问者"} for item in context]
