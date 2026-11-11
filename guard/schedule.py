"""Bounded, anonymous reads of Riot's public global schedule query."""
import datetime as dt
import json
import re
import time
import urllib.request
from urllib.parse import urlencode

# Published homeEvents operation in the official site's persisted-query manifest,
# verified 2026-11-04. This identifier is public, not a credential. Fail closed if
# the publisher retires it; the caller can fall back to public schedule pages.
HOME_EVENTS_ID='e4da9fd0ab89ecdb783912d74799688d1f52f1edc9b87497d3409c74d9b4291a'
MAX_PAGES=12


def compact_event(event):
    if event.get('__typename')!='EventMatch' or not isinstance(event.get('id'),str):return None
    league=event.get('league') or {};teams=[]
    for team in (event.get('matchTeams') or [])[:2]:
        code=team.get('code') or '';identifier=team.get('teamId')
        if not re.fullmatch(r'[A-Za-z0-9.\-]{1,12}',code) or not isinstance(identifier,str):return None
        teams.append({'code':code.upper(),'teamId':identifier,'result':{'gameWins':(team.get('result') or {}).get('gameWins')}})
    if len(teams)!=2:return None
    return {'__typename':'EventMatch','id':event['id'],'startTime':event.get('startTime'),'state':event.get('state'),
            'league':{k:str(league.get(k) or '')[:100] for k in ['id','name','slug']},'matchTeams':teams,
            'match':{'games':[{'id':g.get('id'),'state':g.get('state')} for g in (event.get('match') or {}).get('games',[])[:9]]}}


class OfficialSchedule:
    def __init__(self,cache,clock=time.time,fetch=None):
        self.cache=cache;self.clock=clock;self.fetch=fetch or self._fetch

    def _fetch(self,variables,timeout):
        # No user-provided GraphQL, URLs, headers, credentials or operation IDs.
        parameters={'operationName':'homeEvents','variables':json.dumps(variables,separators=(',',':')),
                    'extensions':json.dumps({'persistedQuery':{'version':1,'sha256Hash':HOME_EVENTS_ID}},separators=(',',':'))}
        request=urllib.request.Request('https://lolesports.com/api/gql?'+urlencode(parameters),headers={
            'Content-Type':'application/json','User-Agent':'Sub2API-Group-FAQ/1.0',
            'apollographql-client-name':'Esports Web','apollographql-client-version':'1.0'})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        with opener.open(request,timeout=timeout) as response:
            raw=response.read(4*1024*1024+1)
            if len(raw)>4*1024*1024:raise ValueError('schedule_too_large')
        value=json.loads(raw)
        if value.get('errors'):raise ValueError('schedule_query_failed')
        data=(value.get('data') or {}).get('esports')
        if not isinstance(data,dict) or not isinstance(data.get('events'),list):raise ValueError('schedule_missing')
        return data

    def collect(self,start,now,deadline):
        path=self.cache/'official-schedule.json'
        if self.cache.is_symlink() or path.is_symlink():raise ValueError('cache_symlink')
        try:
            if path.stat().st_size<=4*1024*1024:
                saved=json.loads(path.read_text())
                if saved.get('schema')==1 and 0<=now-saved['fetched_at']<300 and abs(start-saved['window_start'])<300:
                    return {**saved,'requests':0,'from_cache':True}
        except (OSError,ValueError,KeyError,TypeError):pass
        variables={'hl':'en-US','sport':['lol'],'eventState':['completed'],'pageSize':100,
                   'eventDateStart':dt.datetime.fromtimestamp(start,dt.timezone.utc).date().isoformat(),
                   'eventDateEnd':(dt.datetime.fromtimestamp(now,dt.timezone.utc).date()+dt.timedelta(days=1)).isoformat()}
        events={};seen=set();complete=False;failure=None;requests=0
        for _ in range(MAX_PAGES):
            remaining=deadline-time.monotonic()
            if remaining<=0:failure='schedule_time_budget';break
            requests+=1
            try:data=self.fetch(dict(variables),min(4,remaining))
            except Exception:failure='schedule_unavailable';break
            for event in data.get('events',[]):
                item=compact_event(event)
                if item:events[item['id']]=item
            pages=data.get('pages')
            if not isinstance(pages,dict) or 'newer' not in pages:failure='schedule_pages_missing';break
            token=pages['newer']
            if token is None:complete=True;break
            if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_+\-/=:.]{1,1024}',token) or token in seen:
                failure='schedule_cursor_invalid';break
            seen.add(token);variables['pageToken']=token
        if not complete and failure is None:failure='schedule_page_budget'
        result={'schema':1,'fetched_at':now,'window_start':start,'events':list(events.values()),
                'published_window_exhausted':complete,'failure':failure,'requests':requests,'from_cache':False}
        self.cache.mkdir(parents=True,exist_ok=True)
        raw=json.dumps(result,ensure_ascii=False)
        if len(raw.encode())<=4*1024*1024:
            temporary=self.cache/'official-schedule.tmp'
            if temporary.is_symlink():raise ValueError('cache_symlink')
            temporary.write_text(raw);temporary.chmod(0o600);temporary.replace(path)
        return result
