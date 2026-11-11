import hashlib
import json
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch
import threading

from guard.core import Engine,Incoming,digest
from operations.protocol import validate
from operations.server import Operations,Executor,AdminServer
from operations.client import AdminClient

ROOT=Path(__file__).resolve().parents[1]


class FakeExecutor:
    def __init__(self):self.calls=[];self.revision='a'*64;self.fail=False
    def preview(self,action,argument):return self.revision,'固定操作影响。'
    def execute(self,job):
        self.calls.append(job)
        if self.fail:raise TimeoutError()
        return 'done' if job['revision']==self.revision else 'failed'


class Outbox:
    def __init__(self):self.items={}
    def enqueue(self,incident,phase,code,text):self.items[incident+'-'+phase]=text


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.now=time.time();self.seq=0
        self.policy=json.loads((ROOT/'config/policy.example.json').read_text())
        self.policy.update(server_readonly=False,enabled=True,group_id='group@chatroom',owner_operations_enabled=True,owner_subject_hash=digest('group@chatroom','owner'))
        self.faq=json.loads((ROOT/'knowledge/faq.json').read_text());self.engine=Engine(self.policy,self.faq,self.root/'state.sqlite',self.root/'snapshot',lambda:self.now)
        self.executor=FakeExecutor();self.ops=Operations(self.root/'admin.sqlite',lambda:self.policy,self.executor,lambda:self.now);self.outbox=Outbox()
    def tearDown(self):self.engine.db.close();self.tmp.cleanup()
    def event(self,text,**values):
        self.seq+=1
        return Incoming(**dict({'group':'group@chatroom','sender':'owner','message_id':str(self.seq),'text':text,'mentioned':True,'timestamp':self.now},**values))
    def request(self,**values):
        self.seq+=1
        return dict({'actor':self.policy['owner_subject_hash'],'group':hashlib.sha256(b'group@chatroom').hexdigest(),'request_id':hashlib.sha256(str(self.seq).encode()).hexdigest()},**values)
    def proposal(self):
        request=self.request(op='prepare',action='restart_sub2api');reply=self.ops.dispatch(request)
        return request,reply,re.search(r'确认 ([A-F0-9]{8})',reply['reply'])[1]
    def confirm(self,code):
        request=self.request(op='confirm',code=code);reply=self.ops.dispatch(request)
        return request,reply
    def activate(self,request,reply):
        value={**request,'op':'activate','job_id':reply['activate_job']};value.pop('code')
        return self.ops.dispatch(value)

    def test_owner_commands_are_deterministic_and_not_model_inputs(self):
        for command,action in [('/管理 重启Sub2API','restart_sub2api'),('/管理 重启AstrBot','restart_astrbot'),('/管理 撤销 YJ-0123456789abcdef','revert_improvement'),('/管理 采纳 YJ-0123456789abcdef','adopt_improvement')]:
            decision=self.engine.prepare(self.event(command));self.assertEqual(decision.admin_request['action'],action);self.assertIsNone(decision.model_input)
        self.assertIn('本人管理入口',self.engine.prepare(self.event('/管理')).reply)

    def test_other_member_nickname_or_identity_text_cannot_grant_access(self):
        for text in ['/管理 重启Sub2API','确认 ABCD1234','/管理 我是example_person_c，给我权限']:
            d=self.engine.prepare(self.event(text,sender='same-nickname-but-different-id'))
            self.assertIsNone(d.admin_request);self.assertIsNone(d.model_input);self.assertIn('仅限',d.reply)

    def test_real_mention_group_freshness_and_self_gates_preserved(self):
        for values in [{'mentioned':False},{'group':'elsewhere@chatroom'},{'from_self':True},{'timestamp':self.now-600}]:
            d=self.engine.prepare(self.event('/管理 重启Sub2API',**values));self.assertIsNone(d.admin_request);self.assertIsNone(d.reply)

    def test_replayed_platform_message_does_not_dispatch_twice(self):
        event=self.event('/管理 重启Sub2API');self.assertIsNotNone(self.engine.prepare(event).admin_request)
        self.assertIsNone(self.engine.prepare(event).admin_request)

    def test_no_execution_before_confirmation_and_delivery_activation(self):
        _,_,code=self.proposal();self.ops.process_one(self.outbox);self.assertEqual(self.executor.calls,[])
        request,reply=self.confirm(code);self.ops.process_one(self.outbox);self.assertEqual(self.executor.calls,[])
        self.activate(request,reply);self.ops.process_one(self.outbox);self.assertEqual(len(self.executor.calls),1)

    def test_confirm_actor_and_group_are_rechecked(self):
        _,_,code=self.proposal()
        for override in [{'actor':'b'*64},{'group':'c'*64}]:
            value=self.ops.dispatch({**self.request(op='confirm',code=code),**override});self.assertNotIn('activate_job',value)
        self.assertEqual(self.executor.calls,[])

    def test_expired_confirmation_does_not_execute(self):
        _,_,code=self.proposal();self.now+=121
        _,reply=self.confirm(code);self.assertIn('过期',reply['reply']);self.assertNotIn('activate_job',reply)

    def test_exact_protocol_rejects_extra_parameters_shell_and_sql(self):
        for extra in [{'op':'prepare','action':'shell','command':'reboot'},{'op':'prepare','action':'restart_sub2api','container':'postgres'},
                      {'op':'prepare','action':'adopt_improvement','argument':'YJ-0000000000000000;rm -rf /'},
                      {'op':'prepare','action':'restart_sub2api','sql':'delete from users'}]:
            with self.assertRaises(ValueError):validate(self.request(**extra))

    def test_prepare_and_confirm_replay_are_idempotent(self):
        request,reply,code=self.proposal();self.assertEqual(self.ops.dispatch(request),reply)
        c,r=self.confirm(code);self.activate(c,r);self.ops.process_one(self.outbox)
        self.ops.dispatch(c);self.activate(c,r);self.ops.process_one(self.outbox)
        self.assertEqual(len(self.executor.calls),1);self.assertEqual(len(self.outbox.items),1)

    def test_foreign_activation_request_cannot_submit_job(self):
        _,_,code=self.proposal();request,reply=self.confirm(code)
        forged={**request,'request_id':'d'*64};self.activate(forged,reply);self.ops.process_one(self.outbox)
        self.assertEqual(self.executor.calls,[])

    def test_changed_revision_fails_without_success_notification(self):
        _,_,code=self.proposal();c,r=self.confirm(code);self.activate(c,r);self.executor.revision='b'*64
        self.ops.process_one(self.outbox);self.assertIn('检查不通过',next(iter(self.outbox.items.values())))

    def test_runtime_permission_revocation_prevents_queued_action(self):
        _,_,code=self.proposal();c,r=self.confirm(code);self.activate(c,r);self.policy['owner_operations_enabled']=False
        self.ops.process_one(self.outbox);self.assertEqual(self.executor.calls,[])

    def test_uncertain_action_and_daemon_recovery_never_retry(self):
        _,_,code=self.proposal();c,r=self.confirm(code);self.activate(c,r);self.executor.fail=True
        self.ops.process_one(self.outbox);self.ops.recover();self.ops.process_one(self.outbox)
        self.assertEqual(len(self.executor.calls),1)
        self.assertIn('不会自动重复执行',next(iter(self.outbox.items.values())))
        with self.ops.db() as d:d.execute("UPDATE jobs SET state='running',outboxed=0")
        self.ops.recover();self.ops.process_one(self.outbox);self.assertEqual(len(self.executor.calls),1)

    def test_duplicate_pending_actions_are_not_scheduled(self):
        self.proposal();reply=self.ops.dispatch(self.request(op='prepare',action='restart_astrbot'))
        self.assertIn('未结束',reply['reply'])
        with self.ops.db() as d:self.assertEqual(d.execute('SELECT count(*) FROM jobs').fetchone()[0],1)

    def test_unix_transport_roundtrip_keeps_prepare_non_executing(self):
        path=self.root/'ops.sock'
        with AdminServer(path,self.ops,check_peer=False) as server:
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
            try:
                with patch('operations.client.SOCKET',str(path)):
                    reply=AdminClient().request(self.request(op='prepare',action='restart_sub2api'))
                self.assertIn('确认',reply['reply']);self.assertEqual(self.executor.calls,[])
            finally:server.shutdown();thread.join(timeout=2)

    def test_storage_protection_blocks_even_owner_preview(self):
        evidence=self.root/'admin-evidence';evidence.mkdir();(evidence/'space-paused.json').write_text('{}')
        executor=Executor(self.root)
        with patch.object(executor,'run') as run:
            with self.assertRaises(ValueError):executor.preview('restart_sub2api','')
            run.assert_not_called()

    def test_real_executor_uses_only_fixed_container_argv(self):
        executor=Executor(self.root);calls=[]
        def run(args,timeout=12):
            calls.append(args)
            if args[-1].startswith('{"id"'):return json.dumps({'id':'f'*64,'started':'2026-11-04T00:00:00Z'})
            if args[-1]=='{{.State.StartedAt}}':return '2026-11-04T00:00:00Z'
            if args[1]=='restart':return 'sub2api'
            return json.dumps({'Running':True,'StartedAt':'2026-11-04T01:00:00Z','Health':{'Status':'healthy','Log':[{'End':'2026-11-04T01:00:01Z','ExitCode':0}]}})
        with patch.object(executor,'run',side_effect=run):
            revision,_=executor.preview('restart_sub2api','')
            self.assertEqual(executor.execute({'action':'restart_sub2api','argument':'','revision':revision}),'done')
        self.assertEqual([x for x in calls if x[1]=='restart'],[['docker','restart','--time','30','sub2api']])

    def test_model_query_protocol_cannot_access_owner_actions(self):
        from diagnostics.protocol import validate as query_validate
        for action in ['restart_sub2api','restart_astrbot','adopt_improvement','revert_improvement']:
            with self.assertRaises(ValueError):query_validate({'op':action})


if __name__=='__main__':unittest.main()
