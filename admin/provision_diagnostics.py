"""Operator-only creation of task-owned read-only views. No business DML."""
import json
import os
from pathlib import Path
import subprocess
import time
import re

ROOT=Path('/opt/sub2api-wechat-bot')
MARKER='sub2api-wechat-readonly-diagnostics-20261103'


def database(sql,user='sub2api'):
    proc=subprocess.run(['docker','exec','-i','sub2api-postgres','psql','-X','-qAt',
        '-U',user,'-d','sub2api','-v','ON_ERROR_STOP=1'],input=sql,capture_output=True,text=True,timeout=12)
    if proc.returncode:raise RuntimeError('diagnostic_database_setup_failed')
    return [json.loads(x) for x in proc.stdout.splitlines() if x.startswith('{') or x.startswith('[')]


def views(bindings):
    if not isinstance(bindings,dict) or not 1<=len(bindings)<=100:raise ValueError('invalid_bindings')
    subject_re=re.compile(r'^(?:flower|gentle|member_[a-f0-9]{16})$')
    if (any(not isinstance(subject,str) or not subject_re.fullmatch(subject) for subject in bindings)
        or any(type(key_id) is not int or key_id<=0 for key_id in bindings.values())
        or len(set(bindings.values()))!=len(bindings)):
        raise ValueError('invalid_bindings')
    values=','.join(f"({key_id}::bigint,'{subject}'::text)" for subject,key_id in sorted(bindings.items()))
    return f"""BEGIN;
SET LOCAL lock_timeout='1s'; SET LOCAL statement_timeout='5s';
DO $guard$
BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='bot_diagnostics_ro') THEN
  IF (SELECT shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname='bot_diagnostics_ro') IS DISTINCT FROM '{MARKER}' THEN
   RAISE EXCEPTION 'role_collision';
  END IF;
 ELSE
  CREATE ROLE bot_diagnostics_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION CONNECTION LIMIT 4;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='bot_diagnostics') AND
   (SELECT obj_description(oid,'pg_namespace') FROM pg_namespace WHERE nspname='bot_diagnostics') IS DISTINCT FROM '{MARKER}' THEN
  RAISE EXCEPTION 'schema_collision';
 END IF;
END $guard$;
COMMENT ON ROLE bot_diagnostics_ro IS '{MARKER}';
CREATE SCHEMA IF NOT EXISTS bot_diagnostics;
COMMENT ON SCHEMA bot_diagnostics IS '{MARKER}';
REVOKE ALL ON SCHEMA bot_diagnostics FROM PUBLIC;
GRANT USAGE ON SCHEMA bot_diagnostics TO bot_diagnostics_ro;
ALTER ROLE bot_diagnostics_ro SET default_transaction_read_only=on;
ALTER ROLE bot_diagnostics_ro SET statement_timeout='3s';
ALTER ROLE bot_diagnostics_ro SET lock_timeout='500ms';
ALTER ROLE bot_diagnostics_ro SET idle_in_transaction_session_timeout='5s';
CREATE OR REPLACE VIEW bot_diagnostics.metered WITH (security_barrier=true) AS
 SELECT created_at,input_tokens,output_tokens,cache_creation_tokens,cache_read_tokens,duration_ms,first_token_ms
 FROM public.usage_logs WHERE created_at>=now()-interval '24 hours';
CREATE OR REPLACE VIEW bot_diagnostics.error_events WITH (security_barrier=true) AS
 SELECT created_at,status_code>=400 AS is_failure,
 CASE WHEN nullif(btrim(request_id),'') IS NOT NULL THEN md5(coalesce(api_key_id,0)::text||':'||request_id) END AS request_identity,
 error_phase IN ('upstream','account_auth') AND status_code<400 AS recovered,
 CASE WHEN jsonb_typeof(upstream_errors)='array' THEN jsonb_array_length(upstream_errors) ELSE 0 END AS context_entries
 FROM public.ops_error_logs WHERE NOT is_count_tokens AND created_at>=now()-interval '24 hours';
CREATE OR REPLACE VIEW bot_diagnostics.account_summary WITH (security_barrier=true) AS
 SELECT count(*) AS configured_accounts,
 count(*) FILTER(WHERE status='active' AND schedulable) AS active_schedulable,
 count(*) FILTER(WHERE status='active' AND schedulable AND (expires_at IS NULL OR expires_at>now())
  AND (rate_limit_reset_at IS NULL OR rate_limit_reset_at<=now()) AND (overload_until IS NULL OR overload_until<=now())
  AND (temp_unschedulable_until IS NULL OR temp_unschedulable_until<=now())) AS available_accounts,
 count(*) FILTER(WHERE expires_at<=now()) AS expired_accounts,
 count(*) FILTER(WHERE rate_limit_reset_at>now()) AS rate_limited_accounts,
 count(*) FILTER(WHERE overload_until>now() OR temp_unschedulable_until>now()) AS temporarily_unavailable_accounts
 FROM public.accounts WHERE deleted_at IS NULL;
CREATE OR REPLACE VIEW bot_diagnostics.model_mapping WITH (security_barrier=true) AS
 SELECT DISTINCT entry.key AS source_model,entry.value AS target_model FROM public.accounts a
 CROSS JOIN LATERAL jsonb_each_text(CASE WHEN jsonb_typeof(a.credentials->'model_mapping')='object'
  THEN a.credentials->'model_mapping' ELSE '{{}}'::jsonb END) entry
 WHERE a.deleted_at IS NULL AND a.status='active' AND a.schedulable AND (a.expires_at IS NULL OR a.expires_at>now());
CREATE OR REPLACE VIEW bot_diagnostics.allowed_subjects WITH (security_barrier=true) AS
 SELECT b.subject,(k.id IS NOT NULL AND k.deleted_at IS NULL AND k.status='active' AND (k.expires_at IS NULL OR k.expires_at>now())) AS available
 FROM (VALUES {values}) b(key_id,subject)
 LEFT JOIN public.api_keys k ON k.id=b.key_id;
CREATE OR REPLACE VIEW bot_diagnostics.allowed_tokens WITH (security_barrier=true) AS
 SELECT b.subject,u.created_at,u.input_tokens,u.output_tokens,u.cache_creation_tokens,u.cache_read_tokens,u.actual_cost
 FROM (VALUES {values}) b(key_id,subject)
 JOIN public.api_keys k ON k.id=b.key_id AND k.deleted_at IS NULL AND k.status='active' AND (k.expires_at IS NULL OR k.expires_at>now())
 JOIN public.usage_logs u ON u.api_key_id=b.key_id;
CREATE OR REPLACE VIEW bot_diagnostics.allowed_quota_usage WITH (security_barrier=true) AS
 SELECT b.subject,(k.id IS NOT NULL AND k.deleted_at IS NULL AND k.status='active' AND (k.expires_at IS NULL OR k.expires_at>now())) AS available,
  CASE WHEN k.id IS NOT NULL AND k.deleted_at IS NULL AND k.status='active' AND (k.expires_at IS NULL OR k.expires_at>now()) THEN k.usage_1d END AS usage_1d,
  k.window_1d_start
 FROM (VALUES {values}) b(key_id,subject)
 LEFT JOIN public.api_keys k ON k.id=b.key_id;
REVOKE ALL ON ALL TABLES IN SCHEMA bot_diagnostics FROM PUBLIC;
GRANT SELECT ON ALL TABLES IN SCHEMA bot_diagnostics TO bot_diagnostics_ro;
COMMIT;
"""


def main():
    os.umask(0o077)
    if json.loads((ROOT/'task.json').read_text())['task']!='sub2api-wechat-group-bot-trial-20261103':raise RuntimeError('wrong_task')
    receipt=ROOT/'admin-evidence/query-db-setup.json'
    if receipt.exists():
        binding=json.loads(receipt.read_text())['bindings']
    else:
        rows=database("BEGIN READ ONLY; SET LOCAL statement_timeout='3s'; SELECT json_build_object('name',name,'id',id) "
            "FROM public.api_keys WHERE name IN ('aa','example_key_label') AND deleted_at IS NULL AND status='active' "
            "AND (expires_at IS NULL OR expires_at>now()); ROLLBACK;")
        if len(rows)!=2 or {x['name'] for x in rows}!={'aa','example_key_label'}:raise RuntimeError('usage_binding_not_unique')
        by_name={x['name']:x['id'] for x in rows};binding={'flower':by_name['aa'],'gentle':by_name['example_key_label']}
    database(views(binding))
    permissions=database("SELECT json_build_object('readonly',current_setting('default_transaction_read_only'),"
        "'base_keys_read',has_table_privilege(current_user,'public.api_keys','SELECT'),"
        "'base_usage_write',has_table_privilege(current_user,'public.usage_logs','INSERT,UPDATE,DELETE'),"
        "'views_read',has_table_privilege(current_user,'bot_diagnostics.metered','SELECT'),"
        "'quota_view_read',has_table_privilege(current_user,'bot_diagnostics.allowed_quota_usage','SELECT'));",'bot_diagnostics_ro')[0]
    if permissions!={'readonly':'on','base_keys_read':False,'base_usage_write':False,'views_read':True,'quota_view_read':True}:
        raise RuntimeError('readonly_role_validation_failed')
    receipt.write_text(json.dumps({'task':MARKER,'bindings':binding,'verified_at':time.time(),'permissions':permissions},indent=2)+'\n')
    receipt.chmod(0o600)
    print(json.dumps({'readonly_role_ready':True,'allowed_usage_bindings':len(binding),'permissions':permissions,'key_values_read':False}))


if __name__=='__main__':
    try:main()
    except Exception:
        print(json.dumps({'failed':True,'category':'readonly_setup_requires_review'}));raise SystemExit(1)
