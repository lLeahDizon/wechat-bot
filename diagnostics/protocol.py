from __future__ import annotations

import hashlib
import json
import math
import re
import time

OPS = {'overview','services','resources','host','database','traffic','events','configuration','connectivity','token_usage','member_usage','account_quota'}
WINDOWS = {5,15,60,1440}
TARGETS = {'flower','gentle','both'}
SUBJECT_RE = re.compile(r'^(?:flower|gentle|member_[a-f0-9]{16})$')
MAX_RESPONSE = 65536
MAX_EPOCH = 253402300799


class Rejected(ValueError):
    pass


def validate(value):
    if not isinstance(value,dict) or value.get('op') not in OPS:
        raise Rejected('operation_not_allowed')
    allowed={'op','window_minutes'}
    if value['op']=='token_usage':allowed|={'target','period'}
    if value['op']=='member_usage':allowed|={'target','metrics','period','rank','range_start_epoch','range_end_epoch','commentary'}
    if set(value)-allowed:raise Rejected('unexpected_parameter')
    window=value.get('window_minutes',15)
    if type(window) is not int or window not in WINDOWS:raise Rejected('window_not_allowed')
    result={'op':value['op'],'window_minutes':window}
    if value['op']=='token_usage':
        if value.get('target') not in TARGETS:raise Rejected('usage_target_not_allowed')
        period=value.get('period','today')
        if period not in {'today','window'}:raise Rejected('period_not_allowed')
        result.update(target=value['target'],period=period)
    if value['op']=='member_usage':
        target=value.get('target')
        rank=value.get('rank')
        if type(rank) is not bool:raise Rejected('usage_rank_invalid')
        if target=='all':
            if not rank:raise Rejected('usage_rank_required')
        elif isinstance(target,str):
            if not SUBJECT_RE.fullmatch(target) or rank:raise Rejected('usage_target_not_allowed')
        elif isinstance(target,list):
            if (not 1<=len(target)<=10 or any(not isinstance(item,str) or not SUBJECT_RE.fullmatch(item) for item in target)
                or len(set(target))!=len(target) or rank):
                raise Rejected('usage_targets_not_allowed')
        else:raise Rejected('usage_target_not_allowed')
        metrics=value.get('metrics')
        if (not isinstance(metrics,list) or not 1<=len(metrics)<=2
            or any(not isinstance(x,str) or x not in {'tokens','usd'} for x in metrics)
            or len(set(metrics))!=len(metrics)):
            raise Rejected('usage_metrics_not_allowed')
        if 'commentary' in value:
            if type(value['commentary']) is not bool:raise Rejected('usage_commentary_invalid')
            result['commentary']=value['commentary']
        period=value.get('period','today')
        if period not in {'today','window','range','all'}:raise Rejected('period_not_allowed')
        if period=='window' and window not in {15,60,1440}:raise Rejected('window_not_allowed')
        if period=='range':
            start=value.get('range_start_epoch');end=value.get('range_end_epoch')
            if (type(start) is not int or type(end) is not int or start<0 or end>MAX_EPOCH or start>=end):
                raise Rejected('usage_range_invalid')
            result.update(range_start_epoch=start,range_end_epoch=end)
        elif 'range_start_epoch' in value or 'range_end_epoch' in value:
            raise Rejected('usage_range_unexpected')
        result.update(target=target,metrics=metrics,period=period,rank=rank)
    return result


def evidence(op,data,state='ok',captured_at=None):
    stamp=time.time() if captured_at is None else captured_at
    body={'op':op,'captured_at':stamp,'state':state,'data':data}
    # No user identities, query text, request IDs or source log lines in IDs.
    digest=hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=True).encode()).hexdigest()[:16]
    return {'id':'diag-'+digest,**body}


def safe_tree(value,depth=0):
    if depth>8:return False
    if value is None or isinstance(value,bool):return True
    if isinstance(value,(int,float)):return math.isfinite(value) and abs(value)<10**18
    if isinstance(value,str):
        # Data producers use fixed enums/model IDs, not arbitrary logs or labels.
        return len(value)<=160 and all(c.isalnum() or c in '_.: /+%*-' for c in value)
    if isinstance(value,list):return len(value)<=32 and all(safe_tree(x,depth+1) for x in value)
    if isinstance(value,dict):
        return len(value)<=48 and all(isinstance(k,str) and k.replace('_','').isalnum() and safe_tree(v,depth+1) for k,v in value.items())
    return False


def public_result(value):
    if not safe_tree(value):return False
    if not isinstance(value,dict) or set(value)!={'id','op','captured_at','state','data'}:return False
    body={k:v for k,v in value.items() if k!='id'}
    expected='diag-'+hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=True).encode()).hexdigest()[:16]
    if value['id']!=expected:return False
    from guard.core import unsafe
    def strings(item):
        if isinstance(item,str):return not unsafe(item)
        if isinstance(item,list):return all(strings(x) for x in item)
        if isinstance(item,dict):return all(strings(x) for x in item.values())
        return True
    # The verified digest is opaque metadata. All actual data remains checked.
    return all(strings(v) for k,v in value.items() if k!='id')
