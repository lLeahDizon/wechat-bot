import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from diagnostics.client import QueryBudget
from diagnostics.protocol import Rejected,evidence,public_result,validate
from diagnostics.server import QueryServer
from diagnostics.sources import gateway_logs,Sources


class ProtocolTests(unittest.TestCase):
    def test_rejects_commands_sql_paths_arbitrary_targets_and_long_windows(self):
        for value in [{'op':'restart'},{'op':'services','command':'docker stop gateway'},
                      {'op':'events','sql':'select 1'},{'op':'traffic','path':'/etc/passwd'},
                      {'op':'traffic','window_minutes':True},{'op':'traffic','window_minutes':1441},
                      {'op':'token_usage','target':'unapproved-key'},
                      {'op':'token_usage','target':'flower','key_id':1},
                      {'op':'member_usage','target':'all','metrics':['tokens'],'rank':True,'key_id':1},
                      {'op':'member_usage','target':'api_key_id','metrics':['tokens'],'rank':False},
                      {'op':'member_usage','target':'all','metrics':['tokens'],'rank':False},
                      {'op':'member_usage','target':'flower','metrics':[['tokens']],'rank':False}]:
            with self.subTest(value=value),self.assertRaises(Rejected):validate(value)
        for value in [
            {'op':'member_usage','target':'flower','metrics':['usd'],'period':'range','rank':False,
             'range_start_epoch':True,'range_end_epoch':1800000100},
            {'op':'member_usage','target':'flower','metrics':['tokens'],'period':'range','rank':False,
             'range_start_epoch':1800000100,'range_end_epoch':1800000100},
            {'op':'member_usage','target':'flower','metrics':['tokens'],'period':'today','rank':False,
             'range_start_epoch':1800000000,'range_end_epoch':1800000100},
        ]:
            with self.subTest(value=value),self.assertRaises(Rejected):validate(value)

    def test_member_usage_protocol_allows_only_bound_subject_shapes_and_explicit_metrics(self):
        value=validate({'op':'member_usage','target':'all','metrics':['tokens','usd'],'period':'today','rank':True})
        self.assertEqual(value['target'],'all');self.assertTrue(value['rank'])
        value=validate({'op':'member_usage','target':['flower','member_0123456789abcdef'],
                        'metrics':['tokens'],'period':'window','window_minutes':60,'rank':False})
        self.assertEqual(value['target'],['flower','member_0123456789abcdef'])
        value=validate({'op':'member_usage','target':'all','metrics':['usd'],'period':'window',
                        'window_minutes':60,'rank':True})
        self.assertEqual(value['metrics'],['usd'])
        value=validate({'op':'member_usage','target':'flower','metrics':['tokens','usd'],'period':'range',
                        'range_start_epoch':1793510400,'range_end_epoch':1793770000,'rank':False})
        self.assertEqual(value['range_start_epoch'],1793510400)
        value=validate({'op':'member_usage','target':'all','metrics':['tokens'],'period':'today',
                        'rank':True,'commentary':True})
        self.assertTrue(value['commentary'])
        with self.assertRaises(Rejected):
            validate({'op':'member_usage','target':'all','metrics':['tokens'],'period':'today','rank':True,'commentary':'yes'})

    def test_result_validation_does_not_allow_credentials_addresses_or_paths(self):
        for value in ['Bearer '+'a'*40,'主机203.0.113.5','/etc/service/config','wxid_private']:
            self.assertFalse(public_result(evidence('resources',{'value':value})))
        self.assertTrue(public_result(evidence('resources',{'cpu_percent':30,'available':True})))
        self.assertTrue(public_result(evidence('token_usage',{'total_tokens':14567891234})))
        self.assertTrue(public_result(evidence('configuration',{'source_model':'gpt-*'})))

    def test_canonical_evidence_hash_is_not_mistaken_for_a_phone_number(self):
        # This timestamp produces a valid opaque hash with eleven adjacent digits.
        value=evidence('services',{'healthy':True},captured_at=1793794610.704691)
        self.assertTrue(public_result(value))

    def test_evidence_id_must_match_content_and_nested_identity_stays_private(self):
        value=evidence('services',{'healthy':True});value['id']='diag-0000000000000000'
        self.assertFalse(public_result(value))
        self.assertFalse(public_result(evidence('services',{'nested':{'id':'wxid_private'}})))

    def test_socket_interface_and_five_query_budget(self):
        class Readers:
            count=0
            def execute(self,request):self.count+=1;return {'healthy':True}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'q.sock';readers=Readers()
            with QueryServer(path,readers,{os.getuid()},check_peer=False) as server:
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                budget=QueryBudget(path)
                for _ in range(5):self.assertEqual(budget.query({'op':'services'})['state'],'ok')
                self.assertEqual(budget.query({'op':'services'})['state'],'unavailable')
                self.assertEqual(readers.count,5)
                server.shutdown();thread.join()

    def test_expired_budget_never_connects_or_queries(self):
        clock=[0.0];budget=QueryBudget('/unavailable',lambda:clock[0]);budget.spent=20
        self.assertEqual(budget.query({'op':'resources'})['data']['reason'],'query_budget_exhausted')
        self.assertEqual(budget.count,0)

    def test_postgres_json_aggregate_can_span_multiple_output_lines(self):
        value={'items':[{'subject':'flower','total_tokens':10},{'subject':'gentle','total_tokens':20}]}
        result=SimpleNamespace(returncode=0,stdout=json.dumps(value,indent=2),stderr='')
        with patch('diagnostics.sources.subprocess.run',return_value=result) as run:
            self.assertEqual(Sources().sql('SELECT approved_summary'),value)
            self.assertIn('BEGIN READ ONLY',run.call_args.kwargs['input'])
            self.assertIn("statement_timeout='2500ms'",run.call_args.kwargs['input'])

    def test_member_usage_query_uses_fixed_views_and_record_cost_for_the_requested_period(self):
        source=Sources()
        result={'token_items':[],'usd_items':[]}
        with patch.object(source,'sql',return_value=result) as query:
            data=source.member_usage('all',['tokens','usd'],'today',15,True)
        sql=query.call_args.args[0]
        self.assertIn('bot_diagnostics.allowed_subjects',sql)
        self.assertIn('bot_diagnostics.allowed_tokens',sql)
        self.assertIn('sum(u.cache_creation_tokens)',sql)
        self.assertIn('sum(u.actual_cost)',sql)
        self.assertNotIn('u.cache_write_tokens',sql)
        self.assertIn("AT TIME ZONE 'Asia/Shanghai'",sql)
        self.assertNotIn('public.api_keys',sql)
        self.assertNotIn('api_key_id',sql)
        self.assertNotIn('allowed_quota_usage',sql)
        self.assertEqual(data['period'],'today');self.assertTrue(data['rank'])
        with patch.object(source,'sql',return_value=result) as query:
            data=source.member_usage('flower',['tokens','usd'],'range',15,False,1793510400,1793770000)
        ranged=query.call_args.args[0]
        self.assertIn('to_timestamp(1793510400)',ranged)
        self.assertIn('to_timestamp(1793770000)',ranged)
        self.assertIn('sum(u.actual_cost)',ranged)
        self.assertEqual(data['range_start_epoch'],1793510400)


class GatewayLogTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.path=Path(self.folder.name)/'access.log';self.now=1800000000
    def tearDown(self):self.folder.cleanup()
    def write(self,rows):self.path.write_text(''.join(json.dumps(x)+'\n' for x in rows))
    def row(self,age,status=200,route='/v1/responses'):
        return {'ts':self.now-age,'status':status,'duration':1,'user_id':'never_return_identity',
            'request':{'method':'POST','uri':route,'remote_ip':'203.0.113.6','headers':{'Authorization':['Bearer secret']}}}
    def test_exact_http_rate_excludes_non_model_routes_and_private_fields(self):
        rows=[self.row(400)]+[self.row(i,500 if i<2 else 200) for i in range(10)]+[self.row(2,500,'/admin')]
        self.write(rows);data=gateway_logs(self.path,5,self.now)
        self.assertTrue(data['coverage_complete']);self.assertEqual(data['observed_completed_requests'],10)
        self.assertEqual(data['http_5xx_rate'],.2)
        text=json.dumps(data)
        for value in ['203.0.113.6','secret','never_return_identity','/admin']:self.assertNotIn(value,text)
        self.assertIn('http_200_stream',data['limitations'])
    def test_truncated_or_corrupt_window_does_not_claim_global_error_rate(self):
        self.write([self.row(400)]+[self.row(i) for i in range(100)])
        self.assertIsNone(gateway_logs(self.path,5,self.now,byte_limit=600)['http_5xx_rate'])
        self.path.write_text(self.path.read_text()+'invalid\n')
        self.assertFalse(gateway_logs(self.path,5,self.now)['coverage_complete'])
    def test_query_string_is_not_returned_and_route_matching_remains_exact(self):
        self.write([self.row(400),self.row(1,500,'/v1/responses?private=secret'),self.row(1,500,'/v1/responses/other')])
        data=gateway_logs(self.path,5,self.now);self.assertEqual(data['observed_completed_requests'],1)
        self.assertNotIn('private',json.dumps(data))


if __name__=='__main__':unittest.main()
