"""Database privilege proof without selecting protected values or writing rows."""
import json
from pathlib import Path
import subprocess
import time

ROOT=Path('/opt/sub2api-wechat-bot')


def query(sql):
    p=subprocess.run(['docker','exec','-i','sub2api-postgres','psql','-X','-qAt','-U','bot_diagnostics_ro',
        '-d','sub2api','-v','ON_ERROR_STOP=1'],input="BEGIN READ ONLY; SET LOCAL statement_timeout='3s'; "+sql+'; ROLLBACK;',capture_output=True,text=True,timeout=5)
    if p.returncode:raise RuntimeError('privilege_probe_failed')
    return json.loads(p.stdout.strip())


def main():
    result=query("SELECT json_build_object('superuser',rolsuper,'create_role',rolcreaterole,'create_db',rolcreatedb,"
        "'replication',rolreplication,'bypass_rls',rolbypassrls,'readonly',current_setting('transaction_read_only')) FROM pg_roles WHERE rolname=current_user")
    if any(result[x] for x in ['superuser','create_role','create_db','replication','bypass_rls']) or result['readonly']!='on':
        raise RuntimeError('excess_database_privileges')
    tables=[]
    for table in ['api_keys','accounts','users','usage_logs','ops_error_logs']:
        item=query(f"SELECT json_build_object('base_read',has_table_privilege(current_user,'public.{table}','SELECT'),"
            f"'base_write',has_table_privilege(current_user,'public.{table}','INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'))")
        if any(item.values()):raise RuntimeError('base_table_access_not_denied')
        tables.append({'table':table,**item})
    views=query("SELECT json_build_object('view_count',count(*),'all_select',bool_and(has_table_privilege(current_user,c.oid,'SELECT')),"
        "'any_write',bool_or(has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'))) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='bot_diagnostics' AND c.relkind='v'")
    if views!={'view_count':7,'all_select':True,'any_write':False}:raise RuntimeError('view_privilege_mismatch')
    # LIMIT 0 asks the authorization layer and never reads any secret value.
    for sql in ['SELECT key FROM public.api_keys LIMIT 0','SELECT credentials FROM public.accounts LIMIT 0']:
        p=subprocess.run(['docker','exec','-i','sub2api-postgres','psql','-X','-qAt','-U','bot_diagnostics_ro','-d','sub2api','-v','ON_ERROR_STOP=1'],
            input='BEGIN READ ONLY; '+sql+'; ROLLBACK;',capture_output=True,text=True,timeout=5)
        if p.returncode==0 or 'permission denied' not in p.stderr:raise RuntimeError('secret_column_access_not_denied')
    result={'at':time.time(),'role':result,'base_tables':tables,'views':views,'protected_column_queries_denied':2,'secret_values_read':0,'business_dml_executed':0}
    path=ROOT/'admin-evidence/readonly-privileges.json';path.write_text(json.dumps(result,indent=2)+'\n');path.chmod(0o600)
    print(json.dumps(result))


if __name__=='__main__':
    try:main()
    except Exception:
        print(json.dumps({'failed':True,'category':'readonly_privilege_verification_failed'}));raise SystemExit(1)
