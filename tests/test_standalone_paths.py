"""Standalone help must work outside the checkout without dispatching a task."""
import os
from pathlib import Path
import subprocess
import sys
import pytest


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


@pytest.mark.parametrize('output_encoding', [None, 'cp1252', 'ascii'])
def test_doctor_explains_explicit_missing_cli_from_external_cwd(tmp_path, output_encoding):
    import json
    project = Path(__file__).resolve().parents[1]
    cwd = tmp_path / '诊断 外部路径'; cwd.mkdir()
    env = os.environ.copy(); env.pop('PYTHONPATH', None)
    env['PCC_CODEX_CLI'] = str(cwd/'missing-cli.exe')
    script = str(project/'tools/doctor.py')
    argv = [sys.executable, '-E', script]
    if output_encoding:
        # Exercise the program under a real constrained stream, not PYTHONIOENCODING
        # (which -E ignores). Do not hide the bug by forcing UTF-8 in the test.
        code = ("import runpy,sys; sys.stdout.reconfigure(encoding=sys.argv[1],errors='strict'); "
                "sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')")
        argv = [sys.executable, '-E', '-c', code, output_encoding, script]
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=30)
    assert result.returncode == 1, result.stderr.decode('utf-8', errors='replace')
    assert result.stdout.isascii()
    assert result.stderr == b''
    report = json.loads(result.stdout)
    assert report['error_code'] == 'CLI_PATH_MISSING'
    assert report['fallback_attempted'] is False
    assert report['model_requests'] == 0
    assert str(cwd/'missing-cli.exe') in report['message']
