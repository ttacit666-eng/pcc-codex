import json
from pathlib import Path
import pytest
from pcc import runtime

def test_defaults_do_not_embed_a_personal_path(tmp_path,monkeypatch):
    monkeypatch.setattr(runtime,'BASE',tmp_path)
    for name in ('PCC_CODEX_CLI','PCC_PLUS_HOME','PCC_CONTROLLER_HOME','CODEX_HOME'):
        monkeypatch.delenv(name,raising=False)
    value=runtime.settings()
    assert value['plus_home']==str((Path.home()/'.codex-plus').resolve())
    assert value['controller_home']==str((Path.home()/'.codex').resolve())

def test_config_and_overlap_are_fail_closed(tmp_path,monkeypatch):
    monkeypatch.setattr(runtime,'BASE',tmp_path)
    for name in ('PCC_CODEX_CLI','PCC_PLUS_HOME','PCC_CONTROLLER_HOME','CODEX_HOME'):
        monkeypatch.delenv(name,raising=False)
    (tmp_path/'config').mkdir()
    value={'cli':'codex','plus_home':str(tmp_path/'plus'),'controller_home':str(tmp_path/'pro')}
    (tmp_path/'config/runtime.local.json').write_text(json.dumps(value))
    assert runtime.validate_separation()['plus_home']==str((tmp_path/'plus').resolve())
    monkeypatch.setenv('PCC_PLUS_HOME',str(tmp_path/'pro'/'nested'))
    with pytest.raises(ValueError,match='non-nested'):runtime.validate_separation()

def test_controller_environment_does_not_mutate_parent(tmp_path,monkeypatch):
    monkeypatch.setenv('PCC_CONTROLLER_HOME',str(tmp_path/'controller'))
    monkeypatch.setenv('CODEX_HOME',str(tmp_path/'original'))
    assert runtime.controller_env()['CODEX_HOME']==str((tmp_path/'controller').resolve())
    import os
    assert os.environ['CODEX_HOME']==str(tmp_path/'original')
