from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from diagnostics.monitor import Monitor
from diagnostics.store import Store
from diagnostics.notify import NativeNotifier


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.now=1000000.0
        self.store=Store(Path(self.folder.name)/'monitor.sqlite',lambda:self.now);self.monitor=Monitor(self.store)
    def tearDown(self):self.store.close();self.folder.cleanup()
    def phases(self):return [x[0] for x in self.store.db.execute('SELECT phase FROM notifications ORDER BY at,key')]
    def test_service_two_failures_deduplicates_and_requires_two_recovery_samples(self):
        self.monitor.rule('gateway',True,samples=2);self.assertEqual(self.phases(),[])
        self.now+=60;self.monitor.rule('gateway',True,samples=2);self.assertEqual(self.phases(),['open'])
        key=self.store.pending()[0]['key'];self.store.update_send(key,'accepted')
        for _ in range(4):self.monitor.rule('gateway',True,samples=2)
        self.assertEqual(self.phases(),['open'])
        self.monitor.rule('gateway',False,samples=2);self.assertEqual(self.phases(),['open'])
        self.now+=60;self.monitor.rule('gateway',False,samples=2);self.assertEqual(self.phases(),['open','recovery'])
    def test_unsent_offline_fault_is_summarized_after_verified_recovery(self):
        self.monitor.rule('disk',True);self.monitor.rule('disk',False);self.now+=60;self.monitor.rule('disk',False)
        pending=self.store.pending();self.assertEqual(len(pending),1);self.assertEqual(pending[0]['phase'],'summary')
        self.assertIn('曾在',pending[0]['text'])
    def test_unknown_evidence_never_recovers_an_active_incident(self):
        self.monitor.rule('gateway',True)
        for _ in range(3):self.monitor.rule('gateway',None)
        self.assertEqual(self.phases(),['open'])
    def test_recovery_drops_unsent_upgrade_after_open_was_accepted(self):
        self.monitor.rule('disk',True,severity=1)
        self.store.update_send(self.store.pending()[0]['key'],'accepted')
        self.now+=60;self.monitor.rule('disk',True,severity=2)
        self.monitor.rule('disk',False);self.now+=60;self.monitor.rule('disk',False)
        self.assertEqual([x['phase'] for x in self.store.pending()],['recovery'])
        self.assertEqual(self.store.db.execute("SELECT state FROM notifications WHERE phase='upgrade'").fetchone()[0],'superseded')
    def test_recovery_retains_unknown_upgrade_for_original_receipt_lookup(self):
        sender=self.sender();self.monitor.rule('disk',True,severity=1)
        self.store.update_send(self.store.pending()[0]['key'],'accepted')
        self.now+=60;self.monitor.rule('disk',True,severity=2)
        key=self.store.pending()[0]['key'];self.store.update_send(key,'submitting','send-one','native-one')
        self.monitor.rule('disk',False);self.now+=60;self.monitor.rule('disk',False)
        calls=[]
        def run(args):
            calls.append(args)
            if args==['status']:return {'serverId':'native-one'}
            return {'id':'send-one','accountId':'account-one','destination':'group@chatroom','state':'accepted'}
        with patch.object(sender,'online',return_value=True),patch.object(sender,'run',side_effect=run):
            sender.deliver(self.store)
        self.assertIn(['messages','sends','get','send-one'],calls)
        self.assertFalse(any(x[:2]==['messages','send'] for x in calls))
        self.assertEqual([x['phase'] for x in self.store.pending()],['recovery'])
    def test_cpu_needs_actual_five_minute_observed_duration(self):
        for value in range(5):self.now=1000000+value*60;self.monitor.rule('cpu',True,duration=300)
        self.assertEqual(self.phases(),[]);self.now+=60;self.monitor.rule('cpu',True,duration=300)
        self.assertEqual(self.phases(),['open'])
    def test_severity_upgrade_happens_only_once_per_incident(self):
        self.monitor.rule('disk',True,severity=1);self.now+=60
        for _ in range(3):self.monitor.rule('disk',True,severity=2)
        self.assertEqual(self.phases(),['open','upgrade'])
    def test_gateway_rate_requires_full_window_and_ten_samples(self):
        sample={'traffic':{'gateway':{'coverage_complete':False,'observed_completed_requests':100,'http_5xx_rate':1}}}
        for _ in range(3):self.monitor.observe(sample);self.now+=60
        self.assertEqual(self.phases(),[])
        sample['traffic']['gateway'].update(coverage_complete=True,observed_completed_requests=9)
        for _ in range(3):self.monitor.observe(sample);self.now+=60
        self.assertEqual(self.phases(),[])
        sample['traffic']['gateway']['observed_completed_requests']=10
        self.monitor.observe(sample);self.now+=60;self.monitor.observe(sample)
        self.assertEqual(self.phases(),['open'])
    def test_offline_sender_does_not_submit_messages(self):
        sender=NativeNotifier(self.folder.name)
        with patch.object(sender,'online',return_value=False),patch.object(sender,'run') as run:
            self.monitor.rule('gateway',True);sender.deliver(self.store);run.assert_not_called()

    def sender(self):
        root=Path(self.folder.name)
        (root/'admin-evidence').mkdir(exist_ok=True);(root/'config').mkdir(exist_ok=True)
        (root/'admin-evidence/native-account.json').write_text(json.dumps({'id':'account-one'}))
        (root/'admin-evidence/target-group.json').write_text(json.dumps({'id':'group@chatroom'}))
        (root/'config/policy.json').write_text(json.dumps({'group_id':'group@chatroom','monitor_enabled':True}))
        return NativeNotifier(root)

    def test_sender_submits_once_then_only_polls_same_receipt(self):
        sender=self.sender();calls=[]
        def run(args):
            calls.append(args)
            if args==['status']:return {'serverId':'native-one'}
            return {'id':'send-one','accountId':'account-one','destination':'group@chatroom','state':'queued' if args[:2]==['messages','send'] else 'accepted'}
        self.monitor.rule('gateway',True)
        with patch.object(sender,'online',return_value=True),patch.object(sender,'run',side_effect=run):
            sender.deliver(self.store);sender.deliver(self.store);sender.deliver(self.store)
        self.assertEqual(sum(x[:2]==['messages','send'] for x in calls),1)
        self.assertEqual(sum(x[:3]==['messages','sends','get'] for x in calls),1)
        self.assertEqual(self.store.pending(),[])

    def test_unknown_submission_only_looks_up_original_key(self):
        sender=self.sender();calls=[]
        def run(args):
            calls.append(args)
            if args==['status']:return {'serverId':'native-one'}
            if args[:2]==['messages','send']:return None
            return {'id':'send-one','accountId':'account-one','destination':'group@chatroom','state':'accepted'}
        self.monitor.rule('gateway',True);key=self.store.pending()[0]['key']
        with patch.object(sender,'online',return_value=True),patch.object(sender,'run',side_effect=run):
            sender.deliver(self.store);sender.deliver(self.store)
        self.assertEqual(sum(x[:2]==['messages','send'] for x in calls),1)
        self.assertTrue(any(x[:3]==['messages','sends','lookup'] and x[-1]==key for x in calls))

    def test_changed_native_identity_does_not_resubmit_old_notification(self):
        sender=self.sender();self.monitor.rule('gateway',True);key=self.store.pending()[0]['key']
        self.store.update_send(key,'submitting',server_id='native-old')
        with patch.object(sender,'online',return_value=True),patch.object(sender,'run',return_value={'serverId':'native-new'}) as run:
            sender.deliver(self.store)
        self.assertEqual(run.call_count,1);self.assertEqual(self.store.pending()[0]['state'],'needs_review')

    def test_sampling_gap_does_not_prove_continuous_cpu_exhaustion(self):
        sample={'resources':{'cpu_percent':99}}
        self.monitor.observe(sample);self.now+=360;self.monitor.observe(sample)
        self.assertFalse(any(x['code']=='cpu' for x in self.store.pending()))

    def test_critical_space_does_not_persist_repeating_metric_samples(self):
        self.monitor.observe({'resources':{'disk_available_percent':19}})
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM samples').fetchone()[0],0)


if __name__=='__main__':unittest.main()
