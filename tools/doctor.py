"""Read-only PCC path diagnostics; --repair-cli is explicit project-only maintenance."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcc import runtime

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true', help='Read CLI help/version and effective config; no model call')
    parser.add_argument('--repair-cli', metavar='ABSOLUTE_PATH', help='Verify one recognized candidate and atomically repair local runtime config')
    parser.add_argument('--evidence-dir', help='New directory inside project evidence; required for repair')
    args = parser.parse_args()
    if bool(args.repair_cli) != bool(args.evidence_dir):
        parser.error('--repair-cli and --evidence-dir must be supplied together')
    out = {'model_requests': 0, 'config_priority': ['PCC_CODEX_CLI', 'config/runtime.local.json', 'unique recognized candidate'],
           'runtime_configured': (runtime.BASE / 'config/runtime.local.json').exists()}
    try:
        if args.repair_cli:
            out['repair'] = runtime.repair_cli(args.repair_cli, args.evidence_dir)
        config = runtime.validate_separation()
        out.update(cli=runtime.validate_cli(), cli_source=config['cli_source'],
                   plus_home=config['plus_home'], controller_home=config['controller_home'], home_separation='ok')
        if args.native:
            out['native'] = runtime.probe_cli()
        out['status'] = 'PASS'
    except Exception as error:
        out.update(status='BLOCKED', error_code=getattr(error, 'code', type(error).__name__),
                   message=str(error) if isinstance(error, runtime.RuntimePathError) else type(error).__name__,
                   candidates=runtime.cli_candidates(), fallback_attempted=False)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out['status'] == 'PASS' else 1

if __name__ == '__main__':
    raise SystemExit(main())
