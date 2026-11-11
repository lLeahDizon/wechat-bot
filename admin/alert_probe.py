"""Authorized target-group notification exercise; stable keys across retries."""
import json
from pathlib import Path
import sys
import time

ROOT=Path('/opt/sub2api-wechat-bot')
sys.path.insert(0,str(ROOT/'code'))
from diagnostics.notify import NativeNotifier
from diagnostics.store import Store

TEXTS={
 'open':'【联调演练，非真实故障】告警通知发送测试。本条仅用于验证通知与去重，不代表服务器异常。',
 'recovery':'【联调演练，非真实故障】恢复通知发送测试。本次演练结束；实际恢复通知只依据连续的新鲜监测结果。',
}


def main():
    if json.loads((ROOT/'task.json').read_text())['task']!='sub2api-wechat-group-bot-trial-20261103':raise RuntimeError('wrong_task')
    if (ROOT/'admin-evidence/space-paused.json').exists():raise RuntimeError('storage_pause')
    notifier=NativeNotifier(ROOT)
    if notifier.online() is not True:raise RuntimeError('wechat_not_online')
    account=json.loads((ROOT/'admin-evidence/native-account.json').read_text())['id']
    group=json.loads((ROOT/'admin-evidence/target-group.json').read_text())['id']
    intro=json.loads((ROOT/'admin-evidence/introduction.json').read_text())
    retained=notifier.run(['messages','sends','get',intro['id']])
    if not notifier.identity(retained,account,group,intro['id']):raise RuntimeError('native_receipt_contract_unverified')
    store=Store(ROOT/'diagnostics/acceptance-alerts.sqlite')
    result=[]
    for phase,text in TEXTS.items():
        key=store.enqueue('readonly-acceptance-20261103',phase,'gateway',text)
        for attempt in range(8):
            row=store.db.execute('SELECT state,send_id FROM notifications WHERE key=?',(key,)).fetchone()
            if row['state']=='accepted':break
            notifier.deliver(store,validation=True)
            time.sleep(.5)
        row=store.db.execute('SELECT state,send_id FROM notifications WHERE key=?',(key,)).fetchone()
        if row['state']!='accepted':raise RuntimeError('notification_receipt_needs_review')
        # Calling delivery after acceptance must not create another send.
        notifier.deliver(store,validation=True)
        receipt=notifier.run(['messages','sends','lookup','--idempotency-key',key])
        if not notifier.identity(receipt,account,group,row['send_id']) or receipt.get('state')!='accepted':
            raise RuntimeError('notification_key_receipt_mismatch')
        result.append({'phase':phase,'state':'accepted','same_key_same_send_id':True})
    store.close()
    value={'at':time.time(),'results':result,'real_incident_injected':False,'real_service_changed':False,
           'model_calls':0,'recipient_visibility':'pending_user_check'}
    path=ROOT/'admin-evidence/alert-test.json';path.write_text(json.dumps(value,indent=2)+'\n');path.chmod(0o600)
    print(json.dumps(value))


if __name__=='__main__':
    try:main()
    except Exception as error:
        code=str(error)
        if code not in {'wrong_task','storage_pause','wechat_not_online','native_receipt_contract_unverified',
                        'notification_receipt_needs_review','notification_key_receipt_mismatch'}:code='alert_test_requires_review'
        print(json.dumps({'failed':True,'category':code}));raise SystemExit(1)
