"""Standalone help must work outside the checkout without dispatching a task."""
import os
from pathlib import Path
import subprocess
import sys


def test_receipt_entrypoint_from_unrelated_unicode_directory(tmp_path):
    project = Path(__file__).resolve().parents[1]
    cwd = tmp_path / "外部目录 with spaces"
    cwd.mkdir()
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    # Help must not need a working CLI, credentials, or a model request.
    env["PCC_CODEX_CLI"] = str(cwd / "must-not-run.exe")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-E", str(project / "vendor/tools/run_plus_task.py"), "--help"],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert not list(cwd.iterdir())


def test_doctor_explains_explicit_missing_cli_from_external_cwd(tmp_path):
    import json
    project = Path(__file__).resolve().parents[1]
    cwd = tmp_path / '诊断 外部路径'; cwd.mkdir()
    env = os.environ.copy(); env.pop('PYTHONPATH', None)
    env['PCC_CODEX_CLI'] = str(cwd/'missing-cli.exe')
    result = subprocess.run([sys.executable, '-E', str(project/'tools/doctor.py')],
                            cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report['error_code'] == 'CLI_PATH_MISSING'
    assert report['fallback_attempted'] is False
    assert report['model_requests'] == 0
