"""Fixed passive readers. Raw records and network identities never leave here."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import time

MAX_BYTES = 1024 * 1024
MAX_ROWS = 5000


def bounded(args, timeout=2):
    # A temporary file bounds memory even when an OS utility is unusually noisy.
    import tempfile
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(args, stdout=output, stderr=subprocess.DEVNULL,
                                   env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL':'C'})
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline or output.tell() > MAX_BYTES:
                    raise OSError('reader_limit')
                time.sleep(.01)
            if process.returncode or output.tell() > MAX_BYTES:
                raise OSError('reader_unavailable')
            output.seek(0)
            return output.read(MAX_BYTES + 1).decode('utf-8', errors='replace')
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def exposure():
    text = bounded(['ss','-H','-lntu'])
    counts = {'tcp':0,'udp':0,'loopback':0,'wildcard':0,'other_bindings':0}
    for row in text.splitlines():
        fields = row.split()
        if len(fields) < 5 or fields[0] not in {'tcp','udp'}:
            continue
        counts[fields[0]] += 1
        host = fields[4].rsplit(':',1)[0].strip('[]')
        if host in {'127.0.0.1','::1'}:
            counts['loopback'] += 1
        elif host in {'0.0.0.0','::','*'}:
            counts['wildcard'] += 1
        else:
            counts['other_bindings'] += 1
    return {**counts, 'scope':'local_listeners_not_external_reachability',
            'addresses_and_ports_disclosed':False}


def firewall():
    try:
        raw = bounded(['ufw','status','verbose'])
    except OSError:
        return firewall_config()
    active = bool(re.search(r'^Status: active\s*$',raw,re.M))
    inactive = bool(re.search(r'^Status: inactive\s*$',raw,re.M))
    if not (active or inactive):
        raise OSError('firewall_status_unavailable')
    default = re.search(r'^Default: (deny|reject|allow) \(incoming\)',raw,re.M)
    return {'ufw_active':active,'live_state_verified':True,
            'default_incoming':default.group(1) if default else 'unknown',
            'allow_rule_count':len(re.findall(r'\bALLOW(?: IN)?\b',raw)),
            'deny_rule_count':len(re.findall(r'\b(?:DENY|REJECT)(?: IN)?\b',raw)),
            'scope':'ufw_only_cloud_security_groups_not_observed'}


def firewall_config(etc=Path('/etc')):
    def read(path):
        with path.open('rb') as stream:raw=stream.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise OSError('reader_limit')
        return raw.decode('utf-8',errors='replace')
    config=read(etc/'ufw/ufw.conf');defaults=read(etc/'default/ufw')
    enabled=re.search(r'^\s*ENABLED\s*=\s*(yes|no)\s*$',config,re.M|re.I)
    if enabled is None:raise OSError('firewall_config_unknown')
    policy=re.search(r'^\s*DEFAULT_INPUT_POLICY\s*=\s*[\"\x27]?(DROP|REJECT|ACCEPT)[\"\x27]?\s*$',defaults,re.M)
    counts={'allow_rule_count':0,'deny_rule_count':0};observed=0
    for name in ['user.rules','user6.rules']:
        try:rules=read(etc/'ufw'/name)
        except FileNotFoundError:continue
        observed+=1
        for row in rules.splitlines():
            if not row.startswith('-A ufw-user-input '):continue
            counts['allow_rule_count']+=bool(re.search(r'\s-j ACCEPT(?:\s|$)',row))
            counts['deny_rule_count']+=bool(re.search(r'\s-j (?:DROP|REJECT)(?:\s|$)',row))
    return {'ufw_active':None,'live_state_verified':False,
            'configured_enabled':enabled[1].lower()=='yes',
            'default_incoming':{'DROP':'deny','REJECT':'reject','ACCEPT':'allow'}.get(policy[1] if policy else '', 'unknown'),
            **counts,'observed_config_rule_files':observed,'state':'partial',
            'scope':'configured_rules_only_not_live_kernel_or_cloud_security_groups'}


def ssh_events(start, end):
    # Select fixed services, never interpolate a command supplied by a member.
    raw = bounded(['journalctl','--no-pager','-o','json','-u','ssh.service','-u','sshd.service',
        '--since','@'+str(start),'--until','@'+str(end),'-n',str(MAX_ROWS)],timeout=3)
    rows = raw.splitlines()
    failed = accepted = invalid = 0
    first = last = None
    for row in rows:
        try:
            item = json.loads(row)
            stamp = int(item['__REALTIME_TIMESTAMP']) / 1000000
            message = item.get('MESSAGE','')
            if not start <= stamp <= end or not isinstance(message,str):
                continue
            first = stamp if first is None else min(first,stamp)
            last = stamp if last is None else max(last,stamp)
            failed += bool(re.search(r'Failed (?:password|publickey)|authentication failure|Invalid user',message,re.I))
            accepted += bool(re.search(r'Accepted (?:password|publickey)',message))
        except (ValueError,TypeError,KeyError):
            invalid += 1
    return {'failed_auth_event_rows':failed,'accepted_auth_event_rows':accepted,
            'observed_rows':len(rows),'row_limit':MAX_ROWS,'truncated':len(rows)>=MAX_ROWS,
            'invalid_rows':invalid,'first_observed_epoch':first,'last_observed_epoch':last,
            'requested_start_epoch':start,'requested_end_epoch':end,
            'coverage':'journal_retention_not_verified','scope':'events_not_unique_attempts_or_proof_of_intrusion'}


def versions():
    result = {}
    deadline = time.monotonic()+3
    for label,name in [('gateway','sub2api'),('database','sub2api-postgres'),
                       ('cache','sub2api-redis'),('responder','sub2api-wechat-astrbot'),
                       ('wechat','sub2api-wechat-native')]:
        try:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise OSError('reader_limit')
            value = json.loads(bounded(['docker','inspect','--format','{{json .Config.Image}}',name],timeout=min(1,remaining)))
            tag = value.rsplit(':',1)[-1] if isinstance(value,str) and ':' in value else 'unknown'
            result[label] = tag if re.fullmatch(r'v?\d+(?:\.\d+){1,3}(?:-[A-Za-z0-9.]+)?',tag) else 'unversioned_or_digest'
        except (OSError,ValueError,subprocess.TimeoutExpired):
            result[label] = 'unavailable'
    return {'image_tags':result,'scope':'declared_tags_not_verified_running_binary_versions',
            'vulnerability_verdict':'requires_matching_official_advisory'}


def backups(root):
    # File metadata only; do not open archives or inspect native user profiles.
    items=[];scanned=0
    for directory in [Path(root)/'archives/backups',Path(root)/'backups',Path(root)/'diagnostics']:
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in directory.iterdir():
            scanned+=1
            if scanned>1000:
                return {'state':'partial','reason':'entry_limit','restore_test_verified':False}
            if not path.is_symlink() and path.is_file() and path.name.endswith(('.tar.gz','.tgz','.zip','.gz')):
                stat=path.stat();items.append((stat.st_mtime,stat.st_size))
    return {'archive_count':len(items),'total_mib':round(sum(s for _,s in items)/1024**2,2),
            'latest_archive_epoch':max((t for t,_ in items),default=None),
            'scope':'task_owned_archive_metadata_only_not_database_backup_coverage',
            'restore_test_verified':False}
