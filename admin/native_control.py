"""Operator-only account/token/login bootstrap, with no secret stdout."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

ROOT=Path('/opt/sub2api-wechat-bot')
ALIAS='sub2api-bot'
ACCOUNT=ROOT/'admin-evidence/native-account.json'
LOGIN=ROOT/'admin-evidence/login-operation.json'


def cli(args,account=None,timeout=30,terminal_operation=False):
    command=['docker','exec','sub2api-wechat-native','wx','--json',
        '--server','http://127.0.0.1:6174','--token-file','/run/secrets/native-owner-token']
    if account:command+=['--account',account]
    result=subprocess.run(command+args,capture_output=True,text=True,timeout=timeout)
    if result.returncode:
        # Native returns exit 9 together with a valid retained terminal operation.
        # Keep that state readable; it is not a transport or authentication error.
        try:value=json.loads(result.stdout)
        except ValueError:value=None
        if (terminal_operation and result.returncode==9 and isinstance(value,dict)
                and value.get('id')==args[-1]
                and value.get('state') in {'failed','expired','cancelled'}):
            return value
        # Do not echo QR URLs, access tokens, message data or exception bodies.
        try:code=json.loads(result.stderr).get('error',{}).get('code','native_cli_failed')
        except (ValueError,AttributeError):code='native_cli_failed'
        if not re.fullmatch(r'[a-z_]{1,64}',str(code)):code='native_cli_failed'
        raise RuntimeError(code)
    return json.loads(result.stdout)


def account_id():
    return json.loads(ACCOUNT.read_text())['id']


def bootstrap():
    items=cli(['accounts','list'])['items']
    if ACCOUNT.exists():
        identifier=account_id()
        if not any(item.get('id')==identifier for item in items):raise RuntimeError('recorded_account_missing')
    else:
        if items:raise RuntimeError('unrecorded_accounts_present')
        created=cli(['accounts','create','--alias',ALIAS])
        if isinstance(created.get('account'),dict):created=created['account']
        identifier=created.get('id')
        if not isinstance(identifier,str) or not identifier or len(identifier)>200:raise RuntimeError('account_result_unrecognized')
        ACCOUNT.write_text(json.dumps({'id':identifier,'alias':ALIAS})+'\n')
    token_file=ROOT/'secrets/native-bot-token'
    grant_file=ROOT/'admin-evidence/native-bot-grant.json'
    if not token_file.read_text().strip():
        grants=cli(['tokens','list'])
        rows=grants.get('items',grants.get('grants',[])) if isinstance(grants,dict) else grants
        if any(row.get('name')=='sub2api-astrbot' for row in rows):
            raise RuntimeError('existing_grant_requires_review')
        result=cli(['tokens','create','sub2api-astrbot','--accounts',identifier,'--scopes','read','--scopes','send'])
        token,grant=result.get('token'),result.get('grant',{})
        if not isinstance(token,str) or not token or set(grant.get('scopes',[]))!={'read','send'} or grant.get('accounts')!=[identifier]:
            raise RuntimeError('token_result_unrecognized')
        token_file.write_text(token+'\n');token_file.chmod(0o400);os.chown(token_file,10002,10002)
        grant_file.write_text(json.dumps({k:grant.get(k) for k in ['id','name','accounts','scopes']})+'\n')
    config_path=ROOT/'astrbot-data/cmd_config.json'
    config=json.loads(config_path.read_text(encoding='utf-8-sig'))
    for platform in config['platform']:
        if platform.get('id')=='sub2api-wechat':
            platform.update(account_id=identifier,enable=False)
    config_path.write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n')
    os.chown(config_path,10002,10002);config_path.chmod(0o600)
    return {'account_configured':True,'scoped_token':True,'scopes':['read','send'],'platform_enabled':False}


def status():
    identifier=account_id()
    items=cli(['accounts','list'])['items']
    match=next((item for item in items if item.get('id')==identifier),None)
    if match is None:raise RuntimeError('account_not_found')
    return {'authenticated':bool(match.get('authenticated')),'connection':match.get('connection'),
            'account_field_names':sorted(match.keys())}


def operation():
    value=json.loads(LOGIN.read_text())
    result=cli(['auth','attempts','get',value['id']],account_id(),terminal_operation=True)
    if isinstance(result.get('operation'),dict):result=result['operation']
    qr=result.get('result') or {}
    return value,result,qr


def login_qr(refresh=False):
    current=status()
    if current['authenticated'] and current['connection']=='online':return {'already_authenticated':True}
    if refresh:
        if current['authenticated']:raise RuntimeError('authenticated_account_requires_review')
        if not LOGIN.exists():raise RuntimeError('no_login_operation_to_refresh')
        _,previous,_=operation()
        previous_state=previous.get('state')
        refreshable=previous_state in {'failed','expired','cancelled'}
        refreshable=refreshable or (previous_state=='succeeded'
            and current.get('authenticated') is False
            and current.get('connection')=='disconnected')
        if not refreshable:
            raise RuntimeError('login_operation_still_active')
        # Preserve every terminal receipt, including a successful login whose
        # account later disconnected, before starting one new operation.
        archived=ROOT/'admin-evidence/login-operation.previous.json'
        if archived.exists():archived=archived.with_name('login-operation.previous-'+str(time.time_ns())+'.json')
        if archived.exists():raise RuntimeError('login_receipt_archive_collision')
        LOGIN.replace(archived)
    if not LOGIN.exists():
        key='sub2api-login-'+uuid.uuid4().hex
        result=cli(['auth','login','--strategy','qr','--qr-refresh','manual',
                    '--detach','--idempotency-key',key],account_id())
        if isinstance(result.get('operation'),dict):result=result['operation']
        identifier=result.get('id')
        if not isinstance(identifier,str) or not identifier:raise RuntimeError('login_result_unrecognized')
        LOGIN.write_text(json.dumps({'id':identifier,'idempotency_key':key,'created_at':time.time()})+'\n')
    value,result,qr=operation()
    if not qr.get('qrAvailable'):
        return {'qr_available':False,'operation_state':result.get('state'),'result_fields':sorted(qr.keys())}
    directory=ROOT/'native-data/private-qr'
    directory.mkdir(mode=0o700,exist_ok=True);os.chown(directory,10001,10001)
    filename='login-'+str(time.time_ns())+'.png'
    result=cli(['auth','qr',value['id'],'--output','/var/lib/agent-wechat/private-qr/'+filename],account_id())
    path=directory/filename
    if not path.is_file():raise RuntimeError('qr_file_missing')
    path.chmod(0o600)
    return {'qr_available':True,'qr_path':str(path),'qr_generation':result.get('qrGeneration')}


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['bootstrap','status','login-qr','refresh-qr','operation'])
    action=parser.parse_args().action
    if json.loads((ROOT/'task.json').read_text()).get('task')!='sub2api-wechat-group-bot-trial-20261103':
        raise RuntimeError('wrong_task')
    if action=='bootstrap':result=bootstrap()
    elif action=='status':result=status()
    elif action in {'login-qr','refresh-qr'}:result=login_qr(refresh=action=='refresh-qr')
    else:
        _,value,qr=operation();result={'operation_state':value.get('state'),'qr_available':bool(qr.get('qrAvailable')),'result_fields':sorted(qr.keys())}
    print(json.dumps(result))


if __name__=='__main__':
    try:main()
    except Exception as error:
        code=str(error)
        if not re.fullmatch(r'[a-z_]{1,64}',code):code='operator_step_failed'
        print(json.dumps({'failed':True,'category':code}));raise SystemExit(1)
