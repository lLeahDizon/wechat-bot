"""Task-owned monitor state, anonymous samples and durable alert receipts."""
import json
from pathlib import Path
import sqlite3
import time


class Store:
    def __init__(self,path,clock=time.time):
        self.clock=clock
        path=Path(path);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.db=sqlite3.connect(path)
        path.chmod(0o600)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS incidents(code TEXT PRIMARY KEY,active INTEGER,since REAL,
              bad_count INTEGER,clear_count INTEGER,incident TEXT,severity INTEGER,started REAL);
            CREATE TABLE IF NOT EXISTS notifications(key TEXT PRIMARY KEY,incident TEXT,phase TEXT,
              code TEXT,text TEXT,state TEXT,send_id TEXT,server_id TEXT,at REAL);
            CREATE TABLE IF NOT EXISTS samples(at REAL PRIMARY KEY,data TEXT);
        ''')
        self.db.commit()

    def heartbeat(self,state):
        self.meta('heartbeat',{'at':self.clock(),'state':state})

    def meta(self,key,value=None):
        if value is None:
            row=self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
            return json.loads(row[0]) if row else None
        with self.db:self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,json.dumps(value)))

    def enqueue(self,incident,phase,code,text):
        key='bot-alert-'+incident+'-'+phase
        with self.db:self.db.execute('INSERT OR IGNORE INTO notifications VALUES (?,?,?,?,?,\'pending\',NULL,NULL,?)',
            (key,incident,phase,code,text,self.clock()))
        return key

    def pending(self):
        rows=self.db.execute("SELECT * FROM notifications WHERE state IN ('pending','submitting','needs_review') ORDER BY at,key").fetchall()
        selected=[];seen=set()
        for row in rows:
            if row['incident'] not in seen:selected.append(dict(row));seen.add(row['incident'])
            if len(selected)>=4:break
        return selected

    def update_send(self,key,state,send_id=None,server_id=None):
        assert state in {'submitting','accepted','needs_review','superseded'}
        with self.db:self.db.execute('UPDATE notifications SET state=?,send_id=coalesce(?,send_id),server_id=coalesce(?,server_id) WHERE key=?',
            (state,send_id,server_id,key))

    def prune(self):
        cutoff=self.clock()-7*86400
        with self.db:
            self.db.execute('DELETE FROM samples WHERE at<?',(cutoff,))
            self.db.execute("DELETE FROM notifications WHERE at<? AND state IN ('accepted','superseded')",(cutoff,))

    def close(self):self.db.close()
