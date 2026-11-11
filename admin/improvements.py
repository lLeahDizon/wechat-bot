"""Operator-only review/rollback of this bot's own anonymous improvements."""
import argparse
import json
from pathlib import Path
import re
import subprocess

ROOT=Path('/opt/sub2api-wechat-bot')
PROGRAM="""
import json,sqlite3,sys,time,pathlib
from guard.improvements import listing,rollback,initialize,candidate,save,operator_inspect,operator_adopt
args=json.loads(sys.stdin.read())
db=sqlite3.connect('/AstrBot/data/sub2api_guard/state.sqlite')
facts=json.loads(pathlib.Path('/run/bot-config/faq.json').read_text());initialize(db,facts,time.time())
if args['action']=='list':value={'summary':listing(db)}
elif args['action']=='inspect':value=operator_inspect(db,args['id'],facts)
elif args['action']=='adopt':value={'adopted':operator_adopt(db,args['id'],facts,args['expected_sha'])}
elif args['action']=='revert':
 review=operator_inspect(db,args['id'],facts)
 if args.get('expected_sha') and (not review or review['revision']!=args['expected_sha'] or not review['can_revert']):raise RuntimeError('record_changed')
 value={'reverted':rollback(db,args['id'],facts)}
else:
 if not db.execute('SELECT 1 FROM improvements WHERE id=?',(args['id'],)).fetchone():raise RuntimeError('suggestion_not_found')
 proposal=candidate({'kind':'reply_style','style':json.loads(args['style_json'])},facts)
 if not proposal:raise RuntimeError('invalid_style')
 result=save(db,proposal,True,'operator','operator:'+args['id'],facts,time.time(),'verified_service',explicit_id=args['id'],operator=True)
 value={'id':result[0],'status':result[1],'summary':result[2],'reason':result[3]}
db.close()
print(json.dumps(value,ensure_ascii=False))
"""


def main():
    if json.loads((ROOT/'task.json').read_text())['task']!='sub2api-wechat-group-bot-trial-20261103':raise RuntimeError('wrong_task')
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['list','inspect','adopt','revert','apply-style']);parser.add_argument('id',nargs='?');parser.add_argument('--style-json');parser.add_argument('--expected-sha')
    args=parser.parse_args()
    if args.action!='list' and (not args.id or not re.fullmatch(r'YJ-[a-f0-9]{16}',args.id)):raise RuntimeError('invalid_suggestion_id')
    if args.action=='apply-style' and not args.style_json:raise RuntimeError('style_required')
    if args.expected_sha and not re.fullmatch(r'[a-f0-9]{64}',args.expected_sha):raise RuntimeError('invalid_revision')
    if args.action=='adopt' and not args.expected_sha:raise RuntimeError('revision_required')
    p=subprocess.run(['docker','exec','-i','--user','10002:10002','sub2api-wechat-astrbot','python','-c',PROGRAM],
        input=json.dumps(vars(args)),capture_output=True,text=True,timeout=10)
    if p.returncode:raise RuntimeError('improvement_operation_failed')
    print(json.dumps(json.loads(p.stdout),ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except Exception:
        print(json.dumps({'failed':True,'category':'improvement_operation_requires_review'}));raise SystemExit(1)
