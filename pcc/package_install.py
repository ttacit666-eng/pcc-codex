"""Pinned local-admin package installs. Application policy, NOT network isolation."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from .paths import plain, inside, allowed, digest, save, load

REGISTRY = 'https://registry.npmjs.org/'
PACKAGE = re.compile(r'(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*')
VERSION = re.compile(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?')
MANIFESTS = ('package.json', 'package-lock.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml')

class InstallUncertain(RuntimeError):
    """A child may remain alive; retain the account lock and never auto-retry."""

def validate_targets(g):
    targets = g.get('package_installs', [])
    if not isinstance(targets, list) or len(targets)>20: raise ValueError('package install bounds')
    ids=set()
    for t in targets:
        if set(t)-{'id','kind','directory','profile','packages','runtime','registry','timeout_seconds'}:
            raise ValueError('unknown package installation field')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', t.get('id','')) or t['id'] in ids:
            raise ValueError('duplicate/invalid package install ID')
        ids.add(t['id'])
        if g.get('execution_mode')!='PCC_HOST_TRUSTED' or not {'install','write','run'}<=set(g['actions']):
            raise PermissionError('package install requires explicit host-trusted install/write/run grant')
        if t.get('kind') not in ('npm','dsh'): raise ValueError('unsupported package manager')
        directory=inside(g['root'], t['directory'])
        if not directory.is_dir() or not allowed(t['directory'],g['write_roots']):
            raise PermissionError('install directory outside write grant')
        if t.get('kind')=='dsh':
            profile=t.get('profile','')
            if not re.fullmatch(r'[a-zA-Z0-9_-]+',profile) or t['directory']!='profiles/'+profile:
                raise ValueError('DSH profile path mismatch')
        if t.get('registry')!=REGISTRY: raise ValueError('only public npm registry authorized')
        if type(t.get('timeout_seconds',300)) is not int or not 10<=t.get('timeout_seconds',300)<=900:
            raise ValueError('installation timeout bounds')
        packages=t.get('packages',[])
        if not isinstance(packages,list) or not 1<=len(packages)<=10:raise ValueError('package count bounds')
        names=set()
        for p in packages:
            if set(p)!={'name','version'} or not PACKAGE.fullmatch(p['name']) or not VERSION.fullmatch(p['version']):
                raise ValueError('exact registry package version required')
            if p['name'] in names:raise ValueError('duplicate package')
            names.add(p['name'])
        rt=t.get('runtime',{})
        required={'node','manager'}|({'dsh'} if t['kind']=='dsh' else set())
        if set(rt)!=required:raise ValueError('pinned runtime required')
        for name,item in rt.items():
            if set(item)!={'path','sha256'}:raise ValueError('invalid runtime pin')
            path=plain(item['path'])
            if not Path(item['path']).is_absolute() or not path.is_file() or re.search(r'[\r\n%&|<>^!"]',str(path)):
                raise ValueError('invalid runtime path')
            if digest(path)!=item['sha256']:raise ValueError('runtime hash changed')
        # Do not read potentially secret project npmrc; do not silently inherit it.
        if (directory/'.npmrc').exists():raise PermissionError('project npmrc requires separate review')
        if not inside(directory,'package.json').is_file():raise ValueError('existing project manifest required')
    return targets

def validate_requests(g, plan):
    requests=plan.get('package_installs',[])
    if not isinstance(requests,list) or any(not isinstance(x,str) for x in requests):raise ValueError('only approved install IDs accepted')
    if len(set(requests))!=len(requests):raise ValueError('duplicate installation request')
    targets={t['id']:t for t in g.get('package_installs',[])}
    for ident in requests:
        if ident not in targets or 'install' not in g['actions']:raise PermissionError('package install ID not granted')
        target=targets[ident]['directory']
        for rel in plan.get('delete',[])+[x['destination'] for x in plan.get('publish',[])]:
            if rel==target or rel.startswith(target+'/'):raise ValueError('install conflicts with publish/delete')

def invocation(g,t,folder):
    rt=t['runtime'];node=rt['node']['path'];manager=rt['manager']['path']
    empty=folder/'empty.npmrc';empty.write_text('',encoding='utf-8')
    env={k:v for k,v in os.environ.items() if not k.upper().startswith(('NPM_','PNPM_','NODE_','DSH_')) and not any(x in k.upper() for x in ('TOKEN','API_KEY','SECRET'))}
    env.update(NPM_CONFIG_USERCONFIG=str(empty),NPM_CONFIG_GLOBALCONFIG=str(empty),NPM_CONFIG_IGNORE_SCRIPTS='true',NPM_CONFIG_REGISTRY=REGISTRY,CI='true')
    specs=[p['name']+'@'+p['version'] for p in t['packages']]
    if t['kind']=='npm':
        args=[node,manager,'install','--save-exact','--ignore-scripts','--no-audit','--no-fund','--registry='+REGISTRY,*specs]
        env['NPM_CONFIG_CACHE']=str(folder/'cache')
    else:
        if os.name!='nt':raise ValueError('DSH adapter currently supports Windows only')
        shim=folder/'bin';shim.mkdir()
        (shim/'pnpm.cmd').write_text('@echo off\r\n"'+node+'" "'+manager+'" %*\r\n',encoding='utf-8')
        env['PATH']=str(shim)+os.pathsep+str(Path(node).parent)+os.pathsep+env.get('PATH','')
        env['DSH_HOME']=g['root']
        args=[node,rt['dsh']['path'],'plugin','--profile',t['profile'],'add','--save-exact','--ignore-scripts','--ignore-pnpmfile','--registry='+REGISTRY,*specs]
    return args,env

def install(broker,plan):
    requests=plan.get('package_installs',[])
    if not requests:return
    validate_targets(broker.g);validate_requests(broker.g,plan)
    for ident in requests:
        broker.checkpoint()
        # Atomic intent exists even after failure. No same-run retries.
        folder=broker.run/'package-installs'/ident;folder.mkdir(parents=True,exist_ok=False)
        t=next(t for t in broker.g['package_installs'] if t['id']==ident)
        directory=inside(broker.root,t['directory']);before={}
        for name in MANIFESTS:
            src=inside(directory,name)
            before[name]=digest(src) if src.exists() else None
            if src.exists():
                dest=folder/'before'/name;dest.parent.mkdir(exist_ok=True);shutil.copyfile(src,dest)
        save(folder/'before.json',before)
        args,env=invocation(broker.g,t,folder)
        for name,sha in before.items():
            p=inside(directory,name)
            if (digest(p) if p.exists() else None)!=sha:raise RuntimeError('external manifest conflict')
        broker.record('package_install_intent',{'id':ident,'actor':'broker','packages':t['packages'],'directory':t['directory'],'registry':REGISTRY,'lifecycle_scripts':False,'automatic_retry':False})
        save(folder/'invocation.json',{'argv':args,'cwd':str(directory),'shell':False,'environment_values_recorded':False})
        proc=None
        try:
            proc=subprocess.Popen(args,cwd=directory,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,shell=False)
            save(folder/'process.json',{'pid':proc.pid,'status':'RUNNING'})
            deadline=time.monotonic()+t.get('timeout_seconds',300)
            while proc.poll() is None:
                broker.checkpoint()
                if time.monotonic()>deadline:raise TimeoutError('installation timeout; child tree unknown')
                time.sleep(.1)
            if proc.returncode!=0:raise RuntimeError('package manager exit code '+str(proc.returncode))
            broker.checkpoint()
            manifest=load(inside(directory,'package.json'))
            for p in t['packages']:
                if manifest.get('dependencies',{}).get(p['name'])!=p['version']:
                    raise ValueError('saved package version mismatch')
                # pnpm uses links; resolve only this approved package metadata, never arbitrary files.
                metadata=directory/'node_modules'/p['name']/'package.json'
                resolved=metadata.resolve(strict=True)
                if directory.resolve() not in resolved.parents:raise ValueError('installed metadata outside profile')
                installed=load(resolved)
                if installed.get('name')!=p['name'] or installed.get('version')!=p['version']:
                    raise ValueError('installed package identity mismatch')
            after={name:digest(inside(directory,name)) if (directory/name).exists() else None for name in MANIFESTS}
            result={'id':ident,'actor':'broker','status':'VERIFIED_INSTALLED','exit_code':proc.returncode,'packages':t['packages'],'before':before,'after':after,'activation':'NOT_VERIFIED','lifecycle_scripts':False}
            save(folder/'result.json',result);broker.record('package_install',result)
        except BaseException as error:
            uncertain=proc is not None and proc.poll() is None
            if uncertain:
                try:proc.terminate()
                except OSError:pass
            result={'id':ident,'actor':'broker','status':'RECOVERY_REQUIRED' if uncertain else 'FAILED','exit_code':proc.poll() if proc else None,'error_type':type(error).__name__,'message':str(error)[:200],'rollback':'manual review; backups retained, no reinstall or automatic restore'}
            save(folder/'result.json',result);broker.record('package_install_failure',result)
            if uncertain:raise InstallUncertain('installer child ownership unknown; no retry') from error
            raise
