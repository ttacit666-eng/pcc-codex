import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('release_check',Path(__file__).resolve().parents[1]/'tools/release_check.py')
release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)

def test_release_excludes_deployment_state_and_credentials(tmp_path):
    for name in ('state/auth.json','config/deployment.json','.env','evidence/private.md','config/runtime.local.json','pcc/example.py','config/deployment.example.json'):
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('synthetic')
    paths={p.relative_to(tmp_path).as_posix() for p in release.source_files(tmp_path)}
    assert paths=={'pcc/example.py','config/deployment.example.json'}

def test_scanner_reports_rules_not_secret_text():
    secret='ghp_'+'A'*36
    assert release.violations(secret)==['github_token']
    assert 'personal_windows_home' in release.violations('C:/Users/'+'private-owner'+'/data')
    assert release.violations('C:/Users/example/data')==[]
