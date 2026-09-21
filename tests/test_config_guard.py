import os
import pytest
from pcc.config_guard import restore_owned_trust

def test_restore_exact_owned_stanza(tmp_path):
    p=tmp_path/'config.toml';original=b'approval_policy = "on-request"\n'
    cwd=tmp_path/'work';own=os.path.normcase(os.path.abspath(cwd))
    p.write_bytes(original+('\n[projects.\''+own+'\']\ntrust_level = "trusted"\n').encode())
    result=restore_owned_trust(p,original,cwd,tmp_path/'evidence.json')
    assert p.read_bytes()==original and result['only_owned_stanza_removed']

def test_concurrent_config_not_overwritten(tmp_path):
    p=tmp_path/'config.toml';p.write_bytes(b'other edit\n')
    with pytest.raises(RuntimeError):restore_owned_trust(p,b'original\n',tmp_path/'work',tmp_path/'evidence.json')
    assert p.read_bytes()==b'other edit\n'

def test_unchanged_config_no_write(tmp_path):
    p=tmp_path/'config.toml';p.write_bytes(b'original\n');stamp=p.stat().st_mtime_ns
    result=restore_owned_trust(p,b'original\n',tmp_path/'work',tmp_path/'evidence.json')
    assert not result['only_owned_stanza_removed'] and p.stat().st_mtime_ns==stamp

def test_preservation_failure_retains_model_usage(tmp_path):
    from unittest import mock
    from pcc.host_trusted import HostTrustedExecutor
    from pcc.executor import LiveExecutor,usage
    (tmp_path/'config.toml').write_text('synthetic config')
    cap={'status':'completed','flags':[],'events':[{'type':'turn.completed','usage':{'input_tokens':7}}]}
    with mock.patch.object(usage,'PLUS_HOME',tmp_path),mock.patch.object(LiveExecutor,'run',return_value=cap), \
         mock.patch('pcc.config_guard.restore_owned_trust',side_effect=RuntimeError('concurrent edit')):
        result=HostTrustedExecutor().run(None,{},tmp_path,tmp_path,'',{},'python',{})
    assert result['status']=='RECOVERY_REQUIRED'
    assert result['events'][0]['usage']['input_tokens']==7
