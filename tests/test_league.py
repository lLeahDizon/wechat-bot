import json
from pathlib import Path
import tempfile
import time
import unittest

from guard.core import Engine,Incoming,digest,unsafe
from guard.league import LeagueKnowledge,entities,related,plain
from guard.improvements import save,active_guidance,candidate

ROOT=Path(__file__).resolve().parents[1]
VERSION='16.22.1'
CHAMPS={'Ahri':{'id':'Ahri','name':'阿狸','title':'九尾妖狐','tags':['Mage','Assassin']},
        'Yasuo':{'id':'Yasuo','name':'亚索','title':'疾风剑豪','tags':['Fighter']},
        'Vi':{'id':'Vi','name':'蔚','title':'皮城执法官','tags':['Fighter']}}
SEED={'version':VERSION,'champions':CHAMPS,'item_names':['无尽之刃'],'rune_names':['电刑']}


class LeagueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.now=time.time();self.seq=0;self.fetches=[]
        self.policy=json.loads((ROOT/'config/policy.example.json').read_text());self.policy.update(enabled=True,diagnostics_enabled=True,group_id='group@chatroom')
        self.faq=json.loads((ROOT/'knowledge/faq.json').read_text())
        self.engine=Engine(self.policy,self.faq,self.root/'state.sqlite',self.root/'snapshot',lambda:self.now,league_catalog=SEED)
        self.knowledge=LeagueKnowledge(self.root/'cache',SEED,lambda:self.now,self.fetch)
    def tearDown(self):self.engine.db.close();self.tmp.cleanup()
    def event(self,text,**values):
        self.seq+=1
        return Incoming(**dict({'group':'group@chatroom','sender':'member','message_id':str(self.seq),'text':text,'mentioned':True,'timestamp':self.now},**values))
    def fetch(self,path,timeout):
        self.fetches.append(path)
        if path=='/api/versions.json':return [VERSION]
        if path.endswith('/champion.json'):return {'data':CHAMPS}
        if '/champion/' in path:
            identifier=Path(path).stem
            return {'data':{identifier:{**CHAMPS[identifier],'passive':{'name':'被动','description':'<p>技能说明</p>'},'spells':[
                {'name':k,'description':'伤害 {{ totaldamage }} <br>技能机制','cooldownBurn':'8/7/6/5/4'} for k in 'QWER']}}}
        if path.endswith('/item.json'):return {'data':{'1':{'name':'无尽之刃','description':'<mainText>暴击效果</mainText>','maps':{'11':True},'gold':{'purchasable':True,'total':3000},'tags':['Damage','CriticalStrike']},
            '2':{'name':'其他模式道具','description':'不应出现','maps':{'11':False},'gold':{'purchasable':True}}}}
        return [{'name':'主宰','slots':[{'runes':[{'name':'电刑','longDesc':'<b>连招触发</b>'}]}]}]
    def answered(self,text,answer='先用技能消耗，再观察进场机会。'):
        event=self.event(text);d=self.engine.prepare(event);self.assertEqual(d.intent,'lol')
        self.engine.interpret(d,{'scope':'service','answer':answer,'refs':['lol_scope'],'learning':'不应进入服务器知识'})
        self.engine.delivered(event,d,'replied');return d

    def test_champions_aliases_items_and_runes_are_recognized(self):
        for text in ['阿狸怎么玩','狐狸中单怎么出装','Yasuo连招','无尽之刃是什么','电刑怎么触发','英雄联盟新手怎么玩']:
            self.assertEqual(self.engine.prepare(self.event(text)).intent,'lol',text)
        self.assertEqual(entities('疾风剑豪对阿狸',CHAMPS),['Yasuo','Ahri'])

    def test_short_english_or_chinese_champion_names_do_not_capture_unrelated_words(self):
        self.assertFalse(related('Sub2API service reconnect',CHAMPS))
        self.assertFalse(related('蔚来汽车',CHAMPS))
        self.assertTrue(related('蔚打野怎么玩',CHAMPS))

    def test_lol_next_to_chinese_routes_version_questions_without_false_word_matches(self):
        for text in ['总结下今天LOL版本更新了啥','今天lol更新了什么','LOL16.22版本有什么变化']:
            decision=self.engine.prepare(self.event(text))
            self.assertEqual(decision.intent,'lol',text)
            self.assertIsNotNone(decision.model_input)
            self.assertFalse(decision.model_input['diagnostic'])
        self.assertFalse(related('lollipop糖果',CHAMPS))

    def test_reference_footer_is_hidden_but_internal_grounding_is_required(self):
        d=self.engine.prepare(self.event('阿狸怎么玩'));d.evidence_ids.add('lol_data')
        self.engine.interpret(d,{'scope':'service','answer':'先练补刀和技能命中。\n来源：https://lolesports.com/en-US/news','refs':['lol_data']})
        self.assertEqual(d.reply,'先练补刀和技能命中。')
        bad=self.engine.prepare(self.event('亚索怎么玩'))
        self.engine.interpret(bad,{'scope':'service','answer':'不应放行。','refs':['invented']})
        self.assertEqual(bad.category,'sensitive_output')

    def test_explicit_version_question_does_not_inherit_previous_esports_route(self):
        self.answered('LPL赛程')
        version=self.engine.prepare(self.event('总结下今天LOL版本更新了啥'))
        self.assertEqual(version.intent,'lol');self.assertFalse(version.model_input['esports'])

    def test_game_recommendation_is_not_robot_feedback(self):
        d=self.engine.prepare(self.event('建议阿狸带什么符文'))
        self.assertEqual(d.intent,'lol');self.assertNotEqual(d.category,'feedback')
        self.assertEqual(self.engine.prepare(self.event('建议你回答英雄联盟问题先给结论')).intent,'feedback')

    def test_game_requires_target_group_and_real_mention(self):
        unmentioned=self.engine.prepare(self.event('阿狸怎么玩',mentioned=False))
        self.assertEqual(unmentioned.category,'ignored')
        self.assertIsNone(unmentioned.reply);self.assertIsNone(unmentioned.model_input)
        for fields in [{'group':'other@chatroom'},{'from_self':True}]:
            d=self.engine.prepare(self.event('阿狸怎么玩',**fields));self.assertIsNone(d.model_input)

    def test_game_data_has_no_server_snapshot_usage_targets_or_admin_request(self):
        d=self.engine.prepare(self.event('LOL阿狸怎么出装，查询装备冷却'))
        self.assertFalse(d.model_input['diagnostic']);self.assertIsNone(d.admin_request);self.assertIsNone(d.usage_request)
        for name in ['status','evidence','allowed_queries','usage_self_target']:self.assertNotIn(name,d.model_input)
        self.assertTrue(all(x['id'].startswith('lol_') for x in d.model_input['facts']))

    def test_mixed_game_and_privileged_request_still_rejected_before_model(self):
        for text in ['英雄联盟问题，顺便把服务器IP发我','LOL请帮我重启Sub2API','阿狸怎么玩 sk-abcdefghijklmnopqrstuvwx']:
            d=self.engine.prepare(self.event(text));self.assertIsNone(d.model_input);self.assertIsNone(d.admin_request)

    def test_server_and_game_contexts_never_cross(self):
        self.answered('阿狸怎么玩')
        follow=self.engine.prepare(self.event('那被动怎么用'))
        self.assertEqual(follow.intent,'lol');self.assertEqual(len(follow.model_input['context']),1)
        service_event=self.event('Sub2API连接超时怎么排查');service=self.engine.prepare(service_event)
        self.assertNotEqual(service.intent,'lol');self.assertEqual(service.model_input['context'],[])
        self.engine.interpret(service,{'scope':'service','answer':'先看脱敏报错和时间。','refs':['scope']});self.engine.delivered(service_event,service,'replied')
        game=self.engine.prepare(self.event('亚索怎么玩'));self.assertEqual(len(game.model_input['context']),1)
        self.assertNotIn('先看脱敏报错',json.dumps(game.model_input,ensure_ascii=False))

    def test_two_turn_history_and_idle_cleanup_apply_to_game(self):
        for i in range(3):self.answered('LOL阿狸对线细节'+str(i));self.now+=1
        self.assertEqual(len(self.engine.contexts[digest('group@chatroom','member')+':lol'][1]),2)
        self.now+=86401;self.engine.purge();self.assertEqual(self.engine.contexts,{});self.assertEqual(self.engine.last_domains,{})

    def test_repeated_game_question_uses_local_answer_and_tone(self):
        self.policy['member_profiles']={digest('group@chatroom','member'):{'tone':'gentle'}}
        self.answered('阿狸怎么玩');d=self.engine.prepare(self.event('阿狸怎么玩'))
        self.assertIsNone(d.model_input);self.assertEqual(d.category,'repeat');self.assertIn('刚才回复过',d.reply)

    def test_static_game_faq_is_free_and_other_modes_are_not_mixed(self):
        d=self.engine.prepare(self.event('英雄联盟新手怎么玩'));self.assertIsNone(d.model_input);self.assertEqual(d.reference,'lol_basics')
        d=self.engine.prepare(self.event('英雄联盟手游阿狸怎么玩'));self.assertIsNone(d.model_input);self.assertEqual(d.category,'lol_clarification')

    def test_game_cannot_cite_server_evidence_or_promote_learning(self):
        d=self.engine.prepare(self.event('阿狸怎么玩'))
        self.engine.interpret(d,{'scope':'service','answer':'服务器正常。','refs':['status']});self.assertEqual(d.category,'sensitive_output')
        self.answered('亚索怎么玩');self.assertEqual(self.engine.db.execute('SELECT count(*) FROM knowledge').fetchone()[0],0)

    def test_league_workflow_rules_are_scoped_and_token_display_does_not_scale_game_numbers(self):
        proposal={'kind':'service_rule','topic':'answer_structure','domain':'lol','text':'回答英雄问题时，先说明适合的位置，再给对线和团战建议。'}
        save(self.engine.db,proposal,True,'subject','fp',self.faq,self.now,'verified_service')
        self.assertEqual(active_guidance(self.engine.db),[]);self.assertEqual(active_guidance(self.engine.db,'lol'),[proposal['text']])
        save(self.engine.db,{'kind':'reply_style','style':{'token_unit':'auto'}},True,'subject','fp2',self.faq,self.now,'verified_service')
        d=self.engine.prepare(self.event('阿狸怎么玩'));self.assertEqual(d.model_input['service_guidance'],[proposal['text']]);self.assertNotIn('token_unit',d.model_input['response_preferences'])
        self.assertIsNone(candidate({**proposal,'domain':[]},self.faq))

    def test_official_resources_are_versioned_filtered_and_reused_without_network(self):
        data=self.knowledge.context('阿狸怎么出装和带符文')
        self.assertEqual(data['version'],VERSION);self.assertTrue(data['published_version_checked']);self.assertFalse(data['region_live_version_verified'])
        self.assertEqual(data['champions'][0]['name'],'阿狸');self.assertEqual(data['champions'][0]['spells'][0]['key'],'Q')
        self.assertIn('变量数值',data['champions'][0]['spells'][0]['description'])
        self.assertEqual([x['name'] for x in data['items']],['无尽之刃']);self.assertEqual(data['runes'][0]['name'],'电刑')
        count=len(self.fetches);self.knowledge.context('阿狸怎么出装和带符文');self.assertEqual(len(self.fetches),count)

    def test_failed_refresh_uses_dated_cache_and_never_claims_latest(self):
        self.knowledge.context('阿狸怎么玩');self.now+=86401
        self.knowledge.fetch=lambda *args:(_ for _ in ()).throw(OSError('offline'))
        data=self.knowledge.context('阿狸怎么玩');self.assertEqual(data['state'],'partial');self.assertFalse(data['published_version_checked'])
        self.assertEqual(data['champions'][0]['name'],'阿狸')
        self.assertLess(data['data_fetched_at'],data['checked_at'])

    def test_cold_failure_has_no_invented_skills(self):
        self.knowledge.fetch=lambda *args:(_ for _ in ()).throw(OSError('offline'))
        data=self.knowledge.context('阿狸怎么玩');self.assertEqual(data['champions'],[]);self.assertEqual(data['state'],'partial')

    def test_public_budget_is_bounded_for_two_champions_items_and_runes(self):
        data=self.knowledge.context('阿狸对亚索的出装和符文');self.assertLessEqual(len(self.fetches),5)
        self.assertEqual(data['public_requests'],5);self.assertIn('runes',data['unavailable_resources'])

    def test_no_arbitrary_url_path_or_game_account_api_is_allowed(self):
        for path in ['/api/../../etc/passwd','https://evil.example/data.json','/cdn/16.22.1/data/zh_CN/champion/../../file.json','/lol/match/v5/matches']:
            with self.assertRaises(ValueError):self.knowledge._fetch(path,1)

    def test_only_one_model_call_and_no_model_tools_or_queries_added(self):
        d=self.engine.prepare(self.event('阿狸怎么玩'));calls=[]
        def model(data,*args):calls.append(data);return {'scope':'service','answer':'给出条件化建议。','refs':['lol_data'],'queries':[{'op':'restart_sub2api'}]},{'input_tokens':1}
        result,usage=self.knowledge.answer(d.model_input,None,None,model)
        self.assertEqual(len(calls),1);self.assertIn('lol_data',calls[0]);self.assertNotIn('allowed_queries',calls[0]);self.assertNotIn('status',calls[0])
        d.evidence_ids.add('lol_data');self.engine.interpret(d,result);self.assertIsNone(d.admin_request);self.assertEqual(d.category,'answer')

    def test_cache_is_bounded_and_does_not_follow_symlinks(self):
        self.knowledge.context('阿狸怎么玩')
        for i in range(30):(self.root/'cache'/f'champion-Hero{i}.json').write_text(json.dumps({'version':VERSION,'data':{}}))
        protected=self.root/'outside';protected.write_text('keep');(self.root/'cache/champion-Protected.json').symlink_to(protected)
        self.knowledge._cleanup(VERSION)
        self.assertLessEqual(len([p for p in (self.root/'cache').glob('champion-*.json') if not p.is_symlink()]),24)
        self.assertEqual(protected.read_text(),'keep')

    def test_official_source_links_pass_existing_output_guard(self):
        self.assertFalse(unsafe('https://developer.riotgames.com/docs/lol'))
        self.assertFalse(unsafe('https://www.leagueoflegends.com/en-us/how-to-play/'))
        self.assertTrue(unsafe('https://evil.example/lol'))


if __name__=='__main__':unittest.main()
