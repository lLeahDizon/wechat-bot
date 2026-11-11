"""Rules only: no model calls, repairs, token refreshes or business writes."""
import datetime
import json
import uuid

LABELS={'gateway':'服务基础健康','database':'数据库运行状态','cache':'缓存运行状态','proxy':'网关运行状态',
        'responder':'答疑组件','wechat':'微信消息连接','cpu':'CPU占用','memory':'可用内存',
        'disk':'可用磁盘','bot_data':'机器人存储','http_5xx':'网关HTTP 5xx比例'}


def stamp(value):
    return datetime.datetime.fromtimestamp(value,datetime.timezone(datetime.timedelta(hours=8))).strftime('%m-%d %H:%M:%S')


class Monitor:
    def __init__(self,store):self.store=store

    def rule(self,code,bad,samples=1,duration=0,severity=1):
        now=self.store.clock();db=self.store.db
        row=db.execute('SELECT * FROM incidents WHERE code=?',(code,)).fetchone()
        state=dict(row) if row else dict(code=code,active=0,since=None,bad_count=0,clear_count=0,incident=None,severity=0,started=None)
        if bad is None:
            if not state['active']:state.update(since=None,bad_count=0)
            state['clear_count']=0
        elif bad:
            if state['since'] is None:state['since']=now
            state['bad_count']+=1;state['clear_count']=0
            if not state['active'] and state['bad_count']>=samples and now-state['since']>=duration:
                state.update(active=1,incident=uuid.uuid4().hex,severity=severity,started=state['since'])
                self.store.enqueue(state['incident'],'open',code,
                    f'【服务告警】{LABELS[code]}出现异常。首次观察：{stamp(state["started"])}。只读监测未执行任何修复；请管理员核查。'+
                    (' 此比例只涵盖已结束模型POST的HTTP 5xx，不含200后的流中断或WS内部失败。' if code=='http_5xx' else ''))
            elif state['active'] and severity>state['severity']:
                state['severity']=severity
                self.store.enqueue(state['incident'],'upgrade',code,
                    f'【告警升级】{LABELS[code]}已达到更严重阈值。检查时间：{stamp(now)}。既有空间保护仍独立生效。')
        else:
            state.update(since=None,bad_count=0)
            state['clear_count']+=1
            if state['active'] and state['clear_count']>=2:
                opened=db.execute("SELECT state FROM notifications WHERE incident=? AND phase='open'",(state['incident'],)).fetchone()
                # A recovery makes unsent fault/upgrade notices obsolete. Keep
                # submitting/unknown receipts for lookup without resubmission.
                with db:db.execute("UPDATE notifications SET state='superseded' WHERE incident=? AND state='pending' AND phase IN ('open','upgrade')",(state['incident'],))
                if opened and opened[0]=='pending':
                    phase='summary';text=f'【恢复后补报】{LABELS[code]}曾在{stamp(state["started"])}异常，目前已连续两次检查恢复；期间告警未能送达。恢复确认：{stamp(now)}。'
                else:
                    phase='recovery';text=f'【服务恢复】{LABELS[code]}已连续两次检查恢复。检查时间：{stamp(now)}。'
                self.store.enqueue(state['incident'],phase,code,text)
                state['active']=0
        with db:db.execute('INSERT OR REPLACE INTO incidents VALUES (:code,:active,:since,:bad_count,:clear_count,:incident,:severity,:started)',state)

    def observe(self,sample):
        previous=self.store.meta('last_sample')
        if previous and self.store.clock()-previous['at']>90:
            # Missing samples cannot establish a continuous threshold duration.
            with self.store.db:
                self.store.db.execute('UPDATE incidents SET since=NULL,bad_count=0,clear_count=0 WHERE active=0')
                self.store.db.execute('UPDATE incidents SET clear_count=0 WHERE active=1')
        health=sample.get('health',{}).get('service')
        self.rule('gateway',health!='healthy' if health in {'healthy','degraded','unavailable'} else None,samples=2)
        for code in ['database','cache','proxy','responder']:
            value=sample.get('services',{}).get(code,{})
            bad=(not value['running'] or value.get('health')=='unhealthy') if value.get('observed',False) else None
            self.rule(code,bad,samples=2)
        online=sample.get('wechat_online')
        self.rule('wechat',not online if isinstance(online,bool) else None,samples=2)
        resources=sample.get('resources',{})
        def below(name,limit):
            value=resources.get(name);return value<limit if isinstance(value,(int,float)) else None
        cpu=resources.get('cpu_percent')
        self.rule('cpu',cpu>90 if isinstance(cpu,(int,float)) else None,duration=300)
        self.rule('memory',below('memory_available_percent',15),duration=300)
        disk=resources.get('disk_available_percent');size=resources.get('bot_data_gib')
        self.rule('disk',below('disk_available_percent',25),severity=2 if isinstance(disk,(int,float)) and disk<20 else 1)
        self.rule('bot_data',size>=1 if isinstance(size,(int,float)) else None,severity=2 if isinstance(size,(int,float)) and size>=2 else 1)
        metric=sample.get('traffic',{}).get('gateway',{})
        rate=metric.get('http_5xx_rate');valid=metric.get('coverage_complete') and metric.get('observed_completed_requests',0)>=10 and isinstance(rate,(int,float))
        self.rule('http_5xx',rate>=.2 if valid else None,samples=2)
        now=self.store.clock()
        if previous and now-previous['at']>=180:
            self.store.enqueue(uuid.uuid4().hex,'summary','gateway',
                f'【监测恢复】本地监测曾中断约{int((now-previous["at"])/60)}分钟，现已恢复采样；中断期间服务状况没有连续证据，不能据此判断整机宕机。')
        critical=(isinstance(disk,(int,float)) and disk<20) or (isinstance(size,(int,float)) and size>=2)
        if not critical:
            with self.store.db:self.store.db.execute('INSERT OR REPLACE INTO samples VALUES (?,?)',(now,json.dumps(sample)))
        self.store.meta('last_sample',{'at':now});self.store.heartbeat('ready');self.store.prune()
