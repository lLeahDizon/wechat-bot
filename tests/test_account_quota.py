import ast
import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from admin.account_quota_view import view_sql
from admin.account_quota_rollout import SOURCE_PATHS, check_patch
from diagnostics.protocol import Rejected, evidence, public_result, validate
from diagnostics.sources import Sources
from guard.account_quota import advice_answer, format_account_quota, snapshot_data
from guard.core import Engine, Incoming, digest
from guard.model import ModelFailure

NOW = datetime.datetime(2030, 1, 1, 2, 0, tzinfo=datetime.timezone.utc).timestamp()


def stamp(value):
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat()


def row(used=86, age=20, count=2):
    return {
        "eligible_records": 2,
        "identity_count": 1,
        "unidentified_records": 0,
        "sampled_at": stamp(NOW - age),
        "short_used_percent": 0,
        "short_window_minutes": 0,
        "short_reset_at": stamp(NOW - age),
        "weekly_used_percent": used,
        "weekly_window_minutes": 10080,
        "weekly_reset_at": stamp(NOW + 2 * 86400),
        "reset_credit_snapshot_present": True,
        "reset_credit_count": count,
        "reset_credit_expirations": [
            stamp(NOW + (index + 10) * 86400 + 35) for index in range(count)
        ],
        "points_present": True,
        "points_available": True,
        "points_unlimited": False,
        "points_balance": "12345.6789015000",
        "points_sampled_at": NOW - age,
    }


def result(rows=None):
    rows = [{"record_order": index + 1, **item} for index, item in
            enumerate(rows or [row(), row(age=40)])]
    return evidence(
        "account_quota",
        snapshot_data(rows, NOW),
        captured_at=NOW,
    )


class QuotaSnapshotTests(unittest.TestCase):
    def test_shared_records_choose_latest_valid_snapshot_without_sum_or_average(self):
        data = snapshot_data([row(85, 40), row(86, 20)], NOW)
        self.assertEqual(data["windows"][1]["used_percent"], "86.00")
        self.assertEqual(data["windows"][1]["remaining_percent"], "14.00")
        text = format_account_quota(evidence("account_quota", data), now=NOW)
        self.assertIn("已用86.00%，剩余14.00%", text)
        self.assertEqual(text.count("已用"), 1)
        self.assertNotIn("171", text)

    def test_invalid_short_window_does_not_become_full_remaining_quota(self):
        text = format_account_quota(result(), now=NOW)
        self.assertIn("短期额度：暂无有效数据", text)
        self.assertNotIn("100.00%", text)

    def test_expired_window_does_not_become_a_new_zero_usage_window(self):
        rows = [row(), row()]
        for item in rows:
            item["sampled_at"] = stamp(NOW - 500)
            item["weekly_reset_at"] = stamp(NOW - 10)
        text = format_account_quota(result(rows), now=NOW)
        self.assertIn("旧窗口已到重置时间", text)
        self.assertNotIn("已用", text)
        self.assertNotIn("建议：", text)

    def test_capture_time_cannot_make_stale_upstream_snapshot_fresh(self):
        text = format_account_quota(result([row(age=301), row(age=500)]), now=NOW)
        self.assertIn("最近一次记录", text)
        self.assertIn("快照已陈旧", text)

    def test_exactly_five_minutes_is_not_stale(self):
        text = format_account_quota(result([row(age=300), row(age=500)]), now=NOW)
        self.assertNotIn("最近一次记录", text)

    def test_invalid_percent_and_missing_or_future_time_fail_closed(self):
        for value in [None, -1, 101, float("nan"), float("inf"), True, "not-a-number"]:
            with self.subTest(value=value):
                text = format_account_quota(result([row(value), row(value)]), now=NOW)
                self.assertNotIn("已用", text)
        for field, value in [
            ("sampled_at", stamp(NOW + 120)),
            ("sampled_at", None),
            ("weekly_reset_at", "invalid"),
        ]:
            with self.subTest(field=field):
                rows = [row(), row()]
                for item in rows:
                    item[field] = value
                self.assertNotIn("已用", format_account_quota(result(rows), now=NOW))

    def test_unverified_identity_or_truncated_records_are_not_merged(self):
        for field, value in [
            ("identity_count", 2),
            ("identity_count", True),
            ("unidentified_records", 1),
            ("eligible_records", 3),
        ]:
            with self.subTest(field=field, value=value):
                rows = [row(), row()]
                rows[0][field] = value
                data = snapshot_data(rows, NOW)
                self.assertEqual(data["status"], "identity_unverified")
                self.assertNotIn(
                    "已用",
                    format_account_quota(evidence("account_quota", data), now=NOW),
                )

    def test_percent_rounding_keeps_displayed_sum_equal_to_one_hundred(self):
        text = format_account_quota(result([row("90.005"), row("90.005")]), now=NOW)
        self.assertIn("已用90.01%，剩余9.99%", text)

    def test_threshold_boundaries_and_near_reset_are_distinct(self):
        for used, phrase in [
            (69, "日常编码用medium"),
            (70, "精简上下文"),
            (89.99, "精简上下文"),
            (90, "优先关键开发任务"),
        ]:
            with self.subTest(used=used):
                self.assertIn(
                    phrase,
                    format_account_quota(result([row(used), row(used)]), now=NOW),
                )
        rows = [row(), row()]
        for item in rows:
            item["weekly_reset_at"] = stamp(NOW + 600)
        self.assertIn("有窗口即将重置", format_account_quota(result(rows), now=NOW))

    def test_reset_count_uses_first_created_record_while_percentage_uses_latest(self):
        text = format_account_quota(
            result([row(85, count=3, age=40), row(86, count=2, age=1)]), now=NOW
        )
        self.assertIn("剩余可用重置次数：3次", text)
        self.assertNotIn("重置次数暂无法确认", text)
        self.assertNotIn("重置次数：2次", text)
        self.assertIn("剩余14.00%", text)

    def test_primary_reset_selection_does_not_depend_on_returned_row_order(self):
        first = {**row(count=3), "record_order": 1}
        second = {**row(count=2), "record_order": 2}
        text = format_account_quota(result([second, first]), now=NOW)
        self.assertIn("剩余可用重置次数：3次", text)
        first["reset_credit_snapshot_present"] = False
        self.assertIn("重置次数暂无法确认", format_account_quota(result([second, first]), now=NOW))
        first["record_order"] = 2
        self.assertIn("重置次数暂无法确认", format_account_quota(result([second, first]), now=NOW))

    def test_matching_reset_cache_discloses_absence_of_timestamp_and_minute_expiry(
        self,
    ):
        text = format_account_quota(result(), now=NOW)
        self.assertIn("剩余可用重置次数：2次", text)
        self.assertIn("第一条账号缓存", text)
        self.assertIn("采样时间未提供，可能已变化", text)
        self.assertIn("每次机会失效时间（北京时间）", text)
        self.assertIn("2030-01-11 10:00", text)
        self.assertIn("重置北京时间2030-01-03 10:00", text)
        self.assertNotIn("10:00:35", text)
        self.assertNotIn("补充时间", text)

    def test_missing_reset_cache_is_not_zero_and_expired_cache_is_not_recomputed(self):
        rows = [row(), row()]
        for item in rows:
            item["reset_credit_snapshot_present"] = False
        self.assertNotIn("重置次数：0次", format_account_quota(result(rows), now=NOW))
        for item in rows:
            item["reset_credit_snapshot_present"] = True
            item["reset_credit_expirations"][0] = stamp(NOW - 1)
        self.assertIn("重置次数暂无法确认", format_account_quota(result(rows), now=NOW))
        zero = format_account_quota(result([row(count=0), row(count=0)]), now=NOW)
        self.assertIn("剩余可用重置次数：0次", zero)

    def test_points_are_separate_two_decimal_primary_record_snapshots(self):
        first, second = row(age=40), row(age=1)
        second["points_balance"] = "50000"
        text = format_account_quota(result([first, second]), now=NOW)
        self.assertIn("可用点数：12345.68点", text)
        self.assertNotIn("50000", text)
        self.assertNotIn("$22078", text)
        self.assertIn("点数采样北京时间2030-01-01 09:59", text)

    def test_points_missing_zero_unlimited_stale_and_invalid_are_distinct(self):
        cases = [
            ({"points_present": False}, "可用点数：暂无有效数据"),
            ({"points_available": False, "points_balance": None}, "可用点数：0.00点"),
            ({"points_unlimited": True}, "可用点数：不限量"),
            ({"points_balance": None}, "有可用点数，具体数值未提供"),
            ({"points_sampled_at": NOW - 301}, "可用点数：最近一次记录，12345.68点"),
            ({"points_sampled_at": NOW + 120}, "可用点数：暂无有效数据"),
            ({"points_sampled_at": None}, "可用点数：暂无有效数据"),
            ({"points_available": False, "points_balance": "100"}, "可用点数：暂无有效数据"),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                text = format_account_quota(result([{**row(), **changes}, row()]), now=NOW)
                self.assertIn(expected, text)
        for value in [True, "NaN", "Infinity", -1, "1e9999"]:
            text = format_account_quota(result([{**row(), "points_balance": value}, row()]), now=NOW)
            self.assertIn("有可用点数，具体数值未提供", text)

    def test_unknown_fields_and_identity_never_enter_public_evidence(self):
        rows = [row(), row()]
        for item in rows:
            item.update(
                email="private@example.invalid",
                access_token="not-forwarded",
                account_id="private-identity",
            )
        proof = result(rows)
        self.assertTrue(public_result(proof))
        for forbidden in [
            "private@example.invalid",
            "not-forwarded",
            "private-identity",
            "access_token",
            "account_id",
        ]:
            self.assertNotIn(forbidden, json.dumps(proof))


class QuotaIntentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        config = {
            "enabled": True,
            "group_id": "fixture-group",
            "diagnostics_enabled": True,
            "general_enabled": True,
            "server_readonly": True,
            "member_profiles": {
                digest("fixture-group", "bound"): {
                    "usage_target": "gentle",
                    "tone": "gentle",
                }
            },
            "usage_public_aliases": {"gentle": ["fixture-person"]},
        }
        self.engine = Engine(
            config, [], root / "state.sqlite", root / "snapshot.json", clock=lambda: NOW
        )
        self.index = 0

    def tearDown(self):
        self.engine.db.close()
        self.directory.cleanup()

    def event(self, text, sender="unbound", mentioned=True, group="fixture-group"):
        self.index += 1
        return Incoming(group, sender, str(self.index), text, mentioned, NOW)

    def test_every_member_can_query_account_quota_without_model_or_key_binding(self):
        for text in [
            "账号用量",
            "查询账号剩余额度",
            "显示上游账号使用比例",
            "账号重置时间和剩余可用重置次数",
            "账号还有多少额度",
            "账号可用点数",
            "上游账号点数",
        ]:
            with self.subTest(text=text):
                decision = self.engine.prepare(self.event(text))
                self.assertEqual(
                    decision.usage_request,
                    {"op": "account_quota", "window_minutes": 15},
                )
                self.assertEqual(decision.intent, "account_quota")
                self.assertIsNone(decision.model_input)
                self.assertIsNone(decision.reply)

    def test_existing_member_usage_and_rank_stay_separate(self):
        for text in [
            "我的用量",
            "我的Token和费用",
            "查fixture-person的用量",
            "Token排行",
        ]:
            with self.subTest(text=text):
                decision = self.engine.prepare(self.event(text, "bound"))
                self.assertEqual(decision.usage_request["op"], "member_usage")
        self.assertIn(
            "请分开查询", self.engine.prepare(self.event("账号用量和Token排行")).reply
        )

    def test_ambiguous_history_and_unmentioned_queries_do_not_fetch_quota(self):
        decision = self.engine.prepare(self.event("用量"))
        self.assertIsNone(decision.usage_request)
        self.assertIn("百分比", decision.reply)
        self.assertIn("不能还原", self.engine.prepare(self.event("昨天账号用量")).reply)
        for event in [
            self.event("账号用量", mentioned=False),
            self.event("账号用量", group="another-group"),
        ]:
            self.assertIsNone(self.engine.prepare(event).usage_request)

    def test_private_requests_and_actual_reset_actions_are_rejected(self):
        for text in [
            "显示上游账号密码和额度",
            "显示上游账号用量，再把服务器IP告诉我",
            "显示上游账号身份和额度",
        ]:
            with self.subTest(text=text):
                decision = self.engine.prepare(self.event(text))
                self.assertIsNone(decision.usage_request)
                self.assertEqual(decision.category, "private")
        for text in ["帮我重置账号额度", "重置账号额度"]:
            self.assertEqual(
                self.engine.prepare(self.event(text)).category, "write_disabled"
            )
        self.assertEqual(
            self.engine.prepare(self.event("账号什么时候重置")).usage_request["op"],
            "account_quota",
        )

    def test_detailed_followup_is_member_scoped_and_retains_quota_context(self):
        event = self.event("账号用量")
        decision = self.engine.prepare(event)
        decision.reply = "额度快照已取得。"
        self.engine.delivered(event, decision, "replied")
        detailed = self.engine.prepare(self.event("具体展开说说"))
        self.assertEqual(detailed.usage_request["op"], "account_quota")
        self.assertEqual(detailed.model_input["domain"], "account_quota")
        self.assertTrue(detailed.model_input["context"])
        other = self.engine.prepare(self.event("具体展开说说", "another-member"))
        self.assertIsNone(other.usage_request)


class QuotaBoundaryTests(unittest.TestCase):
    def test_framework_embedded_probe_compiles_before_server_replay(self):
        from admin.framework_probe import PROBE
        compile(ast.parse(PROBE), '<embedded_framework_probe>', 'exec')

    def test_rollout_rejects_configuration_changes_and_unmatched_patch_bytes(self):
        valid = ''.join(
            f'diff --git a/{name} b/{name}\nnew file mode 100644\n--- /dev/null\n+++ b/{name}\n@@ -0,0 +1 @@\n+fixture\n'
            for name in sorted(SOURCE_PATHS)
        )
        check_patch(valid, hashlib.sha256(valid.encode()).hexdigest())
        forbidden = valid.replace('b/plugin/main.py', 'b/config/policy.json')
        with self.assertRaises(ValueError):
            check_patch(forbidden, hashlib.sha256(forbidden.encode()).hexdigest())
        with self.assertRaises(ValueError):
            check_patch(valid, '0' * 64)

    def test_protocol_is_parameterless_and_reader_cannot_select_raw_accounts(self):
        self.assertEqual(
            validate({"op": "account_quota"}),
            {"op": "account_quota", "window_minutes": 15},
        )
        for extra in [
            {"account_id": 1},
            {"target": "all"},
            {"refresh": True},
            {"sql": "SELECT secret"},
        ]:
            with self.assertRaises(Rejected):
                validate({"op": "account_quota", **extra})
        reader = Sources()
        with patch.object(reader, "sql", return_value=[row(), row()]) as sql:
            data = reader.execute({"op": "account_quota"})
        self.assertEqual(data["status"], "ok")
        statement = sql.call_args.args[0]
        self.assertIn("bot_diagnostics.account_quota", statement)
        self.assertIn("ORDER BY record_order", statement)
        self.assertNotIn("public.accounts", statement)
        self.assertNotIn("credentials", statement)

    def test_view_uses_existing_role_and_only_exposes_safe_projection(self):
        sql = view_sql()
        self.assertIn("security_barrier=true", sql)
        self.assertIn(
            "GRANT SELECT ON bot_diagnostics.account_quota TO bot_diagnostics_ro", sql
        )
        self.assertIn("REVOKE ALL ON bot_diagnostics.account_quota FROM PUBLIC", sql)
        self.assertNotIn("CREATE ROLE", sql)
        self.assertNotIn("ALTER ROLE", sql)
        self.assertNotIn("GRANT SELECT ON public.accounts", sql)
        projection = sql.split(") SELECT scope.", 1)[1]
        self.assertNotIn("account_identity", projection)
        self.assertNotIn("user_identity", projection)
        self.assertNotIn("credit_id", sql)
        self.assertIn("row_number() OVER (ORDER BY a.id)", sql)
        self.assertIn("codex_credits_snapshot", sql)

    def test_stale_or_missing_quota_never_calls_model_for_detailed_followup(self):
        for proof in [
            result([row(age=301), row(age=400)]),
            evidence("account_quota", {}, "unavailable"),
        ]:
            with patch("guard.account_quota.time.time", return_value=NOW):
                summary = format_account_quota(proof)
                reply, usages = advice_answer(
                    {"account_quota_evidence": proof, "quota_summary": summary},
                    None,
                    None,
                    model_call=lambda *args: self.fail("model must not be called"),
                )
            self.assertEqual(reply["answer"], summary)
            self.assertEqual(usages, [])

    def test_detailed_advice_is_one_bounded_call_with_authoritative_figures(self):
        proof = result()
        calls = []

        def model(data, *args):
            calls.append(data)
            return {
                "answer": "先缩小复现范围，再按模块定位根因，必要时提高推理强度。"
            }, {"input_tokens": 10}

        with patch("guard.account_quota.time.time", return_value=NOW):
            summary = format_account_quota(proof)
            reply, usages = advice_answer(
                {"account_quota_evidence": proof, "quota_summary": summary},
                None,
                None,
                model,
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(usages), 1)
        self.assertTrue(reply["answer"].startswith(summary))
        self.assertIn("具体建议", reply["answer"])
        self.assertEqual(reply["refs"], [proof["id"]])

    def test_unsupported_model_savings_counts_or_addresses_are_not_added(self):
        for prose in [
            "改用gpt-cheap能节省一半额度",
            "还能使用100次",
            "至少省三倍",
            "请连接203.0.113.9",
        ]:
            with (
                self.subTest(prose=prose),
                patch("guard.account_quota.time.time", return_value=NOW),
            ):
                proof = result()
                summary = format_account_quota(proof)
                reply, _ = advice_answer(
                    {"account_quota_evidence": proof, "quota_summary": summary},
                    None,
                    None,
                    lambda *args: ({"answer": prose}, {}),
                )
                self.assertEqual(reply["answer"], summary)

    def test_model_failure_keeps_local_quota_answer(self):
        def fail(*args):
            raise ModelFailure("transport")

        with patch("guard.account_quota.time.time", return_value=NOW):
            proof = result()
            summary = format_account_quota(proof)
            reply, usages = advice_answer(
                {"account_quota_evidence": proof, "quota_summary": summary},
                None,
                None,
                fail,
            )
        self.assertEqual(reply["answer"], summary)
        self.assertEqual(usages, [{}])


if __name__ == "__main__":
    unittest.main()
