"""Host-only fixed diagnostics and bounded housekeeping for this deployment."""
from __future__ import annotations

import argparse
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request

TASK = 'sub2api-wechat-group-bot-trial-20261103'
ROOT = Path('/opt/sub2api-wechat-bot')
BOT_CONTAINERS = ['sub2api-wechat-astrbot', 'sub2api-wechat-native']
DATA_DIRS = ['native-data', 'native-scratch', 'astrbot-data', 'archives', 'logs', 'secrets','diagnostics']
GIB = 1024**3


def atomic_json(path: Path, value: dict, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    temporary.chmod(mode)
    temporary.replace(path)


def assert_root(root=ROOT):
    if root.is_symlink() or json.loads((root/'task.json').read_text()).get('task') != TASK:
        raise RuntimeError('task_directory_not_verified')


def measure(root: Path) -> int:
    total, seen = 0, set()
    for name in DATA_DIRS:
        directory = root/name
        if not directory.exists():
            continue
        if directory.is_symlink():
            raise RuntimeError('data_directory_symlink')
        for current, directories, files in os.walk(directory, followlinks=False):
            directories[:] = [d for d in directories if not (Path(current)/d).is_symlink()]
            for name in files:
                path = Path(current)/name
                stat = path.lstat()
                if path.is_symlink():
                    continue
                identity = (stat.st_dev, stat.st_ino)
                if identity not in seen:
                    seen.add(identity)
                    total += stat.st_size
    return total


def space_policy(size: int, free: int, total: int, free_inode_fraction=1.0) -> str:
    if total <= 0 or size >= 2*GIB or free/total < .2 or free_inode_fraction < .05:
        return 'critical'
    return 'warning' if size >= GIB else 'normal'


def inspect_running(name: str) -> bool:
    result = subprocess.run(['docker','inspect','--format','{{.State.Running}}',name],
        capture_output=True,text=True,timeout=10)
    return result.returncode == 0 and result.stdout.strip() == 'true'


def collect(root=ROOT, stop=subprocess.run):
    assert_root(root)
    now = time.time()
    size = measure(root)
    disk = shutil.disk_usage(root)
    fs = os.statvfs(root)
    level = space_policy(size,disk.free,disk.total,fs.f_favail/fs.f_files if fs.f_files else 1)
    paused = root/'admin-evidence/space-paused.json'
    if level == 'critical':
        # Only these two exact task-owned names. No global Docker or Sub2API action.
        result = stop(['docker','stop','--time','30',*BOT_CONTAINERS],capture_output=True,text=True,timeout=75)
        atomic_json(paused, {'at':now,'reason':'storage_protection','stop_exit':result.returncode})
    service = 'unknown'
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:8080/health',timeout=5) as response:
            body=json.loads(response.read(16384))
            service='healthy' if response.status==200 and body.get('status')=='ok' else 'degraded'
    except (OSError,ValueError):
        service='unavailable'
    pressure='unknown'
    try:
        memory={line.split(':',1)[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
        pressure='normal' if memory['MemAvailable']/memory['MemTotal'] >= .15 and disk.free/disk.total >= .2 else 'elevated'
    except (OSError,ValueError,KeyError):
        pass
    # The only host data mounted into the responder: fixed public enums + time.
    public={'schema':1,'captured_at':now,'service':service,'pressure':pressure}
    atomic_json(root/'public/public.json',public,0o644)
    private={'captured_at':now,'bot_data_bytes':size,'disk_free_bytes':disk.free,
        'disk_total_bytes':disk.total,'space_level':level,'paused':paused.exists()}
    atomic_json(root/'admin-evidence/storage-status.json',private)
    return private


def cleanup(root=ROOT, now=None):
    assert_root(root)
    now=time.time() if now is None else now
    removed=[]
    logs=root/'logs'
    for directory in [logs,root/'archives',root/'archives/backups',root/'archives/summaries',root/'diagnostics']:
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise RuntimeError('maintenance_directory_not_verified')
    if logs.exists():
        for path in logs.iterdir():
            if path.is_file() and not path.is_symlink() and re.fullmatch(r'events-\d{4}-\d{2}-\d{2}\.jsonl',path.name):
                if now-path.stat().st_mtime > 7*86400:
                    path.unlink();removed.append('expired_runtime_record')
    archives=root/'archives'
    archives.mkdir(mode=0o700,exist_ok=True)
    backups_dir=archives/'backups'
    summaries_dir=archives/'summaries'
    backups_dir.mkdir(mode=0o700,exist_ok=True)
    summaries_dir.mkdir(mode=0o700,exist_ok=True)
    backups=[]
    for path in backups_dir.iterdir():
        if not path.is_file() or path.is_symlink():
            continue
        if re.fullmatch(r'backup-\d{8}T\d{6}Z\.tar\.gz',path.name):
            backups.append(path)
    for path in summaries_dir.iterdir():
        if not path.is_file() or path.is_symlink():continue
        if re.fullmatch(r'weekly-\d{4}-W\d{2}\.attempt\.json',path.name) and now-path.stat().st_mtime>14*86400:
            path.unlink();removed.append('expired_summary_attempt')
        elif re.fullmatch(r'weekly-\d{4}-W\d{2}\.json',path.name) and now-path.stat().st_mtime > 7*86400:
            # Compress anonymous reviewed-source summaries; never touch profiles.
            target=path.with_suffix('.json.gz')
            try:
                fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            except FileExistsError:
                continue
            source_fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            with os.fdopen(fd,'wb') as raw_dest, os.fdopen(source_fd,'rb') as source:
                with gzip.GzipFile(fileobj=raw_dest,mode='wb') as dest:
                    shutil.copyfileobj(source,dest)
            path.unlink();removed.append('compressed_old_summary')
    backups.sort(key=lambda p:p.stat().st_mtime,reverse=True)
    for position,path in enumerate(backups):
        if position>=2 or now-path.stat().st_mtime>14*86400:
            path.unlink();removed.append('expired_backup')
    # Crash scratch cleanup is allowed only while Native is actually stopped.
    # Normal job/private-temp cleanup belongs to Native itself.
    for name in ['rollback-code.tar.gz','rollback-feedback.tar.gz']:
        rollback=root/'diagnostics'/name
        if rollback.is_file() and not rollback.is_symlink() and now-rollback.stat().st_mtime>14*86400:
            rollback.unlink();removed.append('expired_code_rollback')
    diagnostics=root/'diagnostics'
    if diagnostics.exists() and not diagnostics.is_symlink():
        for rollback in diagnostics.iterdir():
            if (rollback.is_file() and not rollback.is_symlink()
                and re.fullmatch(r'rollback-usage-\d{8}T\d{6}Z\.tar\.gz',rollback.name)
                and now-rollback.stat().st_mtime>14*86400):
                rollback.unlink();removed.append('expired_code_rollback')
    atomic_json(root/'admin-evidence/cleanup-status.json',{'at':now,'actions':removed})
    return removed


def backup(root=ROOT):
    assert_root(root)
    if any(inspect_running(name) for name in BOT_CONTAINERS):
        raise RuntimeError('stop_bot_components_before_backup')
    query_service=subprocess.run(['systemctl','is-active','sub2api-wechat-query.service'],capture_output=True,text=True,timeout=5)
    if query_service.returncode==0:raise RuntimeError('stop_query_component_before_backup')
    status=collect(root)
    if status['space_level']=='critical':
        raise RuntimeError('insufficient_space_for_backup')
    size=measure(root)
    disk=shutil.disk_usage(root)
    # Reserve the uncompressed size before starting: compressed output is not
    # assumed to be small enough to avoid hitting the agreed data guardrail.
    if size*2 >= 2*GIB or disk.free-size < disk.total*.2:
        raise RuntimeError('backup_would_exceed_storage_budget')
    archive=root/'archives/backups'/time.strftime('backup-%Y%m%dT%H%M%SZ.tar.gz',time.gmtime())
    archive.parent.mkdir(mode=0o700,exist_ok=True)
    if archive.exists():raise RuntimeError('backup_name_collision')
    with tarfile.open(archive,'x:gz') as tar:
        for name in ['task.json','config','secrets','native-data','astrbot-data','diagnostics']:
            path=root/name
            if path.exists():tar.add(path,arcname=name,recursive=True)
    archive.chmod(0o600)
    cleanup(root)
    return {'archive':archive.name,'bytes':archive.stat().st_size}


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['collect','daily','backup'])
    action=parser.parse_args().action
    if action=='collect':result=collect()
    elif action=='daily':
        result={'cleanup':cleanup(),'storage':collect()}
    else:result=backup()
    print(json.dumps(result))


if __name__=='__main__':main()
