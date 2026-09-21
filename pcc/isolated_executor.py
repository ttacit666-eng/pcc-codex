"""Guest execution contract preparation. No VM creation, model start or live transport.

Fits Controller.execute(task, executor=...) without changing the Windows default.
The injected transport is for explicitly synthetic simulation tests ONLY. Production
activation is deliberately impossible in this revision, even by changing JSON flags.
"""
import base64
import hashlib
import json
import re
from pathlib import PurePosixPath

from .paths import BASE, inside, plain, load, save
from .executor import usage
from .execution_evidence import ExecutionEvidence


class GuestNotReady(RuntimeError):
    pass


def binding(row):
    run = row['run_id']
    if not re.fullmatch(r'[0-9a-f]{32}', run):
        raise ValueError('invalid run id')
    return {'task_id': row['id'], 'iteration': 1, 'run_id': run}


def guest_contract(row, expected_outputs, config):
    """Pure data only. Guest Linux argv is a candidate, not verified installed CLI."""
    bind = binding(row)
    root = PurePosixPath(config['guest_jobs']) / bind['run_id']
    for key in ('guest_cli', 'guest_codex_home', 'guest_jobs', 'guest_python'):
        value = config[key]
        if not value.startswith('/') or '..' in PurePosixPath(value).parts or '\\' in value:
            raise ValueError('absolute guest Linux path required')
    rules = {':root': 'deny', str(root/'input'): 'read',
             str(root/'work'): 'write', str(root/'result'): 'write',
             config['guest_codex_home']: 'deny'}
    rules.update({p: 'read' for p in config['guest_runtime_read_roots']})
    profile = {'filesystem': rules, 'network': {'enabled': False}}
    inline = '{ filesystem = { ' + ', '.join(json.dumps(k)+' = '+json.dumps(v) for k,v in rules.items()) + ' }, network = { enabled = false } }'
    argv = [config['guest_cli'], '--strict-config', '-c', 'default_permissions="pcc-task"',
            '-c', 'permissions.pcc-task='+inline, '-c', 'approval_policy="on-request"',
            '-c', 'forced_login_method="chatgpt"', '-c', 'cli_auth_credentials_store="file"',
            'exec', '--json', '--ephemeral', '--skip-git-repo-check', '--color', 'never',
            '-C', str(root/'work'), '-']
    return {**bind, 'schema': 1, 'cwd': str(root/'work'),
            'env': {'CODEX_HOME': config['guest_codex_home']}, 'argv': argv,
            'policy': profile, 'expected_outputs': list(expected_outputs),
            'policy_sha256': hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest(),
            'status': 'CANDIDATE_NOT_GUEST_VERIFIED'}


class IsolatedExecutor:
    def __init__(self, config=None, *, simulation_transport=None):
        self.config = config or load(BASE/'isolation/backend.json')
        self.transport = simulation_transport
        self.source_kind = 'synthetic' if simulation_transport is not None else 'not_started'
        self.ready = False

    def version(self):
        return 'SIMULATED_GUEST_CLI' if self.transport is not None else 'NOT_RUN_guest_cli_absent'

    def preflight(self, controller, row, job, run, python):
        self.ready = False
        # There is no production activation boolean: deployment and real probes remain required.
        if self.transport is None:
            save(run/'guest-preflight.json', {'status': 'BLOCKED', 'guest_test': 'NOT_RUN',
                 'reason': 'Live guest transport and isolation acceptance not implemented/deployed'})
            raise GuestNotReady('Guest deployment unavailable; no Windows fallback')
        grant = controller.authorized(row['subject'], row['project'], row['version'])
        if grant.get('synthetic') is not True or getattr(self.transport, 'is_simulation', False) is not True:
            raise PermissionError('Simulation requires synthetic grant and explicitly simulated transport')
        body = json.loads(row['body'])
        if body['plan'].get('dependencies'):
            raise GuestNotReady('Host Windows installed packages cannot be reused as Linux dependencies')
        self.contract = guest_contract(row, body['plan']['expected_outputs'], self.config)
        report = self.transport.preflight(self.contract)
        required = ('allowed_read', 'allowed_write', 'outside_read_denied', 'input_write_denied',
                    'outside_write_denied', 'host_network_denied', 'guest_loopback_denied',
                    'credential_mount_absent')
        if report.get('binding') != binding(row) or report.get('policy_sha256') != self.contract['policy_sha256']:
            raise PermissionError('Probe binding/policy mismatch')
        if any(report.get('checks', {}).get(key) is not True for key in required):
            raise PermissionError('Incomplete or failed guest checks')
        save(run/'guest-preflight.json', {**report, 'source_kind': 'synthetic',
             'real_guest_test': 'NOT_RUN', 'warning': 'SIMULATION_ONLY; not OS isolation evidence'})
        self.ready = True

    def sample(self, cwd):
        # Existing executor finally samples even on blocked preflight: do NOT contact accounts then.
        if not self.ready:
            return {}
        return self.transport.sample()

    def _inputs(self, job):
        root = plain(job/'input')
        files = []
        total = 0
        # Only broker-frozen task input, never arbitrary host paths or credentials.
        for p in root.rglob('*'):
            p = plain(p)
            if p.is_dir():
                continue
            data = p.read_bytes()
            total += len(data)
            if total > self.config['max_payload_bytes']:
                raise ValueError('input size budget exceeded')
            files.append({'path': p.relative_to(root).as_posix(),
                          'sha256': hashlib.sha256(data).hexdigest(),
                          'data_b64': base64.b64encode(data).decode('ascii')})
        return files

    def run(self, controller, row, job, run, goal, plan, python, grant):
        if not self.ready:
            raise GuestNotReady('preflight required')
        controller.checkpoint(row)
        request = {**self.contract, 'goal': goal, 'inputs': self._inputs(job)}
        save(run/'guest-invocation.json', {**self.contract, 'source_kind': 'synthetic',
             'model_request': 'NOT_STARTED', 'transport': 'injected_simulation'})
        controller.update(row['id'], 'RUNNING')  # No invented host PID for guest work.
        cap = {'source_kind': 'synthetic', 'status': 'RECOVERY_REQUIRED', 'events': [],
               'flags': ['simulation_only', 'real_guest_NOT_RUN'], 'pid': None, 'exit_code': None}
        # Exactly one request. On lost reply/timeout, retain the occupied controller task.
        try:
            response = self.transport.execute(request)
        except Exception as error:
            cap['flags'].append('guest_transport_'+type(error).__name__)
            return cap
        if not isinstance(response, dict):
            cap['flags'].append('guest_malformed_response_state_unknown')
            return cap
        if response.get('binding') != binding(row):
            cap['flags'].append('guest_response_binding_mismatch')
            return cap
        if response.get('state') != 'terminal':
            cap['flags'].append('guest_state_unknown_query_original_no_retry')
            return cap
        cap['status'] = 'failed'
        try:
            events = response.get('events', [])
            if not isinstance(events, list) or len(events) > self.config['max_events']:
                raise ValueError('invalid event budget')
            diagnostics = ExecutionEvidence(run)
            with (run/'usage-events.jsonl').open('x', encoding='utf-8') as sink:
                for seq, event in enumerate(events, 1):
                    diagnostics.record(event, seq)
                    filtered = usage.filter_event(event, seq)
                    if filtered:
                        cap['events'].append(filtered)
                        sink.write(json.dumps(filtered)+'\n')
            save(run/'execution-summary.json', {**diagnostics.summary(), 'source_kind': 'synthetic'})
            cap['exit_code'] = response.get('exit_code')
            if type(cap['exit_code']) is not int:
                raise ValueError('terminal exit code missing')
            expected = set(plan['expected_outputs'])
            checked = []
            seen = set()
            total = 0
            for output in response.get('outputs', []):
                rel = output['path']
                dest = inside(job/'result', rel)
                if rel not in expected or rel in seen or dest.exists():
                    raise ValueError('unexpected/duplicate/existing output')
                data = base64.b64decode(output['data_b64'], validate=True)
                total += len(data)
                if total > self.config['max_payload_bytes'] or hashlib.sha256(data).hexdigest() != output['sha256']:
                    raise ValueError('output size/hash mismatch')
                checked.append((dest, data))
                seen.add(rel)
            # Copy validated returned bytes only; controller never computes task results.
            controller.checkpoint(row)
            for dest, data in checked:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open('xb') as target:
                    target.write(data)
            completed = any(e['type'] == 'turn.completed' for e in cap['events'])
            failed = any(e['type'] in ('turn.failed', 'error') for e in cap['events'])
            if cap['exit_code'] == 0 and completed and not failed:
                cap['status'] = 'completed'  # Existing broker verifies all required files and input hashes.
        except Exception as error:
            cap['flags'].append('guest_capture_'+type(error).__name__)
        return cap


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='PCC guest preparation; no live execution')
    parser.add_argument('--describe', action='store_true', help='show readiness without contacting CLI or VM')
    args = parser.parse_args()
    if args.describe:
        print(json.dumps({'backend': 'isolated-guest', 'status': 'BLOCKED',
                          'guest_test': 'NOT_RUN', 'plus_model_task': 'NOT_STARTED',
                          'live_transport': 'NOT_IMPLEMENTED',
                          'integration': 'Controller.execute(task, executor=IsolatedExecutor(...))',
                          'default_backend_changed': False}, indent=2))
    else:
        parser.print_help()
