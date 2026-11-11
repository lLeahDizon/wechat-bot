"""Operator-only installation for member-bound token and USD quota reports."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time

ROOT=Path('/opt/sub2api-wechat-bot')
TASK='sub2api-wechat-group-bot-trial-20261103'
SUBJECT_RE=re.compile(r'^(?:flower|gentle|member_[a-f0-9]{16})$')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def atomic_json(path,value,mode=0o600):
    if path.is_symlink():raise RuntimeError('destination_symlink')
    temporary=path.with_name(path.name+'.usage.tmp')
    if temporary.exists():raise RuntimeError('unfinished_atomic_write')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    with os.fdopen(fd,'w',encoding='utf-8') as handle:
        json.dump(value,handle,ensure_ascii=False,indent=2);handle.write('\n');handle.flush();os.fsync(handle.fileno())
    temporary.chmod(mode)
    if os.geteuid()==0:os.chown(temporary,0,0)
    os.replace(temporary,path)


def sql_literal(value):
    if not isinstance(value,str) or not value or any(ord(char)<32 or ord(char)==127 for char in value):
        raise ValueError('invalid_lookup_value')
    return "'"+value.replace("'","''")+"'"


def usage_subject(group,identity):
    digest=hashlib.sha256((group+'\0'+identity).encode()).hexdigest()
    return digest,'member_'+digest[:16]


def hidden_rule(label):
    return {'length':len(label),'digest':hashlib.sha256(label.casefold().encode()).hexdigest()}


def configure_owner_usage(policy,profile_digest,subject,aliases,key_name):
    profiles=policy.setdefault('member_profiles',{})
    current=profiles.get(profile_digest,{})
    if current.get('usage_target') not in {None,subject}:raise RuntimeError('owner_usage_target_conflict')
    profiles[profile_digest]={**current,'tone':current.get('tone','default'),'usage_target':subject}

    public_aliases=policy.setdefault('usage_public_aliases',{})
    folded={}
    for target,names in public_aliases.items():
        if not isinstance(names,list):raise RuntimeError('usage_alias_config_invalid')
        for alias in names:
            if isinstance(alias,str):folded[alias.casefold()]=target
    if any(alias.casefold() in folded and folded[alias.casefold()]!=subject for alias in aliases):
        raise RuntimeError('usage_alias_collision')
    public_aliases[subject]=aliases
    public_names=policy.setdefault('usage_public_names',{})
    public_names[subject]=aliases[0]

    rules=policy.setdefault('hidden_label_rules',[])
    key_rule=hidden_rule(key_name)
    public_words={alias.casefold() for values in public_aliases.values() for alias in values if isinstance(alias,str)}
    rules=[rule for rule in rules if not isinstance(rule,dict) or rule.get('digest')!=key_rule['digest']]
    if key_name.casefold() not in public_words:rules.append(key_rule)
    policy['hidden_label_rules']=rules
    return policy


def resolve_owner(policy):
    group=policy.get('group_id')
    owner=read_json(ROOT/'admin-evidence/owner-binding.json')
    owner_id=owner.get('id')
    if not isinstance(owner_id,str) or not owner_id:raise RuntimeError('owner_identity_unavailable')
    account=read_json(ROOT/'admin-evidence/native-account.json')['id']
    sys.path.insert(0,str(ROOT/'code/admin'))
    import native_control
    result=native_control.cli(['groups','members','list',group,'--limit','100'],account)
    if result.get('nextCursor'):raise RuntimeError('member_catalog_incomplete')
    matches=[m for m in result.get('items',[]) if owner_id in [m.get('username'),m.get('id')]]
    if len(matches)!=1:raise RuntimeError('owner_not_unique_in_target_group')
    identity=matches[0].get('username') or matches[0].get('id')
    if not isinstance(identity,str) or not identity:raise RuntimeError('owner_stable_id_unavailable')
    return identity


def database(sql,user='sub2api',timeout=12):
    result=subprocess.run(['docker','exec','-i','sub2api-postgres','psql','-X','-qAt','-U',user,
        '-d','sub2api','-v','ON_ERROR_STOP=1'],input=sql,capture_output=True,text=True,timeout=timeout)
    if result.returncode:raise RuntimeError('usage_database_operation_failed')
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{') or line.startswith('[')]


def install_bindings(payload):
    if set(payload)!={'owner_key_name','owner_aliases'}:raise ValueError('usage_mapping_shape_invalid')
    key_name=payload['owner_key_name']
    aliases=payload['owner_aliases']
    if (not isinstance(key_name,str) or not 1<=len(key_name)<=100
        or any(ord(char)<32 or ord(char)==127 for char in key_name)):
        raise ValueError('usage_key_lookup_invalid')
    if (not isinstance(aliases,list) or not 2<=len(aliases)<=8
        or any(not isinstance(x,str) or not 1<=len(x)<=64 or any(ord(char)<32 or ord(char)==127 for char in x) for x in aliases)
        or len({x.casefold() for x in aliases})!=len(aliases)):
        raise ValueError('usage_aliases_invalid')

    policy_path=ROOT/'config/policy.json';policy=read_json(policy_path)
    group=policy.get('group_id')
    if (json.loads((ROOT/'task.json').read_text()).get('task')!=TASK or not isinstance(group,str)
        or group in {'','UNCONFIGURED'} or (ROOT/'admin-evidence/space-paused.json').exists()):
        raise RuntimeError('usage_installation_precondition_failed')
    if any(policy.get(k) is not True for k in ['enabled','diagnostics_enabled','monitor_enabled','general_enabled','server_readonly']):
        raise RuntimeError('usage_policy_not_enabled')
    if policy.get('acceptance_prefix'):raise RuntimeError('acceptance_mode_active')

    identity=resolve_owner(policy)
    owner_digest,subject=usage_subject(group,identity)

    key_rows=database("BEGIN READ ONLY; SET LOCAL statement_timeout='3s'; SELECT json_build_object('id',id,'name',name) "
        "FROM public.api_keys WHERE name="+sql_literal(key_name)+" AND deleted_at IS NULL AND status='active' "
        "AND (expires_at IS NULL OR expires_at>now()); ROLLBACK;")
    if len(key_rows)!=1 or key_rows[0].get('name')!=key_name or type(key_rows[0].get('id')) is not int:
        raise RuntimeError('owner_key_not_unique_active')
    key_id=key_rows[0]['id']

    receipt_path=ROOT/'admin-evidence/query-db-setup.json';receipt=read_json(receipt_path)
    bindings=receipt.get('bindings')
    if (not isinstance(bindings,dict) or not {'flower','gentle'}.issubset(bindings)
        or any(not SUBJECT_RE.fullmatch(name) or type(value) is not int or value<=0 for name,value in bindings.items())
        or len(set(bindings.values()))!=len(bindings)):
        raise RuntimeError('existing_usage_bindings_invalid')
    if subject in bindings and bindings[subject]!=key_id:raise RuntimeError('owner_subject_collision')
    if key_id in [value for name,value in bindings.items() if name!=subject]:raise RuntimeError('owner_key_already_bound')
    bindings=dict(bindings);bindings[subject]=key_id

    # A public alias can share text with the private lookup label. Keep it
    # usable as a person name while never including lookup labels in replies.
    configure_owner_usage(policy,owner_digest,subject,aliases,key_name)

    from provision_diagnostics import views
    database(views(bindings))
    permissions=database("BEGIN READ ONLY; SET LOCAL statement_timeout='3s'; SELECT json_build_object("
        "'read_only',current_setting('transaction_read_only')='on',"
        "'base_key_read',has_table_privilege(current_user,'public.api_keys','SELECT'),"
        "'base_usage_read',has_table_privilege(current_user,'public.usage_logs','SELECT'),"
        "'base_write',has_table_privilege(current_user,'public.api_keys','INSERT,UPDATE,DELETE') OR "
        "has_table_privilege(current_user,'public.usage_logs','INSERT,UPDATE,DELETE'),"
        "'quota_view_read',has_table_privilege(current_user,'bot_diagnostics.allowed_quota_usage','SELECT'),"
        "'quota_view_write',has_table_privilege(current_user,'bot_diagnostics.allowed_quota_usage','INSERT,UPDATE,DELETE')); ROLLBACK;",
        user='bot_diagnostics_ro')[0]
    expected={'read_only':True,'base_key_read':False,'base_usage_read':False,'base_write':False,
              'quota_view_read':True,'quota_view_write':False}
    if permissions!=expected:raise RuntimeError('usage_readonly_privileges_failed')

    receipt.update(bindings=bindings,verified_at=time.time(),permissions={
        'readonly':True,'base_key_read':False,'base_usage_read':False,'base_write':False,
        'quota_view_read':True,'quota_view_write':False})
    atomic_json(receipt_path,receipt)
    atomic_json(policy_path,policy,0o444)
    atomic_json(ROOT/'admin-evidence/usage-binding-rollout.json',{
        'at':time.time(),'usage_bindings':len(bindings),'owner_bound':True,
        'owner_aliases':len(aliases),'key_name_persisted':False,'key_secret_read':False,
        'base_key_access_by_bot':False,'business_rows_written':False,
        'owner_profile_digest':owner_digest,'owner_subject':subject,
        'owner_identity_private':identity})
    return {'bindings':len(bindings),'owner_bound':True,'quota_view_readonly':True}


def copy_runtime_files():
    code=ROOT/'code';plugin=ROOT/'plugins/astrbot_plugin_sub2api_guard'
    for source,target in [(code/'plugin',plugin),(code/'guard',plugin/'guard')]:
        for path in source.rglob('*'):
            if '__pycache__' in path.parts or path.suffix=='.pyc':continue
            destination=target/path.relative_to(source)
            if destination.is_symlink():raise RuntimeError('plugin_symlink')
            if path.is_dir():destination.mkdir(parents=True,exist_ok=True);destination.chmod(0o555)
            elif path.is_file():
                destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(path,destination);destination.chmod(0o444)
    client=ROOT/'query-client/diagnostics'
    if client.is_symlink():raise RuntimeError('query_client_symlink')
    client.mkdir(parents=True,exist_ok=True);client.chmod(0o755)
    for name in ['__init__.py','protocol.py','client.py']:
        target=client/name
        if target.is_symlink():raise RuntimeError('query_client_symlink')
        shutil.copyfile(code/'diagnostics'/name,target);target.chmod(0o444)


def source_manifest():
    path=ROOT/'admin-evidence/staged-source-sha256.json'
    staged=read_json(path)
    actual={str(p.relative_to(ROOT/'code')):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (ROOT/'code').rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
    if actual!=staged:raise RuntimeError('staged_source_manifest_mismatch')
    return staged


def live_queries(owner_subject):
    source="""import json
import decimal
import time
from diagnostics.client import QueryBudget
budget=QueryBudget()
personal=budget.query({'op':'member_usage','target':SUBJECT,'metrics':['tokens','usd'],'period':'today','window_minutes':15,'rank':False})
ranking=budget.query({'op':'member_usage','target':'all','metrics':['tokens','usd'],'period':'today','window_minutes':15,'rank':True})
now=int(time.time())
recent=budget.query({'op':'member_usage','target':SUBJECT,'metrics':['tokens','usd'],'period':'range','range_start_epoch':now-86400,'range_end_epoch':now,'window_minutes':15,'rank':False})
history=budget.query({'op':'member_usage','target':'all','metrics':['usd'],'period':'all','window_minutes':15,'rank':True})
def positive(items):
 return sum(decimal.Decimal(item['usd_amount'])>0 for item in items if item.get('binding_available'))
personal_fees=positive(personal.get('data',{}).get('usd_items',[]))
today_rank_fees=positive(ranking.get('data',{}).get('usd_items',[]))
history_fees=positive(history.get('data',{}).get('usd_items',[]))
print(json.dumps({'personal_state':personal['state'],'ranking_state':ranking['state'],'range_state':recent['state'],
 'history_state':history['state'],'personal_positive_fee_subjects':personal_fees,
 'today_rank_positive_fee_subjects':today_rank_fees,'history_positive_fee_subjects':history_fees,'queries':budget.count}))
""".replace('SUBJECT',repr(owner_subject))
    result=subprocess.run(['docker','exec','-u','10002:10002','sub2api-wechat-astrbot','python','-c',source],
        capture_output=True,text=True,timeout=15)
    rows=[line for line in result.stdout.splitlines() if line.startswith('{')]
    if result.returncode or not rows:raise RuntimeError('live_usage_query_failed')
    value=json.loads(rows[-1])
    expected={'personal_state':'ok','ranking_state':'ok','range_state':'ok','history_state':'ok',
        'personal_positive_fee_subjects':1,'today_rank_positive_fee_subjects':3,
        'history_positive_fee_subjects':3,'queries':4}
    if value!=expected:
        raise RuntimeError('live_usage_query_failed')
    return {'personal_query':'ok','ranking_query':'ok','date_range_query':'ok','history_query':'ok',
        'positive_fee_subjects':3,'queries':4,'client_uid':10002}


def container_state(name):
    result=subprocess.run(['docker','inspect','--format',
        '{{.Id}}|{{.State.StartedAt}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}',name],
        capture_output=True,text=True,timeout=3)
    if result.returncode:raise RuntimeError('container_state_unavailable')
    fields=result.stdout.strip().split('|')
    if len(fields)!=4:raise RuntimeError('container_state_invalid')
    return {'id':fields[0],'started_at':fields[1],'state':fields[2],'health':fields[3]}


def verify_snapshot():
    snapshot=read_json(ROOT/'admin-evidence/usage-predeploy-snapshot.json')
    archive_name=snapshot.get('archive','')
    if not isinstance(archive_name,str) or not re.fullmatch(r'rollback-usage-\d{8}T\d{6}Z\.tar\.gz',archive_name):
        raise RuntimeError('usage_rollback_snapshot_invalid')
    archive=ROOT/'diagnostics'/archive_name
    if (not archive.is_file() or archive.is_symlink()
        or hashlib.sha256(archive.read_bytes()).hexdigest()!=snapshot.get('archive_sha256')):
        raise RuntimeError('usage_rollback_snapshot_unavailable')


def verify_range_snapshot(root=ROOT):
    snapshot=read_json(root/'admin-evidence/usage-range-predeploy-snapshot.json')
    archive_name=snapshot.get('archive','')
    if not isinstance(archive_name,str) or not re.fullmatch(r'rollback-usage-range-\d{8}T\d{6}Z\.tar\.gz',archive_name):
        raise RuntimeError('usage_range_snapshot_invalid')
    archive=root/'diagnostics'/archive_name
    if (not archive.is_file() or archive.is_symlink()
        or hashlib.sha256(archive.read_bytes()).hexdigest()!=snapshot.get('archive_sha256')
        or snapshot.get('contains_secrets') is not False or snapshot.get('contains_wechat_data') is not False):
        raise RuntimeError('usage_range_snapshot_unavailable')


def update_usage_view():
    receipt=read_json(ROOT/'admin-evidence/query-db-setup.json')
    bindings=receipt.get('bindings')
    if (not isinstance(bindings,dict) or not bindings or len(bindings)>100
        or any(not SUBJECT_RE.fullmatch(subject) or type(key_id) is not int or key_id<=0 for subject,key_id in bindings.items())
        or len(set(bindings.values()))!=len(bindings)):
        raise RuntimeError('usage_bindings_invalid')
    from provision_diagnostics import views
    database(views(bindings))
    permissions=database("BEGIN READ ONLY; SET LOCAL statement_timeout='3s'; SELECT json_build_object("
        "'read_only',current_setting('transaction_read_only')='on',"
        "'base_key_read',has_table_privilege(current_user,'public.api_keys','SELECT'),"
        "'base_usage_read',has_table_privilege(current_user,'public.usage_logs','SELECT'),"
        "'base_write',has_table_privilege(current_user,'public.api_keys','INSERT,UPDATE,DELETE') OR "
        "has_table_privilege(current_user,'public.usage_logs','INSERT,UPDATE,DELETE'),"
        "'view_count',(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='bot_diagnostics' AND c.relkind='v'),"
        "'views_select',((SELECT bool_and(has_table_privilege(current_user,c.oid,'SELECT')) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='bot_diagnostics' AND c.relkind='v') IS TRUE),"
        "'views_write',coalesce((SELECT bool_or(has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE')) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='bot_diagnostics' AND c.relkind='v'),false),"
        "'usage_view_read',has_table_privilege(current_user,'bot_diagnostics.allowed_tokens','SELECT'),"
        "'usage_view_write',has_table_privilege(current_user,'bot_diagnostics.allowed_tokens','INSERT,UPDATE,DELETE'),"
        "'actual_cost_column',has_column_privilege(current_user,'bot_diagnostics.allowed_tokens','actual_cost','SELECT'),"
        "'key_created_index',EXISTS(SELECT 1 FROM pg_indexes WHERE schemaname='public' AND tablename='usage_logs' AND indexname='idx_usage_logs_api_key_created_at')); ROLLBACK;",
        user='bot_diagnostics_ro')[0]
    expected={'read_only':True,'base_key_read':False,'base_usage_read':False,'base_write':False,
        'view_count':7,'views_select':True,'views_write':False,'usage_view_read':True,
        'usage_view_write':False,'actual_cost_column':True,'key_created_index':True}
    if permissions!=expected:raise RuntimeError('usage_readonly_privileges_failed')
    atomic_json(ROOT/'admin-evidence/usage-range-view-verification.json',{
        'at':time.time(),'fixed_bound_view':True,'view_count':7,'all_select_only':True,
        'base_key_read':False,'base_usage_read':False,'business_write':False,
        'actual_cost_column':True,'key_time_index_present':True,'key_values_read':False})
    return {'view_count':7,'readonly':True,'actual_cost_column':True,'key_time_index_present':True}


def framework_replay():
    result=subprocess.run(['/usr/bin/python3',str(ROOT/'code/admin/framework_probe.py')],
        capture_output=True,text=True,timeout=60)
    rows=[line for line in result.stdout.splitlines() if line.startswith('{')]
    if result.returncode or not rows:raise RuntimeError('framework_probe_failed')
    value=json.loads(rows[-1])
    expected=['framework_import','real_star_instantiation','usage_personal_and_rank_replayed','usage_dates_and_record_cost_replayed',
              'owner_aliases_recognized','raw_identities_absent_from_model_input']
    if any(value.get(name) is not True for name in expected):raise RuntimeError('framework_usage_assertion_failed')
    if any(value.get(name)!=0 for name in ['actual_server_writes','actual_model_calls','actual_messages_sent']):
        raise RuntimeError('framework_probe_side_effect_detected')
    return {'framework_probe':'passed','model_calls':0,'group_messages_sent':0,'server_writes':0}


def range_predeploy_state():
    if (ROOT/'admin-evidence/space-paused.json').exists():raise RuntimeError('storage_pause_requires_review')
    verify_range_snapshot()
    staged=source_manifest()
    names=['sub2api','sub2api-postgres','sub2api-redis','sub2api-wechat-astrbot','sub2api-wechat-native']
    before={name:container_state(name) for name in names}
    if any(value['state']!='running' or value['health']!='healthy' for value in before.values()):
        raise RuntimeError('predeploy_container_unhealthy')
    if subprocess.run(['systemctl','is-active','sub2api-wechat-query.service'],capture_output=True,text=True,timeout=3).stdout.strip()!='active':
        raise RuntimeError('predeploy_query_service_inactive')
    return staged


def prepare_range_view():
    staged=range_predeploy_state()
    view=update_usage_view()
    digest=hashlib.sha256(json.dumps(staged,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    atomic_json(ROOT/'admin-evidence/usage-range-view-prepare-verification.json',{
        'at':time.time(),'source_manifest_sha256':digest,'view_readonly':view['readonly'],
        'view_actual_cost':view['actual_cost_column'],'key_time_index':view['key_time_index_present'],
        'business_rows_written':False})
    return {'prepared':True,'view_readonly':view['readonly'],'view_actual_cost':view['actual_cost_column'],
        'key_time_index':view['key_time_index_present']}


def verify_range_framework():
    staged=range_predeploy_state()
    framework=framework_replay()
    digest=hashlib.sha256(json.dumps(staged,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    atomic_json(ROOT/'admin-evidence/usage-range-framework-verification.json',{
        'at':time.time(),'source_manifest_sha256':digest,'framework_probe':framework['framework_probe'],
        'business_writes':framework['server_writes'],'model_calls':framework['model_calls'],
        'group_messages_sent':framework['group_messages_sent']})
    return {'framework_probe':framework['framework_probe'],'model_calls':0,'group_messages_sent':0,'server_writes':0}


def deploy(require_range_snapshot=True):
    if (ROOT/'admin-evidence/space-paused.json').exists():raise RuntimeError('storage_pause_requires_review')
    verify_range_snapshot() if require_range_snapshot else verify_snapshot()
    binding=read_json(ROOT/'admin-evidence/usage-binding-rollout.json')
    policy=read_json(ROOT/'config/policy.json')
    subject=binding.get('owner_subject')
    if not isinstance(subject,str) or not SUBJECT_RE.fullmatch(subject):raise RuntimeError('owner_usage_binding_missing')
    staged=source_manifest()
    if require_range_snapshot:
        view_receipt=read_json(ROOT/'admin-evidence/usage-range-view-prepare-verification.json')
        framework_receipt=read_json(ROOT/'admin-evidence/usage-range-framework-verification.json')
        digest=hashlib.sha256(json.dumps(staged,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if (view_receipt.get('source_manifest_sha256')!=digest or view_receipt.get('view_readonly') is not True
            or view_receipt.get('view_actual_cost') is not True or view_receipt.get('key_time_index') is not True
            or view_receipt.get('business_rows_written') is not False
            or framework_receipt.get('source_manifest_sha256')!=digest or framework_receipt.get('framework_probe')!='passed'
            or framework_receipt.get('business_writes')!=0 or framework_receipt.get('model_calls')!=0
            or framework_receipt.get('group_messages_sent')!=0):
            raise RuntimeError('usage_range_prepare_verification_failed')
        framework={'framework_probe':framework_receipt['framework_probe']}
        view_result={'view_count':7,'readonly':True,'actual_cost_column':True,'key_time_index_present':True}
    else:
        framework=framework_replay()
        view_result=update_usage_view()
    names=['sub2api','sub2api-postgres','sub2api-redis','sub2api-wechat-astrbot','sub2api-wechat-native']
    before={name:container_state(name) for name in names}
    if any(value['state']!='running' or value['health']!='healthy' for value in before.values()):
        raise RuntimeError('predeploy_container_unhealthy')
    if subprocess.run(['systemctl','is-active','sub2api-wechat-query.service'],capture_output=True,text=True,timeout=3).stdout.strip()!='active':
        raise RuntimeError('predeploy_query_service_inactive')
    copy_runtime_files()
    subprocess.run(['systemctl','restart','sub2api-wechat-query.service'],check=True,timeout=15,
                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run(['docker','restart','--time','30','sub2api-wechat-astrbot'],check=True,timeout=50,
                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        responder=container_state('sub2api-wechat-astrbot')
        if responder['state']=='running' and responder['health']=='healthy':break
        time.sleep(1)
    else:raise RuntimeError('responder_health_timeout')
    after={name:container_state(name) for name in names}
    for name in ['sub2api','sub2api-postgres','sub2api-redis','sub2api-wechat-native']:
        if after[name]!=before[name]:raise RuntimeError('protected_container_changed')
        if after[name]['state']!='running' or after[name]['health']!='healthy':raise RuntimeError('dependency_health_changed')
    query_result=live_queries(subject)
    if subprocess.run(['systemctl','is-active','sub2api-wechat-query.service'],capture_output=True,text=True,timeout=3).stdout.strip()!='active':
        raise RuntimeError('query_service_inactive')
    heartbeat=read_json(ROOT/'astrbot-data/sub2api_guard/heartbeat.json')
    if heartbeat.get('guard')!='ready' or heartbeat.get('server_readonly') is not True or heartbeat.get('general_enabled') is not True:
        raise RuntimeError('responder_features_not_loaded')
    atomic_json(ROOT/'admin-evidence/deployed-source-sha256.json',staged)
    atomic_json(ROOT/'admin-evidence/usage-range-final-verification.json',{
        'at':time.time(),'stage':'enabled_and_verified','usage_bindings':len(read_json(ROOT/'admin-evidence/query-db-setup.json')['bindings']),
        'personal_query':query_result['personal_query'],'ranking_query':query_result['ranking_query'],
        'date_range_query':query_result['date_range_query'],'history_query':query_result['history_query'],
        'framework_probe':framework['framework_probe'],
        'positive_fee_subjects':query_result['positive_fee_subjects'],'views':view_result,
        'all_five_containers_healthy':True,'query_service_active':True,'wechat_restarted':False,
        'sub2api_database_cache_restarted':False,'model_calls':0,'group_messages_sent':0,
        'key_values_read':False,'business_rows_written':False})
    return {'stage':'enabled_and_verified','usage_bindings':len(read_json(ROOT/'admin-evidence/query-db-setup.json')['bindings']),
        'personal_query':'ok','ranking_query':'ok','date_range_query':'ok','history_query':'ok',
        'positive_fee_subjects':query_result['positive_fee_subjects'],'containers_healthy':5,'key_names_in_output':False,
        'model_calls':0,'group_messages_sent':0}


def activate(payload):
    verify_snapshot()
    source_manifest()
    framework=framework_replay()
    binding=install_bindings(payload)
    deployed=deploy(require_range_snapshot=False)
    return {'framework_probe':framework['framework_probe'],'bindings':binding['bindings'],
        'stage':deployed['stage'],'personal_query':deployed['personal_query'],
        'ranking_query':deployed['ranking_query'],'date_range_query':deployed['date_range_query'],
        'history_query':deployed['history_query'],'containers_healthy':deployed['containers_healthy'],
        'model_calls':0,'group_messages_sent':0,'key_names_in_output':False}


def snapshot(root=ROOT,now=None):
    now=time.time() if now is None else now
    if root.is_symlink() or json.loads((root/'task.json').read_text()).get('task')!=TASK:
        raise RuntimeError('task_directory_not_verified')
    if (root/'admin-evidence/space-paused.json').exists():raise RuntimeError('storage_pause_requires_review')
    evidence_path=root/'admin-evidence/usage-predeploy-snapshot.json'
    if evidence_path.exists():raise RuntimeError('usage_snapshot_already_exists')
    disk=shutil.disk_usage(root)
    if disk.total<=0 or disk.free/disk.total<.2:raise RuntimeError('insufficient_disk_for_usage_snapshot')
    diagnostics=root/'diagnostics'
    if diagnostics.is_symlink():raise RuntimeError('diagnostics_directory_symlink')
    diagnostics.mkdir(mode=0o700,exist_ok=True)
    stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime(now))
    archive=diagnostics/f'rollback-usage-{stamp}.tar.gz'
    temporary=diagnostics/(archive.name+'.tmp')
    if archive.exists() or temporary.exists():raise RuntimeError('usage_snapshot_name_collision')
    paths=[root/'code',root/'query-client/diagnostics',root/'plugins/astrbot_plugin_sub2api_guard',
           root/'config/policy.json',root/'compose.json',root/'admin-evidence/query-db-setup.json',
           root/'admin-evidence/deployed-source-sha256.json']
    count=size=0
    try:
        with tarfile.open(temporary,'x:gz') as tar:
            for base in paths:
                if not base.exists():continue
                if base.is_symlink():raise RuntimeError('usage_snapshot_source_symlink')
                if base.is_file():
                    size+=base.stat().st_size
                    if size>=1024**3 or disk.free-size*2<disk.total*.2:raise RuntimeError('usage_snapshot_size_limit')
                    tar.add(base,arcname=str(base.relative_to(root)),recursive=False);count+=1;continue
                for current,directories,files in os.walk(base,followlinks=False):
                    current_path=Path(current)
                    if current_path.is_symlink():raise RuntimeError('usage_snapshot_source_symlink')
                    directories[:]=sorted(name for name in directories if not (current_path/name).is_symlink())
                    for name in sorted(files):
                        item=current_path/name
                        if item.is_symlink() or '__pycache__' in item.parts or item.suffix=='.pyc':continue
                        size+=item.stat().st_size
                        if size>=1024**3 or disk.free-size*2<disk.total*.2:raise RuntimeError('usage_snapshot_size_limit')
                        tar.add(item,arcname=str(item.relative_to(root)),recursive=False);count+=1
        temporary.chmod(0o600)
        if count<10:raise RuntimeError('usage_snapshot_incomplete')
        os.replace(temporary,archive)
        digest=hashlib.sha256(archive.read_bytes()).hexdigest()
        existing=[]
        for item in diagnostics.iterdir():
            if item.is_symlink() or not item.is_file():continue
            if (item.name in {'rollback-code.tar.gz','rollback-feedback.tar.gz'}
                or re.fullmatch(r'rollback-usage-\d{8}T\d{6}Z\.tar\.gz',item.name)):
                existing.append(item)
        existing.sort(key=lambda item:item.stat().st_mtime,reverse=True)
        for position,item in enumerate(existing):
            if position>=2 or now-item.stat().st_mtime>14*86400:
                item.unlink()
        receipt={'at':now,'archive':archive.name,'archive_sha256':digest,
            'archived_source_files':count,'contains_secrets':False,'contains_wechat_data':False}
        atomic_json(evidence_path,receipt)
        return {'snapshot_created':True,'files':count,'source_bytes':size,'rollback_archives_retained':min(2,len(existing)),
                'sha256':digest}
    except Exception:
        if temporary.exists():temporary.unlink()
        if archive.exists() and not evidence_path.exists():archive.unlink()
        raise


def main():
    os.umask(0o077)
    if json.loads((ROOT/'task.json').read_text()).get('task')!=TASK:raise RuntimeError('wrong_task_directory')
    if len(sys.argv)<2 or sys.argv[1] not in {'bind','deploy','snapshot','activate','prepare-range-view','verify-range-framework'}:raise RuntimeError('action_not_allowed')
    if sys.argv[1]=='bind':
        payload=json.load(sys.stdin)
        print(json.dumps(install_bindings(payload)))
    elif sys.argv[1]=='snapshot':
        print(json.dumps(snapshot()))
    elif sys.argv[1]=='activate':
        payload=json.load(sys.stdin)
        print(json.dumps(activate(payload)))
    elif sys.argv[1]=='prepare-range-view':print(json.dumps(prepare_range_view()))
    elif sys.argv[1]=='verify-range-framework':print(json.dumps(verify_range_framework()))
    else:print(json.dumps(deploy()))


if __name__=='__main__':
    try:main()
    except Exception as error:
        category=str(error)
        if not re.fullmatch(r'[a-z_]{1,64}',category):category='usage_rollout_failed'
        print(json.dumps({'failed':True,'category':category}));raise SystemExit(1)
