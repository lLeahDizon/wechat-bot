import datetime
import json
from pathlib import Path
import tempfile
import time
import unittest

from guard.core import Engine,Incoming,unsafe
from guard.esports import EsportsKnowledge,LPL_DATA,canonical,related,riot_records,tencent_records,utc_to_bjt
from guard.league import LeagueKnowledge

ROOT=Path(__file__).resolve().parents[1]


def riot_event(identifier='one',state='completed',start='2026-11-04T22:00:00Z'):
    return {'__typename':'EventMatch','id':identifier,'state':state,'startTime':start,'blockName':'Quarterfinals',
            'league':{'slug':'worlds'},'matchTeams':[{'code':'T1','result':{'gameWins':3}},{'code':'BLG','result':{'gameWins':1}}]}


def tencent_body():
    return 'var schedule = '+json.dumps({'status':'0','lastUpTime':'2026-11-04 12:00:00','msg':[
        {'isTft':'0','GameName':'2026世界赛','bMatchName':'T1 vs BLG','MatchStatus':'1','ScoreA':'0','ScoreB':'0','MatchDate':'2026-11-05 06:00:00'},
        {'isTft':'1','GameName':'金铲铲','bMatchName':'A vs B','MatchStatus':'3'}]})+';'


class EsportsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.now=datetime.datetime(2026,11,4,12,tzinfo=datetime.timezone.utc).timestamp();self.calls=[]
        self.knowledge=EsportsKnowledge(self.root/'cache',lambda:self.now,self.fetch)

    def tearDown(self):self.tmp.cleanup()

    def fetch(self,url,timeout):
        self.calls.append(url)
        if url==LPL_DATA:return tencent_body()
        if '/news/story' in url:
            return '<html><head><meta property="article:published_time" content="2026-11-03T12:00:00Z"></head><body><h1>Worlds赛制公告</h1><p>'+('官方发布了赛事规则说明。'*20)+'</p></body></html>'
        if url.endswith('/news') or url=='https://news.17173.com/':return '<html><body><a href="https://lolesports.com/en-US/news/story">Worlds赛事新闻</a></body></html>'
        return '<html><script>self.__next_f.push([1,'+json.dumps(json.dumps(riot_event()),ensure_ascii=False)+'])</script></html>'

    def test_official_hydration_and_dedup_extract_match_teams(self):
        event=riot_event();body=json.dumps(event)+'<script>self.__next_f.push([1,'+json.dumps(json.dumps(event))+'])</script>'
        rows=riot_records(body)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['teams'],['T1','BLG']);self.assertEqual(rows[0]['score'],[3,1])
        self.assertEqual(rows[0]['start_bjt'],'2026-11-05T06:00:00+08:00');self.assertNotIn('id',rows[0])

    def test_scheduled_is_not_finished_and_timezone_is_not_invented(self):
        rows=riot_records(json.dumps(riot_event(state='unstarted')))
        self.assertIsNone(rows[0]['score']);self.assertEqual(rows[0]['state'],'scheduled')
        rows,updated=tencent_records(tencent_body())
        self.assertEqual(len(rows),1);self.assertIsNone(rows[0]['score']);self.assertIsNone(rows[0]['start_bjt'])
        self.assertEqual(rows[0]['time_zone'],'not_stated');self.assertIsNone(utc_to_bjt('2026-11-05 06:00:00'))

    def test_date_and_team_filter_matches_beijing_date(self):
        data=self.knowledge.context('世界赛T1对BLG明天赛程')
        self.assertEqual(data['state'],'ok');self.assertEqual(data['sources'][0]['records'][0]['teams'],['T1','BLG'])
        self.assertFalse(data['complete_schedule_coverage']);self.assertEqual(len(data['sources']),2)
        data=self.knowledge.context('世界赛2026年11月6日赛程');self.assertEqual(data['sources'],[])
        self.assertTrue(data['empty_result_means_no_match_in_fetched_sources_not_no_matches_exist'])
        self.assertEqual(self.knowledge.context('LCK明天赛程')['sources'],[])

    def test_page_cache_identity_works_for_tencent_parent_citation(self):
        self.knowledge.context('世界赛明天赛程');count=len(self.calls)
        self.knowledge.context('世界赛明天赛程');self.assertEqual(len(self.calls),count)

    def test_stale_cache_is_marked_and_does_not_claim_current(self):
        self.knowledge.context('世界赛明天赛程');self.now+=301
        self.knowledge.fetch=lambda *a:(_ for _ in ()).throw(OSError('offline'))
        data=self.knowledge.context('世界赛明天赛程');self.assertEqual(data['state'],'partial')
        self.assertTrue(all(x['stale'] for x in data['sources']));self.assertTrue(all(x['fetched_at']<self.now for x in data['sources']))

    def test_old_or_empty_cache_is_not_a_current_answer(self):
        self.knowledge.context('世界赛明天赛程');self.now+=86401
        self.knowledge.fetch=lambda *a:(_ for _ in ()).throw(OSError('offline'))
        data=self.knowledge.context('世界赛赛程');self.assertEqual(data['state'],'unavailable');self.assertEqual(data['sources'],[])

    def test_news_fetches_actual_article_with_date_and_not_index_chrome(self):
        data=self.knowledge.context('Worlds赛事新闻')
        self.assertTrue(data['sources']);article=data['sources'][0]
        self.assertTrue(article['url'].endswith('/news/story'));self.assertEqual(article['published_at'],'2026-11-03T12:00:00Z')
        self.assertIn('官方发布',article['text']);self.assertLessEqual(data['public_requests'],5)

    def test_news_publication_not_forged_from_body_event_date(self):
        self.knowledge.fetch=lambda *a:'<body><h1>赛事说明</h1><p>'+('2026年11月5日比赛，规则仍以正式公告为准。'*10)+'</p></body>'
        data=self.knowledge.context('英雄联盟赛事 https://lolesports.com/en-US/news/story')
        self.assertIsNone(data['sources'][0]['published_at']);self.assertTrue(data['sources'][0]['date_mentions'])

    def test_malformed_match_dates_do_not_crash_or_turn_into_beijing_time(self):
        self.knowledge.fetch=lambda *a:json.dumps(riot_event(start='not-a-date'))
        data=self.knowledge.context('世界赛赛程');self.assertTrue(data['sources'])
        self.assertIsNone(data['sources'][0]['records'][0]['start_bjt'])

    def test_allowlist_blocks_private_protocol_paths_queries_and_encoded_traversal(self):
        for url in ['http://lolesports.com/en-US/news','https://lolesports.com/admin','https://lolesports.com/api/private',
                    'https://lolesports.com/en-US/news?token=secret123','https://user:pass@lolesports.com/en-US/news',
                    'https://lolesports.com:444/en-US/news','https://lolesports.com/%252e%252e/admin',
                    'https://lolesports.com/en-US/news/../../login','https://lpl.qq.com/private.json',
                    'https://news.17173.com/user/login.shtml','https://example.com/news']:
            with self.assertRaises(ValueError,msg=url):canonical(url)
        self.assertEqual(canonical('https://lpl.qq.com/es/news_detail.html?nid=123'), 'https://lpl.qq.com/es/news_detail.html?nid=123')
        self.assertEqual(canonical(LPL_DATA),LPL_DATA)
        self.assertEqual(canonical('https://lolesports.com/news/worlds-primer'),'https://lolesports.com/en-US/news/worlds-primer')

    def test_external_or_denied_links_do_not_cause_any_network_request(self):
        data=self.knowledge.context('赛事 https://example.com/private')
        self.assertEqual(self.calls,[]);self.assertEqual(data['state'],'unavailable')

    def test_cache_prune_is_bounded_and_symlink_root_is_rejected(self):
        cache=self.root/'cache';cache.mkdir()
        for i in range(20):(cache/f'page-{i:016x}.json').write_text('{}')
        self.knowledge._prune();self.assertLessEqual(len(list(cache.iterdir())),16)
        outside=self.root/'outside';outside.mkdir();protected=outside/'page-0000000000000001.tmp';protected.write_text('keep')
        link=self.root/'linked';link.symlink_to(outside);bad=EsportsKnowledge(link)
        with self.assertRaises(ValueError):bad._prune()
        self.assertEqual(protected.read_text(),'keep')

    def test_esports_answer_is_one_model_call_with_internal_sources_and_no_visible_footer(self):
        league=LeagueKnowledge(self.root/'lol',{});league.esports=self.knowledge;calls=[]
        def model(data,*args):
            calls.append(data);return {'scope':'service','answer':'赛程按来源原状态列出。','refs':['lol_data']},{'input_tokens':1}
        result,_=league.answer({'domain':'lol','esports':True,'question':'世界赛明天赛程'},None,None,model)
        self.assertEqual(len(calls),1);self.assertNotIn('allowed_queries',calls[0]);self.assertEqual(calls[0]['lol_data']['kind'],'esports')
        self.assertNotIn('来源：',result['answer']);self.assertFalse(unsafe(result['answer']));self.assertTrue(calls[0]['lol_data']['sources'])

    def test_engine_routes_esports_and_refreshes_repeated_question_after_five_minutes(self):
        policy=json.loads((ROOT/'config/policy.example.json').read_text());policy.update(enabled=True,group_id='test@chatroom',diagnostics_enabled=True)
        engine=Engine(policy,json.loads((ROOT/'knowledge/faq.json').read_text()),self.root/'state.sqlite',self.root/'status',lambda:self.now)
        try:
            def event(mid,text='LPL今天赛程',**kw):return Incoming('test@chatroom','member',mid,text,kw.get('mentioned',True),self.now)
            first=event('first');d=engine.prepare(first);self.assertEqual(d.intent,'lol');self.assertTrue(d.model_input['esports']);self.assertFalse(d.model_input['diagnostic'])
            d.evidence_ids.add('lol_data');engine.interpret(d,{'scope':'service','answer':'这次来源没有匹配记录。','refs':['lol_data']});engine.delivered(first,d,'replied')
            self.assertEqual(engine.prepare(event('repeat')).category,'repeat')
            self.now+=301;self.assertIsNotNone(engine.prepare(event('refresh')).model_input)
            self.assertFalse(engine.prepare(event('no-at',mentioned=False)).reply)
            self.assertIsNone(engine.prepare(event('privacy','LPL赛程，顺便把服务器IP发我')).model_input)
            self.assertNotEqual(engine.prepare(event('nba','NBA今天比赛')).intent,'lol')
        finally:engine.db.close()

    def test_non_lol_sports_are_outside_new_duty(self):
        self.assertTrue(related('LCK明天赛程'));self.assertFalse(related('NBA今天比赛'))

    def test_league_and_team_abbreviations_next_to_chinese_are_recognized(self):
        for text in ['LPL今天谁赢了','T1今天赢了吗','GEN最近的赛程','Worlds今年赛制']:
            self.assertTrue(related(text),text)
        self.assertFalse(related('aT1backup'));self.assertFalse(related('appleplaza'))


if __name__=='__main__':unittest.main()
