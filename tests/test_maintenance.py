import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

spec=importlib.util.spec_from_file_location('maintenance',Path(__file__).resolve().parents[1]/'admin/maintenance.py')
maintenance=importlib.util.module_from_spec(spec);spec.loader.exec_module(maintenance)


class MaintenanceTests(unittest.TestCase):
    def test_storage_thresholds(self):
        g=maintenance.GIB
        self.assertEqual(maintenance.space_policy(g-1,80*g,100*g),'normal')
        self.assertEqual(maintenance.space_policy(g,80*g,100*g),'warning')
        self.assertEqual(maintenance.space_policy(2*g,80*g,100*g),'critical')
        self.assertEqual(maintenance.space_policy(0,19*g,100*g),'critical')
        self.assertEqual(maintenance.space_policy(0,80*g,100*g,.04),'critical')

    def test_cleanup_never_touches_native_profile_or_unowned_names(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'task.json').write_text(json.dumps({'task':maintenance.TASK}))
            for name in ['logs','native-data','archives']:(root/name).mkdir()
            now=time.time()
            old=root/'logs/events-2026-01-01.jsonl';old.write_text('anonymous');os.utime(old,(now-8*86400,)*2)
            unowned=root/'logs/not-our-file';unowned.write_text('keep');os.utime(unowned,(now-99*86400,)*2)
            profile=root/'native-data/history.sqlite';profile.write_text('keep');os.utime(profile,(now-99*86400,)*2)
            (root/'archives/backups').mkdir()
            for index in range(4):
                path=root/f'archives/backups/backup-2026010{index+1}T000000Z.tar.gz';path.write_text('backup')
                os.utime(path,(now-index*86400,)*2)
            maintenance.cleanup(root,now)
            self.assertFalse(old.exists());self.assertTrue(unowned.exists());self.assertTrue(profile.exists())
            self.assertEqual(len(list((root/'archives/backups').glob('backup-*'))),2)

    def test_symlinks_are_not_followed_or_deleted(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'task.json').write_text(json.dumps({'task':maintenance.TASK}))
            (root/'archives').mkdir();(root/'archives/backups').mkdir();(root/'native-data').mkdir()
            external=root/'outside';external.write_bytes(b'x'*100)
            (root/'native-data/link').symlink_to(external)
            (root/'archives/backups/backup-20260101T000000Z.tar.gz').symlink_to(external)
            maintenance.cleanup(root)
            self.assertTrue(external.exists())
            self.assertEqual(maintenance.measure(root),0)

    def test_summary_compression_does_not_follow_existing_destination_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'task.json').write_text(json.dumps({'task':maintenance.TASK}))
            summaries=root/'archives/summaries';summaries.mkdir(parents=True)
            source=summaries/'weekly-2026-W01.json';source.write_text('anonymous')
            old=time.time()-8*86400;os.utime(source,(old,old))
            protected=root/'outside';protected.write_text('keep')
            source.with_suffix('.json.gz').symlink_to(protected)
            maintenance.cleanup(root)
            self.assertEqual(protected.read_text(),'keep')
            self.assertTrue(source.exists())

    def test_wrong_task_marker_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'task.json').write_text(json.dumps({'task':'other-task'}))
            with self.assertRaises(RuntimeError):maintenance.cleanup(root)

    def test_cleanup_rejects_symlinked_archive_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'task.json').write_text(json.dumps({'task':maintenance.TASK}))
            (root/'archives').mkdir();external=root/'external';external.mkdir()
            protected=external/'backup-20260101T000000Z.tar.gz';protected.write_text('keep')
            old=time.time()-100*86400;os.utime(protected,(old,old))
            (root/'archives/backups').symlink_to(external,target_is_directory=True)
            with self.assertRaises(RuntimeError):maintenance.cleanup(root)
            self.assertTrue(protected.exists())

    def test_diagnostics_rollback_cleanup_does_not_follow_parent_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'task.json').write_text(json.dumps({'task':maintenance.TASK}))
            external=root/'external';external.mkdir();protected=external/'rollback-code.tar.gz';protected.write_text('keep')
            old=time.time()-100*86400;os.utime(protected,(old,old));(root/'diagnostics').symlink_to(external,target_is_directory=True)
            with self.assertRaises(RuntimeError):maintenance.cleanup(root)
            self.assertEqual(protected.read_text(),'keep')

    def test_usage_code_rollback_obeys_fourteen_day_retention(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'task.json').write_text(json.dumps({'task':maintenance.TASK}))
            diagnostics=root/'diagnostics';diagnostics.mkdir()
            now=time.time();old=diagnostics/'rollback-usage-20261001T000000Z.tar.gz';old.write_text('old')
            recent=diagnostics/'rollback-usage-20261105T000000Z.tar.gz';recent.write_text('recent')
            os.utime(old,(now-15*86400,)*2)
            os.utime(recent,(now-86400,)*2)
            maintenance.cleanup(root,now)
            self.assertFalse(old.exists());self.assertTrue(recent.exists())


if __name__=='__main__':unittest.main()
