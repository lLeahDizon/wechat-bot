"""One fixed group, existing scoped Native client, durable occurrence receipts."""
import asyncio
import time
from urllib.parse import quote

from .core import digest,unsafe,mask_labels
from .reminders import GRACE,stamp


class ReminderDelivery:
    def __init__(self,reminders,clock=time.time):self.reminders=reminders;self.clock=clock

    @staticmethod
    def valid(receipt,client,group):
        return (isinstance(receipt,dict) and isinstance(receipt.get('id'),str) and bool(receipt['id'])
            and receipt.get('accountId')==client.account_id and receipt.get('destination')==group)

    async def reconcile(self,client,group,server_id):
        for row in self.reminders.pending_receipts():
            if row['server_id']!=server_id:
                self.reminders.finish(row['key'],'needs_review');continue
            try:
                receipt=await client.send(row['send_id']) if row['send_id'] else await client.send_by_key(row['key'])
            except Exception:
                # A lost POST acknowledgement is never an instruction to POST again.
                self.reminders.finish(row['key'],'needs_review' if self.clock()-row['at']>GRACE else 'submitting');continue
            if not self.valid(receipt,client,group) or row['send_id'] and receipt['id']!=row['send_id']:
                self.reminders.finish(row['key'],'needs_review');continue
            state=receipt.get('state')
            self.reminders.finish(row['key'],'accepted' if state=='accepted' else 'submitting' if state in {'queued','submitting'} and self.clock()-row['at']<=GRACE else 'needs_review',receipt['id'])

    async def mention(self,client,group,target,bot_id):
        if not target:return None
        try:
            cursor=None;matches=[]
            for _ in range(5):
                params={'limit':100}
                if cursor:params['cursor']=cursor
                result=await client._request('GET',client.prefix+'/groups/'+quote(group,safe='')+'/members',params=params)
                for member in result.get('items',[]):
                    identifier=member.get('username') or member.get('id')
                    if isinstance(identifier,str) and digest(group,identifier)==target:matches.append(identifier)
                cursor=result.get('nextCursor')
                if not cursor:break
            if cursor or len(set(matches))!=1:return None
            identifier=matches[0]
            await client.prepare_group_mentions(group,[bot_id,identifier])
            return {'userIds':[identifier]}
        except Exception:return None

    async def tick(self,adapter,config):
        self.reminders.purge()
        if not config.get('enabled') or config.get('reminders_enabled') is not True:return 'disabled'
        if adapter is None or getattr(adapter,'stopped',True):return 'waiting_connection'
        metadata=adapter.meta()
        if metadata.id!=config.get('bot_instance_id') or metadata.name!='wechat_native':return 'wrong_platform'
        client=adapter.client
        if client.closed:return 'waiting_connection'
        group=config.get('group_id','')
        if not group.endswith('@chatroom'):return 'wrong_group'
        async with asyncio.timeout(15):
            status=await client.status();account=await client.account()
        if isinstance(account.get('account'),dict):account=account['account']
        if account.get('authenticated') is not True or account.get('connection')!='online':return 'waiting_connection'
        server_id=status.get('serverId')
        adapter.state.bind_server(server_id)
        await self.reconcile(client,group,server_id)
        for _ in range(3):
            self.reminders.purge()
            item=self.reminders.claim_due(digest(group),server_id)
            if not item:break
            if item.get('skipped'):continue
            spec=item['spec'];label=spec['label']
            header='【群提醒 '+item['id']+'】'
            if self.clock()-item['at']>=60:header+='（补发，原定'+stamp(item['at'])+'）'
            text=header+'\n'+(label+'，' if label not in {'本人','群内'} else '')+spec['text']
            text=mask_labels(text,config.get('hidden_label_rules',[]))
            if unsafe(text):self.reminders.finish(item['key'],'needs_review');continue
            mentions=await self.mention(client,group,spec.get('target'),adapter.bot_self_id)
            if not self.reminders.current(item):self.reminders.finish(item['key'],'skipped');continue
            try:
                # Native checks the fixed account/destination and polls the same receipt.
                receipt=await client.send_text(group,text,item['key'],mentions)
                if not self.valid(receipt,client,group):raise ValueError('wrong_receipt')
                state='accepted' if receipt.get('state')=='accepted' else 'submitting'
                self.reminders.finish(item['key'],state,receipt['id'])
            except Exception:
                self.reminders.finish(item['key'],'submitting')
        return 'ready'
