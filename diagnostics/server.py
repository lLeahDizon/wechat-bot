"""Unix-only query service; no raw host interface is exposed to the bot."""
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import threading
import time

from .protocol import MAX_RESPONSE, Rejected, evidence, public_result, validate
from .sources import ROOT, Sources

SOCKET=Path('/run/sub2api-wechat-query/query.sock')


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5)
        try:
            if self.server.check_peer:
                _,uid,_=struct.unpack('3i',self.connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                if uid not in self.server.allowed_uids:return
            raw=self.rfile.readline(4098)
            if len(raw)>4096 or not raw.endswith(b'\n'):return
            request=validate(json.loads(raw))
            try:
                data=self.server.sources.execute(request)
                value=evidence(request['op'],data)
                if not public_result(value):raise ValueError('nonpublic_source_result')
            except Exception:
                value=evidence(request['op'],{'reason':'source_unavailable'},'unavailable')
            encoded=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()+b'\n'
            if len(encoded)<=MAX_RESPONSE:self.wfile.write(encoded)
        except (OSError,ValueError,TypeError,Rejected):return


class QueryServer(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=True
    request_queue_size=8

    def __init__(self,path,sources,allowed_uids=None,check_peer=True):
        self.sources=sources
        self.allowed_uids={0,10002} if allowed_uids is None else allowed_uids
        self.check_peer=check_peer
        self.slots=threading.BoundedSemaphore(2)
        super().__init__(str(path),Handler)

    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request);return
        try:super().process_request(request,address)
        except Exception:self.slots.release();raise

    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()


def monitor_loop(sources):
    from .monitor import Monitor
    from .notify import NativeNotifier
    from .store import Store
    store=Store(ROOT/'diagnostics/monitor.sqlite')
    monitor=Monitor(store)
    notifier=NativeNotifier(ROOT)
    while True:
        start=time.monotonic()
        try:
            policy=json.loads((ROOT/'config/policy.json').read_text())
            if policy.get('monitor_enabled'):
                sample={'health':sources.health(),'resources':sources.resources(),
                        'services':sources.services(),'traffic':sources.traffic(5),'wechat_online':notifier.online()}
                monitor.observe(sample)
                notifier.deliver(store)
            else:store.heartbeat('disabled')
        except Exception:
            try:store.heartbeat('sample_unavailable')
            except Exception:pass
        time.sleep(max(1,60-(time.monotonic()-start)))


def main():
    os.umask(0o077)
    if json.loads((ROOT/'task.json').read_text())['task']!='sub2api-wechat-group-bot-trial-20261103':
        raise RuntimeError('wrong_task')
    SOCKET.parent.mkdir(mode=0o750,parents=True,exist_ok=True)
    SOCKET.parent.chmod(0o750)
    os.chown(SOCKET.parent,0,10002)
    if SOCKET.is_symlink():raise RuntimeError('socket_symlink')
    if SOCKET.exists():
        if not SOCKET.is_socket():raise RuntimeError('socket_path_collision')
        SOCKET.unlink()
    sources=Sources()
    with QueryServer(SOCKET,sources) as server:
        SOCKET.chmod(0o660)
        os.chown(SOCKET,0,10002)
        thread=threading.Thread(target=monitor_loop,args=(sources,),daemon=True);thread.start()
        server.serve_forever(poll_interval=.5)


if __name__=='__main__':main()
