"""One base query, optional planned queries, at most two model calls."""
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
 {'op':'token_usage','purpose':'only the two publicly authorized token subjects','target':['flower','gentle','both'],'period':['today','window']},
]


def diagnose(data,key_file,api_base,budget=None,model_call=call):
    budget=budget or QueryBudget();usages=[]
    if data.get('diagnostic',True):
        base_result=budget.query({'op':'overview'})
        if base_result['state']=='unavailable':
            return {'scope':'service','answer':'当前只读诊断组件暂不可用，未能完成服务器查询；本次不能确认服务器内部状态，请稍后再查。',
                    'refs':[base_result['id']],'queries':[]},[],budget.results
    base={**data,'phase':'plan','evidence':budget.results[:],
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
    for item in queries[:4]:
        try:request=validate(item)
        except (Rejected,TypeError):rejected+=1;continue
        budget.query(request)
    followup={**data,'phase':'answer','evidence':budget.results[:],
              'query_limits':{'total_operations':budget.count,'max_operations':5,'query_seconds_used':round(budget.spent,2),
                              'max_query_seconds':20,'rejected_plans':rejected},
              'usage_targets':base['usage_targets']}
    try:
        result,usage=model_call(followup,key_file,api_base);usages.append(usage)
    except ModelFailure as error:
        usages.append({});return {'failure':error.code},usages,budget.results
    return result,usages,budget.results
