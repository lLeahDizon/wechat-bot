import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,MagicMock
from urllib.parse import urlsplit,parse_qs

from guard.predictions import PredictionKnowledge,final_observation,teams_from,render_analysis
from guard.schedule import OfficialSchedule,MAX_PAGES
from guard.league import LeagueKnowledge


class PredictionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.now=dt.datetime(2026,11,4,12,tzinfo=dt.timezone.utc).timestamp();self.calls=[]
        self.knowledge=PredictionKnowledge(self.root,lambda:self.now,self.page,self.feed,self.schedule)
    def tearDown(self):self.tmp.cleanup()
    def event(self,index,days=2):
        return {'__typename':'EventMatch','id':'match'+str(index),'state':'completed','startTime':dt.datetime.fromtimestamp(self.now-days*86400,dt.timezone.utc).isoformat(),
                'matchTeams':[{'code':'T1','teamId':'a','result':{'gameWins':3}},{'code':'BLG','teamId':'b','result':{'gameWins':1}}],
                'match':{'games':[{'id':str(100000000000+index*4+n),'state':'completed'} for n in range(4)]}}
    def page(self,url,timeout):return ''.join(json.dumps(self.event(i)) for i in range(3))
    def schedule(self,variables,timeout):return {'events':[self.event(i) for i in range(3)],'pages':{'newer':None,'older':None}}
    def frame(self,identifier):
        stamp=dt.datetime.fromtimestamp(self.now-86400,dt.timezone.utc).isoformat();meta={'patchVersion':'16.20'};frame={'rfc460Timestamp':stamp,'gameState':'finished'};extra=[]
        for name,team,offset in [('blue','a',0),('red','b',5)]:
            roster=[];players=[]
            for i,role in enumerate(['top','jungle','mid','bottom','support']):
                p={'participantId':offset+i+1,'kills':2,'deaths':1,'assists':4};players.append(p)
                roster.append({'participantId':p['participantId'],'esportsPlayerId':str(200000000000+offset+i),'summonerName':team+' player '+role,'role':role,'championId':'Ahri'})
                extra.append({**p,'championDamageShare':.2})
            meta[name+'TeamMetadata']={'esportsTeamId':team,'participantMetadata':roster};frame[name+'Team']={'totalGold':50000,'totalKills':10,'participants':players,'towers':5,'barons':1,'dragons':['ocean','fire']}
        return {'esportsGameId':identifier,'gameMetadata':meta,'frames':[frame]},{'frames':[{'rfc460Timestamp':stamp,'participants':extra}]}
    def feed(self,kind,identifier,timeout):self.calls.append((kind,identifier));return self.frame(identifier)[0 if kind=='window' else 1]

    def test_aliases_and_missing_teams_are_handled_before_network(self):
        self.assertEqual(teams_from('预测滔搏打京东'),['TES','JDG']);self.assertEqual(teams_from('T1 vs BLG'),['T1','BLG'])
        self.assertEqual(teams_from('预测GENG对T1'),['GEN','T1']);self.assertEqual(teams_from('GEN.G vs T1'),['GEN','T1'])
        result=self.knowledge.context('分析一下这场比赛');self.assertEqual(result['state'],'needs_teams');self.assertEqual(self.calls,[])

    def test_60_day_completed_events_and_per_map_player_statistics_are_deduplicated(self):
        result=self.knowledge.context('预测T1对BLG')
        self.assertEqual(result['state'],'usable_with_limits');self.assertFalse(result['complete_coverage']);self.assertFalse(result['calibrated_win_probability_available'])
        self.assertEqual(result['window_days'],60);self.assertEqual(len(self.calls),24)
        t1=result['statistics'][0];self.assertEqual(t1['series_count'],3);self.assertEqual(t1['map_wins'],9);self.assertEqual(t1['map_losses'],3)
        self.assertEqual(t1['player_maps_available'],12);self.assertEqual(t1['players'][0]['maps'],12);self.assertEqual(t1['players'][0]['kda'],6)
        self.assertEqual(t1['players'][0]['kill_participation'],.6)
        self.assertNotIn('team_id',json.dumps(result));self.assertNotIn('participantId',json.dumps(result))

    def test_cache_reuses_final_frames_and_never_keeps_chat_text(self):
        self.knowledge.context('预测T1对BLG');count=len(self.calls);self.knowledge.context('预测T1对BLG')
        self.assertEqual(len(self.calls),count);self.assertNotIn('预测', (self.root/'completed-games.json').read_text())

    def test_old_future_and_unfinished_events_are_excluded(self):
        old=self.event(0,61);future=self.event(1,-1);unfinished=self.event(2);unfinished['state']='inProgress'
        self.knowledge.page_fetch=lambda *a:json.dumps(old)+json.dumps(future)+json.dumps(unfinished)
        self.knowledge.schedule.fetch=lambda *a:{'events':[old,future,unfinished],'pages':{'newer':None}}
        result=self.knowledge.context('预测T1对BLG');self.assertEqual(result['state'],'insufficient');self.assertEqual(self.calls,[])

    def test_unavailable_player_data_is_not_substituted_with_memory(self):
        self.knowledge.feed_fetch=lambda *a:(_ for _ in ()).throw(OSError('offline'))
        result=self.knowledge.context('预测T1对BLG');self.assertEqual(result['state'],'insufficient')
        self.assertTrue(all(t['players']==[] for t in result['statistics']))

    def test_wrong_game_and_unfinished_or_empty_frames_are_rejected(self):
        self.knowledge.feed_fetch=lambda kind,identifier,timeout:self.feed(kind,'999999999999',timeout)
        self.assertEqual(self.knowledge.context('预测T1对BLG')['state'],'insufficient')
        window,details=self.frame('100000000000');window['frames'][-1]['gameState']='in_game'
        with self.assertRaises(ValueError):final_observation(window,details)
        window,details=self.frame('100000000000');window['frames'][-1]['blueTeam']['totalGold']=0
        with self.assertRaises(ValueError):final_observation(window,details)

    def test_inconsistent_or_unaligned_frames_are_rejected(self):
        window,details=self.frame('100000000000');window['frames'][-1]['blueTeam']['totalKills']=99
        with self.assertRaises(ValueError):final_observation(window,details)
        window,details=self.frame('100000000000');details['frames'][-1]['rfc460Timestamp']='2020-01-01T00:00:00Z'
        with self.assertRaises(ValueError):final_observation(window,details)

    def test_only_official_fixed_feed_paths_are_allowed(self):
        for kind,identifier in [('admin','123456789012'),('window','../token'),('details','https://evil.example')]:
            with self.assertRaises(ValueError):self.knowledge._feed(kind,identifier,1)

    def test_feed_queries_respect_official_broadcast_delay(self):
        response=MagicMock();response.__enter__.return_value=response;response.read.return_value=b'{"frames":[]}'
        opener=MagicMock();opener.open.return_value=response
        with patch('guard.predictions.urllib.request.build_opener',return_value=opener):self.knowledge._feed('window','100000000000',1)
        url=opener.open.call_args[0][0];value=parse_qs(urlsplit(url).query)['startingTime'][0]
        requested=dt.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
        self.assertLessEqual(requested+10,self.now-20)

    def test_prediction_uses_one_model_call_and_keeps_sources_internal(self):
        league=LeagueKnowledge(self.root/'static',{});league.predictions=self.knowledge;calls=[]
        def model(data,*args):calls.append(data);return {'scope':'service','answer':'两队样本相近，判断把握偏低。','refs':['lol_data']},{}
        result,_=league.answer({'domain':'lol','esports':True,'question':'分析T1对BLG','context':[]},None,None,model)
        self.assertEqual(len(calls),1);self.assertEqual(calls[0]['lol_data']['kind'],'prediction');self.assertNotIn('allowed_queries',calls[0]);self.assertNotIn('来源',result['answer'])
        self.assertEqual(len(calls[0]['lol_data']['position_comparisons']),5)
        self.assertNotIn('KDA',result['answer']);self.assertLessEqual(len(result['answer'].splitlines()),3)

    def test_all_official_competitions_and_later_pages_are_discovered(self):
        seen=[]
        def pages(v,timeout):
            seen.append(v)
            event=self.event(len(seen));event['league']={'slug':'cross-region-cup' if len(seen)==1 else 'regional-league'}
            return {'events':[event,event],'pages':{'newer':'next:1' if len(seen)==1 else None}}
        self.knowledge.schedule.fetch=pages
        result=self.knowledge.context('预测T1对BLG')
        self.assertEqual(len(seen),2);self.assertEqual(seen[1]['pageToken'],'next:1');self.assertNotIn('leagues',seen[0])
        self.assertEqual(result['statistics'][0]['series_count'],2)
        self.assertEqual(result['statistics'][0]['competitions'],['cross-region-cup','regional-league'])
        self.assertTrue(result['discovery']['published_window_exhausted'])

    def test_small_sample_still_compares_all_positions_and_concludes(self):
        self.knowledge.schedule.fetch=lambda *a:{'events':[self.event(0)],'pages':{'newer':None}}
        data=self.knowledge.context('预测T1对BLG');self.assertEqual(data['state'],'usable_with_limits');self.assertEqual(data['confidence'],'low')
        answer=render_analysis({'scope':'service','answer':'样本接近，当前无法确定优势。'},data)['answer']
        self.assertIn('结论：',answer);self.assertIn('样本较少',answer)
        self.assertEqual(len(data['position_comparisons']),5)

    def test_missing_player_side_keeps_five_comparisons_and_honest_conclusion(self):
        def feed(kind,identifier,timeout):
            window,details=self.frame(identifier);window['gameMetadata']['redTeamMetadata']['esportsTeamId']='other'
            return window if kind=='window' else details
        self.knowledge.feed_fetch=feed;data=self.knowledge.context('预测T1对BLG')
        answer=render_analysis({'scope':'service','answer':'不允许把此断言当数据'},data)['answer']
        self.assertEqual(data['state'],'insufficient');self.assertEqual(len(data['position_comparisons']),5)
        self.assertIn('无法可靠判断',answer);self.assertIn('数据不足',answer);self.assertNotIn('KDA',answer)

    def test_stable_identity_joins_prior_team_but_same_name_never_joins(self):
        window,details=self.frame('999999999999');value=final_observation(window,details)
        value['sides'][0]['team_id']='old-team'
        # Same display name as the first target player but a different stable ID.
        value['sides'][1]['players'][0]['name']=value['sides'][0]['players'][0]['name']
        value['sides'][1]['players'][0]['kills']=99
        self.root.mkdir(exist_ok=True)
        self.knowledge._save({'999999999999':{'schema':2,'fetched_at':self.now,'competition':'other-official-cup','data':value}})
        data=self.knowledge.context('预测T1对BLG');player=data['statistics'][0]['players'][0]
        self.assertEqual(player['maps'],13);self.assertEqual(player['other_team_maps'],1);self.assertEqual(player['kills_per_map'],2)
        self.assertIn('other-official-cup',player['competitions'])
        self.assertNotIn('player_ref',json.dumps(data));self.assertNotIn('esportsPlayerId',json.dumps(data))

    def test_unverified_identity_is_not_merged_by_name(self):
        def feed(kind,identifier,timeout):
            window,details=self.frame(identifier)
            for side in ['blue','red']:
                for p in window['gameMetadata'][side+'TeamMetadata']['participantMetadata']:p.pop('esportsPlayerId')
            return window if kind=='window' else details
        self.knowledge.feed_fetch=feed;data=self.knowledge.context('预测T1对BLG')
        self.assertTrue(all(p['maps']==1 and not p['identity_verified'] for p in data['statistics'][0]['players']))

    def test_old_cache_schema_is_refetched_with_stable_identities(self):
        self.root.joinpath('completed-games.json').write_text(json.dumps({'100000000000':{'fetched_at':self.now,'data':{}}}))
        self.knowledge.context('预测T1对BLG');self.assertIn(('window','100000000000'),self.calls)

    def test_schedule_failure_falls_back_to_global_and_region_pages(self):
        pages=[]
        self.knowledge.schedule.fetch=lambda *a:(_ for _ in ()).throw(OSError('offline'))
        self.knowledge.page_fetch=lambda url,timeout:pages.append(url) or self.page(url,timeout)
        result=self.knowledge.context('预测T1对BLG')
        self.assertIn('https://lolesports.com/en-US/',pages);self.assertFalse(result['discovery']['published_window_exhausted'])
        self.assertEqual(result['state'],'usable_with_limits')

    def test_schedule_page_budget_cycles_and_cache_are_explicit(self):
        import time
        count=[]
        def pages(v,t):
            count.append(v);return {'events':[self.event(0)],'pages':{'newer':'same'}}
        self.knowledge.schedule.fetch=pages
        result=self.knowledge.schedule.collect(self.now-60*86400,self.now,time.monotonic()+5)
        self.assertFalse(result['published_window_exhausted']);self.assertEqual(result['failure'],'schedule_cursor_invalid')
        result=self.knowledge.schedule.collect(self.now-60*86400,self.now,time.monotonic()+5)
        self.assertEqual(result['requests'],0);self.assertEqual(len(count),2)

    def test_supplemental_creator_is_not_mistaken_for_watched_video(self):
        result=self.knowledge.context('预测T1对BLG，参考苦练李香兰')
        self.assertEqual(result['preferred_supplemental_creator'],'苦练李香兰');self.assertEqual(result['supplemental_opinions'],[])

    def test_summary_highlights_key_positions_and_heroes_without_stat_dump(self):
        data=self.knowledge.context('预测T1对BLG')
        result=render_analysis({'scope':'service','prediction_analysis':{'overall':'样本接近，胜负仍取决于阵容。','focus':['中野联动是关键，留意阿狸的选用','辅助的开团时机可能改变走势']}},data)
        self.assertIn('样本接近',result['answer']);self.assertIn('阿狸',result['answer']);self.assertIn('中野联动',result['answer'])
        self.assertNotIn('KDA',result['answer']);self.assertNotIn('6.0/6.0',result['answer']);self.assertIn('lol_data',result['refs'])
        self.assertLessEqual(len(result['answer'].splitlines()),3);self.assertLess(len(result['answer']),240)

    def test_redundant_and_malformed_focus_stays_bounded(self):
        data=self.knowledge.context('预测T1对BLG')
        result=render_analysis({'scope':'service','prediction_analysis':{'overall':'结论'*100,'focus':['关注中野','关注中野',None,{'not':'text'},'英雄BP'*100,'不应显示的第三项']}},data)
        self.assertEqual(result['answer'].count('关注中野'),1);self.assertNotIn('不应显示的第三项',result['answer']);self.assertLess(len(result['answer']),240)

    def test_renamed_players_merge_but_substitutes_do_not(self):
        def feed(kind,identifier,timeout):
            window,details=self.frame(identifier)
            player=window['gameMetadata']['blueTeamMetadata']['participantMetadata'][0]
            if identifier!='100000000000':player['summonerName']='OLD old-tag'
            return window if kind=='window' else details
        self.knowledge.feed_fetch=feed
        data=self.knowledge.context('预测T1对BLG');player=data['statistics'][0]['players'][0]
        self.assertEqual(player['maps'],12);self.assertEqual(player['name'],'a player top')
        self.root.joinpath('completed-games.json').unlink()
        def substitute(kind,identifier,timeout):
            window,details=self.frame(identifier)
            if identifier=='100000000000':window['gameMetadata']['blueTeamMetadata']['participantMetadata'][0]['esportsPlayerId']='299999999999'
            return window if kind=='window' else details
        self.knowledge.feed_fetch=substitute;data=self.knowledge.context('预测T1对BLG')
        self.assertEqual(data['statistics'][0]['players'][0]['maps'],1);self.assertEqual(data['statistics'][0]['observed_roster_variants'],2)

    def test_less_common_regional_team_pairs_are_recognized(self):
        from guard.esports import related
        self.assertEqual(teams_from('预测BFX对TLAW'),['BFX','TLAW']);self.assertTrue(related('预测BFX对TLAW'))

    def test_latest_roster_uses_finished_time_not_game_list_order(self):
        self.knowledge.schedule.fetch=lambda *a:{'events':[self.event(0)],'pages':{'newer':None}}
        def feed(kind,identifier,timeout):
            window,details=self.frame(identifier)
            if identifier=='100000000003':
                stamp=dt.datetime.fromtimestamp(self.now-1000,dt.timezone.utc).isoformat()
                window['frames'][-1]['rfc460Timestamp']=stamp;details['frames'][-1]['rfc460Timestamp']=stamp
                window['gameMetadata']['blueTeamMetadata']['participantMetadata'][0]['summonerName']='Latest player'
            return window if kind=='window' else details
        self.knowledge.feed_fetch=feed;data=self.knowledge.context('预测T1对BLG')
        self.assertEqual(data['statistics'][0]['players'][0]['name'],'Latest player')

    def test_schedule_transport_is_fixed_anonymous_and_uses_persisted_query(self):
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=b'{"data":{"esports":{"events":[],"pages":{"newer":null}}}}'
        opener=MagicMock();opener.open.return_value=response
        with patch('guard.schedule.urllib.request.build_opener',return_value=opener):self.knowledge.schedule._fetch({'hl':'en-US'},1)
        request=opener.open.call_args[0][0];parsed=urlsplit(request.full_url);query=parse_qs(parsed.query)
        self.assertEqual(parsed.hostname,'lolesports.com');self.assertEqual(parsed.path,'/api/gql')
        self.assertEqual(query['operationName'],['homeEvents']);self.assertNotIn('query',query)
        self.assertNotIn('Authorization',request.headers)


if __name__=='__main__':unittest.main()
