"""Operator-only, one-key bootstrap. Run on the verified host via private stdin.

The deployed v0.2.19 schema/service/repository were checked before this bounded
transaction. Public routing intentionally exposes no account-management API.
Never mount this script, owner credentials, or its evidence directory in AstrBot.
"""
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path('/opt/sub2api-wechat-bot')
TASK = 'sub2api-wechat-group-bot-trial-20261103'
NAME = 'wechat-sub2api-bot-20261103'


def query(sql):
    proc = subprocess.run(['docker', 'exec', '-i', 'sub2api-postgres', 'psql',
        '-U', 'sub2api', '-d', 'sub2api', '-At', '-v', 'ON_ERROR_STOP=1'],
        input=sql, capture_output=True, text=True, timeout=20)
    if proc.returncode:
        raise RuntimeError('database_operation_failed')
    return [json.loads(line) for line in proc.stdout.splitlines() if line.startswith('{')]


def main():
    os.umask(0o077)
    owner_key = json.loads(sys.stdin.readline())
    if not isinstance(owner_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,200}', owner_key):
        raise RuntimeError('invalid_owner_key')
    owner = query("BEGIN READ ONLY; SELECT json_build_object('user_id',k.user_id,'group_id',k.group_id) "
        "FROM api_keys k JOIN users u ON u.id=k.user_id JOIN groups g ON g.id=k.group_id "
        "WHERE k.key='" + owner_key + "' AND k.deleted_at IS NULL AND k.status='active' "
        "AND u.status='active' AND u.deleted_at IS NULL AND g.status='active' AND g.platform='openai'; COMMIT;")
    if len(owner) != 1:
        raise RuntimeError('owner_not_verified')
    user, group = int(owner[0]['user_id']), int(owner[0]['group_id'])
    marker = ROOT / 'task.json'
    if ROOT.exists():
        if not marker.is_file() or json.loads(marker.read_text()).get('task') != TASK:
            raise RuntimeError('deployment_directory_collision')
    else:
        ROOT.mkdir(mode=0o700)
        marker.write_text(json.dumps({'task': TASK, 'created_at': time.time()}) + '\n')
    for name in ['secrets', 'admin-evidence']:
        (ROOT / name).mkdir(mode=0o700, exist_ok=True)
    existing = query(f"BEGIN READ ONLY; SELECT json_build_object('id',id,'key',key,'group_id',group_id,"
        f"'quota',quota,'rate_limit_1d',rate_limit_1d) FROM api_keys WHERE user_id={user} "
        f"AND name='{NAME}' AND deleted_at IS NULL; COMMIT;")
    if len(existing) > 1:
        raise RuntimeError('duplicate_task_keys')
    if existing:
        record = existing[0]
        if record['group_id'] != group or float(record['quota']) != 0 or float(record['rate_limit_1d']) != 0:
            raise RuntimeError('existing_key_policy_mismatch')
        key, key_id, created = record['key'], int(record['id']), False
    else:
        key = 'sk-' + secrets.token_hex(32)
        result = query(f"BEGIN; SELECT pg_advisory_xact_lock(20261103); "
            f"INSERT INTO api_keys(user_id,key,name,group_id,status,quota,quota_used,rate_limit_5h,rate_limit_1d,rate_limit_7d) "
            f"SELECT {user},'{key}','{NAME}',{group},'active',0,0,0,0,0 WHERE NOT EXISTS "
            f"(SELECT 1 FROM api_keys WHERE user_id={user} AND name='{NAME}' AND deleted_at IS NULL) "
            "RETURNING json_build_object('id',id); COMMIT;")
        if len(result) != 1:
            raise RuntimeError('key_creation_race')
        key_id, created = int(result[0]['id']), True
    destination = ROOT / 'secrets/sub2api-key'
    if destination.exists() and destination.read_text().strip() != key:
        raise RuntimeError('secret_file_collision')
    destination.write_text(key + '\n')
    destination.chmod(0o400)
    evidence = {'task': TASK, 'key_id': key_id, 'group_id': group, 'created': created,
                'quota': 0, 'reasoning_effort': 'medium', 'model': 'gpt-6.1-sol',
                'provision_method': 'verified_transactional_insert', 'captured_at': time.time()}
    request = urllib.request.Request('https://api.apiroute.asia/v1/models',
        headers={'Authorization': 'Bearer ' + key})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as response:
            models = json.load(response)
        evidence['models_authenticated'] = True
        evidence['sol_listed'] = any(item.get('id') == 'gpt-6.1-sol' for item in models.get('data', []))
    except urllib.error.HTTPError as error:
        evidence['models_http'] = error.code
    # Paid compatibility probe is deliberately one request, no retries.
    body = {'model': 'gpt-6.1-sol', 'reasoning': {'effort': 'medium'}, 'store': False,
            'stream': False, 'tools': [], 'max_output_tokens': 1024,
            'instructions': 'Reply with exactly READY.',
            'input': [{'role': 'user', 'content': 'Connectivity check.'}]}
    request = urllib.request.Request('https://api.apiroute.asia/v1/responses',
        data=json.dumps(body).encode(), method='POST',
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    probe_path = ROOT / 'admin-evidence/model-probe-20261103.json'
    if probe_path.exists():
        evidence['probe'] = json.loads(probe_path.read_text()).get('probe', {})
        evidence['probe_reused'] = True
    else:
        try:
            with opener.open(request, timeout=120) as response:
                payload = json.load(response)
            answer = ''.join(part.get('text', '') for item in payload.get('output', [])
                if item.get('type') == 'message' for part in item.get('content', [])
                if part.get('type') == 'output_text')
            evidence['probe'] = {'http': 200, 'status': payload.get('status'),
                'model': payload.get('model'), 'ready': answer.strip() == 'READY',
                'usage': payload.get('usage', {})}
        except urllib.error.HTTPError as error:
            evidence['probe'] = {'http': error.code}
        except (OSError, ValueError):
            evidence['probe'] = {'error': 'transport_or_format'}
        probe_path.write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps(evidence))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Fixed error messages only; never dump SQL, response bodies or key data.
        print(json.dumps({'failed': True, 'category': str(error) if str(error) in {
            'invalid_owner_key', 'owner_not_verified', 'deployment_directory_collision',
            'duplicate_task_keys', 'existing_key_policy_mismatch', 'key_creation_race',
            'secret_file_collision', 'database_operation_failed'} else 'bootstrap_failed'}))
        sys.exit(1)
