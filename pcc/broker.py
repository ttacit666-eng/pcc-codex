"""Authorized broker operations. HOST_TRUSTED task bounds are not an OS sandbox."""
import fnmatch
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile
from urllib.parse import urlsplit
from .paths import plain,inside,allowed,digest,save,load

def validate_grant(g,state):
    g=json.loads(json.dumps(g));root=plain(g['root'])
    mode=g.setdefault('execution_mode','PCC_STRICT')
    if mode not in ('PCC_STRICT','PCC_HOST_TRUSTED'):raise ValueError('unsupported execution mode')
    if mode=='PCC_HOST_TRUSTED' and g.get('host_trusted_ack') is not True:
        raise PermissionError('local grant must explicitly acknowledge Full Access without strict isolation')
    if not root.is_dir() or root==Path.home() or root==Path(root.anchor) or root in Path.home().parents:
        raise ValueError('unsafe project root')
    from .runtime import settings
    runtime=settings()
    protected=[Path(runtime['controller_home']),Path(runtime['plus_home']),plain(state)]
    if any(root==p or root in p.parents or p in root.parents for p in protected):raise ValueError('protected root overlap')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',g['project']) or not g.get('subject'):raise ValueError('invalid identity')
    if not set(g['actions']) <= {'read','write','run','delete','install','upload','download'}:raise ValueError('unsupported action')
    if not g.get('external_idle_ack'):raise ValueError('external executors must be separately excluded by project owner')
    for key in ('read_roots','write_roots','delete_roots'):
        for rel in g.setdefault(key,[]):inside(root,rel)
    if not 10<=g.get('timeout_seconds',120)<=1800:raise ValueError('timeout out of range')
    for w in g.setdefault('wheels',[]):
        path=inside(root,w['path'])
        if path.suffix!='.whl' or not re.fullmatch('[0-9a-f]{64}',w['sha256']):raise ValueError('only pinned offline wheel permitted')
        if digest(path)!=w['sha256']:raise ValueError('wheel hash mismatch')
        check_wheel(path)
    for target in g.setdefault('upload_targets',[]):
        u=urlsplit(target['url'])
        local=g.get('synthetic',False) and u.hostname=='127.0.0.1' and u.scheme=='http'
        if not (local or u.scheme=='https') or u.username or u.password or u.fragment or u.query or not u.hostname:
            raise ValueError('exact HTTPS target required (synthetic loopback exception)')
        if not target.get('artifact_patterns') or not 1<=target.get('max_bytes',0)<=10000000:raise ValueError('upload bounds required')
    for target in g.setdefault('download_targets',[]):
        u=urlsplit(target['url'])
        local=g.get('synthetic',False) and u.hostname=='127.0.0.1' and u.scheme=='http'
        if not (local or u.scheme=='https') or u.username or u.password or u.fragment or u.query or not u.hostname:
            raise ValueError('exact HTTPS download target required')
        if not 1<=target.get('max_bytes',0)<=10000000:raise ValueError('download bounds required')
        if target.get('sha256') is not None and not re.fullmatch('[0-9a-f]{64}',target['sha256']):raise ValueError('invalid download hash')
    if len({x['id'] for x in g['download_targets']})!=len(g['download_targets']):raise ValueError('duplicate download target')
    g['root']=str(root)
    from .package_install import validate_targets
    validate_targets(g)
    return g

def check_wheel(path):
    with zipfile.ZipFile(path) as archive:
        members=archive.infolist()
        if len(members)>10000 or sum(x.file_size for x in members)>100000000:raise ValueError('wheel expansion quota')
        for info in members:
            name=info.filename;parts=Path(name).parts
            if name.startswith(('/','\\')) or '..' in parts or '\\' in name or ':' in name or name.lower().endswith('.pth') or any(x.lower() in ('sitecustomize.py','usercustomize.py') for x in parts):
                raise ValueError('wheel startup hook or unsafe path rejected')

def validate_plan(g,p):
    if not isinstance(p,dict) or set(p)-{'read_files','publish','delete','dependencies','uploads','downloads','expected_outputs','package_installs'}:raise ValueError('invalid plan')
    p=json.loads(json.dumps(p))
    for key in ('read_files','publish','delete','dependencies','uploads','expected_outputs'):p.setdefault(key,[])
    # Preserve legacy task fingerprints when the optional extension is unused.
    if p.get('downloads')==[]:p.pop('downloads')
    if p.get('package_installs')==[]:p.pop('package_installs')
    if any(not isinstance(v,list) or len(v)>50 for v in p.values()):raise ValueError('plan bounds')
    root=Path(g['root'])
    def require(action):
        if action not in g['actions']:raise PermissionError('action not granted: '+action)
    require('run')
    for rel in p['read_files']:
        require('read');q=inside(root,rel)
        if not allowed(rel,g['read_roots']) or not q.is_file() or q.stat().st_size>2000000:raise PermissionError('input outside grant/size limit')
    destinations=[]
    for item in p['publish']:
        require('write');inside(root,item['destination']);inside(root,item['artifact'])
        if not allowed(item['destination'],g['write_roots']):raise PermissionError('write outside grant')
        destinations.append(item['destination'].casefold())
    if len(set(destinations))!=len(destinations):raise ValueError('duplicate write destinations')
    for rel in p['delete']:
        require('delete');inside(root,rel)
        if not allowed(rel,g['delete_roots']):raise PermissionError('delete outside regenerative roots')
        if rel.casefold() in destinations:raise ValueError('write/delete conflict')
    for name in p['dependencies']:
        require('install')
        if not any(w['name']==name for w in g['wheels']):raise PermissionError('unapproved offline dependency')
    for item in p['uploads']:
        require('upload');inside(root,item['artifact'])
        t=next((x for x in g['upload_targets'] if x['id']==item['target']),None)
        if not t or not any(fnmatch.fnmatchcase(item['artifact'],pat) for pat in t['artifact_patterns']):raise PermissionError('upload target/artifact not granted')
    for rel in p['expected_outputs']:inside(root,rel)
    input_names={rel.casefold() for rel in p['read_files']}
    for item in p.get('downloads',[]):
        require('download');inside(root,item['destination'])
        if not any(t['id']==item['target'] for t in g.get('download_targets',[])):raise PermissionError('download target not granted')
        key=item['destination'].casefold()
        if key in input_names:raise ValueError('duplicate frozen input destination')
        input_names.add(key)
    from .package_install import validate_requests
    validate_requests(g,p)
    return p

class Broker:
    def __init__(self,controller,row,grant,run):
        self.c=controller;self.r=row;self.g=grant;self.run=run;self.root=plain(grant['root'])
        self.actions=load(run/'actions.json') if (run/'actions.json').exists() else [];self.before={}
    def checkpoint(self):self.c.checkpoint(self.r)
    def freeze_inputs(self,job,plan):
        items=[]
        for rel in plan['read_files']:
            self.checkpoint();src=inside(self.root,rel);dest=inside(job/'input',rel)
            dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dest)
            items.append({'path':rel,'sha256':digest(dest),'bytes':dest.stat().st_size})
        for item in plan.get('downloads',[]):
            self.checkpoint();target=next(t for t in self.g['download_targets'] if t['id']==item['target'])
            dest=inside(job/'input',item['destination'])
            if dest.exists():raise FileExistsError('download cannot overwrite frozen input')
            u=urlsplit(target['url']);cls=http.client.HTTPSConnection if u.scheme=='https' else http.client.HTTPConnection
            conn=cls(u.hostname,u.port,timeout=20)
            self.record('download_intent',{'target':target['id'],'destination':item['destination'],'retry':False})
            try:
                conn.request('GET',u.path or '/')
                response=conn.getresponse()
                if response.status!=200:raise RuntimeError('download status rejected; no redirect/retry')
                body=response.read(target['max_bytes']+1)
                if len(body)>target['max_bytes']:raise ValueError('download size exceeded')
                sha=hashlib.sha256(body).hexdigest()
                if target.get('sha256') and target['sha256']!=sha:raise ValueError('download hash mismatch')
            finally:conn.close()
            self.checkpoint();dest.parent.mkdir(parents=True,exist_ok=True)
            with dest.open('xb') as stream:stream.write(body)
            items.append({'path':item['destination'],'sha256':sha,'bytes':len(body)})
            self.record('download',{'target':target['id'],'destination':item['destination'],'sha256':sha,'bytes':len(body)})
        for rel in [x['destination'] for x in plan['publish']]+plan['delete']:
            p=inside(self.root,rel)
            if p.exists() and not p.is_file():raise ValueError('broker only handles explicit files, not recursive deletion')
            self.before[rel]=digest(p) if p.exists() else None
        save(self.run/'inputs.json',items);return items
    def install(self,plan):
        if not plan['dependencies']:return None
        self.checkpoint();venv=self.run/'project-env'
        subprocess.run([sys.executable,'-m','venv',str(venv)],check=True,timeout=90,capture_output=True)
        py=venv/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        for name in plan['dependencies']:
            self.checkpoint();w=next(x for x in self.g['wheels'] if x['name']==name);path=inside(self.root,w['path'])
            if digest(path)!=w['sha256']:raise ValueError('wheel changed')
            # Wheel ZIP is copied into a controller-owned directory before installation.
            pinned=self.run/'wheels'/path.name;pinned.parent.mkdir(exist_ok=True);shutil.copyfile(path,pinned)
            if digest(pinned)!=w['sha256']:raise ValueError('wheel copy mismatch')
            check_wheel(pinned)
            subprocess.run([str(py),'-m','pip','--isolated','install','--no-index','--no-deps','--disable-pip-version-check',str(pinned)],check=True,timeout=90,capture_output=True)
            self.record('install',{'name':name,'wheel_sha256':w['sha256'],'scope':'per-task project environment'})
        return py
    def record(self,kind,data):
        self.actions.append({'operation':kind,**data});save(self.run/'actions.json',self.actions)
        self.c.audit(self.r['id'],kind,data)
    def freeze_outputs(self,job):
        out=[];total=0
        for parent,dirs,files in os.walk(job/'result',followlinks=False):
            for d in dirs:plain(Path(parent)/d)
            for f in files:
                src=plain(Path(parent)/f);rel=src.relative_to(job/'result').as_posix()
                total+=src.stat().st_size
                if len(out)>=100 or total>10000000:raise ValueError('output quota exceeded')
                dest=inside(self.run/'artifacts',rel);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dest)
                out.append({'path':rel,'sha256':digest(dest),'bytes':dest.stat().st_size})
        return out
    def check_unchanged(self,rel):
        p=inside(self.root,rel)
        if (digest(p) if p.exists() else None)!=self.before[rel]:raise RuntimeError('external write conflict; no overwrite')
        return p
    def apply(self,plan):
        from .package_install import install
        install(self,plan)
        for item in plan['publish']:
            self.checkpoint();src=inside(self.run/'artifacts',item['artifact']);dest=self.check_unchanged(item['destination'])
            if dest.exists():
                backup=inside(self.run/'rollback',item['destination']);backup.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(dest,backup)
            dest.parent.mkdir(parents=True,exist_ok=True)
            temp=dest.with_name('.pcc-'+self.r['run_id']+'.tmp')
            with temp.open('xb') as f:f.write(src.read_bytes());f.flush();os.fsync(f.fileno())
            self.check_unchanged(item['destination']);os.replace(temp,dest)
            self.record('publish',{'path':item['destination'],'sha256':digest(dest),'before_sha256':self.before[item['destination']]})
        for rel in plan['delete']:
            self.checkpoint();src=self.check_unchanged(rel)
            if not src.exists():raise FileNotFoundError('delete object absent')
            quarantine=inside(self.run/'quarantine',rel);quarantine.parent.mkdir(parents=True,exist_ok=True)
            # Same local controller volume is required, no recursive shell delete.
            if src.drive.lower()!=quarantine.drive.lower():raise ValueError('cross-volume quarantine unsupported')
            os.rename(src,quarantine)
            self.record('quarantine',{'path':rel,'sha256':digest(quarantine),'restorable':True})
        for item in plan['uploads']:
            self.checkpoint();t=next(x for x in self.g['upload_targets'] if x['id']==item['target'])
            src=inside(self.run/'artifacts',item['artifact']);payload=src.read_bytes()
            if len(payload)>t['max_bytes']:raise ValueError('upload size limit')
            u=urlsplit(t['url']);cls=http.client.HTTPSConnection if u.scheme=='https' else http.client.HTTPConnection
            conn=cls(u.hostname,u.port,timeout=20)
            # No redirect, proxy, arbitrary method/header, secret or destination from model.
            self.record('upload_intent',{'target':t['id'],'artifact':item['artifact'],'sha256':hashlib.sha256(payload).hexdigest(),'retry':False})
            try:
                conn.request('PUT',u.path or '/',body=payload,headers={'Content-Type':'application/octet-stream','X-PCC-Task':self.r['id']})
                resp=conn.getresponse();status=resp.status;resp.read(4096)
                if not 200<=status<300:raise RuntimeError('upload response rejected; no retry')
            finally:conn.close()
            self.record('upload',{'target':t['id'],'artifact':item['artifact'],'status':status,'sha256':hashlib.sha256(payload).hexdigest()})
    def restore(self,rel):
        self.checkpoint();dest=inside(self.root,rel);src=inside(self.run/'quarantine',rel)
        if dest.exists():raise FileExistsError('restore never overwrites')
        if not allowed(rel,self.g['delete_roots']):raise PermissionError('not restorable')
        dest.parent.mkdir(parents=True,exist_ok=True);os.rename(src,dest);self.record('restore',{'path':rel,'sha256':digest(dest)})
