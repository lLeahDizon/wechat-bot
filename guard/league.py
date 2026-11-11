"""Public LoL static knowledge. No server query or administration interfaces."""
import html
import json
from pathlib import Path
import re
import threading
import time
import urllib.request

from .model import call

EXPLICIT=re.compile(r'英雄联盟|(?<![A-Za-z0-9])lol(?![A-Za-z])|league\s*of\s*legends|召唤师峡谷|嚎哭深渊|大乱斗|云顶之弈|金铲铲|符文|出装|装备|对线|补刀|团战|上单|中单|打野|adc|新英雄|英雄技能|技能连招|玩辅助|辅助位|黑切|金身',re.I)
OTHER_MODE=re.compile(r'手游|云顶|金铲铲|wild\s*rift|\btft\b',re.I)
ALIASES={'狐狸':'Ahri','剑魔':'Aatrox','剑姬':'Fiora','瞎子':'LeeSin','盲僧':'LeeSin','妖姬':'Leblanc',
 '光辉':'Lux','石头人':'Malphite','剑圣':'MasterYi','猴子':'MonkeyKing','寒冰':'Ashe','冰女':'Lissandra',
 '皇子':'JarvanIV','卡牌':'TwistedFate','蛮子':'Tryndamere','轮子妈':'Sivir','德玛':'Garen','狗头':'Nasus',
 '女枪':'MissFortune','女警':'Caitlyn','男枪':'Graves','男刀':'Talon','小炮':'Tristana','大嘴':'KogMaw',
 '努努':'Nunu','大虫子':'Chogath','鳄鱼':'Renekton','诺手':'Darius','船长':'Gangplank','螳螂':'Khazix'}
HOST='https://ddragon.leagueoflegends.com'
SOURCE='https://developer.riotgames.com/docs/lol'
VERSION=re.compile(r'\d{1,2}\.\d{1,2}\.\d{1,3}')


def plain(value,limit=400):
    text=re.sub(r'<[^>]*>',' ',str(value))
    text=re.sub(r'\{\{.*?\}\}','[变量数值以游戏内说明为准]',text)
    return re.sub(r'\s+',' ',html.unescape(text)).strip()[:limit]


def catalogue(path):
    try:
        value=json.loads(Path(path).read_text())
        if not VERSION.fullmatch(value['version']) or not isinstance(value['champions'],dict):return {}
        return value
    except (OSError,ValueError,KeyError,TypeError):return {}


def entities(text,champions):
    matches=[]
    names={**{x:k for k,v in champions.items() for x in [v.get('name',''),v.get('title',''),k] if x},**ALIASES}
    for name,identifier in names.items():
        if identifier not in champions:continue
        if name.isascii():pattern=r'(?<![A-Za-z0-9])'+re.escape(name)+r'(?![A-Za-z0-9])'
        else:
            if len(name)==1 and text.strip()!=name and not re.search(r'英雄|技能|连招|怎么玩|怎么打|符文|出装|对线|打野',text):continue
            pattern=re.escape(name)
        for match in re.finditer(pattern,text,re.I):matches.append((match.start(),match.end(),identifier))
    chosen=[];spans=[]
    for start,end,identifier in sorted(matches,key=lambda x:-(x[1]-x[0])):
        if any(start<b and end>a for a,b in spans):continue
        spans.append((start,end))
        chosen.append((start,identifier))
    return list(dict.fromkeys(identifier for _,identifier in sorted(chosen)))[:2]


def related(text,champions,terms=()):return bool(EXPLICIT.search(text) or entities(text,champions) or any(len(x)>=2 and x in text for x in terms))


class LeagueKnowledge:
    def __init__(self,cache,seed,clock=time.time,fetch=None):
        self.cache=Path(cache);self.seed=seed or {};self.clock=clock;self.fetch=fetch or self._fetch
        self.champions=self.seed.get('champions',{});self.lock=threading.Lock()
        self.terms=set(self.seed.get('item_names',[])+self.seed.get('rune_names',[]));self.current_version=self.seed.get('version','unknown')
        from .esports import EsportsKnowledge
        self.esports=EsportsKnowledge(self.cache.parent/'esports-cache',clock)
        from .predictions import PredictionKnowledge
        self.predictions=PredictionKnowledge(self.cache.parent/'prediction-cache',clock)

    def _fetch(self,path,timeout):
        if not re.fullmatch(r'/api/versions\.json|/cdn/\d{1,2}\.\d{1,2}\.\d{1,3}/data/zh_CN/(?:champion(?:/[A-Za-z0-9]+)?|item|runesReforged)\.json',path):raise ValueError('public_path_denied')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        transport=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        request=urllib.request.Request(HOST+path,headers={'Accept':'application/json'})
        with transport.open(request,timeout=timeout) as response:
            raw=response.read(3*1024*1024+1)
            if len(raw)>3*1024*1024:raise ValueError('public_data_too_large')
        return json.loads(raw)

    def _resource(self,name,path,version,convert,budget):
        if self.cache.is_symlink():raise ValueError('cache_directory_symlink')
        self.cache.mkdir(parents=True,exist_ok=True)
        file=self.cache/(name+'.json');saved=None
        if file.is_symlink():raise ValueError('cache_symlink')
        try:
            if file.stat().st_size>1024*1024:raise ValueError('cache_entry_too_large')
            value=json.loads(file.read_text())
            if value['version']==version and 0<=self.clock()-value['at']<=30*86400:saved=value
        except (OSError,ValueError,KeyError,TypeError):pass
        if saved and self.clock()-saved['at']<86400:
            budget['source_times'][name]=saved['at'];return saved['data'],True
        try:
            remaining=budget['deadline']-time.monotonic()
            if budget['requests']>=5 or remaining<=0:raise ValueError('public_budget')
            budget['requests']+=1
            data=convert(self.fetch(path,min(4,remaining)))
            captured=self.clock();body=json.dumps({'version':version,'at':captured,'data':data},ensure_ascii=False)
            if len(body.encode())>1024*1024:raise ValueError('cache_entry_too_large')
            temp=file.with_suffix('.tmp')
            if temp.is_symlink():raise ValueError('cache_symlink')
            temp.write_text(body);temp.chmod(0o600);temp.replace(file)
            budget['source_times'][name]=captured
            return data,True
        except Exception:
            budget['missing'].append(name)
            if saved:budget['source_times'][name]=saved['at']
            return (saved['data'],False) if saved else (None,False)

    def _cleanup(self,version):
        files=[]
        for path in self.cache.iterdir():
            if path.is_symlink() or not path.is_file():continue
            if not re.fullmatch(r'(?:versions|catalogue|items|runes|champion-[A-Za-z0-9]+)\.(?:json|tmp)',path.name):continue
            if path.suffix=='.tmp':
                if self.clock()-path.stat().st_mtime>60:path.unlink()
                continue
            try:value=json.loads(path.read_text())
            except (OSError,ValueError):path.unlink();continue
            if path.stem!='versions' and value.get('version')!=version:path.unlink();continue
            if path.stem.startswith('champion-'):files.append(path)
        files.sort(key=lambda p:p.stat().st_mtime,reverse=True)
        for path in files[24:]:path.unlink()
        # Only our public-data cache is pruned, never WeChat or delivery state.
        known=[p for p in self.cache.glob('*.json') if p.is_file() and not p.is_symlink()]
        if sum(p.stat().st_size for p in known)>8*1024*1024:
            for path in files:
                if path.exists():path.unlink()

    @staticmethod
    def _catalogue(raw):
        return {k:{n:v[n] for n in ['id','name','title','tags']} for k,v in raw['data'].items() if re.fullmatch(r'[A-Za-z0-9]+',k)}

    @staticmethod
    def _champion(raw):
        v=next(iter(raw['data'].values()))
        return {'name':v['name'],'title':v['title'],'tags':v.get('tags',[]),
                'passive':{'name':v['passive']['name'],'description':plain(v['passive']['description'])},
                'spells':[{'key':key,'name':s['name'],'description':plain(s['description']),
                           'cooldown':s.get('cooldownBurn','')} for key,s in zip('QWER',v['spells'])]}

    @staticmethod
    def _items(raw):
        return [{'name':v['name'],'description':plain(v['description']),'tags':v.get('tags',[]),
                 'gold':v.get('gold',{}).get('total')} for v in raw['data'].values()
                if v.get('maps',{}).get('11') and v.get('gold',{}).get('purchasable') and v.get('inStore',True) and not v.get('hideFromAll')]

    @staticmethod
    def _runes(raw):
        return [{'name':v['name'],'tree':tree['name'],'description':plain(v.get('longDesc') or v.get('shortDesc',''))}
                for tree in raw for slot in tree['slots'] for v in slot['runes']]

    def context(self,question):
        with self.lock:
            budget={'deadline':time.monotonic()+10,'requests':0,'missing':[],'source_times':{}}
            versions,fresh=self._resource('versions','/api/versions.json','',
                lambda raw:[v for v in raw[:10] if isinstance(v,str) and VERSION.fullmatch(v)],budget)
            version=versions[0] if versions else self.seed.get('version')
            if not version or not VERSION.fullmatch(version):return {'state':'unavailable','source':SOURCE,'real_time_stats':False}
            self.current_version=version
            self._cleanup(version)
            champs,_=self._resource('catalogue','/cdn/'+version+'/data/zh_CN/champion.json',version,self._catalogue,budget)
            if not champs and self.seed.get('version')==version:
                champs=self.seed.get('champions',{})
                if isinstance(self.seed.get('captured_at'),(int,float)):budget['source_times']['catalogue_seed']=self.seed['captured_at']
            if champs:self.champions=champs
            payload={'state':'ok','source':SOURCE,'version':version,'published_version_checked':fresh,
                     'checked_at':self.clock(),'region_live_version_verified':False,'real_time_stats':False,
                     'champions':[],'items':[],'runes':[]}
            for identifier in entities(question,champs or {}):
                data,_=self._resource('champion-'+identifier,'/cdn/'+version+'/data/zh_CN/champion/'+identifier+'.json',version,self._champion,budget)
                if data:payload['champions'].append(data)
            tags=set(payload['champions'][0].get('tags',[])) if payload['champions'] else set()
            if re.search(r'出装|装备|买什么|法穿|暴击|护甲|魔抗|冷却|黑切|金身',question) or any(x in question for x in self.seed.get('item_names',[])):
                values,_=self._resource('items','/cdn/'+version+'/data/zh_CN/item.json',version,self._items,budget)
                interests={'SpellDamage','Mana','MagicPenetration'} if 'Mage' in tags else {'Armor','Health','SpellBlock'} if 'Tank' in tags else {'Damage','AttackSpeed','CriticalStrike'}
                ranked=sorted(values or [],key=lambda x:100*(x['name'] in question)+len(interests&set(x.get('tags',[]))),reverse=True)
                payload['items']=ranked[:8]
                self.terms.update(x['name'] for x in (values or []))
            if re.search(r'符文|天赋|电刑|征服者|彗星|艾黎|相位猛冲',question) or any(x in question for x in self.seed.get('rune_names',[])):
                values,_=self._resource('runes','/cdn/'+version+'/data/zh_CN/runesReforged.json',version,self._runes,budget)
                preferred={'电刑','奥术彗星','召唤艾黎','相位猛冲','法力流系带','焦灼'} if 'Mage' in tags else {'征服者','强攻','迅捷步法','不灭之握','余震','致命节奏'}
                payload['runes']=sorted(values or [],key=lambda x:100*(x['name'] in question)+int(x['name'] in preferred),reverse=True)[:8]
                self.terms.update(x['name'] for x in (values or []))
            payload['unavailable_resources']=budget['missing'];payload['public_requests']=budget['requests']
            payload['resource_fetched_at']=budget['source_times']
            payload['data_fetched_at']=min(budget['source_times'].values(),default=self.seed.get('captured_at'))
            if budget['missing']:payload['state']='partial'
            self._cleanup(version)
            return payload

    def answer(self,data,key_file,api_base,model_call=call):
        from .predictions import TRIGGER,render_analysis
        if data.get('esports') and TRIGGER.search(data['question']):
            try:context=self.predictions.context(data['question'],[x['question'] for x in data.get('context',[])])
            except Exception:context={'kind':'prediction','state':'unavailable','statistics':[],'sources':[]}
            result,usage=model_call({**data,'lol_data':context},key_file,api_base)
            return render_analysis(result,context),usage
        if data.get('esports'):
            try:context=self.esports.context(data['question'])
            except Exception:context={'kind':'esports','state':'unavailable','sources':[]}
            result,usage=model_call({**data,'lol_data':context},key_file,api_base)
            return result,usage
        try:context=self.context(data['question'])
        except Exception:context={'state':'unavailable','source':SOURCE,'real_time_stats':False}
        return model_call({**data,'lol_data':context},key_file,api_base)
