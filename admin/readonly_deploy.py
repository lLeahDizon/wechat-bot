"""Operator-only staged rollout. Never called by group messages or the model."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time

ROOT=Path('/opt/sub2api-wechat-bot')


def write(path,value,mode=0o600,uid=0):
    if path.is_symlink():raise RuntimeError('destination_symlink')
    temp=path.with_suffix(path.suffix+'.tmp')
    if temp.exists():raise RuntimeError('unfinished_atomic_write')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');temp.chmod(mode);os.chown(temp,uid,uid);temp.replace(path)


def run(args,timeout=20):
    p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise RuntimeError('deployment_command_failed')
    return p.stdout


def module(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'code/admin'/f'{name}.py')
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


def backup_code():
    path=ROOT/'diagnostics/rollback-code.tar.gz'
    if path.exists():return
    with tarfile.open(path,'x:gz') as archive:
        for name in ['compose.json','config','plugins']:
            source=ROOT/name
            if source.exists():archive.add(source,arcname=name,recursive=True)
    path.chmod(0o600)


def copy_public_files():
    for name in ['__init__.py','protocol.py','client.py']:
        target=ROOT/'query-client/diagnostics'/name
        shutil.copyfile(ROOT/'code/diagnostics'/name,target);target.chmod(0o444)
    target=ROOT/'config/faq.json'
    shutil.copyfile(ROOT/'code/knowledge/faq.json',target);target.chmod(0o444)


def bind_members(policy):
    native=module('native_control');account=native.account_id();group=policy['group_id']
    data=native.cli(['groups','members','list',group,'--limit','100'],account)
    if data.get('nextCursor'):raise RuntimeError('member_catalog_incomplete')
    members=data['items'];receipt=ROOT/'admin-evidence/member-bindings.json'
    if receipt.exists():
        bindings=json.loads(receipt.read_text())['bindings']
        if any(not any((x.get('username') or x.get('id'))==identity for x in members) for identity in bindings.values()):
            raise RuntimeError('recorded_member_not_present')
    else:
        bindings={}
        for subject,label in [('flower','example_person_a'),('gentle','example_person_b')]:
            matched=[x for x in members if label in [x.get('displayName'),x.get('roomDisplayName'),x.get('mentionName')]]
            if len(matched)!=1:raise RuntimeError('member_binding_not_unique')
            identity=matched[0].get('username') or matched[0].get('id')
            if not isinstance(identity,str) or not identity:raise RuntimeError('member_identity_unavailable')
            bindings[subject]=identity
        if len(set(bindings.values()))!=2:raise RuntimeError('member_binding_collision')
        write(receipt,{'verified_at':time.time(),'group':group,'bindings':bindings})
    profiles=policy.setdefault('member_profiles',{})
    for subject,identity in bindings.items():
        digest=hashlib.sha256((group+'\0'+identity).encode()).hexdigest()
        profiles[digest]={'tone':'sharp' if subject=='flower' else 'gentle','usage_target':subject}


def prepare():
    for name,mode in [('diagnostics',0o700),('query-client',0o755),('query-client/diagnostics',0o755)]:
        path=ROOT/name
        if path.is_symlink():raise RuntimeError('directory_symlink')
        path.mkdir(parents=True,exist_ok=True);path.chmod(mode)
    backup_code()
    module('provision_diagnostics').main()
    copy_public_files()
    policy=json.loads((ROOT/'config/policy.json').read_text())
    source=json.loads((ROOT/'code/config/policy.example.json').read_text())
    bind_members(policy)
    policy.setdefault('diagnostics_enabled',False);policy.setdefault('monitor_enabled',False)
    policy['usage_public_aliases']=source['usage_public_aliases'];policy['help']=source['help']
    policy['hidden_label_rules']=[{'length':len(name),'digest':hashlib.sha256(name.casefold().encode()).hexdigest()} for name in ['aa','example_key_label']]
    write(ROOT/'config/policy.json',policy,0o444)
    target=Path('/etc/systemd/system/sub2api-wechat-query.service')
    shutil.copyfile(ROOT/'code/deploy/sub2api-wechat-query.service',target);target.chmod(0o644)
    run(['systemctl','daemon-reload']);run(['systemctl','enable','--now','sub2api-wechat-query.service'])
    print(json.dumps({'prepared':True,'monitor_enabled':policy['monitor_enabled'],'personas_bound':2,'public_ports_added':0}))


def install():
    if not (ROOT/'admin-evidence/query-db-setup.json').exists():raise RuntimeError('prepare_required')
    copy_public_files()
    policy=json.loads((ROOT/'config/policy.json').read_text())
    bind_members(policy)
    source=json.loads((ROOT/'code/config/policy.example.json').read_text())
    policy['usage_public_aliases']=source['usage_public_aliases'];policy['help']=source['help']
    policy.update(diagnostics_enabled=True,monitor_enabled=False,acceptance_prefix='诊断验收1103')
    write(ROOT/'config/policy.json',policy,0o444)
    plugin=ROOT/'plugins/astrbot_plugin_sub2api_guard'
    for source,target in [(ROOT/'code/plugin',plugin),(ROOT/'code/guard',plugin/'guard')]:
        for path in source.rglob('*'):
            if '__pycache__' in path.parts or path.suffix=='.pyc':continue
            dest=target/path.relative_to(source)
            if dest.is_symlink():raise RuntimeError('plugin_symlink')
            if path.is_dir():dest.mkdir(parents=True,exist_ok=True);dest.chmod(0o555)
            elif path.is_file():dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,dest);dest.chmod(0o444)
    compose_path=ROOT/'compose.json';compose=json.loads(compose_path.read_text());bot=compose['services']['astrbot']
    additions=[f'{ROOT}/query-client:/opt/query-client:ro','/run/sub2api-wechat-query:/run/bot-query:ro']
    for volume in additions:
        if volume not in bot['volumes']:bot['volumes'].append(volume)
    bot['environment']['PYTHONPATH']='/opt/bot-runtime:/opt/query-client:/opt/bot-code:/AstrBot'
    write(compose_path,compose)
    run(['docker','compose','-f',str(compose_path),'config','--quiet'])
    run(['systemctl','restart','sub2api-wechat-query.service'])
    run(['docker','compose','-f',str(compose_path),'up','-d','--no-deps','astrbot'],timeout=60)
    staged=json.loads((ROOT/'admin-evidence/staged-source-sha256.json').read_text())
    if not all(hashlib.sha256((ROOT/'code'/name).read_bytes()).hexdigest()==digest for name,digest in staged.items()):
        raise RuntimeError('source_manifest_changed')
    write(ROOT/'admin-evidence/deployed-source-sha256.json',staged)
    write(ROOT/'admin-evidence/readonly-rollout.json',{'stage':'controlled_acceptance','at':time.time(),'personas_bound':2,'usage_subjects':2})
    print(json.dumps({'controlled_acceptance':True,'marker':'诊断验收1103','monitor_enabled':False,'original_services_restarted':False}))


def enable():
    receipt=json.loads((ROOT/'admin-evidence/readonly-acceptance.json').read_text())
    if receipt.get('passed') is not True:raise RuntimeError('acceptance_required')
    policy=json.loads((ROOT/'config/policy.json').read_text());policy.update(diagnostics_enabled=True,monitor_enabled=True,acceptance_prefix='')
    write(ROOT/'config/policy.json',policy,0o444)
    run(['systemctl','restart','sub2api-wechat-query.service'])
    run(['docker','restart','--time','30','sub2api-wechat-astrbot'],timeout=50)
    write(ROOT/'admin-evidence/readonly-rollout.json',{'stage':'enabled','at':time.time(),'personas_bound':2,'usage_subjects':2})
    print(json.dumps({'diagnostics_enabled':True,'monitor_enabled':True,'acceptance_prefix_required':False}))


if __name__=='__main__':
    try:
        os.umask(0o077)
        if json.loads((ROOT/'task.json').read_text())['task']!='sub2api-wechat-group-bot-trial-20261103':raise RuntimeError('wrong_task')
        if (ROOT/'admin-evidence/space-paused.json').exists():raise RuntimeError('storage_pause_requires_review')
        parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['prepare','install','enable']);stage=parser.parse_args().stage
        {'prepare':prepare,'install':install,'enable':enable}[stage]()
    except Exception:
        print(json.dumps({'failed':True,'category':'readonly_rollout_requires_review'}));raise SystemExit(1)
