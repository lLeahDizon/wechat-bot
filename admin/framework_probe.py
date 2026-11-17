"""Real pinned framework, synthetic events, no network or real credentials."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

ROOT=Path('/opt/sub2api-wechat-bot')
PROBE=r'''
import asyncio,hashlib,importlib,json,pathlib,tempfile,types,sys,time
from datetime import UTC,datetime
bindings=json.loads(sys.stdin.read())
member_ids=[bindings['flower'],bindings['gentle'],bindings['owner']]
package=types.ModuleType('acceptance_guard');package.__path__=['/acceptance/plugin'];sys.modules[package.__name__]=package
m=importlib.import_module('acceptance_guard.main')
async def check():
 with tempfile.TemporaryDirectory() as folder:
  root=pathlib.Path(folder);policy=json.loads(pathlib.Path('/run/bot-config/policy.json').read_text());policy.update(enabled=True,diagnostics_enabled=True,acceptance_prefix='')
  seed=json.loads(pathlib.Path('/acceptance/policy-seed.json').read_text())
  aliases=policy.get('usage_public_aliases',{});aliases.update(seed['usage_public_aliases']);policy['usage_public_aliases']=aliases
  names=policy.get('usage_public_names',{});names.update(seed.get('usage_public_names',{}));policy['usage_public_names']=names
  policy.update(help=seed['help'],feedback_enabled=seed.get('feedback_enabled',False),improvement_mode=seed.get('improvement_mode'))
  policy['lol_enabled']=seed.get('lol_enabled',False);policy['reminders_enabled']=seed.get('reminders_enabled',False)
  m.FAQ=pathlib.Path('/acceptance/faq-seed.json');m.LOL_CATALOGUE=pathlib.Path('/acceptance/lol-catalogue.json')
  from guard.core import digest,normalize
  owner_profile=bindings['usage']['owner_profile_digest'];owner_target=bindings['usage']['owner_usage_target'];owner_aliases=bindings['usage']['owner_aliases']
  profiles=policy.setdefault('member_profiles',{});profile=profiles.get(owner_profile,{})
  profiles[owner_profile]={**profile,'tone':profile.get('tone','default'),'usage_target':owner_target}
  policy.setdefault('usage_public_aliases',{})[owner_target]=owner_aliases
  policy.setdefault('usage_public_names',{})[owner_target]=owner_aliases[0]
  policy.update(owner_subject_hash=digest(policy['group_id'],bindings['owner']),owner_operations_enabled=False,server_readonly=True,general_enabled=True)
  p=root/'policy.json';p.write_text(json.dumps(policy));m.POLICY=p;m.STATE=root/'state.sqlite'
  guard=m.Sub2APIGuard(object());sent=[];history_calls=[]
  async def fake_group_history(thread):
   assert thread==policy['group_id']
   history_calls.append(thread)
   return ([{'id':'history-1','threadId':thread,'direction':'incoming','type':1,
     'senderId':'wxid_fixture_member','createdAt':datetime.fromtimestamp(time.time()-3,UTC).isoformat(),
     'body':'持久历史引用标记：此前讨论了服务状态与Redis。'}],True)
  class GroupCache:
   def snapshot_before(self,thread,sequence):
    assert thread==policy['group_id'] and sequence==2
    return [types.SimpleNamespace(id='history-1',sender='wxid_fixture_member',text='先检查服务状态，再看受限只读指标。',truncated=False)]
  class E:
   def __init__(self,text,mid,sender=None,mentioned=True,context_sequence=None):
    self.sender=sender or bindings['gentle'];self.text=text;self.extra={'wechat_native_context_sequence':context_sequence};self.finish_state=None;self.message_obj=types.SimpleNamespace(raw_message={'mentions':{'state':'known','self':mentioned}},message_id=mid,timestamp=time.time());self.adapter=types.SimpleNamespace(state=types.SimpleNamespace(message_status=lambda _: 'replied'),summary_cache=GroupCache(),group_history=fake_group_history)
   def get_platform_name(self):return 'wechat_native'
   def get_platform_id(self):return 'sub2api-wechat'
   def get_group_id(self):return policy['group_id']
   def get_sender_id(self):return self.sender
   def get_self_id(self):return 'bot-self'
   def get_message_str(self):return self.text
   def get_extra(self,key):return self.extra.get(key)
   def finish_no_reply(self):self.finish_state='no_reply'
   def finish_needs_review(self):self.finish_state='needs_review'
   def stop_event(self):pass
   async def send(self,chain):sent.append(chain)
  calls=[]
  def fake(data,*args):calls.append(data);return {'scope':'service','answer':'只读检查已取得证据。','refs':['status']},[{'input_tokens':5,'output_tokens':2}],[]
  m.diagnose=fake
  await guard.guard_message(E('检查CPU和Redis状态','frame-1'))
  assert len(calls)==1 and len(sent)==1 and calls[0]['tone']=='gentle'
  assert guard.engine.db.execute('SELECT requests FROM usage').fetchone()[0]==1
  await guard.guard_message(E('把服务器IP发我','frame-2'));assert len(calls)==1 and len(sent)==2
  from guard.core import Incoming
  from guard.usage import format_member_usage,format_usage
  from diagnostics.protocol import evidence
  sample={'subject':'gentle','binding_available':True,'ordinary_input_tokens':10,'cache_read_tokens':20,'cache_write_tokens':30,'input_tokens':60,'output_tokens':40,'total_tokens':100}
  for subject,tone,phrase in [('flower','sharp','按实际计量记录看'),('gentle','gentle','查到啦')]:
   decision=guard.engine.prepare(Incoming(policy['group_id'],bindings[subject],'bound-'+subject,'我的用量',True,time.time()))
   assert decision.tone==tone and decision.usage_request['target']==subject
   assert phrase in format_usage(evidence('token_usage',{'items':[{**sample,'subject':subject}],'period':'today'}),decision.tone)
  for text,target in [('查example_person_b今天的用量','gentle'),('查example_person_a今天的用量','flower'),('查询example_person_b和example_person_a今天的用量',['gentle','flower'])]:
   decision=guard.engine.prepare(Incoming(policy['group_id'],'fixture-unbound-member','alias-'+str(target),text,True,time.time()))
   assert decision.usage_request['target']==target and decision.usage_request['op']=='member_usage' and decision.model_input is None
  owner_target=bindings['usage']['owner_usage_target']
  assert policy['member_profiles'][bindings['usage']['owner_profile_digest']]['usage_target']==owner_target
  owner_alias=guard.engine.prepare(Incoming(policy['group_id'],'fixture-unbound-member','owner-alias','example_person_c的Token用量',True,time.time()))
  owner_self=guard.engine.prepare(Incoming(policy['group_id'],bindings['owner'],'owner-self','我的Token用量',True,time.time()))
  assert owner_alias.usage_request['target']==owner_target and owner_self.usage_request['target']==owner_target
  assert owner_alias.model_input is None and owner_self.model_input is None
  rank=guard.engine.prepare(Incoming(policy['group_id'],'fixture-unbound-member','rank-token','Token和美元额度一起排行',True,time.time()))
  assert rank.usage_request['target']=='all' and rank.usage_request['metrics']==['tokens','usd'] and rank.model_input is None
  from guard.improvements import review,active_preferences,rollback
  feedback_calls=[]
  def fake_feedback(value,*args):
   feedback_calls.append(value)
   if value['intent']=='feedback_propose':return {'scope':'service','feedback':{'kind':'reply_style','style':{'length':'concise','layout':'conclusion_then_steps'}}},{'input_tokens':3,'output_tokens':1}
   return {'scope':'service','verification':{'approved':True,'refs':['scope'],'reason':'safe_style'}},{'input_tokens':4,'output_tokens':1}
  m.review_feedback=lambda data,*args:review(data,None,None,fake_feedback)
  await guard.guard_message(E('建议你先给结论，再列排查步骤','frame-feedback-1'))
  assert len(feedback_calls)==2 and len(sent)==3 and active_preferences(guard.engine.db)['length']=='concise'
  assert guard.engine.db.execute('SELECT requests FROM usage').fetchone()[0]==3
  await guard.guard_message(E('建议你先给结论，再列排查步骤','frame-feedback-2'))
  await guard.guard_message(E('建议列表','frame-feedback-list'))
  assert len(feedback_calls)==2 and len(sent)==5
  identifier=guard.engine.db.execute('SELECT id FROM improvements').fetchone()[0]
  assert rollback(guard.engine.db,identifier) and active_preferences(guard.engine.db)=={}
  clarification_calls=[]
  def fake_clarification(value,*args):
   clarification_calls.append(value)
   if value['intent']=='feedback_propose':return {'scope':'service','feedback':{'kind':'clarify','options':[{'kind':'reply_style','style':{'token_unit':'auto'}},{'kind':'reply_style','style':{'length':'concise'}}]}},{'input_tokens':2}
   return {'scope':'service','verification':{'approved':True,'refs':['scope'],'reason':'safe_style'}},{'input_tokens':3}
  m.review_feedback=lambda data,*args:review(data,None,None,fake_clarification)
  await guard.guard_message(E('建议调整Token显示单位','frame-clarify'))
  assert len(clarification_calls)==1 and active_preferences(guard.engine.db)=={}
  await guard.guard_message(E('1','frame-choice-other',sender=bindings['flower']))
  await guard.guard_message(E('1','frame-choice-not-at',mentioned=False))
  assert len(clarification_calls)==1 and active_preferences(guard.engine.db)=={}
  await guard.guard_message(E('1','frame-choice-owner'))
  assert len(clarification_calls)==2 and active_preferences(guard.engine.db)=={'token_unit':'auto'}
  await guard.guard_message(E('建议调整Token显示单位','frame-clarify-dedup'))
  assert len(clarification_calls)==2
  rendered=[]
  def render(value,tone,unit,names,commentary=False):
   text=format_member_usage(value,tone,unit,names,commentary);rendered.append(text);return text
  m.format_member_usage=render
  def fake_query(request):
   subjects=(['flower','gentle',owner_target] if request['target']=='all' else request['target'] if isinstance(request['target'],list) else [request['target']])
   amounts={'flower':'0.001','gentle':'0.00001234',owner_target:'0.25'}
   tokens={'flower':2000,'gentle':100,owner_target:500}
   data={'period':request['period'],'rank':request['target']=='all',
    'token_items':[{'subject':subject,'binding_available':True,'ordinary_input_tokens':tokens[subject],
     'cache_read_tokens':0,'cache_write_tokens':0,'input_tokens':tokens[subject],
     'output_tokens':0,'total_tokens':tokens[subject]} for subject in subjects] if 'tokens' in request.get('metrics',[]) else [],
    'usd_items':[{'subject':subject,'binding_available':True,'usd_amount':amounts[subject]} for subject in subjects] if 'usd' in request.get('metrics',[]) else []}
   if request['period']=='range':
    data.update(range_start_epoch=request['range_start_epoch'],range_end_epoch=request['range_end_epoch'])
   return evidence('member_usage',data)
  m.QueryBudget=lambda:types.SimpleNamespace(query=fake_query)
  await guard.guard_message(E('我的用量','frame-million-usage'))
  assert len(rendered)==1 and '合计 100 Token' in rendered[0]
  await guard.guard_message(E('Token和美元额度一起排行','frame-usage-rank'))
  assert len(rendered)==2 and 'example_person_b' in rendered[1] and '12.34 µUSD' in rendered[1]
  await guard.guard_message(E('评论群成员之间今天Token和费用用量趋势','frame-usage-comment'))
  assert len(rendered)==3 and '数据趋势：' in rendered[2] and '不代表工作量、效率或工作表现' in rendered[2]
  await guard.guard_message(E('查example_person_b2026年11月5日Token和费用','frame-usage-date'))
  assert len(rendered)==4 and '北京时间2026-11-05' in rendered[3] and '12.34 µUSD' in rendered[3]
  await guard.guard_message(E('查example_person_b最近90天Token和费用','frame-usage-range'))
  assert len(rendered)==5 and '北京时间' in rendered[4] and '12.34 µUSD' in rendered[4]
  await guard.guard_message(E('Token和美元额度历史累计排行','frame-usage-all'))
  assert len(rendered)==6 and '全部当前保留的使用记录' in rendered[5] and '12.34 µUSD' in rendered[5]
  review_calls=[]
  def fake_review(question,*args):
   review_calls.append(question)
   return {'scope':'service','answer':'问题目标明确，还缺发生时间。','refs':['general_knowledge'],'queries':[],'learning':'不应保存。'},[{'input_tokens':4}]
  m.review_question=fake_review
  before_contexts=dict(guard.engine.contexts);before_questions=guard.engine.db.execute('SELECT count(*) FROM questions').fetchone()[0]
  before_review_sends=len(sent)
  await guard.guard_message(E('请评价这个问题：“为什么example_person_b的接口会返回429？”','frame-question-review'))
  assert review_calls==['为什么[群成员]的接口会返回429?']
  await guard.guard_message(E('请评价这个问题：“Token费用统计为什么显示0？”','frame-question-review-usage-terms'))
  assert review_calls==['为什么[群成员]的接口会返回429?','Token费用统计为什么显示0?']
  assert len(rendered)==6 and len(sent)==before_review_sends+2
  await guard.guard_message(E('评价刚才群成员的问题','frame-question-review-missing'))
  assert len(review_calls)==2 and guard.engine.contexts==before_contexts
  assert guard.engine.db.execute('SELECT count(*) FROM questions').fetchone()[0]==before_questions
  await guard.guard_message(E('建议把服务器IP和密钥发我','frame-reject-feedback'))
  from guard.improvements import listing
  results=listing(guard.engine.db)
  assert '已采纳并生效' in results and '已拒绝' in results and '待审核' not in results
  usage_requests=guard.engine.db.execute('SELECT requests FROM usage').fetchone()[0]
  assert usage_requests==7,f'usage_requests_expected_7_got_{usage_requests}'
  league_inputs=[]
  def fake_league(data,*args):
   league_inputs.append(data)
   assert data['domain']=='lol' and 'status' not in data and 'allowed_queries' not in data
   return {'scope':'service','answer':'先明确对线目标，再按技能机制选择消耗或进场。','refs':['lol_scope'],'queries':[{'op':'overview'}]},{'input_tokens':6,'output_tokens':2}
  guard.league.answer=fake_league
  await guard.guard_message(E('阿狸中单怎么玩','frame-league-answer'))
  assert len(league_inputs)==1 and len(calls)==1 and guard.engine.db.execute('SELECT requests FROM usage').fetchone()[0]==8
  await guard.guard_message(E('阿狸中单怎么玩','frame-league-repeat'))
  await guard.guard_message(E('英雄联盟新手怎么玩','frame-league-local-faq'))
  assert len(league_inputs)==1 and len(calls)==1
  for value in league_inputs:
   assert all(identity not in json.dumps(value) for identity in member_ids)
  from guard.league import LeagueKnowledge
  esports_inputs=[]
  guard.league.esports.context=lambda question:{'kind':'esports','state':'ok','sources':[{'url':'https://lolesports.com/en-US/leagues/worlds','publisher':'LoL Esports','fetched_at':time.time(),'published_at':None,'records':[],'text':''}]}
  def esports_model(data,*args):
   esports_inputs.append(data)
   assert data['domain']=='lol' and data['esports'] and data['lol_data']['kind']=='esports'
   assert 'status' not in data and 'allowed_queries' not in data
   return {'scope':'service','answer':'本次来源未覆盖全部比赛，不能把空记录当作今天无比赛。','refs':['lol_data']},{'input_tokens':3,'output_tokens':2}
  guard.league.answer=lambda data,*args:LeagueKnowledge.answer(guard.league,data,None,None,esports_model)
  await guard.guard_message(E('LPL今天谁赢了','frame-esports-answer'))
  assert len(esports_inputs)==1 and len(calls)==1 and guard.engine.db.execute('SELECT requests FROM usage').fetchone()[0]==9
  rendered=guard.engine.contexts[digest(policy['group_id'],bindings['gentle'])+':lol'][1][-1][1]
  assert normalize('来源：') not in rendered and 'https://' not in rendered
  assert all(identity not in json.dumps(esports_inputs) for identity in member_ids)
  prediction_inputs=[]
  from guard.predictions import ROLES
  comparisons=[{'role':role,'label':label,'state':'comparable_samples','small_sample':True,
    'left':{'name':'左方'+label,'maps':2,'kda':3.0,'kill_participation':0.6},
    'right':{'name':'右方'+label,'maps':3,'kda':2.5,'kill_participation':0.5}} for role,label in ROLES.items()]
  guard.league.predictions.context=lambda question,history:{'kind':'prediction','state':'usable_with_limits','window_days':60,
    'statistics':[],'sources':[],'position_comparisons':comparisons}
  def prediction_model(data,*args):
   prediction_inputs.append(data);assert data['lol_data']['kind']=='prediction' and data['lol_data']['window_days']==60
   return {'scope':'service','answer':'样本接近，关键看中野联动。','prediction_analysis':{'overall':'样本接近，关键看中野联动。','focus':['关注中野的阿狸选用和联动节奏']},'refs':['lol_data']},{'input_tokens':2,'output_tokens':1}
  guard.league.answer=lambda data,*args:LeagueKnowledge.answer(guard.league,data,None,None,prediction_model)
  await guard.guard_message(E('预测T1对BLG','frame-prediction'))
  assert len(prediction_inputs)==1 and len(calls)==1
  rendered=guard.engine.contexts[digest(policy['group_id'],bindings['gentle'])+':lol'][1][-1][1]
  assert len(prediction_inputs[0]['lol_data']['position_comparisons'])==5
  assert normalize('结论：') in rendered and '阿狸' in rendered and '中野' in rendered
  assert 'KDA' not in rendered and '3.0/2.5' not in rendered and 'https://' not in rendered and len(rendered.splitlines())<=3
  admin_calls=[]
  class FakeAdmin:
   def request(self,value):
    admin_calls.append(value)
    if value['op']=='prepare':return {'reply':'准备执行固定动作。确认 ABCD1234'}
    if value['op']=='confirm':return {'reply':'已确认，回执后提交。','activate_job':'GL-0123456789abcdef'}
    return {'reply':'已受理。'}
  m.AdminClient=FakeAdmin
  await guard.guard_message(E('/管理 重启Sub2API','frame-owner-denied'))
  assert admin_calls==[]
  await guard.guard_message(E('/管理 重启Sub2API','frame-owner-propose',sender=bindings['owner']))
  assert admin_calls==[]
  await guard.guard_message(E('确认 ABCD1234','frame-owner-confirm',sender=bindings['owner']))
  assert admin_calls==[]
  assert guard.engine.db.execute('SELECT requests FROM usage').fetchone()[0]==10
  from guard.general import answer as general_answer
  general_calls=[]
  class PublicFixture:
   def collect(self,queries,urls):
    assert queries==['Python pathlib official documentation']
    return {'state':'ok','sources':[{'url':'https://docs.python.org/3/library/pathlib.html','text':'Path.exists reports whether the path exists.','fetched_at':time.time()}]}
  def general_model(data,*args):
   general_calls.append(data)
   assert 'status' not in data and 'allowed_queries' not in data and data['domain']=='general'
   assert all(identity not in json.dumps(data) for identity in member_ids)
   if data['phase']=='general_plan' and not data['requires_research']:return {'scope':'service','answer':'你好，世界。','refs':['general_knowledge']},{'input_tokens':2,'output_tokens':1}
   if data['phase']=='general_plan':return {'scope':'service','web_queries':['Python pathlib official documentation']},{'input_tokens':2,'output_tokens':1}
   return {'scope':'service','answer':'Path.exists用于判断路径是否存在。','refs':['web_data']},{'input_tokens':2,'output_tokens':1}
  m.general_answer=lambda data,*args:general_answer(data,None,None,PublicFixture(),general_model)
  await guard.guard_message(E('帮我翻译Hello world','frame-general-translation'))
  await guard.guard_message(E('查一下Python pathlib官方资料','frame-general-research'))
  assert len(general_calls)==3 and guard.engine.db.execute('SELECT requests FROM usage').fetchone()[0]==13
  rendered=guard.engine.contexts[digest(policy['group_id'],bindings['gentle'])+':general'][1][-1][1]
  assert 'Path.exists' in rendered
  before=len(general_calls);history_before=len(history_calls);banter=E('哈哈今天群里挺热闹','frame-banter',mentioned=False)
  await guard.guard_message(banter)
  assert banter.finish_state=='no_reply' and len(general_calls)==before and len(history_calls)==history_before
  unmentioned=E('刚才群里提到的这件事应该怎么理解？','frame-group-context-unmentioned',mentioned=False,context_sequence=2)
  await guard.guard_message(unmentioned)
  assert unmentioned.finish_state=='no_reply' and len(general_calls)==before and len(history_calls)==history_before
  mentioned=E('刚才群里提到的这件事应该怎么理解？','frame-group-context-mentioned',mentioned=True,context_sequence=2)
  await guard.guard_message(mentioned)
  assert len(general_calls)==before+1 and mentioned.finish_state is None and len(history_calls)==history_before+1,'mention_history_dispatch_missing'
  context=general_calls[-1].get('group_context',[])
  assert context,'group_context_empty'
  expected_context='持久历史引用标记:此前讨论了服务状态与Redis。';actual_context=context[0].get('text','')
  if actual_context!=expected_context:
   mismatch=next((i for i,(left,right) in enumerate(zip(actual_context,expected_context)) if left!=right),min(len(actual_context),len(expected_context)))
   raise AssertionError('group_context_text_mismatch_len_'+str(len(actual_context))+'_at_'+str(mismatch))
  assert context[0].get('speaker')=='群成员1' and 'wxid_fixture_member' not in json.dumps(general_calls[-1]),'group_context_speaker_not_anonymous'
  assert general_calls[-1].get('group_context_state')=='partial','history_page_coverage_missing'
  assert all(identity not in json.dumps(clarification_calls) for identity in member_ids)
  assert all(identity not in json.dumps(feedback_calls) for identity in member_ids)
  assert all(identity not in json.dumps(calls) for identity in member_ids)
  from guard.core import Incoming
  version_question=guard.engine.prepare(Incoming(policy['group_id'],bindings['gentle'],'frame-version-question','总结下今天LOL版本更新了啥',True,time.time()))
  assert version_question.intent=='general' and not version_question.model_input['diagnostic']
  await guard.guard_message(E('明天6点提醒我起床','frame-reminder-propose'))
  code=guard.engine.db.execute("SELECT code FROM reminder_proposals WHERE state='pending'").fetchone()[0]
  await guard.guard_message(E('确认提醒 '+code,'frame-reminder-other',sender=bindings['flower']))
  assert guard.engine.db.execute('SELECT count(*) FROM reminders').fetchone()[0]==0
  await guard.guard_message(E('确认提醒 '+code,'frame-reminder-confirm'))
  assert guard.engine.db.execute('SELECT count(*) FROM reminders').fetchone()[0]==1
  due=guard.engine.db.execute('SELECT next_at FROM reminders').fetchone()[0]
  native_package=types.ModuleType('fixture_native');native_package.__path__=['/acceptance/native'];sys.modules[native_package.__name__]=native_package
  NativeClient=importlib.import_module('fixture_native.native_client').NativeClient
  native_posts=[]
  class FixtureNative(NativeClient):
   @property
   def closed(self):return False
   async def _request(self,method,path,**kwargs):
    if path=='/status':return {'serverId':'fixture-server'}
    if path==self.prefix:return {'authenticated':True,'connection':'online'}
    if path.endswith('/members'):return {'items':[{'username':value} for value in member_ids]}
    if '/members/' in path:
     from urllib.parse import unquote
     user=unquote(path.rsplit('/',1)[-1]);return {'groupId':policy['group_id'],'username':user,'state':'present','member':{'username':user}}
    if method=='POST' and path.endswith('/sends'):
     native_posts.append(kwargs);return {'id':'fixture-send','state':'accepted','accountId':self.account_id,'destination':policy['group_id']}
    raise AssertionError('unexpected_fixture_request')
  client=FixtureNative('https://native:7443','fixture-account','fixture-token')
  native_state=importlib.import_module('fixture_native.state').StateStore(root/'native-fixture.sqlite')
  adapter=types.SimpleNamespace(client=client,state=native_state,stopped=False,bot_self_id='fixture-self',meta=lambda:types.SimpleNamespace(id=policy['bot_instance_id'],name='wechat_native'))
  guard.engine.reminders.clock=lambda:due
  from guard.reminder_delivery import ReminderDelivery
  runner=ReminderDelivery(guard.engine.reminders,lambda:due)
  assert await runner.tick(adapter,policy)=='ready';await runner.tick(adapter,policy)
  assert len(native_posts)==1 and native_posts[0]['json']['mentions']=={'userIds':[bindings['gentle']]}
  assert all(identity not in native_posts[0]['json']['text'] for identity in member_ids)
  assert guard.engine.db.execute('SELECT requests FROM usage').fetchone()[0]==14
  assert guard.engine.db.execute('SELECT state FROM reminder_deliveries').fetchone()[0]=='accepted'
  native_state.db.close()
  quota_module=importlib.import_module('acceptance_guard.guard.account_quota')
  now=time.time()
  iso=lambda at:datetime.fromtimestamp(at,UTC).isoformat()
  qr={'eligible_records':2,'identity_count':1,'unidentified_records':0,'sampled_at':iso(now-20),
      'short_used_percent':0,'short_window_minutes':0,'short_reset_at':iso(now-20),
      'weekly_used_percent':86,'weekly_window_minutes':10080,'weekly_reset_at':iso(now+86400),
      'record_order':1,'reset_credit_snapshot_present':True,'reset_credit_count':2,
      'points_present':True,'points_available':True,'points_unlimited':False,
      'points_balance':'12345.6789015000','points_sampled_at':now-10,
      'reset_credit_expirations':[iso(now+864000),iso(now+950400)]}
  quota_queries=[]
  def quota_query(request):
   assert request=={'op':'account_quota','window_minutes':15}
   quota_queries.append(request)
   return evidence('account_quota',quota_module.snapshot_data([qr,{**qr,'record_order':2,'reset_credit_count':3}]))
  m.QueryBudget=lambda:types.SimpleNamespace(query=quota_query)
  requests_before=guard.engine.db.execute('SELECT requests FROM usage').fetchone()[0]
  sent_before=len(sent)
  await guard.guard_message(E('账号用量','frame-account-quota',sender='fixture-quota-member'))
  quota_text=''.join(getattr(part,'text','') for part in sent[-1].chain)
  assert len(quota_queries)==1 and len(sent)==sent_before+1
  assert '剩余14.00%' in quota_text and normalize('剩余可用重置次数：2次') in quota_text
  assert normalize('可用点数：12345.68点') in quota_text and '点数采样北京时间' in quota_text
  assert guard.engine.db.execute('SELECT requests FROM usage').fetchone()[0]==requests_before
  await guard.guard_message(E('账号用量','frame-account-quota-not-mentioned',mentioned=False))
  assert len(quota_queries)==1 and len(sent)==sent_before+1
  quota_calls=[];original_advice=quota_module.advice_answer
  def quota_model(data,*args):
   quota_calls.append(data)
   return {'answer':'先缩小复现范围，按模块定位，复杂根因再提高推理强度。'},{'input_tokens':3,'output_tokens':1}
  quota_module.advice_answer=lambda data,*args:original_advice(data,None,None,quota_model)
  await guard.guard_message(E('具体展开说说','frame-account-quota-advice',sender='fixture-quota-member'))
  assert len(quota_queries)==2 and len(quota_calls)==1
  assert '具体建议' in ''.join(getattr(part,'text','') for part in sent[-1].chain)
  assert guard.engine.db.execute('SELECT requests FROM usage').fetchone()[0]==requests_before+1
  await guard.terminate()
  print(json.dumps({'account_points_local':True,'primary_reset_record':True,'account_quota_local_without_model':True,'account_quota_detail_one_call':True,'account_quota_strict_mention':True,'framework_import':True,'real_star_instantiation':True,'async_thread_usage_recording':True,'gentle_identity':True,'actual_member_bindings_replayed':3,'both_self_usage_selectors':True,'both_persona_formats':True,'public_alias_queries':3,'usage_personal_and_rank_replayed':True,'usage_dates_and_record_cost_replayed':True,'usage_commentary_and_auto_units_replayed':True,'question_review_isolated':True,'owner_aliases_recognized':True,'raw_identities_absent_from_model_input':True,'privacy_before_model':True,'feedback_two_stage_calls':2,'feedback_dedup_and_local_listing':True,'feedback_rollback':True,'clarification_same_sender_and_mention':True,'clarification_total_model_calls':2,'automatic_stat_units_replayed':True,'only_two_terminal_outcomes':True,'all_member_server_writes_disabled':True,'general_topics_and_public_research':True,'strict_mention_and_persistent_group_context':True,'nonmention_messages_no_reply_or_history_read':True,'group_context_speaker_anonymized':True,'league_one_call_and_no_server_queries':True,'league_repeat_and_faq_local':True,'esports_internal_sources_no_visible_footer':True,'lol_version_question_recognized':True,'prediction_summary_and_key_focus':True,'reminders_member_confirmation_and_native_client_send_dedup':True,'actual_server_writes':0,'actual_model_calls':0,'actual_messages_sent':0}))
asyncio.run(check())
'''


def main():
    image=json.loads((ROOT/'admin-evidence/images.json').read_text())['soulter/astrbot:v4.29.4']['digests'][0]
    args=['docker','run','--rm','-i','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
        '--log-driver','none','--memory','384m','--cpus','0.5','--pids-limit','96','--user','10002:10002','--entrypoint','python',
        '--tmpfs','/tmp:rw,nosuid,size=64m,mode=1777','--tmpfs','/AstrBot/data:rw,nosuid,size=64m,uid=10002,gid=10002,mode=700',
        '-e','PYTHONDONTWRITEBYTECODE=1','-e','PYTHONPATH=/opt/bot-runtime:/opt/query-client:/opt/bot-code:/opt/bot-ops:/AstrBot',
        '-v',str(ROOT/'code/plugin/main.py')+':/acceptance/plugin/main.py:ro',
        '-v',str(ROOT/'code/config/policy.example.json')+':/acceptance/policy-seed.json:ro',
        '-v',str(ROOT/'code/knowledge/faq.json')+':/acceptance/faq-seed.json:ro',
        '-v',str(ROOT/'code/knowledge/lol-catalogue.json')+':/acceptance/lol-catalogue.json:ro',
        '-v',str(ROOT/'code/guard')+':/acceptance/plugin/guard:ro',
        '-v',str(ROOT/'code/vendor/astrbot_plugin_wechat_native')+':/acceptance/native:ro',
        '-v',str(ROOT/'code/guard')+':/opt/bot-code/guard:ro',
        '-v',str(ROOT/'code/operations')+':/opt/bot-ops/operations:ro',
        '-v',str(ROOT/'code/runtime')+':/opt/bot-runtime:ro',
        '-v',str(ROOT/'code')+':/opt/query-client:ro',
        '-v',str(ROOT/'config')+':/run/bot-config:ro',
        '-v',str(ROOT/'public')+':/run/bot-status:ro',image,'-c',PROBE]
    bindings=json.loads((ROOT/'admin-evidence/member-bindings.json').read_text())['bindings']
    if set(bindings)!={'flower','gentle'} or len(set(bindings.values()))!=2:raise RuntimeError('member_bindings_not_verified')
    identity=json.loads((ROOT/'admin-evidence/owner-binding.json').read_text())['id']
    usage_path=ROOT/'admin-evidence/usage-binding-rollout.json'
    if usage_path.exists():
        usage_binding=json.loads(usage_path.read_text())
        identity=usage_binding['owner_identity_private']
        profile_digest=usage_binding['owner_profile_digest'];subject=usage_binding['owner_subject']
    else:
        group=json.loads((ROOT/'config/policy.json').read_text())['group_id']
        profile_digest=hashlib.sha256((group+'\0'+identity).encode()).hexdigest()
        subject='member_'+profile_digest[:16]
    bindings['owner']=identity
    if len(set(bindings.values()))!=3:raise RuntimeError('owner_binding_collision')
    bindings['usage']={'owner_profile_digest':profile_digest,'owner_usage_target':subject,
                       'owner_aliases':['example_person_c','example_person_c']}
    result=subprocess.run(args,input=json.dumps(bindings),capture_output=True,text=True,timeout=45)
    rows=[x for x in result.stdout.splitlines() if x.startswith('{')]
    if result.returncode or not rows:
        # No real credentials or network are mounted in this fixture.
        error=result.stderr.strip().splitlines()
        frames=[int(match.group(1)) for line in error
            if (match:=re.search(r'File "<string>", line (\d+)',line))]
        counter=re.search(r'usage_requests_expected_7_got_(\d+)', '\n'.join(error))
        error_type=next((name for name in ['AssertionError','ImportError','ModuleNotFoundError','TimeoutError']
            if any(name in line for line in error)),'unknown')
        assertion=next((line.partition('AssertionError: ')[2] for line in reversed(error)
            if 'AssertionError: ' in line),'')
        if not re.fullmatch(r'[a-z0-9_]{1,80}',assertion):assertion='assertion_failed'
        print(json.dumps({'failed':True,'exit':result.returncode,
            'fixture_error_type':error_type,'fixture_assertion':assertion,'probe_line':frames[-1] if frames else None,
            'fixture_usage_requests':int(counter.group(1)) if counter else None}));return 1
    value=json.loads(rows[-1]);print(json.dumps(value))
    path=ROOT/'admin-evidence/general-readonly-framework-probe.json'
    path.write_text(json.dumps({'at':time.time(),**value},indent=2)+'\n');path.chmod(0o600)
    return 0


if __name__=='__main__':raise SystemExit(main())
