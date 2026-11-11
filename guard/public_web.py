"""Anonymous, bounded public-web research; no cookies, tools or private networks."""
import datetime
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit

DNS_POOL=ThreadPoolExecutor(max_workers=4,thread_name_prefix='public-dns')
DNS_SLOTS=threading.BoundedSemaphore(4)
BLOCKED_HOSTS={'api.apiroute.asia','localhost','host.docker.internal','metadata.google.internal'}
MAX_BODY=2*1024*1024


def public_url(value):
    from .core import _IDENTITY, _SECRET
    if not isinstance(value,str) or len(value)>2048 or any(ord(c)<33 for c in value) or '\\' in value:raise ValueError('invalid_public_url')
    decoded=value
    for _ in range(3):decoded=unquote(decoded)
    if any(ord(c)<32 for c in decoded) or '\\' in decoded or _SECRET.search(decoded) or _IDENTITY.search(decoded):raise ValueError('encoded_private_url')
    p=urlsplit(value);host=(p.hostname or '').encode('idna').decode().lower()
    if p.scheme!='https' or p.username or p.password or p.port not in {None,443} or not re.fullmatch(r'[a-z0-9.-]{1,253}',host):raise ValueError('public_https_only')
    if '.' not in host or host in BLOCKED_HOSTS or host.endswith(('.local','.internal','.localhost','.onion')):raise ValueError('private_host')
    try:ipaddress.ip_address(host)
    except ValueError:pass
    else:raise ValueError('literal_address_denied')
    path=unquote(unquote(unquote(p.path)))
    if any(x in path.lower().split('/') for x in ['admin','login','oauth','callback','logout','signin','..']):raise ValueError('nonpublic_path')
    if _SECRET.search(value) or _IDENTITY.search(value) or any(re.search(r'token|secret|passw|session|signature|auth|api.?key',key,re.IGNORECASE) for key in parse_qs(p.query)):raise ValueError('credential_url')
    return urlunsplit(('https',host,p.path or '/',p.query,''))


def query_text(value):
    from .core import normalize, redact_question
    if not isinstance(value,str):raise ValueError('invalid_search')
    value=normalize(value).strip()
    if not 2<=len(value)<=180 or redact_question(value)!=value or re.search(r'https?://|\[[^\]]*隐藏[^\]]*\]',value):raise ValueError('private_search')
    return value


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self,host,address,timeout):
        super().__init__(host,timeout=timeout,context=ssl.create_default_context());self.address=address
    def connect(self):
        raw=socket.create_connection((self.address,443),self.timeout)
        try:self.sock=self._context.wrap_socket(raw,server_hostname=self.host)
        except BaseException:raw.close();raise


class PublicHTTP:
    def __init__(self,resolver=None,connection=None):
        self.resolver=resolver or socket.getaddrinfo;self.connection=connection or PinnedHTTPS
        self.injected=resolver is not None or connection is not None
    def resolve(self,host,timeout):
        if not DNS_SLOTS.acquire(timeout=max(.01,min(timeout,2))):raise TimeoutError('dns_busy')
        future=DNS_POOL.submit(self.resolver,host,443,0,socket.SOCK_STREAM)
        future.add_done_callback(lambda _:DNS_SLOTS.release())
        try:rows=future.result(timeout=max(.01,min(timeout,2)))
        except FutureTimeout:future.cancel();raise TimeoutError('dns_timeout') from None
        addresses=list(dict.fromkeys(row[4][0] for row in rows))
        if not addresses or any(not ipaddress.ip_address(x).is_global for x in addresses):raise ValueError('private_dns')
        return next((x for x in addresses if ':' not in x),addresses[0])
    def get(self,url,timeout=5):
        url=public_url(url)
        if self.injected:return self._get(url,timeout)
        # A fixed network worker bounds even slow DNS/header/body trickles. Its
        # program is constant; neither the model nor a page supplies code/args.
        worker="import sys,json; v=json.load(sys.stdin); sys.path.insert(0,v['module_root']); from guard.public_web import PublicHTTP; print(json.dumps(PublicHTTP()._get(v['url'],v['timeout'])))"
        result=subprocess.run([sys.executable,'-c',worker],input=json.dumps({'url':url,'timeout':timeout,
            'module_root':str(Path(__file__).resolve().parent.parent)}),capture_output=True,text=True,timeout=timeout)
        if result.returncode or len(result.stdout)>MAX_BODY*3:raise ValueError('public_worker_unavailable')
        return json.loads(result.stdout)
    def _get(self,url,timeout=5):
        deadline=time.monotonic()+timeout
        for _ in range(3):
            url=public_url(url);p=urlsplit(url);remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('public_deadline')
            address=self.resolve(p.hostname,remaining);remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('public_deadline')
            connection=self.connection(p.hostname,address,min(3,remaining))
            try:
                connection.request('GET',p.path+('?' + p.query if p.query else ''),headers={
                    'User-Agent':'Sub2API-Group-FAQ/1.0','Accept':'text/html,application/rss+xml,application/xml,text/plain,application/json',
                    'Accept-Encoding':'identity'})
                response=connection.getresponse()
                if response.status in {301,302,303,307,308}:
                    url=urljoin(url,response.getheader('Location',''));continue
                if response.status!=200:raise ValueError('public_unavailable')
                kind=(response.getheader('Content-Type','') or '').lower()
                if not any(x in kind for x in ['text/html','text/plain','xml','json']):raise ValueError('unsupported_public_content')
                chunks=[];size=0
                while True:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise TimeoutError('public_deadline')
                    if connection.sock:connection.sock.settimeout(min(3,remaining))
                    chunk=response.read1(min(65536,MAX_BODY+1-size))
                    if not chunk:break
                    chunks.append(chunk);size+=len(chunk)
                    if size>MAX_BODY:raise ValueError('public_too_large')
                raw=b''.join(chunks)
                if len(raw)>MAX_BODY:raise ValueError('public_too_large')
                encoding=re.search(r'charset=["\x27]?([A-Za-z0-9_-]+)',kind)
                text=raw.decode(encoding[1] if encoding else 'utf-8','replace')
                if re.search(r'challenge-form|cf-chl-|verify you are human|访问验证|安全验证|验证码',text[:20000],re.IGNORECASE):raise ValueError('public_challenge')
                return {'url':url,'body':text,'kind':kind}
            finally:connection.close()
        raise ValueError('public_redirect_budget')


class Document(HTMLParser):
    def __init__(self):
        super().__init__();self.skip=[];self.text=[];self.links=[];self.anchor=None;self.title='';self.in_title=False;self.published=None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in {'script','style','noscript','nav','footer','header','aside','form'}:self.skip.append(tag)
        if self.skip:return
        if tag=='title':self.in_title=True
        if tag in {'p','div','li','br','h1','h2','h3'}:self.text.append('\n')
        if tag=='a':self.anchor=[attrs.get('href',''),'']
        if tag=='meta' and (attrs.get('property') in {'article:published_time','datePublished'} or attrs.get('name') in {'pubdate','date','publishdate'}):self.published=attrs.get('content','')[:80]
        if tag=='time' and attrs.get('datetime') and not self.published:self.published=attrs['datetime'][:80]
    def handle_endtag(self,tag):
        if self.skip:
            if self.skip[-1]==tag:self.skip.pop()
            return
        if tag=='title':self.in_title=False
        if tag=='a' and self.anchor:self.links.append(self.anchor);self.anchor=None
    def handle_data(self,value):
        if self.skip:return
        if self.in_title:self.title=(self.title+value)[:180]
        else:self.text.append(value)
        if self.anchor:self.anchor[1]+=value[:250]


def plain(value,limit):
    from .core import redact_question
    value=redact_question(re.sub(r'\s+',' ',value).strip())
    return value[:limit] if value else ''


def excerpt(text,queries):
    if len(text)<=6500:return text
    words=set(re.findall(r'[A-Za-z][A-Za-z0-9_.-]{3,}|[\u4e00-\u9fff]{2,12}',' '.join(queries)))
    words-={'Python','python','official','documentation','docs','官方文档'}
    spans=[(0,700)];lower=text.lower()
    for word in sorted(words,key=len,reverse=True)[:6]:
        start=0
        for _ in range(2):
            at=lower.find(word.lower(),start)
            if at<0:break
            spans.append((max(0,at-350),min(len(text),at+750)));start=at+750
    merged=[]
    for a,b in sorted(spans):
        if merged and a<=merged[-1][1]:merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
        else:merged.append((a,b))
    return '\n[…]\n'.join(text[a:b] for a,b in merged)[:6500]


def search_records(value):
    results=value.get('results') if isinstance(value,dict) else None
    if not isinstance(results,list):raise ValueError('search_results_missing')
    filtered={}
    for item in results[:20]:
        if not isinstance(item,dict):continue
        try:url=public_url(item.get('url'))
        except (ValueError,UnicodeError):continue
        filtered.setdefault(url,{'url':url,'title':plain(str(item.get('title') or ''),180),'snippet':plain(str(item.get('snippet') or ''),600)})
    return list(filtered.values())[:10]


class PublicResearch:
    def __init__(self,cache,fetch=None,clock=time.time,key_file=None,search=None):
        self.cache=Path(cache) if cache is not None else None;self.fetch=fetch or PublicHTTP().get;self.clock=clock;self.lock=threading.Lock()
        self.key_file=Path(key_file) if key_file is not None else None;self.search=search or self._search
    def _search(self,queries,timeout):
        if self.key_file is None:raise ValueError('search_key_unavailable')
        key=self.key_file.read_text().strip()
        if not key or any(c.isspace() for c in key):raise ValueError('search_key_invalid')
        payload={'id':'group-search-'+uuid.uuid4().hex,'model':'gpt-6.1-sol','reasoning':{'effort':'medium'},'input':[],
            'commands':{'search_query':[{'q':query_text(q)} for q in queries[:4]]},
            'settings':{'allowed_callers':['direct'],'external_web_access':True},'max_output_tokens':2000}
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        request=urllib.request.Request('https://api.apiroute.asia/v1/alpha/search',data=json.dumps(payload).encode(),
            headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        with opener.open(request,timeout=timeout) as response:
            raw=response.read(MAX_BODY+1)
            if len(raw)>MAX_BODY:raise ValueError('search_too_large')
        return search_records(json.loads(raw))
    def _read(self,url,budget):
        if self.cache is None:raise ValueError('persistent_research_disabled')
        url=public_url(url);now=self.clock();path=self.cache/(hashlib.sha256(url.encode()).hexdigest()[:24]+'.json')
        if self.cache.is_symlink() or path.is_symlink():raise ValueError('cache_symlink')
        self.cache.mkdir(parents=True,exist_ok=True)
        try:
            if path.stat().st_size<=150000:
                saved=json.loads(path.read_text())
                if saved['url']==url and 0<=now-saved['fetched_at']<300:return saved
        except (OSError,ValueError,KeyError,TypeError):pass
        remaining=budget['deadline']-time.monotonic()
        if budget['requests']>=5 or remaining<=0:raise TimeoutError('research_budget')
        budget['requests']+=1;value=self.fetch(url,min(5,remaining));document=Document()
        if 'html' in value['kind']:
            document.feed(value['body']);text=' '.join(document.text)
        else:text=value['body']
        source={'url':url,'final_url':public_url(value['url']),'title':plain(document.title,180),
                'text':plain(text,40000),'published_at':document.published,'fetched_at':now}
        if not source['text']:raise ValueError('empty_public_page')
        temp=path.with_suffix('.tmp')
        if temp.is_symlink():raise ValueError('cache_symlink')
        temp.write_text(json.dumps(source,ensure_ascii=False));temp.chmod(0o600);temp.replace(path)
        return source
    def collect(self,queries,urls=()):
        if self.cache is None:raise ValueError('persistent_research_disabled')
        with self.lock:return self._collect(queries,urls)

    def collect_ephemeral(self,queries,urls=(),*,max_pages=9,max_requests=10,deadline=None,allowed_hosts=None,
                          search_timeout=8,page_timeout=3):
        """Fetch bounded public evidence without creating or reading a cache."""
        if type(max_pages) is not int or not 0<=max_pages<=10:
            raise ValueError('invalid_page_budget')
        if type(max_requests) is not int or not 1<=max_requests<=10:
            raise ValueError('invalid_request_budget')
        deadline=deadline if isinstance(deadline,(int,float)) else time.monotonic()+30
        allowed={str(host).lower().strip('.') for host in (allowed_hosts or ()) if isinstance(host,str)}
        safe=[];missing=[];records=[];sources=[];requests=0;search_calls=0;pages=0
        for value in list(queries or ())[:4]:
            try:safe.append(query_text(value))
            except (ValueError,TypeError):missing.append('search_query_rejected')
        explicit=[]
        for value in list(urls or ())[:2]:
            try:explicit.append(public_url(value))
            except (ValueError,UnicodeError):missing.append('explicit_url_rejected')
        if safe and requests<max_requests and time.monotonic()<deadline:
            try:
                requests+=1;search_calls=1
                found=self.search(safe,min(search_timeout,max(.1,deadline-time.monotonic())))
                records=search_records(found if isinstance(found,dict) else {'results':found})
            except (OSError,TimeoutError,ValueError,TypeError,UnicodeError,KeyError):missing.append('search_unavailable')
        candidates=list(dict.fromkeys([*explicit,*(row['url'] for row in records)]))
        def host_allowed(url):
            if url in explicit:return True
            host=urlsplit(url).hostname or ''
            return any(host==domain or host.endswith('.'+domain) for domain in allowed)
        candidates=[url for url in candidates if host_allowed(url)]
        explicit_set=set(explicit)
        candidates.sort(key=lambda url:0 if url in explicit_set else 1 if re.search(
            r'(?:\.gov(?:\.[a-z]{2})?|\.gov\.cn|\.edu(?:\.[a-z]{2})?|sec\.gov|hkexnews\.hk|cninfo\.com\.cn|mops\.twse\.com\.tw)',
            urlsplit(url).hostname or '',re.IGNORECASE) else 2)
        for url in candidates:
            if pages>=max_pages or requests>=max_requests or time.monotonic()>=deadline:break
            try:
                remaining=deadline-time.monotonic()
                if remaining<=0:break
                requests+=1;pages+=1
                value=self.fetch(url,min(page_timeout,remaining));document=Document()
                kind=(value.get('kind') or '').lower();body=value.get('body') or ''
                if not isinstance(body,str):raise TypeError('invalid_public_body')
                if 'html' in kind:
                    document.feed(body);text=' '.join(document.text)
                else:text=body
                final_url=public_url(value.get('url') or url)
                if url not in explicit_set and not host_allowed(final_url):
                    raise ValueError('source_redirect_host_rejected')
                source={'url':url,'final_url':final_url,'title':plain(document.title,180),
                    'text':plain(text,40000),'published_at':document.published,'fetched_at':self.clock(),
                    'kind':kind}
                if not source['text']:raise ValueError('empty_public_page')
                sources.append(source)
            except (OSError,TimeoutError,ValueError,TypeError,UnicodeError,KeyError,AttributeError):missing.append('page_unavailable')
        sources=[{**source,'text':excerpt(source['text'],safe)} for source in sources]
        return {'kind':'public_research','state':'ok' if sources and not missing else 'partial' if sources else 'unavailable',
            'as_of_bjt':datetime.datetime.fromtimestamp(self.clock(),datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
            'sources':sources,'search_results':records[:8],'missing':sorted(set(missing)),
            'public_requests':requests,'public_pages':pages,'search_calls':search_calls,
            'source_text_is_untrusted':True,'publication_time_is_not_fetch_time':True,
            'complete_web_coverage':False,'persistent_cache_used':False}

    def _collect(self,queries,urls):
        budget={'requests':0,'deadline':time.monotonic()+20};records=[];missing=[];sources=[]
        safe=[]
        for value in queries[:2]:
            try:safe.append(query_text(value))
            except ValueError:missing.append('search_query_rejected')
        search_calls=0
        if safe:
            try:
                if self.cache.is_symlink():raise ValueError('cache_symlink')
                self.cache.mkdir(parents=True,exist_ok=True)
                search_path=self.cache/('search-'+hashlib.sha256(json.dumps(safe,ensure_ascii=False).encode()).hexdigest()[:24]+'.json')
                if search_path.is_symlink():raise ValueError('cache_symlink')
                saved=None
                try:
                    if search_path.stat().st_size<=100000:
                        saved=json.loads(search_path.read_text())
                        if saved.get('kind')!='search' or not 0<=self.clock()-saved['fetched_at']<300:saved=None
                except (OSError,ValueError,KeyError,TypeError):pass
                if saved:records=search_records(saved)
                else:
                    remaining=budget['deadline']-time.monotonic()
                    if remaining<=0:raise TimeoutError('research_budget')
                    budget['requests']+=1;search_calls=1
                    records=self.search(safe,min(12,remaining))
                    if records:
                        temp=search_path.with_suffix('.tmp')
                        if temp.is_symlink():raise ValueError('cache_symlink')
                        temp.write_text(json.dumps({'kind':'search','fetched_at':self.clock(),'results':records},ensure_ascii=False));temp.chmod(0o600);temp.replace(search_path)
            except Exception:missing.append('search_unavailable')
        candidates=list(dict.fromkeys([*urls[:2],*(r['url'] for r in records)]))
        # Official and first-party material gets read before aggregators.
        explicit=set(urls[:2])
        candidates.sort(key=lambda u:0 if u in explicit else 1 if re.search(r'https://(?:[^/]+\.)?(?:gov\.cn|gov|edu|edu\.cn)/|https://(?:docs\.|developer\.|developers\.)',u) else 2)
        for url in candidates:
            if len(sources)>=3 or budget['requests']>=5 or time.monotonic()>=budget['deadline']:break
            try:sources.append(self._read(url,budget))
            except Exception:missing.append('page_unavailable')
        paths=sorted(self.cache.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True) if self.cache.exists() else []
        total=0
        for index,path in enumerate(paths):
            if path.is_symlink():continue
            total+=path.stat().st_size
            if index>=32 or total>4*1024*1024 or self.clock()-path.stat().st_mtime>86400:path.unlink()
        sources=[{**source,'text':excerpt(source['text'],safe)} for source in sources]
        return {'kind':'public_research','state':'ok' if sources and not missing else 'partial' if sources else 'unavailable',
            'as_of_bjt':datetime.datetime.fromtimestamp(self.clock(),datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
            'sources':sources,'search_results':records[:8],'missing':sorted(set(missing)),'public_requests':budget['requests'],
            'search_calls':search_calls,'search_provider':'sub2api_alpha_search',
            'source_text_is_untrusted':True,'publication_time_is_not_fetch_time':True,'complete_web_coverage':False}
