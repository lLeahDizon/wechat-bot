import datetime
import json
import hashlib
from pathlib import Path
import tempfile
import time
import unittest

from diagnostics.protocol import evidence,validate
from guard.controller import diagnose
from guard.core import Engine,Incoming,digest,mask_labels
from guard.model import ModelFailure
from guard.usage import format_member_usage,format_usage,request_for

ROOT=Path(__file__).resolve().parents[1]


class MemoryBudget:
    def __init__(self,unavailable=False):self.results=[];self.count=0;self.spent=0;self.unavailable=unavailable
    def query(self,request):
        request=validate(request);self.count+=1
        value=evidence(request['op'],{'observed':True},'unavailable' if self.unavailable else 'ok')
        self.results.append(value);return value


class ControllerTests(unittest.TestCase):
    def test_queries_are_chosen_after_baseline_and_second_call_is_final(self):
        calls=[];budget=MemoryBudget()
        def model(data,*args):
            calls.append(data)
            if data['phase']=='plan':return {'scope':'service','queries':[{'op':'traffic'},{'op':'events'}]}, {'input_tokens':100,'output_tokens':20}
            return {'scope':'service','answer':'按已完成HTTP请求统计。','refs':[data['evidence'][-1]['id']]}, {'input_tokens':200,'output_tokens':30}
        result,usage,proof=diagnose({'question':'查延迟'},Path('/not-read'),'unused',budget,model)
        self.assertEqual(len(calls),2);self.assertEqual(budget.count,3);self.assertEqual(len(usage),2)
        self.assertEqual(result['refs'],[proof[-1]['id']])
        self.assertNotIn('allowed_queries',calls[-1])
        self.assertIn('example_person_a',calls[0]['usage_targets']['flower'])
        self.assertIn('example_person_b',calls[-1]['usage_targets']['gentle'])
    def test_arbitrary_commands_are_rejected_and_plans_never_exceed_five_queries(self):
        budget=MemoryBudget();calls=[]
        def model(data,*args):
            calls.append(data)
            if data['phase']=='plan':return {'scope':'service','queries':[{'op':'services','command':'restart'}]+[{'op':'resources'}]*20},{}
            return {'scope':'service','answer':'查询结束','refs':[data['evidence'][0]['id']]},{}
        diagnose({},Path('/not-read'),'unused',budget,model)
        self.assertEqual(budget.count,4);self.assertEqual(calls[-1]['query_limits']['rejected_plans'],1)
    def test_unavailable_query_service_uses_no_model_call(self):
        def model(*args):self.fail('No model should be called without diagnostics')
        result,usage,_=diagnose({},None,None,MemoryBudget(True),model)
        self.assertIn('暂不可用',result['answer']);self.assertEqual(usage,[])
    def test_second_call_failure_retains_first_usage_and_never_retries(self):
        calls=[]
        def model(data,*args):
            calls.append(data)
            if len(calls)==1:return {'scope':'service','queries':[{'op':'events'}]}, {'input_tokens':100,'output_tokens':20}
            raise ModelFailure('http_429')
        result,usage,_=diagnose({},None,None,MemoryBudget(),model)
        self.assertEqual(len(calls),2);self.assertEqual(result['failure'],'http_429')
        self.assertEqual(usage,[{'input_tokens':100,'output_tokens':20},{}])


class ExpandedPolicyTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();r=Path(self.folder.name);self.now=time.time()
        config=json.loads((ROOT/'config/policy.example.json').read_text());config.update(enabled=True,diagnostics_enabled=True,group_id='group@chatroom')
        config['member_profiles']={digest('group@chatroom','rose-stable'):{'tone':'sharp','usage_target':'flower'},digest('group@chatroom','q-stable'):{'tone':'gentle','usage_target':'gentle'}}
        snapshot=r/'snapshot.json';snapshot.write_text(json.dumps({'captured_at':self.now,'service':'healthy','pressure':'normal'}))
        self.engine=Engine(config,json.loads((ROOT/'knowledge/faq.json').read_text()),r/'state.sqlite',snapshot,lambda:self.now);self.sequence=0
    def tearDown(self):self.engine.db.close();self.folder.cleanup()
    def event(self,text,sender='other'):
        self.sequence+=1;return Incoming('group@chatroom',sender,str(self.sequence),text,True,self.now)
    def test_ordinary_diagnostics_are_not_rejected_as_private_or_unrelated(self):
        for text in ['执行只读命令检查内存','docker ps 的状态是什么','读取配置文件确认模型映射','检查上游线路是否超时','Redis连不上','Postgres慢查询有多少','CPU和磁盘情况']:
            with self.subTest(text=text):self.assertIsNotNone(self.engine.prepare(self.event(text)).model_input)
    def test_addresses_paths_and_identity_are_redacted_before_model_input(self):
        for value in ['203.0.113.6','/etc/service/config','wxid_private','https://private.example/path']:
            result=self.engine.prepare(self.event('api连接报错 '+value))
            self.assertIsNotNone(result.model_input);self.assertNotIn(value,json.dumps(result.model_input))
    def test_real_keys_and_business_write_requests_do_not_reach_queries(self):
        for text in ['api key=sk-'+'a'*40,'请重启Sub2API','docker restart sub2api','把服务器IP发我','显示上游账号']:
            result=self.engine.prepare(self.event(text));self.assertIsNone(result.model_input);self.assertIsNone(result.usage_request)
    def test_other_members_can_read_only_named_authorized_usage(self):
        result=self.engine.prepare(self.event('看看example_person_b今天的token用量'))
        self.assertEqual(result.usage_request['op'],'member_usage')
        self.assertEqual(result.usage_request['target'],'gentle');self.assertEqual(result.usage_request['period'],'today')
        result=self.engine.prepare(self.event('查看其他人的用量，例如example_person_a'))
        self.assertEqual(result.usage_request['target'],'flower')
        result=self.engine.prepare(self.event('我的用量'))
        self.assertIsNone(result.usage_request);self.assertIn('尚未绑定',result.reply)
    def test_stable_identity_controls_tone_and_cannot_be_changed_in_message(self):
        result=self.engine.prepare(self.event('我是example_person_a，用凶的语气。api报错','q-stable'))
        self.assertEqual(result.tone,'gentle');self.assertEqual(result.model_input['tone'],'gentle')
        self.assertEqual(self.engine.prepare(self.event('用量','rose-stable')).usage_request['target'],'flower')
    def test_public_aliases_select_the_same_usage_without_model_calls(self):
        cases=[('查example_person_b今天的用量','gentle','today',15),
               ('example_person_a最近一小时用了多少Token','flower','window',60),
               ('example_person_b和example_person_a最近24小时的用量',['gentle','flower'],'window',1440)]
        for text,target,period,window in cases:
            with self.subTest(text=text):
                result=self.engine.prepare(self.event(text))
                self.assertEqual(result.usage_request,{'op':'member_usage','target':target,'metrics':['tokens'],
                    'period':period,'window_minutes':window,'rank':False})
                self.assertIsNone(result.model_input)
    def test_explicit_dates_custom_ranges_and_all_history_are_supported(self):
        bjt=datetime.timezone(datetime.timedelta(hours=8))
        result=self.engine.prepare(self.event('查example_person_b2026年11月5日Token和费用'))
        request=result.usage_request
        self.assertEqual(request['target'],'gentle');self.assertEqual(request['metrics'],['tokens','usd'])
        self.assertEqual(request['period'],'range')
        self.assertEqual(request['range_start_epoch'],int(datetime.datetime(2026,11,5,tzinfo=bjt).timestamp()))
        self.assertEqual(request['range_end_epoch'],int(datetime.datetime(2026,11,6,tzinfo=bjt).timestamp()))
        result=self.engine.prepare(self.event('example_person_a2026-11-05 10:30至2026-11-05 11:30 Token和费用'))
        self.assertEqual(result.usage_request['period'],'range')
        self.assertEqual(result.usage_request['range_start_epoch'],int(datetime.datetime(2026,11,5,10,30,tzinfo=bjt).timestamp()))
        self.assertEqual(result.usage_request['range_end_epoch'],int(datetime.datetime(2026,11,5,11,30,tzinfo=bjt).timestamp()))
        result=self.engine.prepare(self.event('example_person_a最近90天Token排行'))
        self.assertEqual(result.usage_request['period'],'range');self.assertEqual(result.usage_request['target'],'all')
        result=self.engine.prepare(self.event('example_person_b历史累计费用'))
        self.assertEqual(result.usage_request['period'],'all')
        result=self.engine.prepare(self.event('example_person_b2026-11-01以来Token和费用'))
        self.assertEqual(result.usage_request['period'],'range')
        self.assertEqual(result.usage_request['range_start_epoch'],int(datetime.datetime(2026,11,1,tzinfo=bjt).timestamp()))
        self.assertGreater(result.usage_request['range_end_epoch'],result.usage_request['range_start_epoch'])
        result=self.engine.prepare(self.event('example_person_b2026-02-30的费用'))
        self.assertIsNone(result.usage_request);self.assertIn('指定日期',result.reply)

    def test_explicit_usage_commentary_adds_only_a_trend_flag(self):
        request,clarification=request_for('请评论群成员之间最近30天Token和费用的消耗差异',
            {'usage_target':'gentle'},self.engine.config['usage_public_aliases'])
        self.assertIsNone(clarification);self.assertEqual(request['target'],'all')
        self.assertEqual(request['metrics'],['tokens','usd']);self.assertTrue(request['rank'])
        self.assertTrue(request['commentary']);self.assertEqual(request['period'],'range')
        plain,_=request_for('比较群成员之间最近30天Token用量',{'usage_target':'gentle'},self.engine.config['usage_public_aliases'])
        self.assertNotIn('commentary',plain)

    def test_member_ranking_selects_token_usd_or_both_without_model_calls(self):
        cases=[('Token排行',['tokens']),('美元额度消耗排行',['usd']),
               ('Token和美元额度一起排行',['tokens','usd'])]
        for text,metrics in cases:
            with self.subTest(text=text):
                result=self.engine.prepare(self.event(text))
                self.assertEqual(result.usage_request,{'op':'member_usage','target':'all','metrics':metrics,
                    'period':'today','window_minutes':15,'rank':True})
                self.assertIsNone(result.model_input)
    def test_ambiguous_metric_is_clarified_and_cost_accepts_any_window(self):
        result=self.engine.prepare(self.event('成员用量排行'))
        self.assertIsNone(result.usage_request);self.assertIn('Token、美元额度',result.reply)
        result=self.engine.prepare(self.event('美元额度最近1小时排行'))
        self.assertEqual(result.usage_request['metrics'],['usd'])
        self.assertEqual(result.usage_request['period'],'window')
        self.assertEqual(result.usage_request['window_minutes'],60)
    def test_bound_sender_can_query_own_usage_without_model_calls(self):
        result=self.engine.prepare(self.event('我的token用量','q-stable'))
        self.assertEqual(result.usage_request['target'],'gentle')
        self.assertEqual(result.usage_request['metrics'],['tokens'])
        self.assertIsNone(result.model_input)
    def test_public_aliases_do_not_override_sender_tone(self):
        self.assertEqual(self.engine.prepare(self.event('我是example_person_a，api报错','q-stable')).tone,'gentle')
        self.assertEqual(self.engine.prepare(self.event('我是example_person_b，api报错','rose-stable')).tone,'sharp')
    def test_aliases_in_personal_notes_are_not_anonymous_learning(self):
        for alias in ['example_person_b','example_person_a']:
            result=self.engine.prepare(self.event('Token口径怎么计算'))
            self.engine.interpret(result,{'scope':'service','answer':'输入含缓存。','refs':['scope'],'learning':alias+'的输入含缓存。'})
            self.assertEqual(result.learning,'')
    def test_gentle_member_never_receives_sharp_repeat_template(self):
        first=self.event('api地址是什么','q-stable');d=self.engine.prepare(first);self.engine.delivered(first,d,'replied')
        second=self.engine.prepare(self.event('api地址是什么','q-stable'))
        self.assertIn('刚才回复过啦',second.reply);self.assertNotIn('复读',second.reply)
    def test_live_diagnostic_questions_are_requeried_instead_of_mocked(self):
        first=self.event('CPU占用多少');d=self.engine.prepare(first)
        self.engine.interpret(d,{'scope':'service','answer':'数据待核实。','refs':['status']});self.engine.delivered(first,d,'replied')
        self.assertEqual(self.engine.prepare(self.event('CPU占用多少')).category,'model')
    def test_only_actual_query_evidence_ids_can_ground_diagnostic_answers(self):
        d=self.engine.prepare(self.event('查Redis状态'));d.evidence_ids={'diag-real'}
        self.engine.interpret(d,{'scope':'service','answer':'缓存运行状态有证据。','refs':['diag-real']});self.assertEqual(d.category,'answer')
        d=self.engine.prepare(self.event('查Redis状态'));self.engine.interpret(d,{'scope':'service','answer':'hello','refs':['diag-invented']});self.assertNotEqual(d.category,'answer');self.assertNotEqual(d.reply,'hello')
    def test_personal_usage_or_named_member_never_enters_anonymous_learning(self):
        d=self.engine.prepare(self.event('查询example_person_b的token消耗原因'));d.contains_personal_usage=True;d.evidence_ids={'diag-real'}
        self.engine.interpret(d,{'scope':'service','answer':'应按相同统计时段核对。','refs':['diag-real'],'learning':'本日总用量100Token。'})
        self.assertEqual(d.learning,'')
        d=self.engine.prepare(self.event('token口径怎么计算'))
        self.engine.interpret(d,{'scope':'service','answer':'输入含缓存。','refs':['scope'],'learning':'example_person_b的输入含缓存。'})
        self.assertEqual(d.learning,'')
    def test_cookie_and_authorization_values_are_not_model_context(self):
        for text in ['api Cookie: session=synthetic-secret','api Authorization: Basic synthetic-secret']:
            result=self.engine.prepare(self.event(text));self.assertIsNone(result.model_input);self.assertEqual(result.category,'sensitive_input')


class UsageTests(unittest.TestCase):
    def test_hidden_key_labels_cannot_be_echoed_even_if_user_supplies_them(self):
        rules=[{'length':len(x),'digest':hashlib.sha256(x.casefold().encode()).hexdigest()} for x in ['aa','K2']]
        value=mask_labels('AA和K2的Token',rules)
        self.assertNotIn('AA',value);self.assertNotIn('K2',value);self.assertEqual(value,'[名称已隐藏]和[名称已隐藏]的Token')
    def test_four_buckets_are_not_double_counted_or_identified_by_key_name(self):
        item={'subject':'gentle','binding_available':True,'ordinary_input_tokens':10,'cache_read_tokens':20,'cache_write_tokens':30,'input_tokens':60,'output_tokens':40,'total_tokens':100}
        value=evidence('token_usage',{'items':[item],'period':'today'})
        text=format_usage(value,'gentle');self.assertIn('合计 100 Token',text)
        for name in ['example_key_label','aa','api_key_id','key_id']:self.assertNotIn(name,text)
    def test_deleted_binding_does_not_show_zero_as_real_usage(self):
        text=format_usage(evidence('token_usage',{'items':[{'subject':'flower','binding_available':False}],'period':'today'}))
        self.assertIn('关联不可用',text);self.assertNotIn('合计 0',text)
    def test_unapproved_named_key_is_not_silently_replaced_by_own_binding(self):
        query,reply=request_for('未知账户的用量',{'usage_target':'gentle'})
        self.assertIsNone(query);self.assertIn('群昵称',reply)
    def test_group_ranking_uses_public_names_and_never_emits_subject_or_key_labels(self):
        token={'subject':'gentle','binding_available':True,'ordinary_input_tokens':10,'cache_read_tokens':20,
               'cache_write_tokens':30,'input_tokens':60,'output_tokens':40,'total_tokens':100}
        value=evidence('member_usage',{'period':'today','rank':True,'token_items':[token],
            'usd_items':[{'subject':'gentle','binding_available':True,'usd_amount':'0.00001234'}]})
        text=format_member_usage(value,names={'gentle':'example_person_b'})
        self.assertIn('1. example_person_b：合计 100 Token',text)
        self.assertIn('1. example_person_b 12.34 µUSD',text)
        self.assertNotIn('$0.00',text)
        for value in ['gentle','member_','example_key_label','aa','api_key_id','key_id']:
            self.assertNotIn(value,text)
    def test_personal_report_formats_real_usd_and_does_not_claim_unsupported_periods(self):
        token={'subject':'gentle','binding_available':True,'ordinary_input_tokens':10,'cache_read_tokens':20,
               'cache_write_tokens':30,'input_tokens':60,'output_tokens':40,'total_tokens':100}
        value=evidence('member_usage',{'period':'today','rank':False,'token_items':[token],
            'usd_items':[{'subject':'gentle','binding_available':True,'usd_amount':'1.23000000'}]})
        text=format_member_usage(value,unit='tokens',names={'gentle':'example_person_b'})
        self.assertIn('合计 100 Token',text);self.assertIn('$1.23',text)
        self.assertIn('使用记录的实际扣费字段',text)

    def test_statistics_auto_scale_and_round_only_the_display(self):
        token={'subject':'gentle','binding_available':True,'ordinary_input_tokens':1_000_000,
               'cache_read_tokens':0,'cache_write_tokens':0,'input_tokens':1_000_000,
               'output_tokens':234_567,'total_tokens':1_234_567}
        result=evidence('member_usage',{'period':'today','rank':False,'token_items':[token],
            'usd_items':[{'subject':'gentle','binding_available':True,'usd_amount':'0.00001234'}]})
        text=format_member_usage(result,names={'gentle':'example_person_b'})
        self.assertIn('合计 1.23 M Token',text);self.assertIn('输入 1.00 M Token，输出 234.57 K Token',text)
        self.assertIn('12.34 µUSD',text)

    def test_money_keeps_usd_readable_and_never_rounds_a_positive_fee_to_zero(self):
        cases=[('0.25','$0.25'),('0.005','5.00 mUSD'),('0.00001234','12.34 µUSD'),
               ('999.995','1.00 KUSD'),('1e-15','1.00E-15 USD'),
               ('999999999999999999','1,000,000,000.00 BUSD')]
        for raw,rendered in cases:
            with self.subTest(raw=raw):
                result=evidence('member_usage',{'period':'today','rank':False,'token_items':[],
                    'usd_items':[{'subject':'gentle','binding_available':True,'usd_amount':raw}]})
                text=format_member_usage(result)
                self.assertIn(rendered,text)
                self.assertNotIn('0.00 pUSD',text)

    def test_token_counts_scale_through_peta_and_exa(self):
        for count,unit in [(10**15,'P Token'),(10**18,'E Token')]:
            with self.subTest(unit=unit):
                item={'subject':'gentle','binding_available':True,'ordinary_input_tokens':count,
                    'cache_read_tokens':0,'cache_write_tokens':0,'input_tokens':count,
                    'output_tokens':0,'total_tokens':count}
                result=evidence('member_usage',{'period':'today','rank':False,'token_items':[item],
                    'usd_items':[]})
                self.assertIn('合计 1.00 '+unit,format_member_usage(result))

    def test_explicit_usage_comment_is_descriptive_and_disclaims_work_performance(self):
        def token(subject,total):
            return {'subject':subject,'binding_available':True,'ordinary_input_tokens':total,
                'cache_read_tokens':0,'cache_write_tokens':0,'input_tokens':total,
                'output_tokens':0,'total_tokens':total}
        result=evidence('member_usage',{'period':'today','rank':True,'token_items':[
            token('flower',1_000_000),token('gentle',100)],'usd_items':[
            {'subject':'flower','binding_available':True,'usd_amount':'0.001'},
            {'subject':'gentle','binding_available':True,'usd_amount':'0.00001234'}]})
        text=format_member_usage(result,names={'flower':'example_person_a','gentle':'example_person_b'},commentary=True)
        self.assertIn('数据趋势：',text);self.assertIn('example_person_a',text);self.assertIn('example_person_b',text)
        self.assertIn('不代表工作量、效率或工作表现',text)
        plain=format_member_usage(result,names={'flower':'example_person_a','gentle':'example_person_b'})
        self.assertNotIn('数据趋势：',plain)
    def test_custom_date_range_is_shown_in_the_reply(self):
        bjt=datetime.timezone(datetime.timedelta(hours=8))
        start=int(datetime.datetime(2026,11,5,tzinfo=bjt).timestamp())
        end=int(datetime.datetime(2026,11,7,tzinfo=bjt).timestamp())
        token={'subject':'gentle','binding_available':True,'ordinary_input_tokens':1,'cache_read_tokens':0,
               'cache_write_tokens':0,'input_tokens':1,'output_tokens':2,'total_tokens':3}
        value=evidence('member_usage',{'period':'range','range_start_epoch':start,'range_end_epoch':end,
            'rank':False,'token_items':[token],'usd_items':[{'subject':'gentle','binding_available':True,'usd_amount':'0.25'}]})
        text=format_member_usage(value,names={'gentle':'example_person_b'})
        self.assertIn('北京时间2026-11-05至2026-11-06',text)
        self.assertIn('$0.25',text)

    def test_member_usage_result_never_prints_a_missing_display_mapping(self):
        value=evidence('member_usage',{'period':'today','rank':True,'token_items':[
            {'subject':'member_0123456789abcdef','binding_available':True,'ordinary_input_tokens':0,'cache_read_tokens':0,
             'cache_write_tokens':0,'input_tokens':0,'output_tokens':1,'total_tokens':1}]})
        text=format_member_usage(value)
        self.assertIn('显示映射不完整',text);self.assertNotIn('member_0123456789abcdef',text)
    def test_empty_ranking_is_not_reported_as_a_broken_member_binding(self):
        value=evidence('member_usage',{'period':'today','rank':True,'token_items':[],'usd_items':[]})
        text=format_member_usage(value)
        self.assertIn('暂无已记录用量',text);self.assertNotIn('联系管理员',text)


if __name__=='__main__':unittest.main()
