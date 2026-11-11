import re

ACTIONS={'restart_sub2api','restart_astrbot','adopt_improvement','revert_improvement'}


def validate(v):
    if not isinstance(v,dict):raise ValueError('invalid_request')
    fields={'actor','group','request_id','op'}
    for key in ['actor','group','request_id']:
        if not isinstance(v.get(key),str) or not re.fullmatch(r'[a-f0-9]{64}',v[key]):raise ValueError('invalid_identity')
    op=v.get('op')
    if op=='prepare':
        fields|={'action'}
        if v.get('action') not in ACTIONS:raise ValueError('action_denied')
        if v['action'].endswith('_improvement'):
            fields.add('argument')
            if not isinstance(v.get('argument'),str) or not re.fullmatch(r'YJ-[a-f0-9]{16}',v['argument']):raise ValueError('invalid_argument')
    elif op=='confirm':
        fields.add('code')
        if not isinstance(v.get('code'),str) or not re.fullmatch(r'[A-F0-9]{8}',v['code']):raise ValueError('invalid_code')
    elif op in {'activate','status'}:
        fields.add('job_id')
        if not isinstance(v.get('job_id'),str) or not re.fullmatch(r'GL-[a-f0-9]{16}',v['job_id']):raise ValueError('invalid_job')
    else:raise ValueError('operation_denied')
    if set(v)!=fields:raise ValueError('unexpected_parameters')
    return v
