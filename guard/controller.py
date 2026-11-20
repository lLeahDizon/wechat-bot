"""Bounded evidence planning with an isolated production wall-clock boundary."""
import json
from pathlib import Path
import subprocess
import sys
import time
from diagnostics.client import QueryBudget
from diagnostics.protocol import Rejected,validate

from .model import call,ModelFailure

ALLOWED=[
 {'op':'resources','purpose':'CPU/memory/disk and bot storage'},
 {'op':'host','purpose':'host load, uptime, swap, process and anonymous network counters'},
 {'op':'database','purpose':'read-only database connection, transaction and cache counters'},
 {'op':'services','purpose':'gateway/database/cache/proxy/bot observed state'},
 {'op':'traffic','purpose':'completed gateway HTTP5xx and metered latency'},
 {'op':'events','purpose':'separate failure and recovered upstream event counts'},
 {'op':'configuration','purpose':'anonymous availability and active model mappings'},
 {'op':'connectivity','purpose':'fixed public domains host direct TLS only'},
 {'op':'exposure','purpose':'anonymous local listener counts, not proof of external reachability'},
 {'op':'firewall','purpose':'UFW active/default policy and rule counts, excludes cloud security groups'},
 {'op':'ssh','purpose':'bounded SSH authentication event counts, coverage may be incomplete'},
 {'op':'versions','purpose':'declared image version tags, consult official advisories before a vulnerability verdict'},
 {'op':'backups','purpose':'task archive metadata, does not prove database coverage or tested restore'},
 {'op':'history','purpose':'bounded retained host metric samples and coverage'},
 {'op':'token_usage','purpose':'only the two publicly authorized token subjects','target':['flower','gentle','both'],'period':['today','window']},
]


def diagnose(data,key_file,api_base,budget=None,model_call=call,clock=time.monotonic):
    deadline=clock()+60
    budget=budget or QueryBudget(max_operations=10,max_seconds=60,deadline=deadline);usages=[]
    if data.get('diagnostic',True):
        base_result=budget.query({'op':'overview'})
        if base_result['state']=='unavailable':
            return {'scope':'service','answer':'当前只读诊断组件暂不可用，未能完成服务器查询；本次不能确认服务器内部状态，请稍后再查。',
                    'refs':[base_result['id']],'queries':[]},[],budget.results
    base={**data,'phase':'plan','evidence':budget.results[:],'current_epoch':int(time.time()),
          'remaining_queries':10-budget.count,'remaining_seconds':max(0,round(deadline-clock(),2)),
          'allowed_queries':ALLOWED,'allowed_window_minutes':[5,15,60,1440],
          'usage_targets':{'flower':'example_person_a（example_person_a）','gentle':'example_person_b（example_person_b）'}}
    try:
        first,usage=model_call(base,key_file,api_base);usages.append(usage)
    except ModelFailure as error:
        usages.append({});return {'failure':error.code},usages,budget.results
    queries=first.get('queries',[])
    if first.get('scope')!='service' or not isinstance(queries,list) or not queries:
        return first,usages,budget.results
    rejected=0
    seen=set()
    while queries and len(usages)<=3 and budget.count<10 and clock()<deadline:
        added=0
        for item in queries[:10-budget.count]:
            if budget.count>=10 or clock()>=deadline:break
            try:request=validate(item)
            except (Rejected,TypeError):rejected+=1;continue
            identity=json.dumps(request,sort_keys=True)
            if identity in seen:continue
            seen.add(identity);budget.query(request);added+=1
        if not added or budget.count>=10 or len(usages)>=3:break
        follow={**base,'evidence':budget.results[:],'remaining_queries':10-budget.count,
                'followup_round':len(usages),'remaining_seconds':max(0,round(deadline-clock(),2))}
        try:
            planned,usage=model_call(follow,key_file,api_base);usages.append(usage)
        except ModelFailure as error:
            usages.append({});return {'failure':error.code},usages,budget.results
        if planned.get('scope')=='service' and planned.get('answer') and not planned.get('queries'):
            return planned,usages,budget.results
        queries=planned.get('queries',[])
        if not isinstance(queries,list):break
    followup={**data,'phase':'answer','evidence':budget.results[:],
              'query_limits':{'total_operations':budget.count,'max_operations':10,'query_seconds_used':round(budget.spent,2),
                              'max_total_seconds':60,'rejected_plans':rejected},
              'usage_targets':base['usage_targets']}
    try:
        if clock()>=deadline:raise ModelFailure('diagnostic_deadline')
        result,usage=model_call(followup,key_file,api_base);usages.append(usage)
    except ModelFailure as error:
        usages.append({});return {'failure':error.code},usages,budget.results
    return result,usages,budget.results


def run_isolated(data,key_file,api_base):
    """Kill the worker at the deadline, including a slow HTTP body read."""
    from diagnostics.protocol import public_result
    request={'data':data,'key_file':str(key_file),'api_base':api_base}
    root=Path(__file__).resolve().parents[1]
    try:
        completed=subprocess.run([sys.executable,'-m','guard.diagnostic_worker'],cwd=root,
            input=json.dumps(request,ensure_ascii=False),capture_output=True,text=True,timeout=60,
            env={'PATH':'/usr/bin:/bin','PYTHONPATH':'/opt/query-client',
                 'PYTHONIOENCODING':'utf-8','PYTHONDONTWRITEBYTECODE':'1'})
    except subprocess.TimeoutExpired:
        return {'failure':'diagnostic_deadline'},[],[]
    if completed.returncode or len(completed.stdout)>1024*1024:
        return {'failure':'diagnostic_worker_failed'},[],[]
    try:
        value=json.loads(completed.stdout)
        result,usages,proof=value['result'],value['usages'],value['evidence']
        if (not isinstance(result,dict) or not isinstance(usages,list) or len(usages)>4
            or not all(isinstance(x,dict) for x in usages) or not isinstance(proof,list)
            or len(proof)>10 or not all(public_result(x) for x in proof)):
            raise ValueError('invalid_worker_output')
        return result,usages,proof
    except (ValueError,KeyError,TypeError):
        return {'failure':'diagnostic_worker_failed'},[],[]
