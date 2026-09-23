"""No CLI/model calls: synthetic paths and subprocess/RPC fakes verify fail-closed repair."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from pcc import runtime

@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'BASE', tmp_path)
    for key in ('PCC_CODEX_CLI', 'PCC_PLUS_HOME', 'PCC_CONTROLLER_HOME', 'CODEX_HOME'):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / 'config').mkdir()
    cli = tmp_path / 'official desktop' / 'codex.exe'
    cli.parent.mkdir(); cli.write_bytes(b'synthetic CLI, never executable')
    value = {'cli': str(cli), 'plus_home': str(tmp_path/'plus'), 'controller_home': str(tmp_path/'controller')}
    path = tmp_path / 'config/runtime.local.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    return path, value

def test_explicit_missing_config_never_uses_path(config, monkeypatch):
    path, value = config
    Path(value['cli']).unlink()
    which = Mock(side_effect=AssertionError('Must not discover around explicit configuration'))
    monkeypatch.setattr(runtime.shutil, 'which', which)
    with pytest.raises(runtime.RuntimePathError, match='CLI_PATH_MISSING'):
        runtime.validate_cli()
    which.assert_not_called()

def test_environment_precedence_and_empty_override(config, monkeypatch):
    path, value = config
    monkeypatch.setenv('PCC_CODEX_CLI', str(path.parent/'absent.exe'))
    with pytest.raises(runtime.RuntimePathError, match='CLI_PATH_MISSING'):
        runtime.validate_cli()
    monkeypatch.setenv('PCC_CODEX_CLI', '')
    with pytest.raises(runtime.RuntimePathError, match='CLI_CONFIG_INVALID'):
        runtime.settings()

def test_relative_cli_never_executes_arbitrary_path(config, monkeypatch):
    monkeypatch.setenv('PCC_CODEX_CLI', 'codex')
    with pytest.raises(runtime.RuntimePathError, match='CLI_PATH_NOT_ABSOLUTE'):
        runtime.validate_cli()

def test_discovery_rejects_arbitrary_path_and_ambiguous_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'BASE', tmp_path)
    monkeypatch.delenv('PCC_CODEX_CLI', raising=False)
    rogue = tmp_path / 'unrecognized' / 'codex.exe'
    rogue.parent.mkdir(); rogue.touch()
    monkeypatch.setattr(runtime.shutil, 'which', lambda _: str(rogue))
    desktop = tmp_path / 'recognized'
    monkeypatch.setattr(runtime, '_desktop_bin', lambda: desktop)
    assert runtime.cli_candidates() == []
    for version in ('one', 'two'):
        (desktop/version).mkdir(parents=True)
        (desktop/version/'codex.exe').touch()
    assert len(runtime.cli_candidates()) == 2
    with pytest.raises(runtime.RuntimePathError, match='CLI_DISCOVERY_UNRESOLVED'):
        runtime.validate_cli()

def test_startup_probe_is_local_help_only(config, monkeypatch):
    replies = ['codex-cli 0.155.0-test', '--strict-config', '--json --ephemeral --skip-git-repo-check', '--stdio']
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs['env']['CODEX_HOME'] == config[1]['plus_home']
        return SimpleNamespace(stdout=replies[len(calls)-1])
    monkeypatch.setattr(runtime.subprocess, 'run', run)
    result = runtime.preflight_runtime()
    assert [x[1:] for x in calls] == [['--version'], ['--help'], ['exec','--help'], ['app-server','--help']]
    assert result['model_requests'] == 0
    assert 'effective_config' not in result

def test_discovery_deduplicates_path_spelling(tmp_path, monkeypatch):
    desktop = tmp_path/'recognized'
    version = desktop/'version'; version.mkdir(parents=True)
    binary = version/'codex.exe'; binary.touch()
    # Simulate Windows normcase even in CI on case-sensitive hosts.
    alias = version/'codex.EXE'; alias.touch(exist_ok=True)
    monkeypatch.setattr(runtime, '_desktop_bin', lambda: desktop)
    monkeypatch.setattr(runtime.shutil, 'which', lambda _: str(alias))
    monkeypatch.setattr(runtime.os.path, 'normcase', lambda p: str(p).lower())
    assert len(runtime.cli_candidates()) == 1

def test_startup_probe_rejects_incompatible_cli(config, monkeypatch):
    monkeypatch.setattr(runtime.subprocess, 'run', lambda argv, **kw: SimpleNamespace(stdout='codex-cli 1.0' if argv[-1]=='--version' else 'no required options'))
    with pytest.raises(runtime.RuntimePathError, match='CLI_INTERFACE_INCOMPATIBLE'):
        runtime.preflight_runtime()

def test_plus_environment_only_changes_child(config, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', config[1]['controller_home'])
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-secret-never-sent')
    child = runtime.plus_environment()
    assert child['CODEX_HOME'] == config[1]['plus_home']
    assert 'OPENAI_API_KEY' not in child
    assert os.environ['CODEX_HOME'] == config[1]['controller_home']
    assert os.environ['OPENAI_API_KEY'] == 'synthetic-secret-never-sent'

def test_repair_preserves_homes_and_backup(config, monkeypatch):
    path, value = config; before = path.read_bytes()
    candidate = path.parent/'new-cli.exe'; candidate.touch()
    monkeypatch.setattr(runtime, 'cli_candidates', lambda: [str(candidate)])
    monkeypatch.setattr(runtime, 'probe_cli', lambda cli: {'model_requests':0})
    evidence = path.parent.parent/'evidence/repair'
    result = runtime.repair_cli(str(candidate), evidence)
    after = json.loads(path.read_text(encoding='utf-8'))
    assert after == dict(value, cli=str(candidate))
    assert (evidence/'runtime.local.before.json').read_bytes() == before
    assert result['before_sha256'] == hashlib.sha256(before).hexdigest()
    assert result['after_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result['account_homes_unchanged'] is True

def test_repair_rejects_unrecognized_and_active_override(config, monkeypatch):
    path, value = config; before = path.read_bytes()
    monkeypatch.setattr(runtime, 'cli_candidates', lambda: [])
    with pytest.raises(runtime.RuntimePathError, match='CLI_SOURCE_UNRECOGNIZED'):
        runtime.repair_cli(value['cli'], path.parent.parent/'evidence/rejected')
    monkeypatch.setenv('PCC_CODEX_CLI', value['cli'])
    with pytest.raises(runtime.RuntimePathError, match='CLI_ENV_OVERRIDE_ACTIVE'):
        runtime.repair_cli(value['cli'], path.parent.parent/'evidence/rejected')
    assert path.read_bytes() == before

def test_repair_refuses_config_changed_during_validation(config, monkeypatch):
    path, value = config
    monkeypatch.setattr(runtime, 'cli_candidates', lambda: [value['cli']])
    changed = dict(value, plus_home=str(path.parent/'different-plus'))
    def probe(_):
        path.write_text(json.dumps(changed), encoding='utf-8')
        return {'model_requests':0}
    monkeypatch.setattr(runtime, 'probe_cli', probe)
    with pytest.raises(runtime.RuntimePathError, match='RUNTIME_CONFIG_CHANGED'):
        runtime.repair_cli(value['cli'], path.parent.parent/'evidence/rejected')
    assert json.loads(path.read_text()) == changed

def test_repair_incompatible_candidate_does_not_write(config, monkeypatch):
    path, value = config; before = path.read_bytes()
    monkeypatch.setattr(runtime, 'cli_candidates', lambda: [value['cli']])
    def blocked(_):
        raise runtime.RuntimePathError('CLI_CONFIG_INCOMPATIBLE', 'synthetic rejection')
    monkeypatch.setattr(runtime, 'probe_cli', blocked)
    with pytest.raises(runtime.RuntimePathError, match='CLI_CONFIG_INCOMPATIBLE'):
        runtime.repair_cli(value['cli'], path.parent.parent/'evidence/rejected')
    assert path.read_bytes() == before
    assert not (path.parent.parent/'evidence/rejected').exists()

def test_repair_refuses_service_lock_without_touching_it(config):
    path, value = config; before = path.read_bytes()
    state = path.parent.parent/'state'; state.mkdir()
    lock = state/'service.lock'; lock.write_text('synthetic owner')
    with pytest.raises(runtime.RuntimePathError, match='CLI_REPAIR_SERVICE_ACTIVE'):
        runtime.repair_cli(value['cli'], path.parent.parent/'evidence/rejected')
    assert path.read_bytes() == before
    assert lock.read_text() == 'synthetic owner'

def test_project_grant_cannot_overlap_custom_account_home(config):
    from pcc.broker import validate_grant
    path, value = config
    root = Path(value['plus_home'])/'synthetic-project'; root.mkdir(parents=True)
    with pytest.raises(ValueError, match='protected root overlap'):
        validate_grant({'root':str(root)}, path.parent.parent/'state')
