import json
import socket

SOCKET='/run/bot-admin/admin.sock'


class AdminClient:
    def request(self,value):
        body=json.dumps(value,separators=(',',':')).encode()+b'\n'
        if len(body)>2048:raise ValueError('admin_request_too_large')
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as stream:
            stream.settimeout(8);stream.connect(SOCKET);stream.sendall(body)
            with stream.makefile('rb') as source:raw=source.readline(4097)
        if len(raw)>4096 or not raw.endswith(b'\n'):raise ValueError('invalid_admin_response')
        result=json.loads(raw)
        if not isinstance(result,dict) or not isinstance(result.get('reply'),str):raise ValueError('invalid_admin_response')
        return result
