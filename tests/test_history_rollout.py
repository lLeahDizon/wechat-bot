import copy
import importlib.util
import json
import os
from pathlib import Path
import tarfile
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "history_rollout_fixture", ROOT / "admin/history_rollout.py")
rollout = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rollout)


class HistoryRolloutConfigTests(unittest.TestCase):
    def setUp(self):
        self.group = "target@chatroom"
        self.account = "native-account"
        self.policy = {
            "group_id": self.group,
            "enabled": True,
            "general_enabled": True,
            "server_readonly": True,
            "owner_operations_enabled": False,
            "acceptance_prefix": "",
        }
        self.platform = {
            "id": "sub2api-wechat",
            "type": "wechat_native",
            "enable": True,
            "account_id": self.account,
            "allowed_groups": [self.group],
            "group_reply_mode": "all",
            "enable_private_reply": False,
            "enable_media": False,
            "unique_session": True,
            "enable_group_summary": True,
            "summary_max_messages": 200,
            "summary_window_minutes": 60,
            "token": "",
        }
        self.config = {"platform": [self.platform], "other": {"preserve": True}}

    def test_only_target_group_mode_changes_to_mention(self):
        original = copy.deepcopy(self.config)
        previous = rollout.configure_mention(self.config, self.policy, self.account, self.group)
        self.assertEqual(previous, "all")
        expected = copy.deepcopy(original)
        expected["platform"][0]["group_reply_mode"] = "mention"
        self.assertEqual(self.config, expected)

    def test_duplicate_or_expanded_group_bindings_fail_closed(self):
        self.config["platform"].append(copy.deepcopy(self.platform))
        with self.assertRaisesRegex(RuntimeError, "native_platform_not_unique"):
            rollout.target_configuration(self.config, self.policy, self.account, self.group)
        self.config["platform"] = [self.platform]
        self.platform["allowed_groups"] = [self.group, "other@chatroom"]
        with self.assertRaisesRegex(RuntimeError, "native_target_binding_mismatch"):
            rollout.target_configuration(self.config, self.policy, self.account, self.group)

    def test_inline_token_or_write_policy_blocks_rollout(self):
        self.platform["token"] = "must-not-be-snapshotted"
        with self.assertRaisesRegex(RuntimeError, "inline_native_token_requires_review"):
            rollout.target_configuration(self.config, self.policy, self.account, self.group)
        self.platform["token"] = ""
        self.policy["owner_operations_enabled"] = True
        with self.assertRaisesRegex(RuntimeError, "group_policy_boundary_mismatch"):
            rollout.target_configuration(self.config, self.policy, self.account, self.group)


class HistoryRolloutSnapshotTests(unittest.TestCase):
    def test_snapshot_contains_only_code_config_and_rotates_to_two_archives(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "task.json").write_text(json.dumps({"task": rollout.TASK}))
            for directory in [
                "plugins/astrbot_plugin_wechat_native",
                "plugins/astrbot_plugin_sub2api_guard/guard",
                "astrbot-data",
                "diagnostics",
                "admin-evidence",
            ]:
                (root / directory).mkdir(parents=True, exist_ok=True)
            for index in range(12):
                target = root / "plugins/astrbot_plugin_sub2api_guard/guard" / f"module-{index}.py"
                target.write_text("source")
            (root / "plugins/astrbot_plugin_wechat_native/adapter.py").write_text("adapter")
            config = root / "astrbot-data/cmd_config.json"
            config.write_text(json.dumps({"platform": [{"token": ""}]}))
            now = time.time()
            old_archives = [
                root / "diagnostics/rollback-code.tar.gz",
                root / "diagnostics/rollback-group-context-20261106T093507Z.tar.gz",
                root / "diagnostics/rollback-usage-20261106T080113Z.tar.gz",
            ]
            for index, archive in enumerate(old_archives):
                archive.write_bytes(b"old-rollback")
                os.utime(archive, (now - (index + 1) * 3600,) * 2)

            with patch.object(rollout.shutil, "disk_usage",
                              return_value=SimpleNamespace(total=100 * 1024 ** 3,
                                                          free=80 * 1024 ** 3)):
                result = rollout.snapshot(root, now)
            archive = root / "diagnostics" / result["archive"]
            self.assertTrue(archive.is_file())
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            with tarfile.open(archive, "r:gz") as saved:
                names = saved.getnames()
            self.assertIn("astrbot-data/cmd_config.json", names)
            self.assertTrue(any(name.startswith("plugins/astrbot_plugin_wechat_native/") for name in names))
            self.assertTrue(any(name.startswith("plugins/astrbot_plugin_sub2api_guard/") for name in names))
            self.assertFalse(any(name.startswith("native-data/") for name in names))
            retained = rollout._rollback_paths(root / "diagnostics")
            self.assertEqual(len(retained), 2)
            receipt = json.loads((root / "admin-evidence/history-predeploy-snapshot.json").read_text())
            self.assertEqual(receipt["retained_archives"], 2)
            self.assertEqual(receipt["rotated_archives"], 2)
            self.assertFalse(receipt["contains_chat_history"])


if __name__ == "__main__":
    unittest.main()
