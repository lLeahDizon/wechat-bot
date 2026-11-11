"""Fixed-account/group alert sender with retained receipt identity."""
import json
from pathlib import Path
import subprocess


class NativeNotifier:
    def __init__(self,root):
        self.root=Path(root)

    def run(self,args):
        account=json.loads((self.root/'admin-evidence/native-account.json').read_text())['id']
        token=(self.root/'secrets/native-bot-token').read_text().strip()
        command=['docker','exec','-i','sub2api-wechat-native','wx','--json',
            '--server','http://127.0.0.1:6174','--token-stdin','--account',account,*args]
        try:
            result=subprocess.run(command,input=token+'\n',capture_output=True,text=True,timeout=5)
            if len(result.stdout)>65536:return None
            value=json.loads(result.stdout)
            return value if isinstance(value,dict) else None
        except (OSError,ValueError,subprocess.TimeoutExpired):return None

    def online(self):
        account=json.loads((self.root/'admin-evidence/native-account.json').read_text())['id']
        value=self.run(['accounts','get',account])
        if value and isinstance(value.get('account'),dict):value=value['account']
        if not value or not isinstance(value.get('authenticated'),bool):return None
        return value['authenticated'] and value.get('connection')=='online'

    def identity(self,receipt,account,group,send_id=None):
        return (isinstance(receipt,dict) and isinstance(receipt.get('id'),str)
            and receipt.get('accountId')==account and receipt.get('destination')==group
            and (send_id is None or receipt['id']==send_id))

    def deliver(self,store,validation=False):
        if self.online() is not True:return
        account=json.loads((self.root/'admin-evidence/native-account.json').read_text())['id']
        group=json.loads((self.root/'admin-evidence/target-group.json').read_text())['id']
        policy=json.loads((self.root/'config/policy.json').read_text())
        if group!=policy['group_id'] or not (policy.get('monitor_enabled') or validation):return
        status=self.run(['status']);server_id=status.get('serverId') if status else None
        if not isinstance(server_id,str) or not server_id:return
        from guard.core import unsafe
        for item in store.pending():
            if unsafe(item['text']):store.update_send(item['key'],'needs_review');continue
            if item['server_id'] and item['server_id']!=server_id:
                store.update_send(item['key'],'needs_review');continue
            if item['state']=='pending':
                store.update_send(item['key'],'submitting',server_id=server_id)
                receipt=self.run(['messages','send',group,'--text',item['text'],
                                  '--idempotency-key',item['key'],'--detach'])
            else:
                receipt=self.run(['messages','sends','get',item['send_id']]) if item['send_id'] else self.run(
                    ['messages','sends','lookup','--idempotency-key',item['key']])
            if not self.identity(receipt,account,group,item['send_id']):
                store.update_send(item['key'],'needs_review');continue
            state=receipt.get('state')
            if state=='accepted':store.update_send(item['key'],'accepted',receipt['id'],server_id)
            elif state in {'queued','submitting'}:store.update_send(item['key'],'submitting',receipt['id'],server_id)
            else:store.update_send(item['key'],'needs_review',receipt['id'],server_id)
