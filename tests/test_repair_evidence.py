import json
from unittest import mock
import pytest
from pcc.isolation import probe,RuntimeIsolationError
from pcc.execution_evidence import ExecutionEvidence
from pcc.controller import Controller
from test_controller import grant,plan,Fake

def test_command_start_denied_and_final_text_retained(tmp_path):
    d=ExecutionEvidence(tmp_path)
    d.record({'type':'item.started','item':{'id':'a','type':'command_execution','command':'synthetic cmd','status':'in_progress'}},1)
    d.record({'type':'item.completed','item':{'id':'a','type':'command_execution','exit_code':1,'aggregated_output':'PermissionError: access denied','status':'failed'}},2)
    d.record({'type':'item.completed','item':{'id':'b','type':'agent_message','text':'blocked by permission'}},3)
    assert d.summary()['command_items_started']==1 and d.summary()['command_items_completed']==1
    assert 'PermissionError' in (tmp_path/'execution-events.jsonl').read_text()
    assert 'blocked' in (tmp_path/'executor-response.md').read_text()

def test_no_tools_is_observation_not_success(tmp_path):
    d=ExecutionEvidence(tmp_path);d.record({'type':'item.completed','item':{'id':'a','type':'agent_message','text':'cannot execute'}},1)
    assert d.summary()['command_items_started']==0
    assert d.summary()['agent_messages_completed']==1

def test_missing_results_after_terminal_is_failed_with_usage(tmp_path):
    c=Controller(tmp_path/'control');v=c.grant(grant(tmp_path/'project'))['version'];r=c.submit('owner','synthetic',v,'synthetic diagnostic',plan())
    class Missing(Fake):
        def run(self,*args):
            x=super().run(*args);(args[2]/'result/summary.json').unlink();return x
    e=Missing();out=c.execute(r['id'],e)
    assert e.calls==1 and out['status']=='FAILED'
    assert 'expected output missing' in (c.root/'runs'/r['run_id']/'failure.json').read_text()
    u=c.result('owner',r['id'])['usage']['roles']['plus_executor']['tokens']
    assert u['observed_partial']['input_plus_output_tokens']==120
    assert u['totals']['input_plus_output_tokens'] is None

@pytest.mark.parametrize('timeout',[False,True])
def test_gate_positive_controls_and_timeout_not_denial(tmp_path,timeout):
    for x in ('input','work','result','control'):(tmp_path/x).mkdir()
    data={k:{'success':False,'type':'PermissionError','errno':13} for k in ('outside_read','unlisted_read','outside_write','input_write','network')}
    data.update(allowed_read={'success':True},allowed_write={'success':True})
    if timeout:data['network']={'success':False,'type':'TimeoutError','errno':10060}
    class RPC:
        def __init__(self,*a):pass
        def close(self):pass
        def call(self,m,*a,**kw):
            if m=='config/read':return {'result':{'config':{'default_permissions':'pcc-task','sandbox_mode':None,'approval_policy':'on-request'}}}
            return {'result':{'exitCode':0,'stdout':json.dumps(data)}}
    with mock.patch('pcc.isolation.SandboxRPC',RPC):
        if timeout:
            with pytest.raises(RuntimeIsolationError):probe(tmp_path,tmp_path/'control','python',['codex','exec'],{})
        else:assert probe(tmp_path,tmp_path/'control','python',['codex','exec'],{})['accepted']

def test_hardlink_is_rejected_without_admin(tmp_path):
    import os
    from pcc.paths import plain
    a=tmp_path/'a';a.write_text('synthetic');b=tmp_path/'b';os.link(a,b)
    with pytest.raises(ValueError):plain(b)
