"""Portable, non-secret runtime paths. Never reads authentication material."""
import json
import os
from pathlib import Path
import shutil
import hashlib
import re
import subprocess
import uuid

BASE = Path(__file__).resolve().parents[1]

class RuntimePathError(ValueError):
    """Stable non-secret diagnostic; never silently substitute an executable."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(f'{code}: {message}')

def _config():
    path = BASE / 'config/runtime.local.json'
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    except (OSError, ValueError) as error:
        raise RuntimePathError('RUNTIME_CONFIG_INVALID', 'Cannot read non-secret runtime configuration') from error
    if not isinstance(value, dict):
        raise RuntimePathError('RUNTIME_CONFIG_INVALID', 'Runtime configuration must be an object')
    return value

def _plain(path):
    path = Path(path).expanduser().absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise RuntimePathError('RUNTIME_REPARSE_PATH', 'Runtime paths must not traverse reparse points')
    return path

def _desktop_bin():
    local = os.environ.get('LOCALAPPDATA')
    return Path(local) / 'OpenAI/Codex/bin' if local else None

def recognized_cli(path):
    """Bounded discovery: official desktop version directory or official npm binary."""
    path = Path(path).expanduser().absolute()
    desktop = _desktop_bin()
    if desktop and path.name.lower() == 'codex.exe' and path.parent.parent == desktop:
        return True
    parts = tuple(p.lower() for p in path.parts)
    # Native executable inside a named official package, never a PATH .cmd shim.
    for i in range(len(parts) - 2):
        if parts[i:i+2] == ('node_modules', '@openai') and (parts[i+2] == 'codex' or parts[i+2].startswith('codex-')):
            return path.name.lower() in ('codex', 'codex.exe')
    return False

def cli_candidates():
    """No drive scan, install, download, login or model request."""
    found = {}
    active = shutil.which('codex')
    candidates = [Path(active)] if active else []
    desktop = _desktop_bin()
    if desktop and desktop.is_dir():
        candidates.extend(desktop.glob('*/codex.exe'))
    for candidate in candidates:
        try:
            candidate = _plain(candidate)
            if candidate.is_file() and recognized_cli(candidate):
                found[os.path.normcase(str(candidate))] = str(candidate)
        except (OSError, RuntimePathError):
            continue
    return sorted(found.values())

def settings():
    config = _config()
    if 'PCC_CODEX_CLI' in os.environ:
        cli, source = os.environ['PCC_CODEX_CLI'], 'environment'
    elif 'cli' in config:
        cli, source = config['cli'], 'runtime.local.json'
    else:
        candidates = cli_candidates()
        # Resolve a single recognized candidate only. Help remains usable when not configured.
        cli, source = (candidates[0], 'recognized-discovery') if len(candidates) == 1 else (str(BASE / 'config/.unresolved-codex'), 'unresolved')
    if not isinstance(cli, str) or not cli.strip():
        raise RuntimePathError('CLI_CONFIG_INVALID', 'An explicitly configured CLI cannot be empty')
    return {
        'cli': cli,
        'cli_source': source,
        'plus_home': str(Path(os.environ.get('PCC_PLUS_HOME') or config.get('plus_home') or Path.home()/'.codex-plus').expanduser().absolute()),
        'controller_home': str(Path(os.environ.get('PCC_CONTROLLER_HOME') or config.get('controller_home') or os.environ.get('CODEX_HOME') or Path.home()/'.codex').expanduser().absolute()),
    }

def validate_cli(cli=None):
    value = settings()
    if cli is None and value['cli_source'] == 'unresolved':
        raise RuntimePathError('CLI_DISCOVERY_UNRESOLVED', 'No unique recognized CLI candidate; configure an explicit absolute path')
    candidate = str(cli) if cli is not None else value['cli']
    if not Path(candidate).expanduser().is_absolute():
        raise RuntimePathError('CLI_PATH_NOT_ABSOLUTE', 'Configure an absolute CLI path; no PATH fallback was attempted')
    path = _plain(candidate)
    if not path.is_file():
        raise RuntimePathError('CLI_PATH_MISSING', f'Configured CLI is absent: {path}; no fallback was attempted')
    return str(path)

def validate_separation(plus_home=None):
    config = settings()
    plus = Path(plus_home or config['plus_home']).absolute()
    controller = Path(config['controller_home']).absolute()
    for target in (plus, controller):
        for part in (target, *target.parents):
            if part.is_symlink() or (part.exists() and getattr(part.lstat(),'st_file_attributes',0)&0x400):
                raise ValueError('Runtime homes must not traverse reparse points')
    plus, controller = plus.resolve(), controller.resolve()
    if plus == controller or plus in controller.parents or controller in plus.parents:
        raise ValueError('Plus and controller homes must be separate, non-nested directories')
    if plus == Path.home() or plus == Path(plus.anchor):
        raise ValueError('Plus home must be a dedicated directory')
    return config

def controller_env():
    env = os.environ.copy()
    env['CODEX_HOME'] = settings()['controller_home']
    return env

def plus_environment(plus_home=None):
    config = validate_separation(plus_home)
    env = os.environ.copy()
    for key in list(env):
        if (key.upper().startswith(('CODEX_', 'OPENAI_', 'CHATGPT_', 'C2C_')) and key != 'CODEX_CA_CERTIFICATE') or key in ('RUST_LOG', 'RUST_LOG_STYLE'):
            env.pop(key, None)
    env['CODEX_HOME'] = str(plus_home or config['plus_home'])
    return env

def probe_cli(cli=None, cwd=None, effective_config=True):
    """Read CLI help/version and effective config only; no account or model call."""
    cli = validate_cli(cli)
    config = validate_separation()
    env = plus_environment()
    def read_help(argv):
        try:
            return subprocess.run([cli, *argv], env=env, capture_output=True, text=True, timeout=15, check=True).stdout
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimePathError('CLI_NATIVE_CHECK_FAILED', 'CLI version/help check failed; no fallback or retry') from error
    version = read_help(['--version']).strip()
    if not re.fullmatch(r'codex-cli [0-9A-Za-z.+-]+', version):
        raise RuntimePathError('CLI_VERSION_UNRECOGNIZED', 'CLI did not return an official Codex version signature')
    for argv, required in ((['--help'], ('--strict-config',)), (['exec', '--help'], ('--json', '--ephemeral', '--skip-git-repo-check')), (['app-server', '--help'], ('--stdio',))):
        result = read_help(argv)
        if not all(option in result for option in required):
            raise RuntimePathError('CLI_INTERFACE_INCOMPATIBLE', 'Required PCC CLI options are unavailable')
    report = {'cli': cli, 'cli_version': version, 'recognized_source': recognized_cli(cli),
              'cli_sha256': hashlib.sha256(Path(cli).read_bytes()).hexdigest(),
              'plus_home': config['plus_home'], 'controller_home': config['controller_home'], 'model_requests': 0}
    if not effective_config:
        return report
    from .sandbox_rpc import SandboxRPC
    prefix = [cli, '--strict-config', '-c', 'default_permissions=":danger-full-access"',
              '-c', 'approval_policy="never"', '-c', 'forced_login_method="chatgpt"',
              '-c', 'cli_auth_credentials_store="file"', 'exec']
    rpc = SandboxRPC(prefix, cwd or BASE, env)
    try:
        reply = rpc.call('config/read', {'includeLayers': False, 'cwd': str(cwd or BASE)})
        effective = reply.get('result', {}).get('config', {})
        expected = {'default_permissions': ':danger-full-access', 'sandbox_mode': None,
                    'approval_policy': 'never', 'forced_login_method': 'chatgpt', 'cli_auth_credentials_store': 'file'}
        if 'error' in reply or any(effective.get(k) != v for k, v in expected.items()):
            raise RuntimePathError('CLI_CONFIG_INCOMPATIBLE', 'Effective PCC_HOST_TRUSTED/auth configuration did not match; no policy bypass')
    finally:
        rpc.close()
    report['effective_config'] = {k: effective.get(k) for k in expected}
    return report

def preflight_runtime():
    """Service startup gate: bounded local --version/--help only, before taking locks."""
    return probe_cli(effective_config=False)

def repair_cli(candidate, evidence_dir):
    """Explicit maintenance only. Back up non-secret config, verify, compare, replace."""
    if (BASE / 'state/service.lock').exists():
        raise RuntimePathError('CLI_REPAIR_SERVICE_ACTIVE', 'PCC service lock exists; use the approved idle-service stop procedure before repair')
    if 'PCC_CODEX_CLI' in os.environ:
        raise RuntimePathError('CLI_ENV_OVERRIDE_ACTIVE', 'Remove the process override before repairing local config; no environment is modified')
    path = _plain(BASE / 'config/runtime.local.json')
    before = path.read_bytes() if path.exists() else None
    original = _config()
    if set(original) - {'cli', 'plus_home', 'controller_home'}:
        raise RuntimePathError('RUNTIME_CONFIG_UNKNOWN_FIELDS', 'Refuse to back up unrecognized runtime fields')
    candidate = validate_cli(candidate)
    if Path(candidate) not in [Path(p) for p in cli_candidates()]:
        raise RuntimePathError('CLI_SOURCE_UNRECOGNIZED', 'Candidate is not in bounded recognized discovery')
    homes = {k: settings()[k] for k in ('plus_home', 'controller_home')}
    report = probe_cli(candidate)
    if homes != {k: settings()[k] for k in homes} or before != (path.read_bytes() if path.exists() else None):
        raise RuntimePathError('RUNTIME_CONFIG_CHANGED', 'Configuration changed during validation')
    evidence = _plain(evidence_dir)
    if not evidence.is_relative_to(_plain(BASE / 'evidence')):
        raise RuntimePathError('REPAIR_EVIDENCE_SCOPE', 'Repair backups must stay inside this project evidence directory')
    evidence.mkdir(parents=True, exist_ok=False)
    if before is not None:
        (evidence / 'runtime.local.before.json').write_bytes(before)
    updated = dict(original, cli=candidate)
    after = (json.dumps(updated, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    report.update(before_sha256=hashlib.sha256(before).hexdigest() if before is not None else None,
                  after_sha256=hashlib.sha256(after).hexdigest(), original_existed=before is not None,
                  changed_fields=['cli'], account_homes_unchanged=True)
    (evidence / 'repair.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temp.open('xb') as sink:
        sink.write(after); sink.flush(); os.fsync(sink.fileno())
    # Check again immediately before replacement; never merge over another writer.
    if before != (path.read_bytes() if path.exists() else None):
        temp.unlink()
        raise RuntimePathError('RUNTIME_CONFIG_CHANGED', 'Configuration changed before atomic replacement')
    os.replace(temp, path)
    return report
