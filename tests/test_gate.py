import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import pytest
from pcc.isolation import probe,RuntimeIsolationError
from pcc.executor import arguments,usage

@pytest.mark.parametrize('bad',['outside_read','unlisted_read','outside_write','network','allowed_write','allowed_read','input_write'])
def test_real_dispatch_gate_fails_closed(tmp_path,bad):
    for n in ('work','input','result','control'):(tmp_path/n).mkdir()
    states={k:{'success':False,'type':'PermissionError','errno':13} for k in ('outside_read','unlisted_read','outside_write','input_write','network')}
    states.update(allowed_write={'success':True},allowed_read={'success':True})
    states[bad]={'success':False,'type':'PermissionError','errno':13} if bad.startswith('allowed_') else {'success':True}
    class RPC:
        def __init__(self,*a):pass
        def call(self,method,*a,**kw):
            if method=='config/read':return {'result':{'config':{'default_permissions':'pcc-task','sandbox_mode':None,'approval_policy':'on-request'}}}
            return {'result':{'exitCode':0,'stdout':json.dumps(states)}}
        def close(self):pass
    with mock.patch('pcc.isolation.SandboxRPC',RPC):
        with pytest.raises(RuntimeIsolationError):probe(tmp_path,tmp_path/'control','python', ['codex','exec'],{})

def test_gate_no_bypass_argument(tmp_path):
    args=arguments(tmp_path,tmp_path/'control',__import__('sys').executable)
    assert 'approval_policy="on-request"' in args
    assert not any(x.startswith('--dangerously-') or x=='danger-full-access' or x=='approval_policy="never"' for x in args)

def test_windows_junction_rejected(tmp_path):
    import os,subprocess
    from pcc.paths import plain
    if os.name!='nt':pytest.skip('Windows-only junction probe')
    target=tmp_path/'target';target.mkdir();link=tmp_path/'junction'
    env={**os.environ,'PCC_TEST_LINK':str(link),'PCC_TEST_TARGET':str(target)}
    result=subprocess.run(['powershell.exe','-NoProfile','-Command','New-Item -ItemType Junction -Path $env:PCC_TEST_LINK -Target $env:PCC_TEST_TARGET | Out-Null'],env=env,capture_output=True)
    assert result.returncode==0
    try:
        with pytest.raises(ValueError):plain(link)
    finally:
        subprocess.run(['powershell.exe','-NoProfile','-Command','Remove-Item -LiteralPath $env:PCC_TEST_LINK -Force'],env=env,check=True,capture_output=True)
    assert target.is_dir()
