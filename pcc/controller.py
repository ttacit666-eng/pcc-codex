"""Persistent authority and single-executor task journal, no implicit service startup."""
import datetime as dt
import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from .paths import BASE,plain,inside,save,load

TERMINAL={'LOCAL_CHECK','FAILED','BLOCKED','CANCELLED'}
def now():return dt.datetime.now(dt.timezone.utc).isoformat()

class Controller:
    def __init__(self,state=None):
        self.root=plain(state or BASE/'state');self.root.mkdir(parents=True,exist_ok=True)
        self.dbpath=self.root/'registry.sqlite3'
        with self.db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS grants(project TEXT,version INTEGER,subject TEXT,enabled INTEGER,body TEXT,PRIMARY KEY(project,version));
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,run_id TEXT,project TEXT,version INTEGER,subject TEXT,idem TEXT UNIQUE,status TEXT,body TEXT,created TEXT,updated TEXT,pid INTEGER,cancel INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,time TEXT,task TEXT,event TEXT,body TEXT);
            ''')
    def db(self):
        c=sqlite3.connect(self.dbpath,timeout=15);c.row_factory=sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL');return c
    def audit(self,task,event,body):
        with self.db() as c:c.execute('INSERT INTO audit(time,task,event,body) VALUES(?,?,?,?)',(now(),task,event,json.dumps(body)))
    def grant(self,g):
        # Local administrative command only; never exposed as MCP.
        from .broker import validate_grant
        g=validate_grant(g,self.root)
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            version=c.execute('SELECT COALESCE(MAX(version),0)+1 FROM grants WHERE project=?',(g['project'],)).fetchone()[0]
            c.execute('UPDATE grants SET enabled=0 WHERE project=?',(g['project'],))
            c.execute('INSERT INTO grants VALUES(?,?,?,?,?)',(g['project'],version,g['subject'],1,json.dumps(g)))
        return {'project':g['project'],'version':version}
    def revoke(self,project):
        with self.db() as c:c.execute('UPDATE grants SET enabled=0 WHERE project=?',(project,))
        self.audit(None,'grant_revoked',{'project':project})
    def authorized(self,subject,project,version=None):
        with self.db() as c:
            r=c.execute('SELECT * FROM grants WHERE project=? AND subject=? AND enabled=1 ORDER BY version DESC LIMIT 1',(project,subject)).fetchone()
        if not r or (version is not None and r['version']!=version):raise PermissionError('project grant missing/revoked/version changed')
        return {**json.loads(r['body']),'version':r['version']}
    def capabilities(self,subject):
        with self.db() as c:rows=c.execute('SELECT project,version,body FROM grants WHERE subject=? AND enabled=1',(subject,)).fetchall()
        return {'service':'PCC independent executor','marker':'pcc-implementation-20260916','version':'0.1.0','projects':[{'id':r['project'],'version':r['version'],'actions':json.loads(r['body'])['actions']} for r in rows],
                'execution_modes':['PCC_STRICT','PCC_HOST_TRUSTED'],'mode_selection':'saved local project grant only; HOST_TRUSTED is Full Access, not strict isolation',
                'package_installation':{'kinds':['npm','dsh'],'request_field':'package_installs','selectors':'saved grant IDs only','scripts':'disabled','actor':'broker','activation':'not automatically verified'},'single_plus_executor':True,'external_concurrency':'not excluded','review':'independent conversation; LOCAL_CHECK is not REVIEW'}
    def submit(self,subject,project,version,goal,plan,request_label=''):
        if (self.root/'disabled.flag').exists():raise PermissionError('PCC dispatch paused by local owner')
        g=self.authorized(subject,project,version)
        if not isinstance(goal,str) or len(goal)>16000:raise ValueError('invalid goal')
        if not isinstance(request_label,str) or len(request_label)>128:raise ValueError('invalid request label')
        goal=goal.strip().removeprefix('<进行pcc协作模式>').strip()
        if not goal:return {'status':'MODE_READY','dispatched':False}
        from .broker import validate_plan
        plan=validate_plan(g,plan)
        body={'goal':goal,'plan':plan,'request_label':request_label}
        fingerprint=hashlib.sha256(json.dumps([subject,project,version,body],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT id FROM tasks WHERE idem=?',(fingerprint,)).fetchone()
            if old:return self.status(subject,old['id'])
            live=c.execute("SELECT id FROM tasks WHERE status NOT IN ('LOCAL_CHECK','FAILED','BLOCKED','CANCELLED') LIMIT 1").fetchone()
            if live:raise RuntimeError('PCC account occupied; query existing task '+live['id'])
            task='pcc-'+dt.datetime.now().strftime('%Y%m%d')+'-'+uuid.uuid4().hex[:12];run=uuid.uuid4().hex
            c.execute('INSERT INTO tasks(id,run_id,project,version,subject,idem,status,body,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',(task,run,project,version,subject,fingerprint,'QUEUED',json.dumps(body),now(),now()))
        self.audit(task,'submitted',{'goal_sha256':hashlib.sha256(goal.encode()).hexdigest(),'grant_version':version})
        return self.status(subject,task)
    def row(self,subject,task):
        with self.db() as c:r=c.execute('SELECT * FROM tasks WHERE id=? AND subject=?',(task,subject)).fetchone()
        if not r:raise PermissionError('task not authorized')
        # Revocation blocks subsequent data retrieval too.
        self.authorized(subject,r['project'],r['version'])
        return dict(r)
    def status(self,subject,task):
        r=self.row(subject,task)
        out={k:r[k] for k in ('id','run_id','project','version','status','created','updated','pid','cancel')}
        out['process_observation']='not_started' if not r['pid'] else 'unknown'
        if r['pid'] and os.name=='nt':
            import ctypes
            api=ctypes.WinDLL('kernel32',use_last_error=True)
            api.OpenProcess.restype=ctypes.c_void_p
            api.OpenProcess.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_uint32]
            api.CloseHandle.argtypes=[ctypes.c_void_p]
            handle=api.OpenProcess(0x1000,False,r['pid'])
            if handle:
                out['process_observation']='pid_present_identity_not_revalidated'
                api.CloseHandle(handle)
            else:out['process_observation']='not_observable_or_exited'
        out['recovery_rule']='No automatic reclaim or retry; PID absence alone does not prove all children stopped'
        return out
    def update(self,task,status,pid=None):
        with self.db() as c:c.execute('UPDATE tasks SET status=?,updated=?,pid=COALESCE(?,pid) WHERE id=?',(status,now(),pid,task))
    def claim(self,task):
        if (self.root/'disabled.flag').exists():raise PermissionError('PCC dispatch paused')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            n=c.execute("UPDATE tasks SET status='CLAIMED',updated=? WHERE id=? AND status='QUEUED'",(now(),task)).rowcount
            if n!=1:raise RuntimeError('already claimed; NEVER redispatch')
            return dict(c.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone())
    def checkpoint(self,r):
        self.authorized(r['subject'],r['project'],r['version'])
        with self.db() as c:cancel=c.execute('SELECT cancel FROM tasks WHERE id=?',(r['id'],)).fetchone()[0]
        if cancel:raise InterruptedError('cancel requested')
    def cancel(self,subject,task):
        r=self.row(subject,task)
        with self.db() as c:
            if r['status']=='QUEUED':c.execute("UPDATE tasks SET status='CANCELLED',cancel=1,updated=? WHERE id=?",(now(),task))
            elif r['status'] not in TERMINAL:c.execute('UPDATE tasks SET cancel=1,updated=? WHERE id=?',(now(),task))
        return {**self.status(subject,task),'note':'request only; running process and prior side effects are not undone'}
    def result(self,subject,task,offset=0,limit=20):
        r=self.row(subject,task);d=self.root/'runs'/r['run_id']
        if offset<0 or not 1<=limit<=50:raise ValueError('invalid page')
        data=load(d/'result.json') if (d/'result.json').exists() else {'status':r['status'],'artifacts':[]}
        if (d/'actions.json').exists():data['actions']=load(d/'actions.json')
        items=data.get('artifacts',[]);data={**data,'artifacts':items[offset:offset+limit],'offset':offset,'total':len(items),'truncated':offset+limit<len(items)}
        if (d/'usage.json').exists():data['usage']=load(d/'usage.json')
        data['independent_review']='REVIEW_REQUIRED'
        return data
    def artifact(self,subject,task,path,offset=0,limit=12000):
        r=self.row(subject,task);d=self.root/'runs'/r['run_id']
        manifest=load(d/'result.json').get('artifacts',[])
        entry=next((x for x in manifest if x['path']==path),None)
        if entry is None:raise PermissionError('artifact not in frozen manifest')
        p=inside(d/'artifacts',path);b=p.read_bytes()
        if hashlib.sha256(b).hexdigest()!=entry['sha256']:raise ValueError('artifact changed')
        if offset<0 or not 1<=limit<=24000:raise ValueError('invalid page')
        return {'path':path,'sha256':entry['sha256'],'hash_scope':'full frozen raw bytes','offset':offset,'total_bytes':len(b),'truncated':offset+limit<len(b),'text':b[offset:offset+limit].decode('utf-8',errors='replace')}
    def reconcile(self):
        # Called on explicit worker startup. Unknown ownership remains occupied indefinitely.
        with self.db() as c:c.execute("UPDATE tasks SET status='RECOVERY_REQUIRED' WHERE status IN ('CLAIMED','SAMPLING','RUNNING','BROKER')")
    def execute(self,task,executor=None):
        from .executor import execute
        return execute(self,task,executor)
