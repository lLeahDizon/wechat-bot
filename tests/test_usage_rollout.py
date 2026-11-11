import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('usage_rollout',ROOT/'admin/usage_rollout.py')
rollout=importlib.util.module_from_spec(spec);spec.loader.exec_module(rollout)
view_spec=importlib.util.spec_from_file_location('provision_diagnostics',ROOT/'admin/provision_diagnostics.py')
provision=importlib.util.module_from_spec(view_spec);view_spec.loader.exec_module(provision)


class UsageRolloutTests(unittest.TestCase):
    def test_range_deploy_requires_a_matching_private_current_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);diagnostics=root/'diagnostics';evidence=root/'admin-evidence'
            diagnostics.mkdir();evidence.mkdir()
            archive=diagnostics/'rollback-usage-range-20261106T120000Z.tar.gz';archive.write_bytes(b'current-source')
            receipt={'archive':archive.name,'archive_sha256':rollout.hashlib.sha256(archive.read_bytes()).hexdigest(),
                     'contains_secrets':False,'contains_wechat_data':False}
            (evidence/'usage-range-predeploy-snapshot.json').write_text(json.dumps(receipt))
            rollout.verify_range_snapshot(root)
            archive.write_bytes(b'changed')
            with self.assertRaisesRegex(RuntimeError,'usage_range_snapshot_unavailable'):
                rollout.verify_range_snapshot(root)

    def test_subject_is_stable_group_scoped_and_does_not_contain_identity(self):
        digest,subject=rollout.usage_subject('group-a','stable-member-id')
        self.assertEqual((digest,subject),rollout.usage_subject('group-a','stable-member-id'))
        self.assertNotEqual(subject,rollout.usage_subject('group-b','stable-member-id')[1])
        self.assertNotIn('stable-member-id',digest+subject)
        self.assertRegex(subject,r'^member_[a-f0-9]{16}$')

    def test_key_name_lookup_is_sql_quoted_and_rejects_control_characters(self):
        self.assertEqual(rollout.sql_literal("a'b"),"'a''b'")
        with self.assertRaises(ValueError):rollout.sql_literal('name\nother')

    def test_public_alias_sharing_key_name_is_not_hidden_as_a_key_label(self):
        profile='a'*64;subject='member_'+profile[:16]
        key_rule=rollout.hidden_rule('example_person_c');other_rule=rollout.hidden_rule('aa')
        policy={'member_profiles':{},'usage_public_aliases':{'flower':['example_person_a'],'gentle':['example_person_b']},
                'usage_public_names':{},'hidden_label_rules':[key_rule,other_rule]}
        rollout.configure_owner_usage(policy,profile,subject,['example_person_c','example_person_c'],'example_person_c')
        self.assertEqual(policy['usage_public_names'][subject],'example_person_c')
        self.assertEqual(policy['usage_public_aliases'][subject],['example_person_c','example_person_c'])
        self.assertEqual(policy['hidden_label_rules'],[other_rule])
        self.assertEqual(policy['member_profiles'][profile]['usage_target'],subject)
    def test_alias_collision_with_another_member_fails_closed(self):
        policy={'usage_public_aliases':{'flower':['example_person_a'],'gentle':['example_person_b']}}
        with self.assertRaisesRegex(RuntimeError,'usage_alias_collision'):
            rollout.configure_owner_usage(policy,'a'*64,'member_'+'a'*16,['example_person_b','example_person_c'],'example_person_c')

    def test_generated_database_views_use_only_numeric_ids_and_opaque_subjects(self):
        subject='member_0123456789abcdef'
        sql=provision.views({'flower':11,'gentle':12,subject:13})
        self.assertIn('(13::bigint,\'member_0123456789abcdef\'::text)',sql)
        self.assertIn('bot_diagnostics.allowed_quota_usage',sql)
        self.assertIn('u.actual_cost',sql)
        self.assertNotIn("u.created_at>=now()-interval '24 hours'",sql)
        self.assertNotRegex(sql,r'k\.(?:key|name)\b')
        with self.assertRaises(ValueError):provision.views({'member_bad-subject':13})

    def test_snapshot_keeps_two_private_code_archives_and_records_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'task.json').write_text(json.dumps({'task':rollout.TASK}))
            for directory in ['code/pkg','query-client/diagnostics','plugins/astrbot_plugin_sub2api_guard',
                              'config','compose','admin-evidence','diagnostics']:
                (root/directory).mkdir(parents=True,exist_ok=True)
            for index in range(12):(root/f'code/pkg/file-{index}.py').write_text('source')
            for path in [root/'config/policy.json',root/'compose.json',root/'query-client/diagnostics/client.py',
                         root/'plugins/astrbot_plugin_sub2api_guard/main.py',root/'admin-evidence/query-db-setup.json',
                         root/'admin-evidence/deployed-source-sha256.json']:
                path.write_text('{}')
            now=time.time()
            old=root/'diagnostics/rollback-code.tar.gz';old.write_bytes(b'old')
            recent=root/'diagnostics/rollback-feedback.tar.gz';recent.write_bytes(b'recent')
            os.utime(old,(now-15*86400,)*2);os.utime(recent,(now-86400,)*2)
            with patch.object(rollout.shutil,'disk_usage',return_value=SimpleNamespace(total=100*1024**3,free=80*1024**3)):
                result=rollout.snapshot(root,now)
            receipt=json.loads((root/'admin-evidence/usage-predeploy-snapshot.json').read_text())
            archive=root/'diagnostics'/receipt['archive']
            self.assertTrue(result['snapshot_created']);self.assertTrue(archive.is_file())
            self.assertEqual(archive.stat().st_mode & 0o777,0o600)
            self.assertEqual(rollout.hashlib.sha256(archive.read_bytes()).hexdigest(),receipt['archive_sha256'])
            self.assertFalse(old.exists());self.assertTrue(recent.exists())
            self.assertNotIn('key_name',receipt)


if __name__=='__main__':unittest.main()
