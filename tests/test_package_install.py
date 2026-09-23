import json
import sys
from pathlib import Path
import pytest
from pcc.controller import Controller
from pcc.broker import validate_grant,validate_plan,Broker
from pcc.package_install import invocation,InstallUncertain
from pcc.paths import digest,load
from test_controller import grant,plan,Fake

@pytest.fixture
def configured(tmp_path):
    g=grant(tmp_path/'project');g.update(execution_mode='PCC_HOST_TRUSTED',host_trusted_ack=True)
    folder=Path(g['root'])/'profiles/web';folder.mkdir(parents=True)
    (folder/'package.json').write_text('{"dependencies":{}}')
    manager=tmp_path/'fake_manager.py'
    manager.write_text('''import sys,json
from pathlib import Path
p=Path('package.json');data=json.loads(p.read_text())
data['dependencies']['example-package']='1.2.3';p.write_text(json.dumps(data))
m=Path('node_modules/example-package');m.mkdir(parents=True,exist_ok=True)
(m/'package.json').write_text(json.dumps({'name':'example-package','version':'1.2.3'}))
''')
    pin=lambda p:{'path':str(p),'sha256':digest(p)}
    target={'id':'example','kind':'npm','directory':'profiles/web','packages':[{'name':'example-package','version':'1.2.3'}],
            'registry':'https://registry.npmjs.org/','runtime':{'node':pin(Path(sys.executable)),'manager':pin(manager)},'timeout_seconds':10}
    g['write_roots'].append('profiles/web');g['package_installs']=[target]
    c=Controller(tmp_path/'control');v=c.grant(g)['version']
    return c,g,v,target,manager

def test_real_nonmodel_process_install_receipt_and_duplicate(configured):
    c,g,v,t,manager=configured;p={**plan(),'package_installs':['example']}
    r=c.submit('owner','synthetic',v,'synthetic installation',p);fake=Fake()
    result=c.execute(r['id'],fake)
    assert result['status']=='LOCAL_CHECK'
    action=next(a for a in result['actions'] if a['operation']=='package_install')
    assert action['actor']=='broker' and action['activation']=='NOT_VERIFIED'
    assert action['exit_code']==0 and action['lifecycle_scripts'] is False
    run=c.root/'runs'/r['run_id']
    assert (run/'package-installs/example/before/package.json').read_text()=='{"dependencies":{}}'
    assert c.submit('owner','synthetic',v,'synthetic installation',p)['id']==r['id']
    with pytest.raises(RuntimeError):c.execute(r['id'],fake)
    assert fake.calls==1
    assert c.result('owner',r['id'])['usage']['roles']['plus_executor']['tokens']['totals']['input_plus_output_tokens']==120

@pytest.mark.parametrize('field,value',[('name','example;whoami'),('version','latest'),('version','^1.2.3'),('name','https://example.com/pkg')])
def test_unpinned_injection_denied(configured,field,value):
    c,g,v,t,_=configured;t['packages'][0][field]=value
    with pytest.raises(ValueError):validate_grant(g,c.root)

def test_policy_denials_and_legacy_fingerprint(configured):
    c,g,v,t,_=configured
    for request in [['unknown'],['example','example'],[{'id':'example','command':'x'}]]:
        with pytest.raises((PermissionError,ValueError)):validate_plan(g,{'package_installs':request})
    g['delete_roots'].append('profiles/web')
    with pytest.raises(ValueError):validate_plan(g,{'package_installs':['example'],'delete':['profiles/web/package.json']})
    assert validate_plan(g,plan())==validate_plan(g,{**plan(),'package_installs':[]})
    g['execution_mode']='PCC_STRICT'
    with pytest.raises(PermissionError):validate_grant(g,c.root)

def test_runtime_drift_before_spawn(configured):
    c,g,v,t,manager=configured;manager.write_text('raise Exception("changed")')
    with pytest.raises(ValueError,match='hash changed'):validate_grant(g,c.root)

def test_failed_install_retains_usage(configured):
    c,g,v,t,manager=configured;manager.write_text('raise SystemExit(17)');t['runtime']['manager']['sha256']=digest(manager)
    v=c.grant(g)['version'];r=c.submit('owner','synthetic',v,'fail', {**plan(),'package_installs':['example']})
    result=c.execute(r['id'],Fake());assert result['status']=='FAILED'
    failure=next(a for a in result['actions'] if a['operation']=='package_install_failure')
    assert failure['exit_code']==17
    tokens=c.result('owner',r['id'])['usage']['roles']['plus_executor']['tokens']
    assert tokens['observed_partial']['input_plus_output_tokens']==120
    assert 'statistics_incomplete' in tokens['flags']

def test_timeout_keeps_recovery_lock(configured,monkeypatch):
    import pcc.package_install as module
    c,g,v,t,_=configured
    class Running:
        pid=999991
        def poll(self):return None
        def terminate(self):pass
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**kw:Running())
    times=iter([0,100]);monkeypatch.setattr(module.time,'monotonic',lambda:next(times))
    r=c.submit('owner','synthetic',v,'timeout',{**plan(),'package_installs':['example']})
    result=c.execute(r['id'],Fake());assert result['status']=='RECOVERY_REQUIRED'
    with pytest.raises(RuntimeError):c.submit('owner','synthetic',v,'new task',plan())

def test_dsh_command_fixed_flags_and_environment(configured,tmp_path,monkeypatch):
    c,g,v,t,manager=configured;t.update(kind='dsh',profile='web');t['runtime']['dsh']=t['runtime']['manager']
    monkeypatch.setenv('NODE_OPTIONS','bad');monkeypatch.setenv('NPM_TOKEN','synthetic')
    if sys.platform!='win32':pytest.skip('Windows DSH shim')
    args,env=invocation(g,t,tmp_path)
    assert args[2:7]==['plugin','--profile','web','add','--save-exact']
    assert '--ignore-scripts' in args and '--ignore-pnpmfile' in args
    assert env['DSH_HOME']==g['root'] and 'NODE_OPTIONS' not in env and 'NPM_TOKEN' not in env
    assert (tmp_path/'bin/pnpm.cmd').is_file()

def test_npmrc_rejected_without_read(configured):
    c,g,v,t,_=configured;(Path(g['root'])/'profiles/web/.npmrc').write_text('synthetic')
    with pytest.raises(PermissionError,match='npmrc'):validate_grant(g,c.root)

def test_pnpm_hardlink_metadata_read_only(configured):
    import os
    from pcc.package_install import verify_installed
    c,g,v,t,manager=configured
    directory=Path(g['root'])/'profiles/web'
    (directory/'package.json').write_text(json.dumps({'dependencies':{'example-package':'1.2.3'}}))
    metadata=directory/'node_modules/example-package/package.json';metadata.parent.mkdir(parents=True)
    store=directory/'store.json';store.write_text(json.dumps({'name':'example-package','version':'1.2.3'}))
    os.link(store,metadata)
    verify_installed(directory,t['packages'])
    with pytest.raises(ValueError,match='hard-linked'):load(metadata)
    with pytest.raises(ValueError):verify_installed(directory,[{'name':'../escape','version':'1.2.3'}])

def test_explicit_hardlink_recovery(configured,monkeypatch):
    import pcc.package_install as module
    from pcc.install_recovery import recover
    c,g,v,t,manager=configured
    original=module.verify_installed
    def legacy(*args):raise ValueError('hard-linked file rejected')
    monkeypatch.setattr(module,'verify_installed',legacy)
    r=c.submit('owner','synthetic',v,'recover',{**plan(),'package_installs':['example']})
    assert c.execute(r['id'],Fake())['status']=='FAILED'
    monkeypatch.setattr(module,'verify_installed',original)
    result=recover(c,r['id'],'explicit owner authorization')
    assert result['status']=='VERIFIED_INSTALLED'
    assert result['actions'][0]['reinstalled'] is False
    assert c.status('owner',r['id'])['status']=='FAILED'
    assert c.result('owner',r['id'])['install_recovery']['status']=='VERIFIED_INSTALLED'
    with pytest.raises(RuntimeError,match='already attempted'):recover(c,r['id'],'again')
