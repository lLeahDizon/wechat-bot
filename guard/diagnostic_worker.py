"""Short-lived diagnostic worker; no shell or model-supplied executable input."""
import json
from pathlib import Path
import sys

from .controller import diagnose


def main():
    raw=sys.stdin.read(128*1024+1)
    if len(raw)>128*1024:raise ValueError('input_limit')
    request=json.loads(raw)
    if (not isinstance(request,dict) or set(request)!={'data','key_file','api_base'}
        or not isinstance(request['data'],dict) or request['api_base']!='https://api.apiroute.asia/v1'):
        raise ValueError('invalid_worker_request')
    result,usages,evidence=diagnose(request['data'],Path(request['key_file']),request['api_base'])
    sys.stdout.write(json.dumps({'result':result,'usages':usages,'evidence':evidence},ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,TypeError,KeyError):
        sys.stdout.write(json.dumps({'result':{'failure':'diagnostic_worker_failed'},'usages':[],'evidence':[]}))
