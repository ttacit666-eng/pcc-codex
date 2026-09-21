"""SIMULATION ONLY for guest responses; filesystem, registry and receipt code run locally."""
import base64
import hashlib
import json
from copy import deepcopy
import pytest

from pcc.controller import Controller
from pcc.isolated_executor import IsolatedExecutor, guest_contract
from pcc.paths import BASE, load
from test_controller import grant, plan, sample


def output(path='summary.json', data=b'{"count":4,"sum":100,"mean":25}'):
    return {'path': path, 'data_b64': base64.b64encode(data).decode(),
            'sha256': hashlib.sha256(data).hexdigest()}


class SimulatedGuest:
    is_simulation = True
    calls = 0
    def preflight(self, contract):
        return {'binding': {k: contract[k] for k in ('task_id','iteration','run_id')},
                'policy_sha256': contract['policy_sha256'],
                'checks': {k: True for k in ('allowed_read','allowed_write','outside_read_denied',
                    'input_write_denied','outside_write_denied','host_network_denied',
                    'guest_loopback_denied','credential_mount_absent')}}
    def sample(self):
        return sample()
    def execute(self, request):
        self.calls += 1
        self.request = request
        return {'binding': {k: request[k] for k in ('task_id','iteration','run_id')},
                'state': 'terminal', 'exit_code': 0, 'outputs': [output()],
                'events': [{'type':'thread.started','thread_id':'simulated-only'},
                    {'type':'turn.started'},
                    {'type':'item.completed','item':{'type':'agent_message','id':'a','text':'simulated result'}},
                    {'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':80,'output_tokens':20}}]}


def setup_task(tmp_path, guest=None):
    c=Controller(tmp_path/'control')
    v=c.grant(grant(tmp_path/'project'))['version']
    r=c.submit('owner','synthetic',v,'synthetic adapter test',plan())
    g=guest or SimulatedGuest()
    return c,r,g,IsolatedExecutor(simulation_transport=g)


def test_live_backend_always_blocked_even_json_enabled(tmp_path):
    c,r,g,e=setup_task(tmp_path)
    cfg=deepcopy(e.config);cfg['live_enabled']=True
    out=c.execute(r['id'],IsolatedExecutor(cfg))
    assert out['status']=='BLOCKED' and g.calls==0
    d=c.root/'runs'/r['run_id']
    assert load(d/'guest-preflight.json')['guest_test']=='NOT_RUN'
    assert load(d/'samples-after.json')=={}


def test_simulated_result_reuses_registry_broker_receipt(tmp_path):
    c,r,g,e=setup_task(tmp_path)
    out=c.execute(r['id'],e)
    assert out['status']=='LOCAL_CHECK' and g.calls==1
    assert json.loads((tmp_path/'project/results/summary.json').read_bytes())['sum']==100
    u=c.result('owner',r['id'])['usage']
    assert u['source_kind']=='synthetic'
    assert u['roles']['plus_executor']['tokens']['totals']['input_plus_output_tokens']==120
    assert c.result('owner',r['id'])['independent_review']=='REVIEW_REQUIRED'
    assert g.request['env']['CODEX_HOME']=='/var/lib/pcc/auth/plus'
    assert g.request['inputs'][0]['path']=='input/data.csv'
    with pytest.raises(RuntimeError):c.execute(r['id'],e)
    assert g.calls==1


@pytest.mark.parametrize('kind',['timeout','wrong_binding','unknown_state','malformed'])
def test_uncertain_transport_keeps_task_occupied(tmp_path,kind):
    class Guest(SimulatedGuest):
        def execute(self,request):
            result=super().execute(request)
            if kind=='timeout':raise TimeoutError('simulated lost response')
            if kind=='malformed':return None
            if kind=='wrong_binding':result['binding']['run_id']='0'*32
            else:result['state']='unknown'
            return result
    c,r,g,e=setup_task(tmp_path,Guest())
    assert c.execute(r['id'],e)['status']=='RECOVERY_REQUIRED'
    with pytest.raises(RuntimeError):c.submit('owner','synthetic',r['version'],'another task',plan())
    with pytest.raises(RuntimeError):c.execute(r['id'],e)
    assert g.calls==1


@pytest.mark.parametrize('kind',['traversal','hash','duplicate','missing','unexpected','exit_failure'])
def test_invalid_or_missing_output_retains_observed_usage(tmp_path,kind):
    class Guest(SimulatedGuest):
        def execute(self,request):
            result=super().execute(request)
            if kind=='traversal':result['outputs']=[output('../outside')]
            if kind=='hash':result['outputs'][0]['sha256']='0'*64
            if kind=='duplicate':result['outputs']*=2
            if kind=='missing':result['outputs']=[]
            if kind=='unexpected':result['outputs']=[output('unapproved.txt')]
            if kind=='exit_failure':result['exit_code']=1
            return result
    c,r,g,e=setup_task(tmp_path,Guest())
    assert c.execute(r['id'],e)['status']=='FAILED'
    tokens=c.result('owner',r['id'])['usage']['roles']['plus_executor']['tokens']
    assert tokens['observed_partial']['input_plus_output_tokens']==120
    assert tokens['totals']['input_plus_output_tokens'] is None
    assert not (tmp_path/'project/results/summary.json').exists()
    assert g.calls==1


@pytest.mark.parametrize('kind',['missing_check','policy','binding','not_simulation'])
def test_invalid_guest_gate_prevents_dispatch(tmp_path,kind):
    class Guest(SimulatedGuest):
        is_simulation=kind!='not_simulation'
        def preflight(self,contract):
            result=super().preflight(contract)
            if kind=='missing_check':del result['checks']['outside_read_denied']
            if kind=='policy':result['policy_sha256']='invalid'
            if kind=='binding':result['binding']['task_id']='different'
            return result
    c,r,g,e=setup_task(tmp_path,Guest())
    assert c.execute(r['id'],e)['status']=='BLOCKED'
    assert g.calls==0


def test_candidate_contract_has_no_host_paths_or_unsafe_flags():
    contract=guest_contract({'id':'pcc-test','run_id':'a'*32},['summary.json'],load(BASE/'isolation/backend.json'))
    assert contract['policy']['network']['enabled'] is False
    assert contract['policy']['filesystem'][':root']=='deny'
    serialized=json.dumps(contract)
    for forbidden in ('C:', 'danger-full-access', '--yolo', 'approval_policy=never'):
        assert forbidden not in serialized
    assert contract['status']=='CANDIDATE_NOT_GUEST_VERIFIED'


def test_non_synthetic_grant_cannot_use_simulator(tmp_path):
    c,r,g,e=setup_task(tmp_path)
    original=c.authorized
    c.authorized=lambda *a,**kw:{**original(*a,**kw),'synthetic':False}
    assert c.execute(r['id'],e)['status']=='BLOCKED'
    assert g.calls==0
