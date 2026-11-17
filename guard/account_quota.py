"""Anonymous shared-account snapshots; queries never probe or reset upstreams."""

from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
import re
import time

BJT = datetime.timezone(datetime.timedelta(hours=8), "Asia/Shanghai")
MAX_EPOCH = 253402300799
STALE_SECONDS = 300
ACCOUNT = re.compile(r"账号|帐号|(?:上游|共享|当前|服务)(?:账户|套餐)|^(?:账户|套餐)")
METRIC = re.compile(
    r"用量|额度|配额|余额|百分|比例|剩|余量|可用|重置|使用率|使用情况|还能用"
)
PERSONAL = re.compile(
    r"(?<![A-Za-z])keys?(?![A-Za-z])|密钥|tokens?|令牌|美元|费用|消费|排行|排名|\$",
    re.I,
)
FOLLOW = re.compile(
    r"^(?:那|详细|具体|展开|建议|推荐|我在|正在|我需要|怎么|如何|还剩|还有|剩余|额度|用量)"
)
ADVICE = re.compile(
    r"建议|推荐|怎么用|如何用|模型|推理|上下文|编码|代码|项目|架构|调试|批量|重构|详细|展开|具体"
)
DETAIL = re.compile(
    r"详细|具体|展开|针对|结合.{0,40}(?:代码|项目|任务)|(?:我在|正在|我需要).{0,80}(?:开发|调试|重构|编程|代码|架构|项目)"
)
OTHER_ACCOUNT = re.compile(
    r"github|gitlab|aws|阿里云|腾讯云|银行卡|运营商|手机话费", re.I
)
PUBLIC_SUBJECT = re.compile(
    r"上游(?:账号|帐号|账户)(?=[\s的]*(?:用量|额度|配额|使用|剩余|余额|周|短期|周期|重置|百分))"
)
RESET_ACTION = re.compile(
    r"(?:执行|触发|消耗).{0,8}重置|(?:帮我|立刻|马上|直接).{0,10}重置|^重置(?:一下|账号|帐号|额度|配额)"
)


def privacy_text(text):
    # Only the public quota subject is exempted, never credentials or identity.
    return (
        PUBLIC_SUBJECT.sub("共享账号", text)
        if ACCOUNT.search(text) and METRIC.search(text)
        else text
    )


def request_for(text, *, follow=False, aliases=None, bound_member=False):
    if not isinstance(text, str):
        return None, None, False
    explicit = bool(ACCOUNT.search(text) and METRIC.search(text))
    if OTHER_ACCOUNT.search(text) and not re.search(
        r"sub2api|本服务|本服务器|当前服务器", text, re.I
    ):
        return None, None, False
    contextual = bool(
        follow and FOLLOW.search(text) and (ADVICE.search(text) or METRIC.search(text))
    )
    bare = bool(
        re.fullmatch(r"(?:用量|额度|剩余用量|还剩多少|还剩多少额度)[?？。!！\s]*", text)
    )
    if not explicit and not contextual:
        if bare and bound_member:
            return None, None, False
        return (
            (None, "你想查共享账号的额度百分比，还是个人Key的Token／费用用量？", False)
            if bare
            else (None, None, False)
        )
    named = any(
        alias in text
        for names in (aliases or {}).values()
        for alias in names
        if isinstance(alias, str) and alias
    )
    if PERSONAL.search(text) or named:
        if explicit:
            return (
                None,
                "请分开查询：共享账号额度百分比，或个人Key的Token／费用和排行。",
                False,
            )
        return None, None, False
    if re.search(r"昨天|昨日|前天|上周|上月|历史|\d{4}[-年/]\d", text):
        return (
            None,
            "账号额度只能读取最近保存的快照，不能还原指定历史日期；个人Key的Token和费用仍可按日期查询。",
            False,
        )
    return (
        {"op": "account_quota", "window_minutes": 15},
        None,
        bool(DETAIL.search(text)),
    )


def epoch(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, str) and "T" in value:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            result = parsed.timestamp()
        else:
            result = float(value)
        return result if math.isfinite(result) and 0 < result < MAX_EPOCH else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def percentage(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite() or not 0 <= result <= 100:
            return None
        return result.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    except (ValueError, TypeError, InvalidOperation):
        return None


def snapshot_data(rows, now=None):
    """Only a view's bounded anonymous fields enter this pure normalizer."""
    now = time.time() if now is None else now
    empty = {"scope": "shared_upstream_account", "status": "unavailable", "windows": []}
    if (
        not isinstance(rows, list)
        or not 1 <= len(rows) <= 16
        or any(not isinstance(r, dict) for r in rows)
    ):
        return empty
    if any(
        any(
            type(r.get(k)) is not int
            for k in ["eligible_records", "identity_count", "unidentified_records"]
        )
        or r.get("eligible_records") != len(rows)
        or r.get("identity_count") != 1
        or r.get("unidentified_records") != 0
        for r in rows
    ):
        return {**empty, "status": "identity_unverified"}
    windows = []
    for kind, prefix in [("short_term", "short"), ("weekly", "weekly")]:
        candidates = []
        for row in rows:
            sampled = epoch(row.get("sampled_at"))
            reset = epoch(row.get(prefix + "_reset_at"))
            used = percentage(row.get(prefix + "_used_percent"))
            minutes = row.get(prefix + "_window_minutes")
            if (
                sampled is None
                or sampled > now + 60
                or reset is None
                or reset <= sampled
                or used is None
                or type(minutes) is not int
                or not 0 < minutes <= 525600
            ):
                continue
            candidates.append((sampled, reset, used, minutes))
        if not candidates:
            windows.append({"kind": kind, "state": "unavailable"})
            continue
        sampled, reset, used, minutes = max(candidates, key=lambda value: value[0])
        if reset <= now:
            windows.append(
                {
                    "kind": kind,
                    "state": "expired",
                    "sampled_at": sampled,
                    "reset_at": reset,
                }
            )
            continue
        windows.append(
            {
                "kind": kind,
                "state": "stale" if now - sampled > STALE_SECONDS else "ok",
                "used_percent": str(used),
                "remaining_percent": str(Decimal(100) - used),
                "window_minutes": minutes,
                "sampled_at": sampled,
                "reset_at": reset,
            }
        )
    return {
        "scope": "shared_upstream_account",
        "status": "ok",
        "windows": windows,
        "reset_credits": reset_credit_data(rows, now),
    }


def reset_credit_data(rows, now):
    snapshots = []
    for row in rows:
        count = row.get("reset_credit_count")
        raw = row.get("reset_credit_expirations")
        if (
            row.get("reset_credit_snapshot_present") is not True
            or type(count) is not int
            or not 0 <= count <= 32
            or not isinstance(raw, list)
            or len(raw) != count
        ):
            return {"state": "unavailable"}
        values = [epoch(item) for item in raw]
        if any(item is None for item in values):
            return {"state": "unavailable"}
        snapshots.append((count, tuple(sorted(values))))
    if len(set(snapshots)) != 1:
        return {"state": "conflict"}
    count, expires = snapshots[0]
    if any(value <= now for value in expires):
        return {"state": "expired"}
    # These are stored opportunities, not proof that a credit remains unspent.
    # Neither account usage timestamps nor paid-credit timestamps date this cache.
    return {
        "state": "cached",
        "count": count,
        "expires_at": list(expires),
        "sampled_at": None,
    }


def _minute(value):
    stamp = epoch(value)
    if stamp is None:
        raise ValueError("invalid_quota_time")
    return datetime.datetime.fromtimestamp(stamp, BJT).strftime("%Y-%m-%d %H:%M")


def usable_windows(data, now=None):
    now = time.time() if now is None else now
    valid = []
    for item in data.get("windows", []):
        if not isinstance(item, dict) or item.get("state") not in {"ok", "stale"}:
            continue
        used, remaining = (
            percentage(item.get("used_percent")),
            percentage(item.get("remaining_percent")),
        )
        sampled, reset = epoch(item.get("sampled_at")), epoch(item.get("reset_at"))
        if (
            used is None
            or remaining is None
            or used + remaining != 100
            or sampled is None
            or reset is None
            or sampled > now + 60
            or reset <= now
            or reset <= sampled
        ):
            continue
        valid.append(
            {**item, "state": "stale" if now - sampled > STALE_SECONDS else "ok"}
        )
    return valid


def recommendation(data, now=None):
    now = time.time() if now is None else now
    windows = usable_windows(data, now)
    if not windows:
        return ""
    if any(w["state"] == "stale" for w in windows):
        return "建议：快照已陈旧，先保守安排必要任务，当前剩余额度以新采样为准。"
    remaining = min(percentage(w["remaining_percent"]) for w in windows)
    if any(w["reset_at"] - now <= 1800 for w in windows):
        return "建议：有窗口即将重置，批量任务可稍后安排；关键排错仍按任务需要选择推理强度。"
    if remaining <= 10:
        return "建议：优先关键开发任务，日常用medium和短上下文，复杂问题按需提高推理；批量任务可等重置。"
    if remaining <= 30:
        return "建议：日常编码用medium并精简上下文，复杂排错再按需提高推理；实际模型以服务器映射为准。"
    return "建议：日常编码用medium，复杂排错或架构任务按需提高推理；实际模型以服务器映射为准。"


def format_account_quota(result, tone="default", now=None):
    now = time.time() if now is None else now
    if (
        not isinstance(result, dict)
        or result.get("state") != "ok"
        or not isinstance(result.get("data"), dict)
    ):
        return "暂未取得可核对的账号额度快照，本次不猜测百分比。"
    data = result["data"]
    if data.get("status") == "identity_unverified":
        return "共享账号关系暂无法核实，本次不合并或猜测额度。"
    if data.get("status") != "ok" or not isinstance(data.get("windows"), list):
        return "暂未取得有效的账号额度快照，本次不猜测百分比。"
    kinds = [item.get("kind") for item in data["windows"] if isinstance(item, dict)]
    if len(kinds) != 2 or set(kinds) != {"short_term", "weekly"}:
        return "账号额度窗口格式异常，本次不猜测百分比。"
    valid = {item.get("kind"): item for item in usable_windows(data, now)}
    lines = ["查到啦，共享账号额度：" if tone == "gentle" else "共享账号额度："]
    for raw in data["windows"]:
        kind = raw.get("kind") if isinstance(raw, dict) else None
        if kind not in {"short_term", "weekly"}:
            continue
        label = "周额度" if kind == "weekly" else "短期额度"
        item = valid.get(kind)
        if item:
            if kind == "weekly" and item.get("window_minutes") != 10080:
                label = "较长期额度"
            record = "最近一次记录，" if item["state"] == "stale" else ""
            lines.append(
                f"{label}：{record}已用{percentage(item['used_percent']):.2f}%，剩余{percentage(item['remaining_percent']):.2f}%；重置北京时间{_minute(item['reset_at'])}。"
            )
            lines.append(f"采样北京时间{_minute(item['sampled_at'])}。")
        elif raw.get("state") == "expired" or (
            epoch(raw.get("reset_at")) is not None and epoch(raw.get("reset_at")) <= now
        ):
            lines.append(label + "：旧窗口已到重置时间，暂未取得新窗口数据。")
        else:
            lines.append(label + "：暂无有效数据。")
    credits = data.get("reset_credits", {})
    if (
        isinstance(credits, dict)
        and credits.get("state") == "cached"
        and type(credits.get("count")) is int
        and 0 <= credits["count"] <= 32
        and isinstance(credits.get("expires_at"), list)
        and len(credits["expires_at"]) == credits["count"]
        and all(
            epoch(value) is not None and epoch(value) > now
            for value in credits["expires_at"]
        )
    ):
        lines.append(
            f"可用重置次数缓存记录：{credits['count']}次（采样时间未提供，可能已变化）。"
        )
        if credits["expires_at"]:
            lines.append(
                "每次机会失效时间（北京时间）："
                + "、".join(_minute(value) for value in credits["expires_at"])
                + "。"
            )
    else:
        reason = (
            "缓存不一致"
            if isinstance(credits, dict) and credits.get("state") == "conflict"
            else "缺少可核实的有效快照"
        )
        lines.append("剩余可用重置次数暂无法确认（" + reason + "）。")
    note = recommendation(data, now)
    if note:
        lines.append(note)
    return "\n".join(lines)


def advice_answer(data, key_file, api_base, model_call=None):
    """A detailed follow-up uses one ordinary model call; facts stay fixed."""
    from .model import ModelFailure, call

    model_call = call if model_call is None else model_call
    evidence = data["account_quota_evidence"]
    local = data["quota_summary"]
    windows = (
        usable_windows(evidence.get("data", {}))
        if evidence.get("data", {}).get("status") == "ok"
        else []
    )
    result = {
        "scope": "service",
        "answer": local,
        "refs": [evidence["id"]],
        "queries": [],
        "learning": "",
    }
    if not windows or any(w["state"] != "ok" for w in windows):
        return result, []
    try:
        answer, usage = model_call(data, key_file, api_base)
    except ModelFailure:
        result["answer"] = format_account_quota(evidence, data.get("tone", "default"))
        return result, [{}]
    result["answer"] = format_account_quota(evidence, data.get("tone", "default"))
    if not usable_windows(evidence.get("data", {})):
        return result, [usage]
    text = answer.get("answer", "") if isinstance(answer, dict) else ""
    from .core import unsafe

    # Detailed prose cannot replace quota figures, invent savings, or recommend
    # unverified model names. The validated local summary remains authoritative.
    if (
        isinstance(text, str)
        and 0 < len(text) <= 500
        and not unsafe(text)
        and not re.search(
            r"\d|%|\$|USD|gpt-|claude-|gemini-|倍|(?:还能|可用).{0,6}(?:次|条)|(?:更省|最省).{0,4}额度",
            text,
            re.I,
        )
    ):
        result["answer"] += "\n具体建议：" + text
    return result, [usage]
