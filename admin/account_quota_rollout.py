"""Operator-only, patch-based quota rollout; each restart is attempted once."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
import time

from .account_quota_view import view_sql
from .history_rollout import (
    ASTRBOT, PROTECTED, ROOT, container_states, native_state_metrics,
    native_status, preflight, read_json, run, task_root, write_json,
)

SOURCE_PATHS = {
    "admin/account_quota_view.py", "admin/account_quota_rollout.py",
    "admin/framework_probe.py", "admin/verify_readonly_permissions.py",
    "diagnostics/protocol.py", "diagnostics/sources.py",
    "guard/account_quota.py", "guard/core.py", "guard/model.py",
    "plugin/main.py", "tests/test_account_quota.py",
}
RECEIPT = ROOT / "admin-evidence/account-quota-rollout.json"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_targets():
    plugin = ROOT / "plugins/astrbot_plugin_sub2api_guard"
    return {
        **{name: plugin / name for name in
           ["guard/account_quota.py", "guard/core.py", "guard/model.py"]},
        "plugin/main.py": plugin / "main.py",
        "diagnostics/protocol.py": ROOT / "query-client/diagnostics/protocol.py",
    }


def check_patch(patch, expected):
    if not patch or len(patch.encode()) > 128 * 1024:
        raise ValueError("invalid_patch_size")
    if hashlib.sha256(patch.encode()).hexdigest() != expected:
        raise ValueError("patch_digest_mismatch")
    rows = run(["git", "apply", "--numstat", "-"], input_text=patch)
    names = []
    for line in rows.splitlines():
        added, removed, name = line.split("\t")
        if not added.isdecimal() or not removed.isdecimal() or name not in SOURCE_PATHS:
            raise ValueError("patch_path_not_allowed")
        names.append(name)
    if not names or len(names) != len(set(names)):
        raise ValueError("patch_scope_mismatch")


def protected_hashes():
    names = ["config/policy.json", "astrbot-data/cmd_config.json",
             "admin-evidence/member-bindings.json", "admin-evidence/owner-binding.json",
             "admin-evidence/usage-binding-rollout.json"]
    return {name: sha(ROOT / name) for name in names if (ROOT / name).exists()}


def database(sql):
    return run(["docker", "exec", "-i", "sub2api-postgres", "psql", "-X", "-qAt",
                "-U", "sub2api", "-d", "sub2api", "-v", "ON_ERROR_STOP=1"],
               input_text=sql, timeout=12)


def prepare(patch, expected):
    task_root()
    if RECEIPT.exists():
        raise RuntimeError("quota_rollout_already_started_reconcile_receipt")
    check_patch(patch, expected)
    before = preflight()
    code = ROOT / "code"
    for name in SOURCE_PATHS:
        path = code / name
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise RuntimeError("source_symlink")
    for name, target in runtime_targets().items():
        source = code / name
        if source.exists() != target.exists() or (source.exists() and sha(source) != sha(target)):
            raise RuntimeError("runtime_source_drift")
    run(["git", "-C", str(code), "apply", "--check", "-"], input_text=patch)
    old_view = database("BEGIN READ ONLY; SET LOCAL statement_timeout='3s'; "
        "SELECT json_build_object('definition',pg_get_viewdef(c.oid,true),"
        "'comment',obj_description(c.oid,'pg_class'),'acl',c.relacl::text) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='bot_diagnostics' AND c.relname='account_quota'; ROLLBACK;")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    archive = ROOT / "diagnostics" / ("rollback-usage-" + stamp + ".tar.gz")
    paths = [code / name for name in SOURCE_PATHS]
    paths.extend(runtime_targets().values())
    paths.extend([ROOT / "admin-evidence/staged-source-sha256.json",
                  ROOT / "admin-evidence/deployed-source-sha256.json"])
    with tarfile.open(archive, "x:gz") as backup:
        for path in paths:
            if path.is_symlink():
                raise RuntimeError("backup_symlink")
            if path.exists():
                backup.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
    archive.chmod(0o600)
    receipt = {
        "phase": "backed_up", "at": time.time(), "patch_sha256": expected,
        "archive": archive.name, "archive_sha256": sha(archive),
        "previous_view": old_view, "protected_hashes": protected_hashes(),
        "before": before, "restart_attempts": {"query": 0, "astrbot": 0},
    }
    write_json(RECEIPT, receipt)
    run(["git", "-C", str(code), "apply", "-"], input_text=patch)
    for name in SOURCE_PATHS:
        (code / name).chmod(0o444)
    receipt.update(phase="source_prepared", changed_sha256={name: sha(code / name) for name in SOURCE_PATHS})
    write_json(RECEIPT, receipt)
    return {"phase": receipt["phase"], "backup_created": True, "changed_paths": len(SOURCE_PATHS),
            "runtime_restarted": False, "private_templates_preserved": True}


def check_receipt():
    task_root()
    receipt = read_json(RECEIPT)
    archive = ROOT / "diagnostics" / receipt["archive"]
    if (not re.fullmatch(r"rollback-usage-\d{8}T\d{6}Z\.tar\.gz", archive.name)
            or archive.is_symlink() or sha(archive) != receipt["archive_sha256"]):
        raise RuntimeError("rollback_archive_invalid")
    if any(sha(ROOT / "code" / name) != value for name, value in receipt["changed_sha256"].items()):
        raise RuntimeError("prepared_source_changed")
    if protected_hashes() != receipt["protected_hashes"]:
        raise RuntimeError("protected_configuration_changed")
    return receipt


def verify_prepared():
    receipt = check_receipt()
    if receipt["phase"] != "source_prepared":
        raise RuntimeError("source_prepared_phase_required")
    probe = run(["python3", str(ROOT / "code/admin/framework_probe.py")], timeout=55)
    rows = [json.loads(line) for line in probe.splitlines() if line.startswith("{")]
    required = ["account_quota_local_without_model", "account_quota_detail_one_call",
                "account_quota_strict_mention", "usage_personal_and_rank_replayed",
                "account_points_local", "primary_reset_record"]
    value = rows[-1] if rows else {}
    if (any(value.get(name) is not True for name in required)
            or any(value.get(name) != 0 for name in
                   ["actual_server_writes", "actual_model_calls", "actual_messages_sent"])):
        raise RuntimeError("quota_framework_probe_failed")
    database(view_sql())
    run(["python3", str(ROOT / "code/admin/verify_readonly_permissions.py")], timeout=30)
    from diagnostics.sources import Sources
    from diagnostics.protocol import evidence, public_result
    data = Sources().account_quota()
    if data.get("status") != "ok" or not public_result(evidence("account_quota", data)):
        raise RuntimeError("anonymous_quota_projection_failed")
    receipt.update(phase="verified", framework=value, verified_at=time.time())
    write_json(RECEIPT, receipt)
    return {"phase": "verified", "framework_replay": True, "readonly_projection": True,
            "protected_table_access": False, "model_calls": 0, "group_messages_sent": 0}


def activate():
    receipt = check_receipt()
    if receipt["phase"] != "verified" or any(receipt["restart_attempts"].values()):
        raise RuntimeError("activation_requires_verified_unattempted_receipt")
    current = preflight()
    if current["containers"] != receipt["before"]["containers"]:
        raise RuntimeError("container_changed_before_activation")
    for name, target in runtime_targets().items():
        if target.is_symlink() or target.parent.is_symlink():
            raise RuntimeError("runtime_symlink")
        temporary = target.with_name(target.name + ".quota.tmp")
        if temporary.exists() or temporary.is_symlink():
            raise RuntimeError("unfinished_runtime_write")
        shutil.copyfile(ROOT / "code" / name, temporary)
        temporary.chmod(0o444)
        os.replace(temporary, target)
    receipt.update(phase="activating", activation_at=time.time())
    receipt["restart_attempts"]["query"] = 1
    write_json(RECEIPT, receipt)
    run(["systemctl", "restart", "sub2api-wechat-query.service"], timeout=15)
    receipt["restart_attempts"]["astrbot"] = 1
    write_json(RECEIPT, receipt)
    run(["docker", "restart", "--time", "30", ASTRBOT], timeout=50)
    receipt.update(phase="activated")
    write_json(RECEIPT, receipt)
    return {"phase": "activated", "restart_attempts": receipt["restart_attempts"]}


def verify_live():
    receipt = check_receipt()
    if receipt["phase"] not in {"activated", "live_verified"}:
        raise RuntimeError("activated_phase_required")
    states = container_states()
    if any(states[name] != receipt["before"]["containers"][name] for name in PROTECTED):
        raise RuntimeError("protected_container_changed")
    if any(item["state"] != "running" or item["health"] != "healthy" for item in states.values()):
        raise RuntimeError("container_health_not_ready")
    native = native_status()
    if not native.get("authenticated") or native.get("connection") != "online":
        raise RuntimeError("native_not_online")
    metrics = native_state_metrics()
    if metrics["cursor"] < receipt["before"]["state"]["cursor"]:
        raise RuntimeError("cursor_regressed")
    if metrics["status_counts"].get("needs_review", 0) < receipt["before"]["state"]["status_counts"].get("needs_review", 0):
        raise RuntimeError("uncertain_records_changed")
    from diagnostics.client import QueryBudget
    from guard.account_quota import format_account_quota
    value = QueryBudget(Path("/run/sub2api-wechat-query/query.sock")).query({"op": "account_quota"})
    if value.get("state") != "ok" or value.get("data", {}).get("status") != "ok":
        raise RuntimeError("live_quota_query_failed")
    heartbeat = read_json(ROOT / "astrbot-data/sub2api_guard/heartbeat.json")
    if heartbeat.get("guard") != "ready" or heartbeat.get("at", 0) < receipt["activation_at"]:
        raise RuntimeError("guard_heartbeat_not_ready")
    if any(sha(target) != receipt["changed_sha256"][name] for name, target in runtime_targets().items()):
        raise RuntimeError("runtime_hash_mismatch")
    manifest = {str(path.relative_to(ROOT / "code")): sha(path)
                for path in (ROOT / "code").rglob("*") if path.is_file()
                and not path.is_symlink() and "__pycache__" not in path.parts and path.suffix != ".pyc"}
    for name in ["staged-source-sha256.json", "deployed-source-sha256.json"]:
        write_json(ROOT / "admin-evidence" / name, manifest)
    receipt.update(phase="live_verified", live_verified_at=time.time(), live_evidence=value,
                   final_cursor=metrics["cursor"], group_acceptance="waiting_new_true_mention")
    write_json(RECEIPT, receipt)
    return {"phase": "live_verified", "query": value, "rendered": format_account_quota(value),
            "containers_healthy": True, "protected_containers_unchanged": True,
            "native_online": True, "cursor_preserved": True, "bindings_preserved": True,
            "restart_attempts": receipt["restart_attempts"], "group_acceptance": receipt["group_acceptance"]}


def main():
    global RECEIPT
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["prepare", "verify-prepared", "activate", "verify-live"])
    parser.add_argument("--patch-sha256")
    parser.add_argument("--revision", choices=["initial", "primary-reset-points"], default="initial")
    args = parser.parse_args()
    if args.revision != "initial":
        RECEIPT = ROOT / "admin-evidence" / ("account-quota-" + args.revision + ".json")
    if args.stage == "prepare":
        value = prepare(sys.stdin.read(128 * 1024 + 1), args.patch_sha256)
    else:
        value = {"verify-prepared": verify_prepared, "activate": activate, "verify-live": verify_live}[args.stage]()
    print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        message = str(error)
        code = message if re.fullmatch(r"[a-z_]{1,100}", message) else type(error).__name__
        print(json.dumps({"failed": True, "category": code}))
        raise SystemExit(1)
