from __future__ import annotations

import json
from pathlib import Path
import socket
import time

from .protocol import MAX_RESPONSE, evidence, validate

SOCKET=Path('/run/bot-query/query.sock')


class QueryBudget:
    def __init__(self,path=SOCKET,clock=time.monotonic):
        self.path,self.clock=path,clock
        self.spent=0.0
        self.count=0
        self.results=[]

    def query(self,request):
        request=validate(request)
        if self.count>=5 or self.spent>=20:
            return evidence(request['op'],{'reason':'query_budget_exhausted'},'unavailable')
        self.count+=1
        started=self.clock()
        operation_deadline=started+min(5,20-self.spent)
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                connection.settimeout(max(.01,min(5,20-self.spent)))
                connection.connect(str(self.path))
                connection.sendall(json.dumps(request,separators=(',',':')).encode()+b'\n')
                raw=bytearray()
                while b'\n' not in raw:
                    if self.clock()>=operation_deadline:raise TimeoutError('query_timeout')
                    connection.settimeout(max(.01,operation_deadline-self.clock()))
                    chunk=connection.recv(min(4096,MAX_RESPONSE+1-len(raw)))
                    if not chunk:raise ValueError('empty_response')
                    raw.extend(chunk)
                    if len(raw)>MAX_RESPONSE:raise ValueError('oversize_response')
                value=json.loads(raw.partition(b'\n')[0])
                if (not isinstance(value,dict) or value.get('op')!=request['op']
                    or value.get('state') not in {'ok','partial','unavailable'}
                    or not isinstance(value.get('id'),str) or not value['id'].startswith('diag-')
                    or not isinstance(value.get('captured_at'),(int,float))):
                    raise ValueError('invalid_response')
                from .protocol import public_result
                if not public_result(value) or not time.time()-120<=value['captured_at']<=time.time()+10:
                    raise ValueError('unsafe_or_stale_response')
        except (OSError,ValueError,TypeError):
            value=evidence(request['op'],{'reason':'query_unavailable'},'unavailable')
        finally:self.spent+=max(0,self.clock()-started)
        self.results.append(value)
        return value
