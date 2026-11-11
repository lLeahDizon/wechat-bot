"""Read public LoL competition records and articles from explicit publishers."""
import datetime
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit,urlunsplit,urljoin,quote,unquote

BJT=datetime.timezone(datetime.timedelta(hours=8))
HOSTS={'lolesports.com':('LoL Esports','official'),'lpl.qq.com':('腾讯LPL','official'),
       'lol.qq.com':('腾讯英雄联盟','official'),'gol.gg':('Games of Legends','statistics'),
       'www.gol.gg':('Games of Legends','statistics'),'news.17173.com':('17173','professional_news')}
TRIGGER=re.compile(r'(?<![A-Za-z0-9])(?:lpl|lck|lec|lcs|lta|lcp|cblol|msi|worlds)(?![A-Za-z0-9])|全球总决赛|世界赛|季中赛|先锋赛|赛程|赛果|战队|转会|赛事|比分|比赛|职业选手|电竞新闻|电竞动态',re.I)
OTHER_SPORT=re.compile(r'(?<![A-Za-z0-9])(?:nba|dota2?|cs2|csgo)(?![A-Za-z0-9])|英超|欧冠|足球|篮球|王者荣耀',re.I)
DATE=re.compile(r'20\d{2}[-/]\d{1,2}[-/]\d{1,2}|20\d{2}年\d{1,2}月\d{1,2}日|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d{2}',re.I)
LPL_DATA='https://lpl.qq.com/web201612/data/LOL_MATCH2_MATCH_HOMEPAGE_BMATCH_LIST.js'
TEAM_ALIASES={'滔搏':'TES','京东':'JDG','哔哩哔哩':'BLG','微博':'WBG','三星':'GEN','GENG':'GEN','GEN.G':'GEN','SKT':'T1'}
TEAM_CODES={'T1','GEN','HLE','KT','DK','BLG','TES','JDG','WBG','IG','AL','WE','NIP','LGD','G2','FNC','FLY','TL','C9','KC','MKOI','LYON','CFO','TSW','PSG','GAM'}


def related(text):
    if OTHER_SPORT.search(text) and not re.search(r'英雄联盟|\blol\b|\blpl\b|\blck\b',text,re.I):return False
    team=any(re.search(r'(?<![A-Za-z0-9])'+re.escape(code)+r'(?![A-Za-z0-9])',text,re.I) for code in TEAM_CODES)
    aliases={code for alias,code in TEAM_ALIASES.items() if alias.lower() in text.lower()}
    pair=re.search(r'(?<![A-Za-z0-9])[A-Za-z0-9.]{2,8}\s*(?:vs\.?|对阵|对|打|和|与)\s*[A-Za-z0-9.]{2,8}(?![A-Za-z0-9])',text,re.I)
    prediction=(len(aliases)>=2 or pair) and re.search(r'分析|预测|谁能赢|谁会赢|对局|对比|赢面',text)
    return bool(TRIGGER.search(text) or team or prediction or re.search(r'https://(?:lolesports\.com|lpl\.qq\.com|lol\.qq\.com|(?:www\.)?gol\.gg|news\.17173\.com)/',text,re.I))


def canonical(url):
    p=urlsplit(url)
    if p.scheme!='https' or p.hostname not in HOSTS or p.username or p.password or p.port not in {None,443}:raise ValueError('publisher_denied')
    path=unquote(p.path)
    if p.hostname=='lolesports.com' and (path=='/news' or path.startswith('/news/')):path='/en-US'+path
    if len(url)>450 or any(ord(c)<32 for c in url) or '\\' in path or '%' in path or any(x.lower() in {'..','admin','login','logout','oauth','callback','signin','user'} for x in path.split('/')):raise ValueError('public_path_denied')
    if p.hostname=='lolesports.com' and not re.fullmatch(r'/(?:[a-z]{2}(?:-[A-Za-z]{2})?/)?(?:(?:lolesports/)?news(?:/[A-Za-z0-9-]+)?|leagues/[a-z0-9-]+)?/?',path):raise ValueError('public_path_denied')
    if p.hostname in {'lpl.qq.com','lol.qq.com'} and url!=LPL_DATA and not (path=='/' or re.fullmatch(r'/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.(?:s?html)',path)):raise ValueError('public_path_denied')
    if p.hostname=='news.17173.com' and not (path=='/' or re.fullmatch(r'/content/[A-Za-z0-9_/-]+\.shtml',path)):raise ValueError('public_path_denied')
    if p.hostname in {'gol.gg','www.gol.gg'} and not path.startswith(('/tournament/','/game/','/teams/','/players/','/esports/')):raise ValueError('stats_path_denied')
    if path.endswith('.js') and url!=LPL_DATA:raise ValueError('script_not_a_source')
    value=urlunsplit(('https',p.hostname,quote(path,safe='/-_.~'),p.query,''))
    from .core import unsafe
    if unsafe(value):raise ValueError('nonpublic_url')
    return value


def clean(text):
    from .core import redact_question
    value=redact_question(re.sub(r'[ \t]+',' ',text))
    return value if value is not None else ''


class Page(HTMLParser):
    def __init__(self):
        super().__init__();self.skip=[];self.text=[];self.links=[];self.anchor=None;self.title='';self.in_title=False;self.published=None
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag in {'script','style','noscript','nav','footer','header','aside','form'}:self.skip.append(tag)
        if self.skip:return
        if tag=='title':self.in_title=True
        if tag=='meta' and a.get('property')=='og:title':self.title=a.get('content','')[:160]
        if tag=='meta' and (a.get('property') in {'article:published_time','datePublished'} or a.get('name') in {'pubdate','date','publishdate'}):self.published=a.get('content','')[:80]
        if tag=='time' and a.get('datetime') and not self.published:self.published=a['datetime'][:80]
        if tag=='a':self.anchor=[a.get('href',''),'']
        if tag in {'p','div','tr','li','h1','h2','h3','h4','br'}:self.text.append('\n')
        if tag in {'td','th'}:self.text.append(' | ')
    def handle_endtag(self,tag):
        if self.skip:
            if tag==self.skip[-1]:self.skip.pop()
            return
        if tag=='title':self.in_title=False
        if tag=='a' and self.anchor:
            self.links.append(self.anchor);self.anchor=None
    def handle_data(self,text):
        if self.skip:return
        if self.in_title:self.title=(self.title+text)[:160]
        else:self.text.append(text)
        if self.anchor:self.anchor[1]+=text[:200]


def utc_to_bjt(raw):
    try:
        d=datetime.datetime.fromisoformat(raw.replace('Z','+00:00'))
        return d.astimezone(BJT).isoformat() if d.tzinfo else None
    except (ValueError,TypeError,AttributeError):return None


def match_objects(body):
    result={};decoder=json.JSONDecoder();chunks=[body]
    # Decode the public page's JSON strings; never evaluate JavaScript.
    for match in re.finditer(r'self\.__next_f\.push\(\[1,',body):
        try:value,_=decoder.raw_decode(body[match.end():])
        except ValueError:continue
        if isinstance(value,str):chunks.append(value)
    for chunk in chunks:
      for match in re.finditer(r'\{\s*"__typename"\s*:\s*"EventMatch"',chunk):
        try:v,_=decoder.raw_decode(chunk[match.start():])
        except ValueError:continue
        if not isinstance(v.get('id'),str) or v['id'] in result:continue
        result[v['id']]=v
        if len(result)>=200:break
    return list(result.values())


def riot_records(body):
    result={}
    for v in match_objects(body):
        league=v.get('league') or {};game=v.get('match') or {}
        teams=[];scores=[]
        for item in v.get('matchTeams',v.get('participants',game.get('teams',[])))[:2]:
            team=item.get('team') or item.get('participant') or item
            teams.append(clean(team.get('code') or team.get('name') or '待定')[:80])
            score=(item.get('result') or {}).get('gameWins')
            scores.append(score if type(score) is int else None)
        if len(teams)!=2:continue
        state={'unstarted':'scheduled','inProgress':'ongoing','completed':'completed'}.get(v.get('state'),'unknown')
        score=scores if all(type(x) is int and 0<=x<=20 for x in scores) and state in {'ongoing','completed'} else None
        result[v['id']]=dict(league=clean(league.get('slug') or league.get('name') or '')[:80],stage=clean(v.get('blockName') or '')[:80],teams=teams,
            state=state,score=score,start_raw=v.get('startTime'),start_bjt=utc_to_bjt(v.get('startTime')),time_zone='explicit_offset')
    return list(result.values())


def tencent_records(body):
    start=body.find('{')
    value,_=json.JSONDecoder().raw_decode(body[start:])
    if value.get('status')!='0' or not isinstance(value.get('msg'),list):return [],None
    records=[]
    for x in value['msg'][:1000]:
        if str(x.get('isTft','0'))!='0' or re.search(r'手游|云顶|金铲铲',x.get('GameName','')):continue
        names=re.split(r'\s+(?:vs|VS)\s+',x.get('bMatchName',''))
        teams=[x.get('TeamShortNameA') or (names[0] if len(names)==2 else '待定'),x.get('TeamShortNameB') or (names[1] if len(names)==2 else '待定')]
        state={'1':'scheduled','2':'ongoing','3':'completed'}.get(str(x.get('MatchStatus')),'unknown');score=None
        if state in {'ongoing','completed'}:
            try:
                scores=[int(x['ScoreA']),int(x['ScoreB'])]
                if all(0<=i<=20 for i in scores):score=scores
            except (ValueError,TypeError,KeyError):pass
        records.append({'league':clean(x.get('GameName') or '')[:80],'stage':clean(x.get('GameProcName') or '')[:80],'teams':[clean(t)[:80] for t in teams],'state':state,'score':score,
            'best_of':x.get('GameModeName'),'start_raw':x.get('MatchDate'),'start_bjt':utc_to_bjt(x.get('MatchDate')),'time_zone':'not_stated'})
    return records,value.get('lastUpTime')


class EsportsKnowledge:
    def __init__(self,cache,clock=time.time,fetch=None):
        self.cache=Path(cache);self.clock=clock;self.fetch=fetch or self._fetch;self.lock=threading.Lock()
    def _fetch(self,url,timeout):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        transport=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        host=urlsplit(url).hostname;deadline=time.monotonic()+timeout
        for _ in range(3):
            url=canonical(url)
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('public_budget')
            try:
                request=urllib.request.Request(url,headers={'User-Agent':'Sub2API-Group-FAQ/1.0','Accept-Encoding':'identity'})
                with transport.open(request,timeout=remaining) as r:
                    raw=r.read(4*1024*1024+1)
                    if len(raw)>4*1024*1024:raise ValueError('source_too_large')
                    charset=r.headers.get_content_charset();m=re.search(br'charset\s*=\s*["\x27]?([A-Za-z0-9_-]+)',raw[:3000])
                    return raw.decode(charset or (m[1].decode() if m else 'utf-8'),'replace')
            except urllib.error.HTTPError as error:
                if error.code not in {301,302,303,307,308}:raise
                next_url=urljoin(url,error.headers.get('Location',''));error.close();url=canonical(next_url)
                if urlsplit(url).hostname!=host:raise ValueError('cross_publisher_redirect')
        raise ValueError('redirect_budget')

    def _page(self,url,budget):
        url=canonical(url);now=self.clock();name=hashlib.sha256(url.encode()).hexdigest()[:16]
        if self.cache.is_symlink():raise ValueError('cache_symlink')
        self.cache.mkdir(parents=True,exist_ok=True);path=self.cache/('page-'+name+'.json');saved=None
        if path.is_symlink():raise ValueError('cache_symlink')
        try:
            if path.stat().st_size<=1024*1024:
                value=json.loads(path.read_text())
                if value.get('cache_url',value['url'])==url and 0<=now-value['fetched_at']<=86400:saved=value
        except (OSError,ValueError,KeyError,TypeError):pass
        if saved and now-saved['fetched_at']<300:return saved
        try:
            remaining=budget['deadline']-time.monotonic()
            if budget['count']>=5 or remaining<=0:raise ValueError('public_budget')
            budget['count']+=1;body=self.fetch(url,min(5,remaining))
            if any(x in body.lower() for x in ['cf-chl-','verify you are human','access denied']):raise ValueError('source_unavailable')
            host=urlsplit(url).hostname;publisher,tier=HOSTS[host]
            document={'url':url,'publisher':publisher,'tier':tier,'fetched_at':now,'records':[],'text':'','links':[],'published_at':None}
            if url==LPL_DATA:
                document['records'],document['site_updated_raw']=tencent_records(body);document['title']='腾讯LPL公开赛程';document['url']='https://lpl.qq.com/'
            else:
                document['records']=riot_records(body) if host=='lolesports.com' else []
                p=Page();p.feed(body);text=clean(''.join(p.text));document.update(title=clean(p.title),published_at=p.published)
                document['date_mentions']=DATE.findall(text)[:5]
                document['published_at']=clean(p.published)[:80] if p.published else None
                # Index pages and schedule chrome are discovery material, not evidence.
                article=bool(re.search(r'/news/[^/]+/?$|/content/|/tournament/(?:tournament-matchlist|tournament-stats)/',urlsplit(url).path))
                if article and len(text)>100:document['text']=text[:10000]
                if document['records']:document['published_at']=None
                for href,title in p.links:
                    try:target=canonical(urljoin(url,href))
                    except ValueError:continue
                    if target!=url and re.search(r'/news/|/content/|/tournament/(?:tournament-matchlist|tournament-stats)/',target):
                        document['links'].append({'url':target,'title':clean(title)[:180]})
                    if len(document['links'])>=60:break
                if host=='news.17173.com' and urlsplit(url).path=='/':document['text']=''
            if not document['records'] and not document['text'] and not document['links']:raise ValueError('no_public_content')
            # Cache identity uses the requested URL even when the citation is a parent page.
            stored={**document,'cache_url':url};encoded=json.dumps(stored,ensure_ascii=False)
            if len(encoded.encode())>1024*1024:raise ValueError('cache_entry_too_large')
            temp=path.with_suffix('.tmp')
            if temp.is_symlink():raise ValueError('cache_symlink')
            temp.write_text(encoded);temp.chmod(0o600);temp.replace(path)
            return document
        except Exception:
            budget['missing'].append(urlsplit(url).hostname)
            return {**saved,'stale':True} if saved else None

    def _prune(self):
        if self.cache.is_symlink():raise ValueError('cache_symlink')
        if not self.cache.exists():return
        entries=[]
        for p in self.cache.iterdir():
            if p.is_symlink() or not p.is_file() or not re.fullmatch(r'page-[a-f0-9]{16}\.(?:json|tmp)',p.name):continue
            if p.suffix=='.tmp' or self.clock()-p.stat().st_mtime>86400:p.unlink();continue
            entries.append(p)
        entries.sort(key=lambda x:x.stat().st_mtime,reverse=True);total=0
        for i,p in enumerate(entries):
            total+=p.stat().st_size
            if i>=16 or total>2*1024*1024:p.unlink()

    def context(self,question):
        with self.lock:return self._context(question)

    def _context(self,question):
        budget={'count':0,'deadline':time.monotonic()+15,'missing':[]};documents=[]
        league=next((x.lower() for x in ['LPL','LCK','LEC','LCS','LCP','CBLOL','MSI'] if re.search(r'(?<![A-Za-z0-9])'+x+r'(?![A-Za-z0-9])',question,re.I)),None)
        if re.search(r'全球总决赛|世界赛|worlds|S\d{1,2}赛',question,re.I):league='worlds'
        provided=re.findall(r'https://[^\s<>"）)]+',question)[:2]
        for url in provided:
            try:doc=self._page(url.rstrip('。.,，'),budget)
            except ValueError:doc=None
            if doc:documents.append(doc)
        news=bool(re.search(r'新闻|消息|动态|转会|阵容|公告|规则|赛制|门票|场馆|举办|哪里|在哪',question))
        if not provided:
            if not news or re.search(r'赛程|赛果|比分|几点|谁赢|几比|对阵',question):
                urls=['https://lolesports.com/en-US'+('/leagues/'+league if league else ''),LPL_DATA]
                for url in urls:
                    doc=self._page(url,budget)
                    if doc:documents.append(doc)
            if news:
                tokens=[x.lower() for x in re.findall(r'[A-Za-z0-9]{2,}',question)]
                for url in ['https://lolesports.com/en-US/news','https://news.17173.com/']:
                    doc=self._page(url,budget)
                    if not doc:continue
                    if doc['text']:documents.append(doc)
                    links=doc['links']
                    if '17173' in url:links=[x for x in links if re.search(r'英雄联盟|LPL|LCK|全球总决赛|MSI|worlds',x['title'],re.I)]
                    links=sorted(links,key=lambda x:sum(t in (x['url']+' '+x['title']).lower() for t in tokens),reverse=True)
                    if links:
                        article=self._page(links[0]['url'],budget)
                        if article and article['text']:documents.insert(0,article)
        teams={code for code in TEAM_CODES if re.search(r'(?<![A-Za-z0-9])'+re.escape(code)+r'(?![A-Za-z0-9])',question,re.I)}
        teams|={code for alias,code in TEAM_ALIASES.items() if alias.lower() in question.lower()}
        current=datetime.datetime.fromtimestamp(self.clock(),BJT);target=None
        if re.search(r'今天|今晚|今日',question):target=current.date().isoformat()
        elif '明天' in question:target=(current.date()+datetime.timedelta(days=1)).isoformat()
        elif '昨天' in question:target=(current.date()-datetime.timedelta(days=1)).isoformat()
        exact=re.search(r'(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})',question)
        if exact:
            try:target=datetime.date(*map(int,exact.groups())).isoformat()
            except ValueError:pass
        year=re.search(r'20\d{2}',question);sources=[]
        def distance(row):
            try:
                raw=row.get('start_bjt') or row.get('start_raw') or ''
                return abs((datetime.datetime.fromisoformat(raw.replace('Z','+00:00')).replace(tzinfo=None)-current.replace(tzinfo=None)).total_seconds())
            except (ValueError,TypeError):return float('inf')
        for doc in documents:
            records=[]
            for row in doc['records']:
                competition=str(row.get('league') or '').lower()
                if league and not (league in competition or league=='worlds' and re.search(r'全球总决赛|世界赛|world championship',competition) or league=='msi' and re.search(r'季中',competition)):continue
                hay=' '.join(str(x) for x in row.get('teams',[])).upper()
                if teams and not all(code in hay for code in teams):continue
                date=row.get('start_bjt') or row.get('start_raw') or ''
                if target and not date.startswith(target):continue
                if year and year[0] not in date:continue
                records.append(row)
            records.sort(key=distance)
            if not records and not doc['text']:continue
            sources.append({k:v for k,v in doc.items() if k not in {'records','links','cache_url'}}|{'records':records[:12],'text':doc['text'][:5000]})
        unique={x['url']:x for x in sources};sources=list(unique.values())[:3];self._prune()
        return {'kind':'esports','state':'ok' if sources and not budget['missing'] and not any(x.get('stale') for x in sources) else 'partial' if sources else 'unavailable',
            'clock_bjt':current.isoformat(),'timezone':'Asia/Shanghai','sources':sources,'public_requests':budget['count'],
            'missing_publishers':sorted(set(budget['missing'])),'complete_schedule_coverage':False,
            'source_priority':'official_first; statistics_and_professional_news_are_supplemental',
            'empty_result_means_no_match_in_fetched_sources_not_no_matches_exist':True}
