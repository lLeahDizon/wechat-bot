"""Owner-only fixed actions with durable confirmation and no shell interface."""
import datetime
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import sqlite3
import struct
import subprocess
import sys
import threading
import time

from .protocol import validate

ROOT=Path('/opt/sub2api-wechat-bot')
SOCKET=Path('/run/sub2api-wechat-admin/admin.sock')
LABELS={'restart_sub2api':'重启Sub2API','restart_astrbot':'重启AstrBot答疑组件',
        'adopt_improvement':'采纳建议','revert_improvement':'撤销建议'}
STATES={'pending':'等待本人确认','confirmed':'已确认，等待答疑回执后提交','queued':'已受理',
        'running':'正在执行','done':'已完成并核验','failed':'检查不通过，未执行',
        'needs_review':'结果待人工核查，不会自动重复执行','expired':'已过期，未执行'}


class Executor:
    def __init__(self,root=ROOT):self.root=Path(root)

    def run(self,args,timeout=12):
        p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
        if p.returncode:raise RuntimeError('fixed_command_failed')
        return p.stdout

    def helper(self,action,identifier,revision=None):
        args=[sys.executable,str(self.root/'code/admin/improvements.py'),action,identifier]
        if revision:args+=['--expected-sha',revision]
        return json.loads(self.run(args,18))

    def preview(self,action,argument):
        if (self.root/'admin-evidence/space-paused.json').exists():raise ValueError('storage_protection_active')
        if action in {'restart_sub2api','restart_astrbot'}:
            name='sub2api' if action=='restart_sub2api' else 'sub2api-wechat-astrbot'
            identity=json.loads(self.run(['docker','inspect',name,'--format','{"id":{{json .Id}},"started":{{json .State.StartedAt}}}']))
            if len(identity['id'])!=64:raise ValueError('container_missing')
            revision=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
            return revision,('会短暂中断模型请求，正在执行的请求可能失败。' if action=='restart_sub2api' else '会短暂中断答疑；微信组件保持运行，完成结果由独立组件发送。')
        value=self.helper('inspect',argument)
        allowed=value and value['can_adopt' if action=='adopt_improvement' else 'can_revert']
        if not allowed:raise ValueError('suggestion_not_eligible')
        from guard.core import unsafe
        if unsafe(value['summary']):raise ValueError('nonpublic_summary')
        return value['revision'],'仅改变机器人自己的已校验建议规则：'+value['summary']+'。'

    def execute(self,job):
        revision,_=self.preview(job['action'],job['argument'])
        if revision!=job['revision']:return 'failed'
        if job['action'] in {'restart_sub2api','restart_astrbot'}:
            name='sub2api' if job['action']=='restart_sub2api' else 'sub2api-wechat-astrbot'
            previous_start=self.run(['docker','inspect',name,'--format','{{.State.StartedAt}}']).strip()
            self.run(['docker','restart','--time','30',name],65)
            deadline=time.monotonic()+75
            while time.monotonic()<deadline:
                state=json.loads(self.run(['docker','inspect',name,'--format','{{json .State}}']))
                started=datetime.datetime.fromisoformat(state['StartedAt'].replace('Z','+00:00')).timestamp()
                health=state.get('Health',{});checks=health.get('Log',[])
                ready=state['Running'] and state['StartedAt']!=previous_start and health.get('Status')=='healthy' and bool(checks)
                if ready:ready=checks[-1].get('ExitCode')==0 and datetime.datetime.fromisoformat(checks[-1]['End'].replace('Z','+00:00')).timestamp()>=started
                if name=='sub2api-wechat-astrbot':
                    try:
                        h=json.loads((self.root/'astrbot-data/sub2api_guard/heartbeat.json').read_text())
                        ready=ready and h.get('guard')=='ready' and h.get('at',0)>=started
                    except (OSError,ValueError):ready=False
                if ready:return 'done'
                time.sleep(2)
            return 'needs_review'
        action='adopt' if job['action']=='adopt_improvement' else 'revert'
        result=self.helper(action,job['argument'],job['revision'])
        return 'done' if result.get('adopted' if action=='adopt' else 'reverted') is True else 'failed'


class Operations:
    def __init__(self,path,policy,executor,clock=time.time):
        self.path=Path(path);self.policy=policy;self.executor=executor;self.clock=clock
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.db() as d:d.executescript('''CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY,request_id TEXT UNIQUE,confirm_request TEXT UNIQUE,actor TEXT,grp TEXT,
          action TEXT,argument TEXT,revision TEXT,code TEXT UNIQUE,expires REAL,state TEXT,at REAL,
          effect TEXT,outboxed INTEGER DEFAULT 0);''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        d=sqlite3.connect(self.path,timeout=5);d.row_factory=sqlite3.Row
        try:
            with d:yield d
        finally:d.close()

    def recover(self):
        with self.db() as d:d.execute("UPDATE jobs SET state='needs_review' WHERE state='running'")

    def status(self,row):return {'reply':row['id']+' '+LABELS[row['action']]+'：'+STATES[row['state']]+'。'}

    def proposal(self,row):
        if row['state']!='pending':return self.status(row)
        return {'reply':row['id']+' 拟执行：'+LABELS[row['action']]+(' '+row['argument'] if row['argument'] else '')+'。\n'+row['effect']+'\n请本人在2分钟内真正@回复：确认 '+row['code']+'。未确认不执行。'}

    def dispatch(self,raw):
        v=validate(raw);p=self.policy();now=self.clock()
        if (p.get('server_readonly') or p.get('owner_operations_enabled') is not True or v['actor']!=p.get('owner_subject_hash')
                or v['group']!=hashlib.sha256(p.get('group_id','').encode()).hexdigest()):
            return {'reply':'无此管理操作权限。'}
        with self.db() as d:
            d.execute('BEGIN IMMEDIATE')
            d.execute("UPDATE jobs SET state='expired' WHERE state IN ('pending','confirmed') AND expires<?",(now,))
            if v['op']=='prepare':
                row=d.execute('SELECT * FROM jobs WHERE request_id=?',(v['request_id'],)).fetchone()
                if row:return self.proposal(row)
                busy=d.execute("SELECT * FROM jobs WHERE state IN ('pending','confirmed','queued','running') LIMIT 1").fetchone()
                if busy:return {'reply':'已有管理操作未结束，请先处理或等待原确认过期。'+self.status(busy)['reply']}
                try:revision,effect=self.executor.preview(v['action'],v.get('argument',''))
                except Exception:return {'reply':'该动作当前无法安全执行：建议可能已生效、未通过校验、来源失效或目标不可用。未发起变更。'}
                from guard.core import mask_labels,normalize
                for _ in range(64):
                    identifier='GL-'+secrets.token_hex(8);code=secrets.token_hex(4).upper()
                    if mask_labels(identifier+' '+code,p.get('hidden_label_rules',[]))==normalize(identifier+' '+code):break
                else:return {'reply':'无法生成可公开确认码，本次未发起变更。'}
                d.execute('INSERT INTO jobs(id,request_id,actor,grp,action,argument,revision,code,expires,state,at,effect) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (identifier,v['request_id'],v['actor'],v['group'],v['action'],v.get('argument',''),revision,code,now+120,'pending',now,effect))
                return self.proposal(d.execute('SELECT * FROM jobs WHERE id=?',(identifier,)).fetchone())
            if v['op']=='confirm':
                row=d.execute('SELECT * FROM jobs WHERE confirm_request=? OR code=?',(v['request_id'],v['code'])).fetchone()
                if not row or row['actor']!=v['actor'] or row['state']=='expired':return {'reply':'确认码无效或已过期，未执行；请重新发起管理指令。'}
                if row['state'] in {'pending','confirmed'}:
                    d.execute("UPDATE jobs SET state='confirmed',confirm_request=? WHERE id=?",(v['request_id'],row['id']))
                    return {'reply':row['id']+' 已确认，答疑回执送达后提交；同一操作只执行一次。','activate_job':row['id']}
                return self.status(row)
            row=d.execute('SELECT * FROM jobs WHERE id=? AND actor=?',(v['job_id'],v['actor'])).fetchone()
            if not row:return {'reply':'未找到本人的该管理操作。'}
            if v['op']=='activate' and row['state']=='confirmed' and row['confirm_request']==v['request_id']:
                d.execute("UPDATE jobs SET state='queued' WHERE id=?",(row['id'],))
                return {'reply':row['id']+' 已受理。'}
            return self.status(row)

    def process_one(self,outbox):
        if self.policy().get('server_readonly'):return
        with self.db() as d:
            d.execute('BEGIN IMMEDIATE')
            row=d.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY at LIMIT 1").fetchone()
            if row:d.execute("UPDATE jobs SET state='running' WHERE id=?",(row['id'],))
        if row:
            try:
                p=self.policy()
                authorized=(p.get('owner_operations_enabled') is True and p.get('owner_subject_hash')==row['actor']
                    and hashlib.sha256(p.get('group_id','').encode()).hexdigest()==row['grp'])
                state=self.executor.execute(dict(row)) if authorized else 'failed'
            except Exception:state='needs_review'
            if state not in {'done','failed','needs_review'}:state='needs_review'
            with self.db() as d:d.execute('UPDATE jobs SET state=? WHERE id=?',(state,row['id']))
        with self.db() as d:
            for item in d.execute("SELECT * FROM jobs WHERE state IN ('done','failed','needs_review') AND outboxed=0").fetchall():
                text='管理结果：'+self.status(item)['reply']
                outbox.enqueue('admin-'+item['id'],'result','owner_operation',text)
                d.execute('UPDATE jobs SET outboxed=1 WHERE id=?',(item['id'],))
            d.execute("DELETE FROM jobs WHERE at<? AND state NOT IN ('queued','running')",(self.clock()-7*86400,))


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(8)
        try:
            if self.server.check_peer:
                _,uid,_=struct.unpack('3i',self.connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                if uid not in self.server.allowed_uids:return
            raw=self.rfile.readline(2049)
            if len(raw)>2048 or not raw.endswith(b'\n'):return
            response=self.server.operations.dispatch(json.loads(raw))
            from guard.core import unsafe
            if unsafe(response['reply']):response={'reply':'管理结果未通过公开信息检查，请联系本会话核查。'}
            body=json.dumps(response,ensure_ascii=False).encode()+b'\n'
            if len(body)<=4096:self.wfile.write(body)
        except Exception:return


class AdminServer(socketserver.UnixStreamServer):
    def __init__(self,path,operations,check_peer=True,allowed_uids=None):
        self.operations=operations;self.check_peer=check_peer
        self.allowed_uids={0,10002} if allowed_uids is None else allowed_uids
        super().__init__(str(path),Handler)


def worker(operations):
    from diagnostics.store import Store
    from diagnostics.notify import NativeNotifier
    store=Store(ROOT/'diagnostics/operations-notifications.sqlite');notifier=NativeNotifier(ROOT)
    next_delivery=0
    while True:
        try:
            operations.process_one(store)
            if store.pending() and time.monotonic()>=next_delivery:
                notifier.deliver(store,validation=True);next_delivery=time.monotonic()+10
            store.prune()
        except Exception:pass
        time.sleep(1)


def main():
    import fcntl
    os.umask(0o077)
    if json.loads((ROOT/'task.json').read_text())['task']!='sub2api-wechat-group-bot-trial-20261103':raise RuntimeError('wrong_task')
    lock=(ROOT/'diagnostics/operations.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    policy=lambda:json.loads((ROOT/'config/policy.json').read_text())
    operations=Operations(ROOT/'diagnostics/operations.sqlite',policy,Executor());operations.recover()
    SOCKET.parent.mkdir(mode=0o750,parents=True,exist_ok=True);SOCKET.parent.chmod(0o750);os.chown(SOCKET.parent,0,10002)
    if SOCKET.is_symlink() or (SOCKET.exists() and not SOCKET.is_socket()):raise RuntimeError('socket_collision')
    if SOCKET.exists():SOCKET.unlink()
    with AdminServer(SOCKET,operations) as server:
        SOCKET.chmod(0o660);os.chown(SOCKET,0,10002)
        threading.Thread(target=worker,args=(operations,),daemon=True).start();server.serve_forever(poll_interval=.5)


if __name__=='__main__':main()
