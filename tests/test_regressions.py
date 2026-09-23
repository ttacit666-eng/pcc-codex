import json
from pathlib import Path
from unittest import mock
import zipfile
import pytest
from pcc.controller import Controller
from pcc.broker import check_wheel
from pcc.executor import LiveExecutor,usage
from pcc.isolation import RuntimeIsolationError
from test_controller import grant,plan,Fake,sample

def test_pause_preserves_existing_query(tmp_path):
    c=Controller(tmp_path/'control');g=grant(tmp_path/'project');v=c.grant(g)['version'];r=c.submit('owner','synthetic',v,'x',plan())
    (c.root/'disabled.flag').write_text('paused')
    with pytest.raises(PermissionError):c.submit('owner','synthetic',v,'new',plan())
    with pytest.raises(PermissionError):c.claim(r['id'])
    assert c.status('owner',r['id'])['status']=='QUEUED'

def test_isolation_failure_never_calls_model(tmp_path):
    c=Controller(tmp_path/'control');v=c.grant(grant(tmp_path/'project'))['version'];r=c.submit('owner','synthetic',v,'x',plan())
    e=LiveExecutor()
    with mock.patch.object(e,'preflight',side_effect=RuntimeIsolationError('probe rejected')),mock.patch.object(e,'sample',return_value=sample()),mock.patch.object(e,'run') as spawn:
        c.execute(r['id'],e);spawn.assert_not_called()
    assert c.status('owner',r['id'])['status']=='BLOCKED'
    assert c.result('owner',r['id'])['usage']['roles']['plus_executor']['tokens']['totals']['input_tokens'] is None

@pytest.mark.parametrize('name',['evil.pth','../outside.py','sitecustomize.py'])
def test_wheel_startup_or_escape_rejected(tmp_path,name):
    p=tmp_path/'evil.whl'
    with zipfile.ZipFile(p,'w') as z:z.writestr(name,'unsafe')
    with pytest.raises(ValueError):check_wheel(p)

def test_revoked_before_execution_does_not_raise_or_spawn(tmp_path):
    c=Controller(tmp_path/'control');v=c.grant(grant(tmp_path/'project'))['version'];r=c.submit('owner','synthetic',v,'x',plan());c.revoke('synthetic')
    f=Fake();out=c.execute(r['id'],f);assert out['status']=='BLOCKED';assert f.calls==0

def test_post_sample_failure_never_reexecutes(tmp_path):
    c=Controller(tmp_path/'control');v=c.grant(grant(tmp_path/'project'))['version'];r=c.submit('owner','synthetic',v,'x',plan())
    class FailAfter(Fake):
        n=0
        def sample(self,cwd):
            self.n+=1
            if self.n>1:raise RuntimeError('unavailable')
            return sample()
    f=FailAfter();c.execute(r['id'],f);assert f.calls==1
    assert c.result('owner',r['id'])['usage']['roles']['plus_executor']['five_hour']['remaining_after_percent'] is None

def test_sample_trust_is_process_only(tmp_path):
    # The argv contract must not depend on a developer machine's real Codex install.
    # Popen stays mocked: this fixture is only an existing path for validation.
    cli = tmp_path / 'synthetic-cli.exe'
    cli.write_bytes(b'synthetic non-executable fixture')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with mock.patch.object(usage, 'CLI', cli), mock.patch.object(
            usage.subprocess, 'Popen', side_effect=OSError('test')) as spawn:
        usage.sample('plus_executor', {}, tmp_path)
    spawn.assert_called_once()
    assert spawn.call_args.kwargs['env'] == {}
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    argv=spawn.call_args.args[0]
    assert argv[1]=='-c' and argv[2].startswith('projects.') and '.trust_level="trusted"' in argv[2]
