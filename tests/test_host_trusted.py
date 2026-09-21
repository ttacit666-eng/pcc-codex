import json
from unittest import mock
import pytest
from pcc.controller import Controller
from pcc.host_trusted import HostTrustedExecutor
from pcc.broker import validate_grant
from pcc.executor import LiveExecutor
from test_controller import grant,plan,sample,Fake


def test_mode_requires_explicit_local_ack(tmp_path):
    g=grant(tmp_path/'project');g['execution_mode']='PCC_HOST_TRUSTED'
    with pytest.raises(PermissionError):validate_grant(g,tmp_path/'control')
    g['host_trusted_ack']=True
    assert validate_grant(g,tmp_path/'control')['execution_mode']=='PCC_HOST_TRUSTED'
    g['execution_mode']='PCC_UNSAFE_TYPO'
    with pytest.raises(ValueError):validate_grant(g,tmp_path/'control')


def test_existing_grant_remains_strict(tmp_path):
    assert validate_grant(grant(tmp_path/'project'),tmp_path/'control')['execution_mode']=='PCC_STRICT'


def test_full_access_argv_does_not_mix_legacy_or_skip_rules(tmp_path):
    e=HostTrustedExecutor();argv=e.arguments(tmp_path,tmp_path/'control','python')
    text=' '.join(argv)
    assert 'default_permissions=":danger-full-access"' in argv
    assert 'approval_policy="never"' in argv
    for forbidden in ('sandbox_mode=', '--sandbox', '--dangerously-bypass', '--ignore-rules', 'permissions.pcc-task'):
        assert forbidden not in text
    assert e.permission_metadata()['execution_mode']=='PCC_HOST_TRUSTED'


def test_saved_mode_selects_executor_without_strict_gate(tmp_path):
    c=Controller(tmp_path/'control');g=grant(tmp_path/'project')
    g.update(execution_mode='PCC_HOST_TRUSTED',host_trusted_ack=True)
    v=c.grant(g)['version'];r=c.submit('owner','synthetic',v,'synthetic host mode selection',plan())
    fake=Fake()
    with mock.patch('pcc.isolation.probe',side_effect=AssertionError('strict gate called')) as strict, \
         mock.patch.object(HostTrustedExecutor,'preflight',return_value={}) as gate, \
         mock.patch.object(HostTrustedExecutor,'version',return_value='synthetic'), \
         mock.patch.object(HostTrustedExecutor,'sample',return_value=sample()), \
         mock.patch.object(HostTrustedExecutor,'run',side_effect=fake.run):
        out=c.execute(r['id'])
    assert out['status']=='LOCAL_CHECK' and out['execution_mode']=='PCC_HOST_TRUSTED'
    assert fake.calls==1 and gate.call_count==1 and strict.call_count==0
    with pytest.raises(RuntimeError):c.execute(r['id'])


def test_submit_cannot_override_mode(tmp_path):
    c=Controller(tmp_path/'control');v=c.grant(grant(tmp_path/'project'))['version']
    with pytest.raises(ValueError):c.submit('owner','synthetic',v,'x',{**plan(),'execution_mode':'PCC_HOST_TRUSTED'})


def test_preflight_rejects_mixed_or_managed_settings(tmp_path):
    c=Controller(tmp_path/'control');g=grant(tmp_path/'project')
    g.update(execution_mode='PCC_HOST_TRUSTED',host_trusted_ack=True)
    v=c.grant(g)['version'];row={'subject':'owner','project':'synthetic','version':v}
    class RPC:
        def __init__(self,*a):pass
        def close(self):pass
        def call(self,*a,**kw):
            return {'result':{'config':{'default_permissions':':danger-full-access','sandbox_mode':'read-only',
                    'approval_policy':'never','forced_login_method':'chatgpt','cli_auth_credentials_store':'file'}}}
    with mock.patch('pcc.host_trusted.SandboxRPC',RPC):
        with pytest.raises(PermissionError):HostTrustedExecutor().preflight(c,row,tmp_path,tmp_path,'python')
