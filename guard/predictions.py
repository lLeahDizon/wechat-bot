"""Trailing-60-day observations from official completed matches and final frames."""
from concurrent.futures import ThreadPoolExecutor,wait
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import urllib.request
from urllib.parse import urlencode

from .esports import EsportsKnowledge,TEAM_ALIASES,TEAM_CODES,match_objects,clean,BJT
from .schedule import OfficialSchedule

TRIGGER=re.compile(r'分析|预测|胜负|赢面|谁会赢|谁能赢|谁更有机会|几成|强弱|对比|对局|看好谁|谁更强')
REGIONS={**dict.fromkeys(['T1','GEN','HLE','KT','DK'],'lck'),**dict.fromkeys(['BLG','TES','JDG','WBG','IG','AL','WE','NIP','LGD'],'lpl'),**dict.fromkeys(['G2','FNC','KC','MKOI'],'lec'),**dict.fromkeys(['FLY','TL','C9','LYON'],'lcs')}
TEAM_CODES=TEAM_CODES|{'KC','MKOI','LYON','CFO','TSW','PSG','GAM'}
ROLES={'top':'上单','jungle':'打野','mid':'中单','bottom':'下路','support':'辅助'}
MAX_NEW_GAMES=96


def teams_from(text):
    matches=[]
    for code in TEAM_CODES:
        for match in re.finditer(r'(?<![A-Za-z0-9])'+re.escape(code)+r'(?![A-Za-z0-9])',text,re.I):matches.append((match.start(),code))
    for alias,code in TEAM_ALIASES.items():
        if alias.lower() in text.lower():matches.append((text.lower().index(alias.lower()),code))
    # Explicit pairs also admit less common official regional teams.
    pair=re.search(r'(?<![A-Za-z0-9])([A-Za-z0-9.]{2,8})\s*(?:vs\.?|对阵|对|打|和|与)\s*([A-Za-z0-9.]{2,8})(?![A-Za-z0-9])',text,re.I)
    if pair:
        for group in [1,2]:matches.append((pair.start(group),pair[group].upper()))
    return list(dict.fromkeys(TEAM_ALIASES.get(code,code) for _,code in sorted(matches)))


def timestamp(text):
    try:
        date=dt.datetime.fromisoformat(text.replace('Z','+00:00'))
        return date.timestamp() if date.tzinfo else None
    except (ValueError,TypeError,AttributeError):return None


def final_observation(window,details):
    frames=window.get('frames') or [];extra=details.get('frames') or []
    if not frames or not extra or frames[-1].get('gameState')!='finished':raise ValueError('not_final')
    last=frames[-1];metadata=window.get('gameMetadata') or {};detail={p['participantId']:p for p in extra[-1].get('participants',[])}
    a=timestamp(last.get('rfc460Timestamp'));b=timestamp(extra[-1].get('rfc460Timestamp'))
    if a is None or b is None or abs(a-b)>30:raise ValueError('frames_not_aligned')
    sides=[]
    for name in ['blue','red']:
        team=last.get(name+'Team') or {};info=metadata.get(name+'TeamMetadata') or {}
        gold=team.get('totalGold');kills=team.get('totalKills');players=[]
        if not isinstance(gold,(int,float)) or gold<10000 or type(kills) is not int or kills<0:raise ValueError('empty_final_stats')
        mapping={p['participantId']:p for p in info.get('participantMetadata',[])}
        if len(mapping)!=5 or len(team.get('participants',[]))!=5:raise ValueError('incomplete_roster')
        for p in team['participants']:
            meta=mapping.get(p.get('participantId'));more=detail.get(p.get('participantId'))
            if not meta or not more:raise ValueError('incomplete_player')
            values=[p.get(k) for k in ['kills','deaths','assists']]
            if any(type(n) is not int or n<0 or n>100 for n in values):raise ValueError('bad_kda')
            damage=more.get('championDamageShare');damage=damage if isinstance(damage,(int,float)) and 0<=damage<=1 else None
            player_name=clean(meta.get('summonerName',''))[:80]
            if not player_name or meta.get('role') not in {'top','jungle','mid','bottom','support'}:raise ValueError('player_identity_missing')
            identity=str(meta.get('esportsPlayerId') or '')
            identity=hashlib.sha256(identity.encode()).hexdigest() if re.fullmatch(r'[0-9]{12,22}',identity) else None
            players.append({'name':player_name,'player_ref':identity,'role':meta.get('role'),'champion':clean(str(meta.get('championId') or ''))[:60],
                'kills':values[0],'deaths':values[1],'assists':values[2],'kp':(values[0]+values[2])/kills if kills else None,'damage_share':damage})
        if sum(p['kills'] for p in players)!=kills:raise ValueError('inconsistent_kills')
        if {p['role'] for p in players}!=set(ROLES):raise ValueError('incomplete_roles')
        identities=[p['player_ref'] for p in players if p['player_ref']]
        if len(identities)!=len(set(identities)):raise ValueError('duplicate_player')
        sides.append({'team_id':str(info.get('esportsTeamId','')),'kills':kills,'players':players,
            'towers':team.get('towers'),'barons':team.get('barons'),'dragons':len(team.get('dragons') or [])})
    return {'finished_at':a,'patch':metadata.get('patchVersion'),'sides':sides}


class PredictionKnowledge:
    def __init__(self,cache,clock=time.time,page_fetch=None,feed_fetch=None,schedule_fetch=None):
        self.cache=Path(cache);self.clock=clock;self.lock=threading.Lock()
        self.page_fetch=page_fetch or EsportsKnowledge(self.cache/'unused')._fetch;self.feed_fetch=feed_fetch or self._feed
        self.schedule=OfficialSchedule(self.cache,clock,schedule_fetch)

    def _feed(self,kind,game_id,timeout):
        if kind not in {'window','details'} or not re.fullmatch(r'[0-9]{12,22}',game_id):raise ValueError('feed_path_denied')
        # Official feed windows must end at least 20 seconds before broadcast time.
        current=dt.datetime.fromtimestamp(self.clock()-60,dt.timezone.utc).replace(microsecond=0)
        current=current.replace(second=current.second//10*10).isoformat().replace('+00:00','Z')
        url='https://feed.lolesports.com/livestats/v1/'+kind+'/'+game_id+'?'+urlencode({'startingTime':current})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*a,**kw):return None
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        with opener.open(url,timeout=timeout) as response:
            raw=response.read(2*1024*1024+1)
            if len(raw)>2*1024*1024:raise ValueError('feed_too_large')
        return json.loads(raw)

    def _load(self):
        if self.cache.is_symlink():raise ValueError('cache_symlink')
        self.cache.mkdir(parents=True,exist_ok=True);path=self.cache/'completed-games.json'
        if path.is_symlink():raise ValueError('cache_symlink')
        try:
            if path.stat().st_size>16*1024*1024:return {}
            value=json.loads(path.read_text())
            return {k:v for k,v in value.items() if re.fullmatch(r'[0-9]{12,22}',k) and v.get('schema')==2 and 0<=self.clock()-v['fetched_at']<=7*86400}
        except (OSError,ValueError,TypeError,KeyError,AttributeError):return {}

    def _save(self,value):
        value=dict(sorted(value.items(),key=lambda kv:kv[1]['fetched_at'],reverse=True)[:1500]);raw=json.dumps(value,ensure_ascii=False)
        if len(raw.encode())>16*1024*1024:return
        path=self.cache/'completed-games.tmp'
        if path.is_symlink():raise ValueError('cache_symlink')
        path.write_text(raw);path.chmod(0o600);path.replace(self.cache/'completed-games.json')

    def context(self,question,history=()):
        with self.lock:return self._context(question,history)

    def _context(self,question,history):
        now=self.clock();start=now-60*86400;teams=teams_from(question)
        if len(teams)<2:
            for previous in reversed(history):teams=list(dict.fromkeys(teams+teams_from(previous)))
        result={'kind':'prediction','state':'needs_teams','window_days':60,'window_start':start,'as_of':now,
            'timezone':'Asia/Shanghai','teams':teams[:2],'statistics':[],'position_comparisons':[],'sources':[],
            'complete_coverage':False,'calibrated_win_probability_available':False,'future_results_used':False,
            'preferred_supplemental_creator':'苦练李香兰','supplemental_opinions':[],
            'supplemental_note':'No video analysis has been read; titles/descriptions are not player statistics.'}
        if len(teams)<2:return result
        teams=teams[:2];deadline=time.monotonic()+25;events={};missing=[]
        discovery=self.schedule.collect(start,now,min(deadline,time.monotonic()+10))
        for event in discovery['events']:
            at=timestamp(event.get('startTime'))
            if event.get('state')=='completed' and at is not None and start<=at<now:events[event['id']]=event
        # Public pages remain a bounded fallback when the published operation is
        # unavailable/partial. They must never be mistaken for global coverage.
        if not discovery['published_window_exhausted']:
            regions=list(dict.fromkeys(['']+[REGIONS[t] for t in teams if t in REGIONS]))
            for region in regions:
                try:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise TimeoutError()
                    url='https://lolesports.com/en-US/'+('leagues/'+region if region else '')
                    body=self.page_fetch(url,min(3,remaining))
                    for event in match_objects(body):
                        at=timestamp(event.get('startTime'))
                        if event.get('state')=='completed' and at is not None and start<=at<now:events[event['id']]=event
                except Exception:missing.append(region or 'global')
        ordered=sorted(events.values(),key=lambda e:e['startTime'],reverse=True)
        cache=self._load();jobs={};by_team={t:[] for t in teams};series={t:[] for t in teams};team_codes={}
        for event in ordered:
            members=event.get('matchTeams') or []
            if len(members)!=2:continue
            for member in members:team_codes[str(member.get('teamId') or '')]=member.get('code')
            for team in members:
                code=team.get('code')
                if code not in teams:continue
                other=next(t for t in members if t is not team)
                a=(team.get('result') or {}).get('gameWins');b=(other.get('result') or {}).get('gameWins')
                if type(a) is not int or type(b) is not int or min(a,b)<0 or max(a,b)>9 or a==b:continue
                league=clean((event.get('league') or {}).get('slug') or (event.get('league') or {}).get('name') or 'unknown')[:100]
                series[code].append({'date':event['startTime'],'opponent':clean(other.get('code') or '')[:20],
                                     'competition':league,'wins':a,'losses':b,'won':a>b})
                for game in (event.get('match') or {}).get('games',[]):
                    identifier=game.get('id')
                    if game.get('state')!='completed' or not isinstance(identifier,str) or not re.fullmatch(r'[0-9]{12,22}',identifier):continue
                    entry=(identifier,str(team.get('teamId') or ''))
                    if entry not in by_team[code]:by_team[code].append(entry)
                    jobs[identifier]={'start':timestamp(event['startTime']),'competition':league}
        # Fairly sample both teams under a single bounded cold-fetch budget.
        selected=[];maximum=max((len(v) for v in by_team.values()),default=0)
        for index in range(maximum):
            for code in teams:
                if index<len(by_team[code]):
                    identifier=by_team[code][index][0]
                    if identifier not in selected:selected.append(identifier)
        cached_selected=[i for i in selected if i in cache]
        selected=cached_selected+[i for i in selected if i not in cache][:MAX_NEW_GAMES]
        failures={}
        def fetch_game(identifier):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError()
            window=self.feed_fetch('window',identifier,min(3,remaining))
            if str(window.get('esportsGameId'))!=identifier:raise ValueError('wrong_game')
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError()
            details=self.feed_fetch('details',identifier,min(3,remaining));value=final_observation(window,details)
            if not max(start,jobs[identifier]['start'])<=value['finished_at']<=now:raise ValueError('outside_window')
            return {'schema':2,'fetched_at':self.clock(),'competition':jobs[identifier]['competition'],'data':value}
        pool=ThreadPoolExecutor(max_workers=4);pending={pool.submit(fetch_game,i):i for i in selected if i not in cache}
        try:
            done,unfinished=wait(pending,timeout=max(0,deadline-time.monotonic()))
            for future in done:
                try:cache[pending[future]]=future.result()
                except Exception as error:
                    reason=(str(error) if isinstance(error,ValueError) and re.fullmatch(r'[a-z_]{1,40}',str(error)) else type(error).__name__)
                    failures[reason]=failures.get(reason,0)+1
            if unfinished:failures['time_budget']=len(unfinished)
            for future in unfinished:future.cancel()
        finally:pool.shutdown(wait=True,cancel_futures=True)
        # All successfully read games retain their original event identity. A
        # prior team's observations can join only by verified stable player ID,
        # never by a display name, team tag or role alone.
        for code in teams:
            observed=[];patches=set();rosters=[];current={};unresolved=0;dates=[]
            for identifier,team_id in by_team[code]:
                value=cache.get(identifier,{}).get('data')
                if not value or not start<=value.get('finished_at',0)<=now:continue
                side=next((s for s in value['sides'] if s['team_id']==team_id),None)
                if not side:continue
                observed.append(side);patches.add(value.get('patch'));dates.append(value['finished_at'])
                rosters.append([(p['role'],p['player_ref'] or p['name']) for p in side['players']])
                for p in side['players']:
                    if not p['player_ref']:unresolved+=1
                    previous=current.get(p['role'])
                    if previous is None or value['finished_at']>cache[previous[1]]['data']['finished_at']:
                        current[p['role']]=(p,identifier,team_id)
            public_players=[]
            for role in ROLES:
                if role not in current:continue
                player,origin,origin_team=current[role];samples=[];seen=set()
                for identifier,record in cache.items():
                    value=record.get('data') or {}
                    if not start<=value.get('finished_at',0)<=now:continue
                    for side in value.get('sides',[]):
                        for item in side['players']:
                            identity_matches=bool(player['player_ref'] and item['player_ref']==player['player_ref'])
                            if not identity_matches and not (identifier==origin and side['team_id']==origin_team):continue
                            if item['role']!=role or (identifier,side['team_id']) in seen:continue
                            if not player['player_ref'] and item['role']!=player['role']:continue
                            seen.add((identifier,side['team_id']))
                            samples.append((item,side['team_id'],record.get('competition') or 'unknown',value['finished_at']))
                public_players.append(player_summary(player,samples,{t for _,t in by_team[code]},team_codes))
            records=series[code];wins=sum(r['wins'] for r in records);losses=sum(r['losses'] for r in records)
            result['statistics'].append({'team':code,'series_count':len(records),'series_wins':sum(r['won'] for r in records),
                'map_wins':wins,'map_losses':losses,'map_win_rate':round(wins/(wins+losses),3) if wins+losses else None,
                'recent_series':records[:8],'competitions':sorted({r['competition'] for r in records}),
                'player_maps_selected':sum(i in selected for i,_ in by_team[code]),'player_maps_available':len(observed),
                'player_maps_discovered':len(by_team[code]),'players':public_players,
                'observed_roster_variants':len({tuple(sorted(r)) for r in rosters}),'patches':sorted(p for p in patches if p),
                'observed_from':min(dates,default=None),'observed_to':max(dates,default=None),
                'unverified_player_observations':unresolved,'coverage_complete':False,
                'player_sample_truncated':wins+losses>len(observed)})
        for role,label in ROLES.items():
            pair=[next((p for p in t['players'] if p['role']==role),None) for t in result['statistics']]
            result['position_comparisons'].append({'role':role,'label':label,'left':pair[0],'right':pair[1],
                'state':'comparable_samples' if all(pair) else 'missing_one_side' if any(pair) else 'missing_both',
                'small_sample':any(p is None or p['maps']<5 for p in pair)})
        self._save(cache)
        usable=all(p['state']=='comparable_samples' for p in result['position_comparisons'])
        sufficient=usable and all(t['series_count']>=3 and t['player_maps_available']>=5 for t in result['statistics'])
        result.update(state='usable_with_limits' if usable else 'insufficient',confidence='limited' if sufficient else 'low',
            discovery={k:v for k,v in discovery.items() if k!='events'},missing_regions=missing,source_failures=failures,
            player_history_coverage='observed_same_id_games_only; unknown prior teams are not inferred',
            missing_player_games=sum(i not in cache for i in selected),sources=[{'publisher':'Riot LoL Esports','fetched_at':now}],
            summary='All official competition types in the public global schedule; partial trailing-60-day observations, verified player identities only; five positions and a conclusion required.')
        return result


def player_summary(player,samples,target_teams,team_codes):
    total={k:sum(p[k] for p,_,_,_ in samples) for k in ['kills','deaths','assists']};maps=len(samples)
    champions={}
    for p,_,_,_ in samples:champions[p['champion']]=champions.get(p['champion'],0)+1
    def mean(metric):
        values=[p[metric] for p,_,_,_ in samples if p.get(metric) is not None]
        return round(sum(values)/len(values),3) if values else None
    return {'name':player['name'],'role':player['role'],'maps':maps,'identity_verified':bool(player['player_ref']),
        'team_maps':sum(t in target_teams for _,t,_,_ in samples),'other_team_maps':sum(t not in target_teams for _,t,_,_ in samples),
        'teams_observed':sorted({team_codes.get(t,'其他已观察战队') for _,t,_,_ in samples}),
        'competitions':sorted({c for _,_,c,_ in samples}),
        'observed_from':min((at for *_,at in samples),default=None),'observed_to':max((at for *_,at in samples),default=None),
        'kda':round((total['kills']+total['assists'])/total['deaths'],2) if total['deaths'] else None,
        'zero_deaths':total['deaths']==0,
        **{k+'_per_map':round(total[k]/maps,2) if maps else None for k in total},
        'kill_participation':mean('kp'),'damage_share':mean('damage_share'),
        'champion_samples':sorted(champions.items(),key=lambda kv:-kv[1])[:4]}


def render_analysis(result,context):
    """Summarize the internal comparisons without dumping individual statistics."""
    if result.get('scope')!='service' or not context.get('position_comparisons'):return result
    analysis=result.get('prediction_analysis') or {}
    if not isinstance(analysis,dict):analysis={}
    def short(value,limit):
        return clean(value).split('\n')[0][:limit] if isinstance(value,str) else ''
    overall=short(analysis.get('overall') or result.get('answer'),90) or '当前样本不足，暂无法判断胜负倾向。'
    if context['state']=='insufficient':overall='目前部分选手数据不足，暂无法可靠判断胜负。'
    focus=analysis.get('focus')
    if not isinstance(focus,list):focus=[]
    focus=list(dict.fromkeys(x for value in focus if (x:=short(value,55))))[:2]
    if not focus:focus=['留意关键位置的临场表现与英雄BP，近期阵容不保证首发。']
    lines=['结论：'+overall,'看点：'+'；'.join(focus)]
    caveat='基于近60天已读取样本。'
    if any(p['small_sample'] for p in context['position_comparisons']):caveat+='部分样本较少，判断把握偏低。'
    lines.append(caveat)
    return {**result,'answer':'\n'.join(lines),'refs':list(dict.fromkeys([*(result.get('refs') or []),'lol_data']))}
