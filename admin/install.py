"""Operator-only idempotent configuration for the dedicated deployment."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess

ROOT=Path('/opt/sub2api-wechat-bot')
TASK='sub2api-wechat-group-bot-trial-20261103'


def read_json(path):
    # AstrBot writes cmd_config.json with UTF-8 BOM after filling defaults.
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write_json(path,value,mode=0o600,owner=None):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    path.chmod(mode)
    if owner is not None:os.chown(path,owner,owner)


def directory(path,owner,mode=0o700):
    if path.is_symlink():raise RuntimeError('directory_symlink_rejected')
    path.mkdir(parents=True,exist_ok=True)
    path.chmod(mode);os.chown(path,owner,owner)


def internal_tls():
    ca_key=ROOT/'secrets/native-ca-key.pem'
    ca_cert=ROOT/'config/native-ca.pem'
    key=ROOT/'secrets/native-server-key.pem'
    cert=ROOT/'secrets/native-server-cert.pem'
    def openssl(*args):
        result=subprocess.run(['openssl',*map(str,args)],capture_output=True,timeout=30)
        if result.returncode:raise RuntimeError('internal_tls_generation_failed')
    if not ca_key.exists():
        openssl('req','-x509','-newkey','rsa:2048','-nodes','-sha256','-days','365',
                '-subj','/CN=sub2api-wechat-local-ca','-keyout',ca_key,'-out',ca_cert)
    if not ca_key.is_file() or not ca_cert.is_file():raise RuntimeError('incomplete_ca_state')
    if not key.exists() or not cert.exists():
        csr=ROOT/'admin-evidence/native-server.csr'
        extensions=ROOT/'admin-evidence/native-server.ext'
        extensions.write_text('subjectAltName=DNS:native,DNS:localhost,IP:127.0.0.1\n'
            'extendedKeyUsage=serverAuth\nkeyUsage=digitalSignature,keyEncipherment\nbasicConstraints=CA:FALSE\n')
        openssl('req','-new','-newkey','rsa:2048','-nodes','-sha256','-subj','/CN=native',
                '-keyout',key,'-out',csr)
        openssl('x509','-req','-in',csr,'-CA',ca_cert,'-CAkey',ca_key,'-CAcreateserial',
                '-out',cert,'-days','365','-sha256','-extfile',extensions)
        csr.unlink();extensions.unlink()
    openssl('verify','-CAfile',ca_cert,cert)
    openssl('x509','-in',cert,'-checkhost','native','-noout')
    ca_key.chmod(0o400);ca_cert.chmod(0o444)
    for path in [key,cert]:
        path.chmod(0o600);os.chown(path,10001,10001)
    bundle=ROOT/'config/native-trust-bundle.pem'
    if not bundle.exists():
        source="import ssl,pathlib,sys;sys.stdout.buffer.write(pathlib.Path(ssl.get_default_verify_paths().cafile).read_bytes())"
        proc=subprocess.run(['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL',
            '--memory','128m','--user','10002:10002','--entrypoint','python','soulter/astrbot:v4.29.4','-c',source],
            capture_output=True,timeout=30)
        if proc.returncode or b'BEGIN CERTIFICATE' not in proc.stdout:raise RuntimeError('system_ca_unavailable')
        bundle.write_bytes(proc.stdout+b'\n'+ca_cert.read_bytes())
        bundle.chmod(0o444)


def main():
    os.umask(0o077)
    if json.loads((ROOT/'task.json').read_text()).get('task')!=TASK:
        raise RuntimeError('wrong_task_directory')
    if (ROOT/'admin-evidence/space-paused.json').exists():
        raise RuntimeError('storage_pause_requires_operator_review')
    code=ROOT/'code'
    for name,uid,mode in [('config',0,0o755),('secrets',0,0o700),('public',0,0o755),
                          ('native-data',10001,0o700),('native-scratch',10001,0o700),
                          ('astrbot-data',10002,0o700),('plugins',0,0o755),
                          ('archives',0,0o700),('archives/backups',0,0o700),
                          ('archives/summaries',10002,0o700),('logs',0,0o700)]:
        directory(ROOT/name,uid,mode)
    for name,uid in [('native-owner-token',10001),('native-bot-token',10002)]:
        path=ROOT/'secrets'/name
        if not path.exists():path.write_text((secrets.token_hex(32) if name=='native-owner-token' else '')+'\n')
        path.chmod(0o400);os.chown(path,uid,uid)
    key=ROOT/'secrets/sub2api-key'
    if not key.is_file():raise RuntimeError('sub2api_key_missing')
    key.chmod(0o400);os.chown(key,10002,10002)
    policy=ROOT/'config/policy.json'
    if not policy.exists():shutil.copyfile(code/'config/policy.example.json',policy)
    policy.chmod(0o444)
    shutil.copyfile(code/'knowledge/faq.json',ROOT/'config/faq.json')
    (ROOT/'config/faq.json').chmod(0o444)
    internal_tls()
    client_root=ROOT/'query-client/diagnostics'
    directory(ROOT/'query-client',0,0o755);directory(client_root,0,0o755)
    for name in ['__init__.py','protocol.py','client.py']:
        shutil.copyfile(code/'diagnostics'/name,client_root/name);(client_root/name).chmod(0o444)
    server=ROOT/'config/native-server.toml'
    server.write_text('schemaVersion = 1\nlisten = "127.0.0.1:6174"\nnetworkListen = "0.0.0.0:7443"\n'
        'tlsCertFile = "/run/secrets/native-server-cert.pem"\ntlsKeyFile = "/run/secrets/native-server-key.pem"\n'
        'dataDir = "/var/lib/agent-wechat"\ntokenFile = "/run/secrets/native-owner-token"\n')
    server.chmod(0o400);os.chown(server,10001,10001)
    # TLS bridge only: no published ports, host networking or host control mounts.
    images=json.loads((ROOT/'admin-evidence/images.json').read_text())
    astrbot_image=images['soulter/astrbot:v4.29.4']['digests'][0]
    native_image=images['ghcr.io/thisnick/agent-wechat-native:v1.3.3']['digests'][0]
    native_plugin=ROOT/'plugins/astrbot_plugin_wechat_native'
    guard_plugin=ROOT/'plugins/astrbot_plugin_sub2api_guard'
    # Only task-owned plugin directories are updated; persistent state is separate.
    for source,destination in [(code/'vendor/astrbot_plugin_wechat_native',native_plugin),(code/'plugin',guard_plugin)]:
        shutil.copytree(source,destination,dirs_exist_ok=True)
    shutil.copytree(code/'guard',guard_plugin/'guard',dirs_exist_ok=True)
    for path in (ROOT/'plugins').rglob('*'):
        if path.is_symlink():raise RuntimeError('plugin_symlink_rejected')
        path.chmod(0o555 if path.is_dir() else 0o444)
        os.chown(path,0,0)
    config_path=ROOT/'astrbot-data/cmd_config.json'
    if not config_path.exists():
        config={
          'config_version':5,'log_level':'WARNING','log_file_enable':False,'trace_enable':False,
          'disable_metrics':True,'disable_builtin_commands':True,'t2i':False,'admins_id':[],
          'dashboard':{'enable':False,'host':'127.0.0.1','port':6185},
          'plugin_set':['astrbot_plugin_wechat_native','astrbot_plugin_sub2api_guard'],
          'provider':[],'provider_sources':[],
          'provider_settings':{'enable':False,'computer_use_runtime':'none','web_search':False,
                               'proactive_capability':{'add_cron_tools':False}},
          'provider_stt_settings':{'enable':False},'provider_tts_settings':{'enable':False},
          'provider_ltm_settings':{'group_message_history_enable':False,'group_icl_enable':False,
                                   'active_reply':{'enable':False}},
          'platform_settings':{'unique_session':True,'enable_id_white_list':False,
                               'id_whitelist_log':False,'no_permission_reply':False,'reply_with_quote':False,
                               'rate_limit':{'count':0,'time':60,'strategy':'discard'}},
          'platform':[{'id':'sub2api-wechat','type':'wechat_native','enable':False,
                       'server_url':'https://native:7443','account_id':'UNCONFIGURED',
                       'token':'','token_file':'/run/secrets/native-bot-token',
                       'enable_private_reply':False,'enable_media':False,
                       'group_reply_mode':'mention','allowed_groups':['UNCONFIGURED@chatroom'],
                       'unique_session':True,'enable_group_summary':False}]
        }
        write_json(config_path,config,owner=10002)
    else:
        config=read_json(config_path)
        for platform in config.get('platform',[]):
            if platform.get('id')=='sub2api-wechat':platform['server_url']='https://native:7443'
        write_json(config_path,config,owner=10002)
    (ROOT/'astrbot-data/sub2api_guard').mkdir(exist_ok=True)
    os.chown(ROOT/'astrbot-data/sub2api_guard',10002,10002)
    common={'restart':'unless-stopped','read_only':True,'cap_drop':['ALL'],
            'security_opt':['no-new-privileges:true'],'logging':{'driver':'none'},
            'networks':['bot'],'stop_grace_period':'45s'}
    compose={'name':'sub2api-wechat','services':{
      'native':{**common,'image':native_image,'container_name':'sub2api-wechat-native',
        'user':'10001:10001','mem_limit':'384m','cpus':'0.50','pids_limit':128,
        'environment':{'TZ':'Asia/Shanghai','TMPDIR':'/var/cache/agent-wechat','RUST_LOG':'off'},
        'tmpfs':['/tmp:rw,noexec,nosuid,size=64m,mode=1777'],
        'volumes':[f'{ROOT}/native-data:/var/lib/agent-wechat',f'{ROOT}/native-scratch:/var/cache/agent-wechat',
          f'{ROOT}/config/native-server.toml:/etc/agent-wechat/server/config.toml:ro',
          f'{ROOT}/secrets/native-owner-token:/run/secrets/native-owner-token:ro',
          f'{ROOT}/secrets/native-server-key.pem:/run/secrets/native-server-key.pem:ro',
          f'{ROOT}/secrets/native-server-cert.pem:/run/secrets/native-server-cert.pem:ro'],
        'healthcheck':{'test':['CMD','wx','--json',
          '--server','http://127.0.0.1:6174','--token-file','/run/secrets/native-owner-token','status'],
          'interval':'30s','timeout':'10s','retries':3,'start_period':'20s'}},
      'astrbot':{**common,'image':astrbot_image,'container_name':'sub2api-wechat-astrbot',
        'user':'10002:10002','mem_limit':'768m','cpus':'0.75','pids_limit':192,
        'environment':{'TZ':'Asia/Shanghai','PYTHONDONTWRITEBYTECODE':'1',
          'PYTHONPATH':'/opt/bot-runtime:/opt/query-client:/opt/bot-code:/AstrBot','XDG_CACHE_HOME':'/tmp/bot-cache','PIP_NO_CACHE_DIR':'1',
          'SSL_CERT_FILE':'/run/bot-config/native-trust-bundle.pem'},
        'tmpfs':['/tmp:rw,noexec,nosuid,size=128m,mode=1777'],
        'volumes':[f'{ROOT}/astrbot-data:/AstrBot/data',f'{ROOT}/plugins:/AstrBot/data/plugins:ro',
          f'{ROOT}/config:/run/bot-config:ro',f'{ROOT}/public:/run/bot-status:ro',
          f'{ROOT}/secrets/sub2api-key:/run/secrets/sub2api-key:ro',
          f'{ROOT}/secrets/native-bot-token:/run/secrets/native-bot-token:ro',
          f'{ROOT}/code/runtime:/opt/bot-runtime:ro',f'{ROOT}/code/guard:/opt/bot-code/guard:ro',
          f'{ROOT}/code/admin/weekly.py:/opt/bot-code/admin/weekly.py:ro',
          f'{ROOT}/archives/summaries:/run/bot-archives',f'{ROOT}/query-client:/opt/query-client:ro']+
          (['/run/sub2api-wechat-query:/run/bot-query:ro'] if Path('/run/sub2api-wechat-query').is_dir() else []),
        'healthcheck':{'test':['CMD','python','-c',
          "import json,time;from pathlib import Path;p=Path('/AstrBot/data/sub2api_guard/heartbeat.json');d=json.loads(p.read_text());assert d['guard']=='ready' and time.time()-d['at']<180"],
          'interval':'30s','timeout':'10s','retries':3,'start_period':'90s'}}},
      'networks':{'bot':{'driver':'bridge','name':'sub2api-wechat-bot'}}}
    write_json(ROOT/'compose.json',compose)
    if not (ROOT/'public/public.json').exists():
        write_json(ROOT/'public/public.json',{'schema':1,'captured_at':0,'service':'unknown','pressure':'unknown'},0o644)
    manifest={}
    for path in code.rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts:
            manifest[str(path.relative_to(code))]=hashlib.sha256(path.read_bytes()).hexdigest()
    write_json(ROOT/'admin-evidence/deployed-source-sha256.json',manifest)
    result=subprocess.run(['docker','compose','-f',str(ROOT/'compose.json'),'config','--quiet'],capture_output=True,text=True)
    if result.returncode:raise RuntimeError('compose_validation_failed')
    print(json.dumps({'configured':True,'platform_enabled':read_json(config_path)['platform'][0]['enable'],
                      'images_pinned':True,'secrets_printed':False,'compose_valid':True}))


if __name__=='__main__':main()
