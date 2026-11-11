import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from guard.model import ModelFailure

spec=importlib.util.spec_from_file_location('weekly',Path(__file__).resolve().parents[1]/'admin/weekly.py')
weekly=importlib.util.module_from_spec(spec);spec.loader.exec_module(weekly)


class WeeklyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.state=self.root/'state.sqlite';self.archives=self.root/'summaries';self.archives.mkdir()
        self.faq=self.root/'faq.json';self.faq.write_text(json.dumps([{'id':'scope','answer':'仅本服务答疑'}]))
        db=sqlite3.connect(self.state)
        db.execute('CREATE TABLE knowledge(topic,note,at)')
        db.execute('INSERT INTO knowledge VALUES (?,?,?)',('scope','仅本服务答疑',time.time()))
        db.execute('CREATE TABLE private_chats(body)');db.execute("INSERT INTO private_chats VALUES ('never-read-this-transcript')")
        db.commit();db.close()
        self.patches=patch.multiple(weekly,STATE=self.state,ARCHIVES=self.archives,FAQ=self.faq)
        self.patches.start()

    def tearDown(self):self.patches.stop();self.temp.cleanup()

    def test_one_summary_attempt_per_week_even_after_failure(self):
        with patch.object(weekly,'call',side_effect=ModelFailure('transport')) as caller:
            self.assertEqual(weekly.run()['status'],'failed')
            self.assertEqual(weekly.run()['status'],'already_attempted')
            self.assertEqual(caller.call_count,1)

    def test_summary_input_does_not_include_chat_transcripts(self):
        with patch.object(weekly,'call',return_value=({'scope':'service','answer':'本周仅有服务使用说明。'},{})) as caller:
            self.assertEqual(weekly.run()['status'],'summarized')
            self.assertNotIn('never-read-this-transcript',json.dumps(caller.call_args.args[0]))
            body=json.loads(next(p for p in self.archives.glob('weekly-*.json') if '.attempt.' not in p.name).read_text())
            self.assertTrue(body['historical'])
            self.assertFalse(body['runtime_faq_auto_promoted'])

    def test_sensitive_summary_is_not_saved(self):
        with patch.object(weekly,'call',return_value=({'scope':'service','answer':'地址203.0.113.12'},{})):
            self.assertEqual(weekly.run()['status'],'failed')
            self.assertEqual(len(list(self.archives.glob('weekly-*.json'))),1)

    def test_personal_usage_subject_is_not_archived_as_anonymous_faq(self):
        with patch.object(weekly,'call',return_value=({'scope':'service','answer':'example_person_b今天用了100Token。'},{})):
            self.assertEqual(weekly.run()['status'],'failed')
            self.assertFalse(any(p.name.endswith('.json') and '.attempt.' not in p.name for p in self.archives.iterdir()))

    def test_improvement_summary_uses_existing_weekly_call_without_private_payload(self):
        db=sqlite3.connect(self.state)
        db.execute('CREATE TABLE improvements(kind,summary,status,updated,payload)')
        db.execute('INSERT INTO improvements VALUES (?,?,?,?,?)',('reply_style','先给结论，再列步骤','applied',time.time(),'never-forward-private-payload'))
        db.commit();db.close()
        with patch.object(weekly,'call',return_value=({'scope':'service','answer':'本周优化了答复的组织方式。'},{})) as caller:
            self.assertEqual(weekly.run()['status'],'summarized')
            data=caller.call_args.args[0]
            self.assertEqual(data['improvement_records'],[{'kind':'reply_style','summary':'先给结论，再列步骤','status':'applied'}])
            self.assertNotIn('never-forward-private-payload',json.dumps(data))
            self.assertEqual(weekly.run()['status'],'already_attempted');self.assertEqual(caller.call_count,1)


if __name__=='__main__':unittest.main()
