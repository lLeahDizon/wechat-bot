import asyncio
import datetime as dt
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import types
import unittest

from guard.core import Engine,Incoming,digest,unsafe,normalize
from guard.reminders import Reminders,parse_spec,Invalid,GRACE,BJT
from guard.reminder_delivery import ReminderDelivery

ROOT=Path(__file__).resolve().parents[1]


class ReminderFixture:
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.seq=0
        self.now=dt.datetime(2026,11,4,20,0,tzinfo=BJT).timestamp()
        self.group='fixture@chatroom';self.owner=digest(self.group,'owner');self.other=digest(self.group,'other');self.gentle=digest(self.group,'wxid_gentle_fixture')
        self.config=json.loads((ROOT/'config/policy.example.json').read_text());self.config.update(enabled=True,group_id=self.group,reminders_enabled=True,owner_subject_hash=self.owner,member_profiles={self.gentle:{'usage_target':'gentle','tone':'gentle'}})
        self.engine=Engine(self.config,json.loads((ROOT/'knowledge/faq.json').read_text()),self.root/'state.sqlite',self.root/'status',lambda:self.now)
        self.store=self.engine.reminders;self.grp=digest(self.group)
    def tearDown(self):self.engine.db.close();self.tmp.cleanup()
    def request(self,text,actor=None):
        self.seq+=1;return self.store.request(text,actor or self.owner,self.grp,digest(str(self.seq)),self.config)
    def propose(self,text,actor=None):
        reply=self.request(text,actor);self.assertIn('未确认不保存',reply)
        return re.search(r'TX-[A-F0-9]{8}',reply)[0],re.search(r'确认提醒 ([A-F0-9]{8})',reply)[1]
    def create(self,text='明天6点提醒我起床',actor=None):
        identifier,code=self.propose(text,actor);reply=self.request('确认提醒 '+code,actor);self.assertIn('已保存',reply);return identifier


class ReminderTests(ReminderFixture,unittest.TestCase):
    def test_exact_initial_request_binds_xiaoyu_and_beijing_tomorrow(self):
        identifier=self.create('提醒example_person_b明天6点起来刷token')
        row=self.store.visible(identifier,self.owner,self.grp,self.config)
        self.assertEqual(row['next_at'],dt.datetime(2026,11,5,6,tzinfo=BJT).timestamp())
        self.assertEqual(row['spec']['text'],'起来刷token');self.assertEqual(row['spec']['target'],self.gentle)
        self.assertNotIn('wxid_',json.dumps(row));self.assertEqual(row['actor'],self.owner)

    def test_fixed_chinese_daily_and_weekly_time(self):
        for text,mode,hour,minute,weekday in [('每天晚上十点半提醒我休息','daily',22,30,None),('每周五18:15提醒大家收尾','weekly',18,15,4),('2026年11月5日06:00提醒我起床','once',6,0,None)]:
            spec=parse_spec(text,self.owner,self.config,self.now)
            self.assertEqual((spec['mode'],spec['hour'],spec['minute'],spec['weekday']),(mode,hour,minute,weekday))
        spec=parse_spec('一分钟后提醒我喝水',self.owner,self.config,self.now);self.assertEqual(spec['at'],self.now+60)

    def test_invalid_or_ambiguous_time_never_silently_changes_schedule(self):
        for text in ['今天6点提醒我起床','明天25点提醒我起床','每周18点提醒我收尾','每小时提醒我喝水','明天晚上12点提醒我起床','2026年2月30日6点提醒我起床','每天明天6点提醒我起床','明天6点提醒我工作直到周五']:
            with self.assertRaises(Invalid,msg=text):parse_spec(text,self.owner,self.config,self.now)

    def test_confirmation_is_required_and_bound_to_actor_group_and_expiry(self):
        identifier,code=self.propose('明天6点提醒我起床')
        self.assertIsNone(self.store.visible(identifier,self.owner,self.grp,self.config))
        self.assertNotIn('已保存',self.request('确认提醒 '+code,self.other))
        self.assertNotIn('已保存',self.store.confirm(code,self.owner,digest('othergroup'),'x',self.config))
        self.now+=121;self.assertNotIn('已保存',self.request('确认提醒 '+code))
        self.assertIsNone(self.store.visible(identifier,self.owner,self.grp,self.config))

    def test_new_proposal_supersedes_only_same_member_and_confirmation_is_idempotent(self):
        _,old=self.propose('明天6点提醒我起床');identifier,new=self.propose('明天7点提醒我起床')
        self.assertNotIn('已保存',self.request('确认提醒 '+old))
        self.request('确认提醒 '+new);self.assertIn('不会重复',self.request('确认提醒 '+new))
        self.assertEqual(self.engine.db.execute('select count(*) from reminders').fetchone()[0],1)
        self.assertIsNotNone(self.store.visible(identifier,self.owner,self.grp,self.config))

    def test_members_manage_only_own_and_owner_can_manage_all(self):
        identifier=self.create(actor=self.other)
        self.assertNotIn(identifier,self.request('我的提醒',self.owner));self.assertIn(identifier,self.request('我的提醒',self.other))
        foreign=digest(self.group,'stranger');self.assertIn('未找到',self.request('查看提醒 '+identifier,foreign))
        self.assertIn('未找到',self.request('取消提醒 '+identifier,foreign));self.assertIn('仅',self.request('所有提醒',self.other))
        self.assertIn(identifier,self.request('所有提醒',self.owner))
        reply=self.request('修改提醒 '+identifier+' 为 明天7点',self.owner);code=re.search(r'确认提醒 ([A-F0-9]{8})',reply)[1]
        self.assertIn('已保存',self.request('确认提醒 '+code,self.owner))
        updated=self.store.visible(identifier,self.other,self.grp,self.config);self.assertEqual(updated['actor'],self.other)
        self.assertEqual(updated['spec']['text'],'起床');self.assertEqual(updated['spec']['target'],self.other)

    def test_changed_version_invalidates_an_earlier_edit_preview(self):
        identifier=self.create(actor=self.other)
        first=self.request('修改提醒 '+identifier+' 为 明天7点',self.other);code=re.search(r'确认提醒 ([A-F0-9]{8})',first)[1]
        second=self.request('修改提醒 '+identifier+' 为 明天8点',self.owner);owner_code=re.search(r'确认提醒 ([A-F0-9]{8})',second)[1]
        self.request('确认提醒 '+owner_code,self.owner)
        self.assertIn('已改变',self.request('确认提醒 '+code,self.other))

    def test_cancel_is_confirmed_and_erases_content(self):
        identifier=self.create();reply=self.request('取消提醒 '+identifier)
        self.assertEqual(self.store.visible(identifier,self.owner,self.grp,self.config)['state'],'active')
        code=re.search(r'确认提醒 ([A-F0-9]{8})',reply)[1];self.assertIn('已取消',self.request('确认提醒 '+code))
        row=self.store.visible(identifier,self.owner,self.grp,self.config);self.assertEqual(row['spec'],{});self.assertIsNone(row['next_at'])

    def test_cancel_after_claim_before_submit_invalidates_occurrence(self):
        identifier=self.create();self.now=dt.datetime(2026,11,5,6,tzinfo=BJT).timestamp()
        item=self.store.claim_due(self.grp,'server-one');reply=self.request('取消提醒 '+identifier)
        code=re.search(r'确认提醒 ([A-F0-9]{8})',reply)[1];self.assertIn('已取消',self.request('确认提醒 '+code))
        self.assertFalse(self.store.current(item))

    def test_engine_uses_no_model_and_preserves_mention_freshness_group_and_privacy(self):
        def event(mid,text='明天6点提醒我起床',**changes):return Incoming(**dict({'group':self.group,'sender':'owner','message_id':mid,'text':text,'mentioned':True,'timestamp':self.now},**changes))
        d=self.engine.prepare(event('create'));self.assertEqual(d.intent,'reminder');self.assertIsNone(d.model_input);self.assertIsNone(d.admin_request)
        for i,changes in enumerate([{'mentioned':False},{'group':'other@chatroom'},{'from_self':True},{'timestamp':self.now-600}]):self.assertIsNone(self.engine.prepare(event(str(i),**changes)).reply)
        d=self.engine.prepare(event('secret','明天6点提醒我 sk-abcdefghijklmnopqrstuvw'));self.assertEqual(d.category,'sensitive_input')
        self.assertIsNone(self.engine.prepare(event('create')).reply)
        self.assertIsNone(self.store.request('服务器会发告警提醒吗',self.owner,self.grp,'x',self.config))
        self.assertIsNone(self.store.request('建议你提醒成员先描述报错',self.owner,self.grp,'y',self.config))

    def test_restart_keeps_active_schedules_and_no_occurrence_is_claimed_twice(self):
        identifier=self.create();self.store=Reminders(self.engine.db,lambda:self.now)
        self.now=dt.datetime(2026,11,5,6,tzinfo=BJT).timestamp()
        item=self.store.claim_due(self.grp,'server-one');self.assertEqual(item['id'],identifier)
        self.assertIsNone(self.store.claim_due(self.grp,'server-one'))
        self.assertEqual(self.store.visible(identifier,self.owner,self.grp,self.config)['spec'],{})

    def test_daily_catchup_is_single_and_old_occurrences_are_skipped(self):
        identifier=self.create('每天6点提醒我起床');self.now=dt.datetime(2026,11,5,6,10,tzinfo=BJT).timestamp()
        item=self.store.claim_due(self.grp,'server-one');self.assertNotIn('skipped',item)
        self.assertEqual(self.store.visible(identifier,self.owner,self.grp,self.config)['next_at'],dt.datetime(2026,11,6,6,tzinfo=BJT).timestamp())
        self.now+=3*86400;self.assertTrue(self.store.claim_due(self.grp,'server-one')['skipped'])
        self.assertGreater(self.store.visible(identifier,self.owner,self.grp,self.config)['next_at'],self.now)
        self.assertIsNone(self.store.claim_due(self.grp,'server-one'))

    def test_missed_once_never_bursts_on_reconnect_and_terminal_metadata_is_pruned(self):
        identifier=self.create();self.now=dt.datetime(2026,11,5,7,tzinfo=BJT).timestamp()
        self.assertTrue(self.store.claim_due(self.grp,'server-one')['skipped']);self.assertIsNone(self.store.claim_due(self.grp,'server-one'))
        self.assertIn('已错过',self.request('查看提醒 '+identifier))
        self.now+=8*86400;self.store.purge();self.assertIsNone(self.store.visible(identifier,self.owner,self.grp,self.config))

    def test_active_long_term_schedule_is_not_pruned_and_pending_text_expires(self):
        identifier=self.create('每天6点提醒我起床');self.propose('明天8点提醒我清理材料')
        self.now+=8*86400;self.store.purge()
        self.assertIsNotNone(self.store.visible(identifier,self.owner,self.grp,self.config))
        self.assertEqual(self.engine.db.execute('select count(*) from reminder_proposals').fetchone()[0],0)


class Client:
    closed=False;account_id='fixture-account';prefix='/accounts/fixture-account'
    def __init__(self,group):self.group=group;self.posts=[];self.reads=[];self.fail=False;self.online=True;self.server_id='server-one';self.recipient='wxid_gentle_fixture'
    async def status(self):return {'serverId':self.server_id}
    async def account(self):return {'authenticated':True,'connection':'online' if self.online else 'offline'}
    async def _request(self,*args,**kwargs):return {'items':[{'username':self.recipient}], 'nextCursor':None}
    async def prepare_group_mentions(self,*args):return None
    def receipt(self):return {'id':'receipt-one','state':'accepted','accountId':self.account_id,'destination':self.group}
    async def send_text(self,group,text,key,mentions):
        self.posts.append((group,text,key,mentions))
        if self.fail:raise TimeoutError()
        return self.receipt()
    async def send_by_key(self,key):self.reads.append(key);return self.receipt()
    async def send(self,identifier):self.reads.append(identifier);return self.receipt()


class DeliveryTests(ReminderFixture,unittest.IsolatedAsyncioTestCase):
    def adapter(self):
        client=Client(self.group);binding={}
        def bind(value):
            if binding and binding['server_id']!=value:raise ValueError('changed_server')
            binding['server_id']=value
        return types.SimpleNamespace(client=client,stopped=False,bot_self_id='bot-fixture',state=types.SimpleNamespace(bind_server=bind),meta=lambda:types.SimpleNamespace(id=self.config['bot_instance_id'],name='wechat_native'))

    async def test_due_send_uses_one_stable_key_actual_mention_and_no_private_ids_in_text(self):
        self.create('提醒example_person_b明天6点起来刷token');self.now=dt.datetime(2026,11,5,6,tzinfo=BJT).timestamp()
        adapter=self.adapter();runner=ReminderDelivery(self.store,lambda:self.now)
        self.assertEqual(await runner.tick(adapter,self.config),'ready');await runner.tick(adapter,self.config)
        self.assertEqual(len(adapter.client.posts),1)
        group,text,key,mentions=adapter.client.posts[0];self.assertEqual(group,self.group);self.assertEqual(mentions,{'userIds':['wxid_gentle_fixture']})
        self.assertFalse(unsafe(text));self.assertIn(normalize('example_person_b，起来刷token'),text);self.assertEqual(self.engine.db.execute('select state from reminder_deliveries').fetchone()[0],'accepted')

    async def test_uncertain_send_recovers_by_read_only_receipt_and_never_posts_again(self):
        self.create();self.now=dt.datetime(2026,11,5,6,tzinfo=BJT).timestamp();adapter=self.adapter();adapter.client.fail=True;runner=ReminderDelivery(self.store,lambda:self.now)
        await runner.tick(adapter,self.config);self.assertEqual(len(adapter.client.posts),1)
        self.now+=11;await runner.tick(adapter,self.config)
        self.assertEqual(len(adapter.client.posts),1);self.assertEqual(len(adapter.client.reads),1)
        self.assertEqual(self.engine.db.execute('select state from reminder_deliveries').fetchone()[0],'accepted')

    async def test_offline_and_wrong_platform_do_not_claim_or_send(self):
        self.create();self.now=dt.datetime(2026,11,5,6,tzinfo=BJT).timestamp();adapter=self.adapter();adapter.client.online=False;runner=ReminderDelivery(self.store,lambda:self.now)
        self.assertEqual(await runner.tick(adapter,self.config),'waiting_connection');self.assertEqual(adapter.client.posts,[])
        self.assertEqual(self.engine.db.execute('select count(*) from reminder_deliveries').fetchone()[0],0)
        adapter.client.online=True;adapter.meta=lambda:types.SimpleNamespace(id='other',name='wechat_native')
        self.assertEqual(await runner.tick(adapter,self.config),'wrong_platform')

    async def test_native_identity_change_and_wrong_receipt_never_trigger_resend(self):
        self.create();self.now=dt.datetime(2026,11,5,6,tzinfo=BJT).timestamp();adapter=self.adapter();adapter.client.fail=True;runner=ReminderDelivery(self.store,lambda:self.now)
        await runner.tick(adapter,self.config);self.now+=11;adapter.client.server_id='changed'
        with self.assertRaises(ValueError):await runner.tick(adapter,self.config)
        self.assertEqual(len(adapter.client.posts),1)
