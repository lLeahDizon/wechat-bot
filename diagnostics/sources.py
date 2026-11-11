"""Privileged fixed readers. No caller-supplied command, SQL, filename or URL."""
from __future__ import annotations

import datetime
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import threading
import time
import urllib.request

ROOT=Path('/opt/sub2api-wechat-bot')
CONTAINERS={'gateway':'sub2api','database':'sub2api-postgres','cache':'sub2api-redis',
            'responder':'sub2api-wechat-astrbot','wechat':'sub2api-wechat-native'}
POST_ROUTES={'/v1/responses','/v1/chat/completions','/v1/completions'}
MODEL_ID=re.compile(r'^(?:gpt|o[1-9]|claude|gemini|\*)[A-Za-z0-9.*_+-]{0,78}$')


def command(args,timeout=3):
    result=subprocess.run(args,capture_output=True,text=True,timeout=timeout,
                          env={**os.environ,'LC_ALL':'C'})
    if result.returncode or len(result.stdout)>131072:raise OSError('reader_unavailable')
    return result.stdout


def percentile(values,percent):
    if not values:return None
    values=sorted(values);position=(len(values)-1)*percent
    low,high=math.floor(position),math.ceil(position)
    return round(values[low]+(values[high]-values[low])*(position-low),2)


def gateway_logs(path,minutes,now=None,byte_limit=8*1024*1024):
    """Read bounded tail, discard identities/bodies before returning any data."""
    now=time.time() if now is None else now
    start=now-minutes*60
    with path.open('rb') as handle:
        size=os.fstat(handle.fileno()).st_size
        offset=max(0,size-byte_limit);handle.seek(offset);raw=handle.read(byte_limit)
    if offset:raw=raw.partition(b'\n')[2]
    earliest=None;count=errors=client_errors=0;durations=[];invalid=0
    if raw and not raw.endswith(b'\n'):invalid+=1
    for line in raw.splitlines():
        try:
            item=json.loads(line);stamp=item.get('ts')
            if not isinstance(stamp,(int,float)) or not math.isfinite(stamp):continue
            earliest=stamp if earliest is None else min(earliest,stamp)
            if not start<=stamp<=now:continue
            request=item.get('request') or {};uri=request.get('uri','')
            if request.get('method')!='POST' or not isinstance(uri,str) or uri.split('?',1)[0] not in POST_ROUTES:continue
            status=item.get('status')
            if type(status) is not int or not 100<=status<=599:continue
            count+=1;errors+=status>=500;client_errors+=400<=status<500
            duration=item.get('duration')
            if isinstance(duration,(int,float)) and math.isfinite(duration) and 0<=duration<86400:
                durations.append(duration*1000)
        except (ValueError,TypeError,AttributeError):invalid+=1
    complete=earliest is not None and earliest<=start and invalid==0
    return {'metric':'gateway_http_5xx','window_minutes':minutes,'coverage_complete':complete,
        'observed_completed_requests':count,'http_5xx':errors,'http_4xx':client_errors,
        'http_5xx_rate':round(errors/count,6) if count and complete else None,
        'completed_http_duration_p95_ms':percentile(durations,.95) if complete else None,
        'limitations':'does_not_cover_http_200_stream_errors_or_ws_turns'}


class Sources:
    def __init__(self,root=ROOT):
        self.root=root
        self.lock=threading.RLock()
        self.cpu_previous=None
        self.cpu_value=None

    def sql(self,select):
        # The role sees only dedicated views. SQL text is constructed solely by
        # the fixed methods below after protocol validation, never by the model.
        text="BEGIN READ ONLY; SET LOCAL statement_timeout='2500ms'; SET LOCAL lock_timeout='500ms'; "+select+'; ROLLBACK;'
        result=subprocess.run(['docker','exec','-i','sub2api-postgres','psql','-X','-qAt',
            '-U','bot_diagnostics_ro','-d','sub2api','-v','ON_ERROR_STOP=1'],
            input=text,capture_output=True,text=True,timeout=4)
        if result.returncode or len(result.stdout)>32768:raise OSError('database_reader_unavailable')
        # json_agg(row) may include literal formatting newlines between rows.
        # psql -qAt emits this one SELECT value without transaction tags.
        try:value=json.loads(result.stdout.strip())
        except ValueError:raise OSError('database_reader_format') from None
        if not isinstance(value,(dict,list)):raise OSError('database_reader_format')
        return value

    def resources(self):
        with self.lock:
            counters=[int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
            total,idle=sum(counters),counters[3]+counters[4];stamp=time.monotonic()
            if self.cpu_previous and stamp-self.cpu_previous[0]>=5:
                _,old_total,old_idle=self.cpu_previous
                if total>old_total:self.cpu_value=round(100*(1-(idle-old_idle)/(total-old_total)),2)
                self.cpu_previous=(stamp,total,idle)
            elif self.cpu_previous is None:self.cpu_previous=(stamp,total,idle)
            memory={line.split(':',1)[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
            fs=os.statvfs(self.root);free=fs.f_bavail*fs.f_frsize;capacity=fs.f_blocks*fs.f_frsize
            size=None
            try:
                stored=json.loads((self.root/'admin-evidence/storage-status.json').read_text())
                if time.time()-stored['captured_at']<120:size=stored['bot_data_bytes']
            except (OSError,ValueError,KeyError,TypeError):pass
            return {'cpu_percent':self.cpu_value,'memory_available_percent':round(100*memory['MemAvailable']/memory['MemTotal'],2),
                'memory_available_mib':round(memory['MemAvailable']/1024,2),'disk_available_percent':round(100*free/capacity,2),
                'disk_available_gib':round(free/1024**3,2),'bot_data_mib':round(size/1024**2,2) if size is not None else None,
                'bot_data_gib':round(size/1024**3,4) if size is not None else None}

    def services(self):
        result={}
        deadline=time.monotonic()+3
        for label,name in CONTAINERS.items():
            try:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise OSError('reader_budget')
                raw=command(['docker','inspect','--format','{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}} {{.State.OOMKilled}}',name],timeout=min(1,remaining))
                running,health,oom=raw.strip().split()
                result[label]={'observed':True,'running':running,'health':health if health in {'healthy','unhealthy','starting'} else 'unknown',
                    'oom':oom=='true'}
                result[label]['running']=running=='true'
            except (OSError,ValueError,subprocess.TimeoutExpired):result[label]={'observed':False,'running':None,'health':'unknown','oom':False}
        try:
            proc=subprocess.run(['systemctl','is-active','caddy'],capture_output=True,text=True,timeout=max(.01,min(.5,deadline-time.monotonic())))
            state=proc.stdout.strip();observed=state in {'active','inactive','failed','activating','deactivating'}
            result['proxy']={'observed':observed,'running':state=='active' if observed else None}
        except (OSError,subprocess.TimeoutExpired):result['proxy']={'observed':False,'running':None}
        return result

    def host(self):
        loads=[float(v) for v in Path('/proc/loadavg').read_text().split()[:3]]
        memory={line.split(':')[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
        received=sent=0
        for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
            name,values=line.split(':',1);values=values.split()
            if name.strip()!='lo':received+=int(values[0]);sent+=int(values[8])
        established=waiting=0
        for filename in ['/proc/net/tcp','/proc/net/tcp6']:
            for line in Path(filename).read_text().splitlines()[1:]:
                state=line.split()[3];established+=state=='01';waiting+=state=='06'
        return {'load_1m':loads[0],'load_5m':loads[1],'load_15m':loads[2],
            'uptime_hours':round(float(Path('/proc/uptime').read_text().split()[0])/3600,2),
            'swap_total_mib':round(memory['SwapTotal']/1024,2),
            'swap_used_mib':round((memory['SwapTotal']-memory['SwapFree'])/1024,2),
            'process_count':sum(p.name.isdigit() for p in Path('/proc').iterdir()),
            'tcp_established':established,'tcp_time_wait':waiting,
            'network_received_mib':round(received/1024**2,2),'network_sent_mib':round(sent/1024**2,2),
            'network_counter_scope':'all_non_loopback_interfaces_since_boot_not_internet_throughput'}

    def database(self):
        return self.sql("SELECT json_build_object('connections',numbackends,'committed_transactions',xact_commit,"
            "'rolled_back_transactions',xact_rollback,'deadlocks',deadlocks,'blocks_read',blks_read,"
            "'blocks_hit',blks_hit,'temporary_mib',round(temp_bytes/1048576.0,2),"
            "'database_mib',round(pg_database_size(datid)/1048576.0,2),"
            "'scope','cumulative_database_statistics_not_individual_query_text') "
            "FROM pg_stat_database WHERE datname=current_database()")

    def health(self):
        try:
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open('http://127.0.0.1:8080/health',timeout=1) as response:
                data=json.loads(response.read(8192))
                return {'service':'healthy' if response.status==200 and data.get('status')=='ok' else 'degraded'}
        except (OSError,ValueError):return {'service':'unavailable'}

    def traffic(self,window):
        result={}
        try:result['gateway']=gateway_logs(Path('/var/log/caddy/sub2api-access.log'),window)
        except (OSError,ValueError):result['gateway']={'metric':'gateway_http_5xx','coverage_complete':False,'http_5xx_rate':None}
        try:
            result['metered']=self.sql("SELECT json_build_object('metered_records',count(*),"
                "'metered_duration_p50_ms',percentile_cont(0.5) WITHIN GROUP(ORDER BY duration_ms) FILTER(WHERE duration_ms>=0),"
                "'metered_duration_p95_ms',percentile_cont(0.95) WITHIN GROUP(ORDER BY duration_ms) FILTER(WHERE duration_ms>=0),"
                "'metered_ttfb_p95_ms',percentile_cont(0.95) WITHIN GROUP(ORDER BY first_token_ms) FILTER(WHERE first_token_ms>=0)) "
                f"FROM bot_diagnostics.metered WHERE created_at>=now()-interval '{window} minutes'")
        except (OSError,ValueError,subprocess.TimeoutExpired):result['metered']={'state':'unavailable'}
        return result

    def events(self,window):
        return self.sql("SELECT json_build_object('final_failure_event_rows',count(*) FILTER(WHERE is_failure),"
            "'logged_failure_requests_with_id',count(DISTINCT request_identity) FILTER(WHERE is_failure),"
            "'failure_events_missing_identity',count(*) FILTER(WHERE is_failure AND request_identity IS NULL),"
            "'recovered_upstream_error_event_rows',count(*) FILTER(WHERE recovered),"
            "'recorded_upstream_context_entries',coalesce(sum(context_entries),0),"
            "'limitations','events_are_not_unique_end_to_end_requests') "
            f"FROM bot_diagnostics.error_events WHERE created_at>=now()-interval '{window} minutes'")

    def configuration(self):
        data=self.sql("SELECT json_build_object('accounts',(SELECT row_to_json(a) FROM bot_diagnostics.account_summary a),"
            "'mappings',(SELECT coalesce(json_agg(x),'[]'::json) FROM "
            '(SELECT source_model,target_model FROM bot_diagnostics.model_mapping LIMIT 30) x))')
        return {'account_availability':data['accounts'],'model_mappings':[x for x in data['mappings']
            if MODEL_ID.fullmatch(x['source_model']) and MODEL_ID.fullmatch(x['target_model'])],
            'excluded_mapping_entries':sum(not (MODEL_ID.fullmatch(x['source_model']) and MODEL_ID.fullmatch(x['target_model'])) for x in data['mappings']),
            'account_identity_disclosed':False,'authentication_probe_performed':False}

    def token_usage(self,target,period,window):
        condition="u.created_at >= date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'" if period=='today' else f"u.created_at>=now()-interval '{window} minutes'"
        selector="b.subject IN ('flower','gentle')" if target=='both' else f"b.subject='{target}'"
        data=self.sql("SELECT json_build_object('items',coalesce(json_agg(x),'[]'::json)) FROM (SELECT b.subject,b.available AS binding_available,"
            "count(u.created_at) AS metered_records,coalesce(sum(input_tokens),0) AS ordinary_input_tokens,"
            "coalesce(sum(cache_creation_tokens),0) AS cache_write_tokens,coalesce(sum(cache_read_tokens),0) AS cache_read_tokens,"
            "coalesce(sum(input_tokens::bigint+cache_creation_tokens+cache_read_tokens),0) AS input_tokens,"
            "coalesce(sum(output_tokens),0) AS output_tokens,"
            "coalesce(sum(input_tokens::bigint+output_tokens+cache_creation_tokens+cache_read_tokens),0) AS total_tokens "
            f"FROM bot_diagnostics.allowed_subjects b LEFT JOIN bot_diagnostics.allowed_tokens u ON u.subject=b.subject AND {condition} "
            f"WHERE {selector} GROUP BY b.subject,b.available) x")
        for item in data['items']:
            if not item['binding_available']:
                for name in list(item):
                    if name.endswith('_tokens') or name=='metered_records':item[name]=None
        data.update(period=period,window_minutes=window if period=='window' else None,timezone='Asia/Shanghai',
                    input_includes_cache=True,reasoning_already_in_output=True)
        # A deleted/expired binding has no rows and is not silently rebound.
        return data

    def member_usage(self,target,metrics,period,window,rank,range_start_epoch=None,range_end_epoch=None):
        if target=='all':selector='TRUE'
        else:
            subjects=[target] if isinstance(target,str) else target
            selector='b.subject IN ('+','.join("'"+subject+"'" for subject in subjects)+')'
        ctes=[f"bound AS (SELECT b.subject,b.available AS binding_available FROM bot_diagnostics.allowed_subjects b WHERE {selector})"]
        day_start="date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'"
        if period=='today':condition=f"u.created_at>={day_start} AND u.created_at<=now()"
        elif period=='window':condition=f"u.created_at>=now()-interval '{window} minutes' AND u.created_at<=now()"
        elif period=='range':condition=(f"u.created_at>=to_timestamp({range_start_epoch}) "
            f"AND u.created_at<LEAST(to_timestamp({range_end_epoch}),now())")
        else:condition='u.created_at>=to_timestamp(0) AND u.created_at<=now()'
        columns=[]
        if 'tokens' in metrics:
            columns.extend([
                "coalesce(sum(u.input_tokens),0)::bigint AS ordinary_input_tokens",
                "coalesce(sum(u.cache_read_tokens),0)::bigint AS cache_read_tokens",
                "coalesce(sum(u.cache_creation_tokens),0)::bigint AS cache_write_tokens",
                "coalesce(sum(u.input_tokens::bigint+u.cache_read_tokens+u.cache_creation_tokens),0)::bigint AS input_tokens",
                "coalesce(sum(u.output_tokens),0)::bigint AS output_tokens",
                "coalesce(sum(u.input_tokens::bigint+u.cache_read_tokens+u.cache_creation_tokens+u.output_tokens),0)::bigint AS total_tokens",
            ])
        if 'usd' in metrics:columns.append("coalesce(sum(u.actual_cost),0)::numeric::text AS usd_amount")
        ctes.append("usage AS (SELECT b.subject,b.binding_available,"+','.join(columns)+
            " FROM bound b LEFT JOIN bot_diagnostics.allowed_tokens u ON u.subject=b.subject AND "+condition+
            " GROUP BY b.subject,b.binding_available)")
        def items(name,fields,order):
            predicate=''
            if rank:
                positive='total_tokens>0' if name=='token_items' else 'usd_amount::numeric>0'
                predicate=' WHERE binding_available AND '+positive
            return (f"coalesce((SELECT json_agg(x) FROM (SELECT subject,binding_available,{fields} FROM usage"
                f"{predicate} ORDER BY {order},subject LIMIT 10) x),'[]'::json)")
        tokens="'[]'::json"
        usd="'[]'::json"
        if 'tokens' in metrics:
            tokens=items('token_items','ordinary_input_tokens,cache_read_tokens,cache_write_tokens,input_tokens,output_tokens,total_tokens','total_tokens DESC')
        if 'usd' in metrics:
            usd=items('usd_items','usd_amount','usd_amount::numeric DESC')
        data=self.sql('WITH '+','.join(ctes)+" SELECT json_build_object('token_items',"+tokens+
            ",'usd_items',"+usd+")")
        data.update(period=period,window_minutes=window if period=='window' else None,rank=rank)
        if period=='range':data.update(range_start_epoch=range_start_epoch,range_end_epoch=range_end_epoch)
        return data

    def connectivity(self):
        # A child deadline also bounds DNS resolution, which socket timeouts do
        # not consistently bound. Targets and program text are compile-time.
        source="""import socket,ssl,time,json
result={'path':'host_direct','authenticated_probe':False}
for label,host in [('service','api.apiroute.asia'),('public_upstream','chatgpt.com')]:
 start=time.monotonic()
 try:
  with socket.create_connection((host,443),timeout=1) as raw:
   with ssl.create_default_context().wrap_socket(raw,server_hostname=host):pass
  result[label]={'tls_verified':True,'elapsed_ms':round((time.monotonic()-start)*1000,2)}
 except OSError:result[label]={'tls_verified':False}
result['limitations']='does_not_verify_account_auth_or_container_proxy_path'
print(json.dumps(result))
"""
        return json.loads(command(['/usr/bin/python3','-c',source],timeout=3))

    def overview(self):
        return {'health':self.health(),'resources':self.resources(),'services':self.services()}

    def execute(self,request):
        from .protocol import validate
        request=validate(request)
        op=request['op'];window=request['window_minutes']
        if op in {'overview','services','resources','host','database','configuration','connectivity'}:return getattr(self,op)()
        if op in {'traffic','events'}:return getattr(self,op)(window)
        if op=='token_usage':return self.token_usage(request['target'],request['period'],window)
        if op=='member_usage':return self.member_usage(request['target'],request['metrics'],request['period'],window,
            request['rank'],request.get('range_start_epoch'),request.get('range_end_epoch'))
        raise ValueError('operation_not_allowed')
