"""Fixed-scope rollout for mention-only replies and bounded group history."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time

ROOT = Path("/opt/sub2api-wechat-bot")
TASK = "sub2api-wechat-group-bot-trial-20261103"
ASTRBOT = "sub2api-wechat-astrbot"
PROTECTED = ["sub2api", "sub2api-postgres", "sub2api-redis", "sub2api-wechat-native"]
KNOWN_ROLLBACKS = re.compile(
    r"rollback-(?:code|feedback)\.tar\.gz|"
    r"rollback-(?:group-context|usage|mention-history)-\d{8}T\d{6}Z\.tar\.gz"
)
GIB = 1024 ** 3


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value, *, mode=0o600, owner=None):
    if path.is_symlink() or path.parent.is_symlink():
        raise RuntimeError("destination_symlink")
    temporary = path.with_name(path.name + ".history.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeError("unfinished_atomic_write")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(mode)
    if owner is not None:
        os.chown(temporary, owner[0], owner[1])
    os.replace(temporary, path)


def run(args, *, timeout=20, input_text=None):
    result = subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("rollout_command_failed")
    return result.stdout


def task_root(root=ROOT):
    if root.is_symlink() or read_json(root / "task.json").get("task") != TASK:
        raise RuntimeError("wrong_task_directory")
    if (root / "admin-evidence/space-paused.json").exists():
        raise RuntimeError("storage_pause_requires_review")


def target_configuration(config, policy, account, group):
    if policy.get("group_id") != group:
        raise RuntimeError("target_group_mismatch")
    platforms = [item for item in config.get("platform", [])
                 if item.get("id") == "sub2api-wechat"]
    if len(platforms) != 1:
        raise RuntimeError("native_platform_not_unique")
    platform = platforms[0]
    if (platform.get("enable") is not True or platform.get("account_id") != account
            or platform.get("allowed_groups") != [group]):
        raise RuntimeError("native_target_binding_mismatch")
    if platform.get("group_reply_mode") not in {"all", "mention"}:
        raise RuntimeError("native_reply_mode_unexpected")
    if (platform.get("enable_private_reply") is not False
            or platform.get("enable_media") is not False
            or platform.get("unique_session") is not True
            or platform.get("enable_group_summary") is not True
            or platform.get("summary_max_messages") != 200
            or platform.get("summary_window_minutes") != 60):
        raise RuntimeError("native_config_boundary_mismatch")
    if platform.get("token"):
        raise RuntimeError("inline_native_token_requires_review")
    if (policy.get("enabled") is not True or policy.get("server_readonly") is not True
            or policy.get("owner_operations_enabled") is not False
            or policy.get("acceptance_prefix") or policy.get("general_enabled") is not True):
        raise RuntimeError("group_policy_boundary_mismatch")
    return platform["group_reply_mode"]


def configure_mention(config, policy, account, group):
    old_mode = target_configuration(config, policy, account, group)
    platform = next(item for item in config["platform"] if item.get("id") == "sub2api-wechat")
    platform["group_reply_mode"] = "mention"
    return old_mode


def container_state(name):
    output = run(["docker", "inspect", "--format",
        "{{.Id}}|{{.State.Status}}|{{.State.Health.Status}}|{{.State.StartedAt}}", name], timeout=8)
    parts = output.strip().split("|")
    if len(parts) != 4:
        raise RuntimeError("container_state_unavailable")
    return {"id": parts[0], "state": parts[1], "health": parts[2], "started_at": parts[3]}


def container_states():
    return {name: container_state(name) for name in [*PROTECTED, ASTRBOT]}


def native_status(root=ROOT):
    sys.path.insert(0, str(root / "code/admin"))
    import native_control
    return native_control.status()


def group_watermark(root=ROOT):
    account = read_json(root / "admin-evidence/native-account.json")["id"]
    group = read_json(root / "admin-evidence/target-group.json")["id"]
    token = (root / "secrets/native-bot-token").read_text().strip()
    command = ["docker", "exec", "-i", "sub2api-wechat-native", "wx", "--json",
        "--server", "http://127.0.0.1:6174", "--token-stdin", "--account", account,
        "messages", "list", group, "--limit", "1"]
    result = subprocess.run(command, input=token + "\n", capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError("native_history_metadata_unavailable")
    try:
        value = json.loads(result.stdout)
        cursor = value.get("asOfEventCursor")
        if (not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdecimal()
                or len(cursor) > 19):
            raise ValueError
        items = value.get("items")
        if (not isinstance(items, list)
                or any(not isinstance(item, dict) or item.get("threadId") != group for item in items)):
            raise ValueError
        return int(cursor)
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError("native_history_metadata_invalid") from None


def native_state_metrics(root=ROOT):
    found = []
    for path in (root / "astrbot-data").rglob("*.sqlite3"):
        if path.is_symlink():
            continue
        connection = None
        try:
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
            connection.execute("PRAGMA query_only=ON")
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if {"checkpoint", "messages", "sends"}.issubset(tables):
                cursor = connection.execute("SELECT cursor FROM checkpoint WHERE id=1").fetchone()[0]
                if not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdecimal():
                    raise RuntimeError("native_cursor_invalid")
                queued = connection.execute(
                    "SELECT count(*) FROM messages WHERE status='queued'").fetchone()[0]
                statuses = dict(connection.execute(
                    "SELECT status,count(*) FROM messages GROUP BY status").fetchall())
                found.append({"cursor": int(cursor), "queued": queued,
                    "status_counts": statuses})
        except sqlite3.Error:
            continue
        finally:
            if connection is not None:
                connection.close()
    if len(found) != 1:
        raise RuntimeError("native_state_database_not_unique")
    return found[0]


def wait_native_caught_up(root, minimum_cursor, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        state = native_state_metrics(root)
        watermark = group_watermark(root)
        if (state["queued"] == 0 and state["cursor"] >= minimum_cursor
                and state["cursor"] >= watermark):
            return state, watermark
        if time.monotonic() >= deadline:
            raise RuntimeError("native_event_queue_not_caught_up")
        time.sleep(2)


def source_manifest(root=ROOT):
    staged = read_json(root / "admin-evidence/staged-source-sha256.json")
    code = root / "code"
    actual = {str(path.relative_to(code)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in code.rglob("*") if path.is_file() and not path.is_symlink()
        and "__pycache__" not in path.parts and path.suffix != ".pyc"}
    if actual != staged:
        raise RuntimeError("staged_source_manifest_mismatch")
    return staged


def run_framework_probe(root=ROOT):
    result = subprocess.run(["/usr/bin/python3", str(root / "code/admin/framework_probe.py")],
        capture_output=True, text=True, timeout=60)
    rows = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if result.returncode or not rows:
        raise RuntimeError("framework_probe_failed")
    try:
        value = json.loads(rows[-1])
    except ValueError:
        raise RuntimeError("framework_probe_invalid") from None
    required = ["strict_mention_and_persistent_group_context",
                "nonmention_messages_no_reply_or_history_read",
                "group_context_speaker_anonymized"]
    if (any(value.get(key) is not True for key in required)
            or value.get("actual_server_writes") != 0
            or value.get("actual_model_calls") != 0
            or value.get("actual_messages_sent") != 0):
        raise RuntimeError("framework_probe_assertion_failed")
    return value


def _rollback_paths(diagnostics):
    return [path for path in diagnostics.iterdir()
            if path.is_file() and not path.is_symlink() and KNOWN_ROLLBACKS.fullmatch(path.name)]


def snapshot(root=ROOT, now=None):
    task_root(root)
    now = time.time() if now is None else now
    diagnostics = root / "diagnostics"
    if diagnostics.is_symlink():
        raise RuntimeError("diagnostics_directory_symlink")
    diagnostics.mkdir(mode=0o700, exist_ok=True)
    diagnostics.chmod(0o700)
    disk = shutil.disk_usage(root)
    if disk.total <= 0 or disk.free / disk.total < 0.2:
        raise RuntimeError("insufficient_disk_for_history_snapshot")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    archive = diagnostics / f"rollback-mention-history-{stamp}.tar.gz"
    temporary = diagnostics / (archive.name + ".tmp")
    if archive.exists() or temporary.exists() or temporary.is_symlink():
        raise RuntimeError("history_snapshot_name_collision")
    paths = [root / "plugins/astrbot_plugin_wechat_native",
             root / "plugins/astrbot_plugin_sub2api_guard",
             root / "astrbot-data/cmd_config.json"]
    total = count = 0
    snapshot_files = []
    for base in paths:
        if not base.exists() or base.is_symlink():
            raise RuntimeError("history_snapshot_source_missing")
        candidates = [base] if base.is_file() else list(base.rglob("*"))
        for path in candidates:
            if path.is_symlink():
                raise RuntimeError("history_snapshot_source_symlink")
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            total += path.stat().st_size
            count += 1
            snapshot_files.append(path)
    if count < 10 or total >= GIB or disk.free - total * 2 < disk.total * 0.2:
        raise RuntimeError("history_snapshot_size_limit")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as raw:
            with tarfile.open(fileobj=raw, mode="w:gz") as archive_file:
                for path in sorted(snapshot_files):
                    archive_file.add(path, arcname=str(path.relative_to(root)), recursive=False)
            raw.flush()
            os.fsync(raw.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, archive)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    known = _rollback_paths(diagnostics)
    previous = [path for path in known if path != archive and now - path.stat().st_mtime <= 14 * 86400]
    previous.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    keep = {archive, *previous[:1]}
    removed = 0
    for path in known:
        if path not in keep or now - path.stat().st_mtime > 14 * 86400:
            path.unlink()
            removed += 1
    receipt = {"at": now, "archive": archive.name, "sha256": digest,
        "files": count, "source_bytes": total, "retained_archives": 2 if previous else 1,
        "rotated_archives": removed, "contains_chat_history": False}
    write_json(root / "admin-evidence/history-predeploy-snapshot.json", receipt)
    return receipt


def _json_tree(source, destination, rollback_dir):
    if source.is_symlink() or not source.is_dir() or destination.is_symlink():
        raise RuntimeError("plugin_tree_not_verified")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise RuntimeError("plugin_tree_symlink")
    staging = destination.parent / ("." + destination.name + ".history-new")
    old = rollback_dir / (destination.name + ".history-old")
    if staging.exists() or old.exists():
        raise RuntimeError("plugin_tree_transition_exists")
    try:
        shutil.copytree(source, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for path in staging.rglob("*"):
            if path.is_symlink():
                raise RuntimeError("plugin_tree_symlink")
            path.chmod(0o555 if path.is_dir() else 0o444)
        staging.chmod(0o555)
    except Exception:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise
    if destination.exists():
        os.replace(destination, old)
    try:
        os.replace(staging, destination)
    except Exception:
        if old.exists() and not destination.exists():
            os.replace(old, destination)
        raise
    return old if old.exists() else None


def _restore_plugin(old, destination):
    if old is not None and old.exists():
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(old, destination)


def _wait_healthy(deadline_seconds=90):
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        state = container_state(ASTRBOT)
        if state["state"] == "running" and state["health"] == "healthy":
            return state
        time.sleep(1)
    raise RuntimeError("astrbot_health_timeout")


def preflight(root=ROOT):
    task_root(root)
    config_path = root / "astrbot-data/cmd_config.json"
    config = read_json(config_path)
    policy = read_json(root / "config/policy.json")
    group = read_json(root / "admin-evidence/target-group.json")["id"]
    account = read_json(root / "admin-evidence/native-account.json")["id"]
    mode = target_configuration(config, policy, account, group)
    containers = container_states()
    if any(item["state"] != "running" or item["health"] != "healthy"
           for item in containers.values()):
        raise RuntimeError("predeploy_container_unhealthy")
    if run(["systemctl", "is-active", "sub2api-wechat-query.service"], timeout=5).strip() != "active":
        raise RuntimeError("query_service_inactive")
    status = native_status(root)
    if status.get("authenticated") is not True or status.get("connection") != "online":
        raise RuntimeError("native_account_not_online")
    metrics, watermark = wait_native_caught_up(root, 13255, timeout=10)
    disk = shutil.disk_usage(root)
    sys.path.insert(0, str(root / "code/admin"))
    import maintenance
    data_bytes = maintenance.measure(root)
    if disk.free / disk.total < 0.2 or data_bytes >= 2 * GIB:
        raise RuntimeError("storage_guardrail_failed")
    if read_json(root / "astrbot-data/sub2api_guard/heartbeat.json").get("guard") != "ready":
        raise RuntimeError("guard_not_ready")
    return {"mode": mode, "containers": containers, "native": status, "state": metrics,
        "watermark": watermark, "data_bytes": data_bytes,
        "disk_free_percent": round(disk.free / disk.total * 100, 2),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest()}


def install(root=ROOT):
    before = preflight(root)
    staged = source_manifest(root)
    probe = run_framework_probe(root)
    after_probe = preflight(root)
    if before["config_sha256"] != after_probe["config_sha256"]:
        raise RuntimeError("native_config_changed_during_probe")
    snapshot_result = snapshot(root)
    config_path = root / "astrbot-data/cmd_config.json"
    policy = read_json(root / "config/policy.json")
    group = read_json(root / "admin-evidence/target-group.json")["id"]
    account = read_json(root / "admin-evidence/native-account.json")["id"]
    current_config = read_json(config_path)
    target_configuration(current_config, policy, account, group)

    stopped = False
    config_updated = False
    old_trees = []
    native_target = root / "plugins/astrbot_plugin_wechat_native"
    guard_target = root / "plugins/astrbot_plugin_sub2api_guard"
    try:
        current = preflight(root)
        if (current["state"]["queued"] != 0
                or current["config_sha256"] != before["config_sha256"]):
            raise RuntimeError("predeploy_state_changed")
        plugin_root = root / "plugins"
        if plugin_root.is_symlink() or any(
            (plugin_root / name).exists() for name in [
                ".astrbot_plugin_wechat_native.history-new",
                ".astrbot_plugin_sub2api_guard.history-new",
            ]):
            raise RuntimeError("plugin_tree_transition_exists")
        run(["docker", "stop", "--time", "30", ASTRBOT], timeout=45)
        stopped = True
        config = read_json(config_path)
        if hashlib.sha256(config_path.read_bytes()).hexdigest() != current["config_sha256"]:
            raise RuntimeError("native_config_changed_before_write")
        configure_mention(config, policy, account, group)
        state = config_path.stat()
        write_json(config_path, config, mode=state.st_mode & 0o777,
                   owner=(state.st_uid, state.st_gid))
        config_updated = True
        rollback_dir = root / "diagnostics"
        old = _json_tree(root / "code/vendor/astrbot_plugin_wechat_native",
                         native_target, rollback_dir)
        if old is not None:
            old_trees.append((old, native_target))
        guard_staging = guard_target.parent / ".astrbot_plugin_sub2api_guard.history-new"
        if guard_staging.exists() or guard_staging.is_symlink():
            raise RuntimeError("plugin_tree_transition_exists")
        if any(path.is_symlink() for source in [root / "code/plugin", root / "code/guard"]
               for path in source.rglob("*")):
            raise RuntimeError("plugin_tree_symlink")
        try:
            shutil.copytree(root / "code/plugin", guard_staging,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copytree(root / "code/guard", guard_staging / "guard",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for path in guard_staging.rglob("*"):
                if path.is_symlink():
                    raise RuntimeError("plugin_tree_symlink")
                path.chmod(0o555 if path.is_dir() else 0o444)
            guard_staging.chmod(0o555)
        except Exception:
            if guard_staging.exists() and not guard_staging.is_symlink():
                shutil.rmtree(guard_staging)
            raise
        guard_old = rollback_dir / "astrbot_plugin_sub2api_guard.history-old"
        if guard_old.exists() or guard_old.is_symlink():
            raise RuntimeError("plugin_tree_transition_exists")
        if guard_target.exists():
            os.replace(guard_target, guard_old)
        try:
            os.replace(guard_staging, guard_target)
        except Exception:
            if guard_old.exists() and not guard_target.exists():
                os.replace(guard_old, guard_target)
            raise
        if guard_old.exists():
            old_trees.append((guard_old, guard_target))
        run(["docker", "start", ASTRBOT], timeout=20)
        _wait_healthy()
        after = container_states()
        for name in PROTECTED:
            if after[name] != before["containers"][name]:
                raise RuntimeError("protected_container_changed")
        native = native_status(root)
        if native.get("authenticated") is not True or native.get("connection") != "online":
            raise RuntimeError("native_account_changed")
        if run(["systemctl", "is-active", "sub2api-wechat-query.service"], timeout=5).strip() != "active":
            raise RuntimeError("query_service_changed")
        final_config = read_json(config_path)
        mode = target_configuration(final_config, policy, account, group)
        if mode != "mention":
            raise RuntimeError("mention_mode_not_active")
        heartbeat = read_json(root / "astrbot-data/sub2api_guard/heartbeat.json")
        if heartbeat.get("guard") != "ready" or heartbeat.get("server_readonly") is not True:
            raise RuntimeError("guard_not_ready_after_restart")
        final_state, final_watermark = wait_native_caught_up(
            root, before["state"]["cursor"], timeout=45)
        stopped = False
        cleanup_pending = 0
        for old, destination in old_trees:
            if old.exists():
                try:
                    shutil.rmtree(old)
                except OSError:
                    cleanup_pending += 1
        write_json(root / "admin-evidence/deployed-source-sha256.json", staged)
        evidence = {"at": time.time(), "stage": "mention_history_enabled",
            "source_files": len(staged), "group_reply_mode": "mention",
            "previous_group_reply_mode": before["mode"],
            "persistent_history_limit": 200, "context_messages": 20, "context_chars": 6000,
            "framework_probe_passed": True,
            "strict_mention_and_persistent_group_context": probe["strict_mention_and_persistent_group_context"],
            "nonmention_messages_no_reply_or_history_read": probe["nonmention_messages_no_reply_or_history_read"],
            "source_manifest_sha256": hashlib.sha256(json.dumps(staged, sort_keys=True,
                separators=(",", ":")).encode()).hexdigest(),
            "actual_model_calls": 0,
            "actual_group_messages_sent": 0, "business_writes": 0,
            "protected_containers_unchanged": True, "native_online": True,
            "query_service_active": True, "state_cursor": final_state["cursor"],
            "event_watermark": final_watermark, "queued": final_state["queued"],
            "needs_review": final_state["status_counts"].get("needs_review", 0),
            "temporary_plugin_backup_dirs_remaining": cleanup_pending,
            "rollback_archive": snapshot_result["archive"],
            "rollback_archive_sha256": snapshot_result["sha256"],
            "rollback_archives_retained": snapshot_result["retained_archives"],
            "rollback_archives_rotated": snapshot_result["rotated_archives"]}
        write_json(root / "admin-evidence/mention-history-rollout.json", evidence)
        return {key: evidence[key] for key in ["stage", "group_reply_mode", "source_files",
            "framework_probe_passed", "actual_model_calls", "actual_group_messages_sent",
            "business_writes", "protected_containers_unchanged", "native_online",
            "state_cursor", "event_watermark", "queued", "rollback_archives_retained",
            "rollback_archives_rotated"]}
    except Exception:
        if stopped:
            try:
                state = container_state(ASTRBOT)
                if state["state"] == "running":
                    run(["docker", "stop", "--time", "30", ASTRBOT], timeout=45)
                if config_updated:
                    for old, destination in reversed(old_trees):
                        _restore_plugin(old, destination)
                    run(["docker", "start", ASTRBOT], timeout=20)
                    _wait_healthy()
            except Exception:
                raise RuntimeError("history_rollout_recovery_failed") from None
            if not config_updated:
                raise RuntimeError("service_stopped_config_not_updated") from None
        raise


def main():
    os.umask(0o077)
    task_root()
    if len(sys.argv) != 2 or sys.argv[1] != "install":
        raise RuntimeError("action_not_allowed")
    print(json.dumps(install(), ensure_ascii=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        category = str(error)
        if not re.fullmatch(r"[a-z_]{1,64}", category):
            category = "history_rollout_failed"
        print(json.dumps({"failed": True, "category": category}, ensure_ascii=True))
        raise SystemExit(1)
