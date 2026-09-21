"""Read-only installation check. --native runs CLI --version/--help only."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pcc.runtime import settings,validate_separation

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--native',action='store_true');args=p.parse_args()
    config=settings();out={'runtime_configured':(Path(__file__).resolve().parents[1]/'config/runtime.local.json').exists(),'model_requests':0}
    try:validate_separation();out['home_separation']='ok'
    except ValueError as error:out['home_separation']=str(error)
    if args.native:
        try:
            version=subprocess.run([config['cli'],'--version'],capture_output=True,text=True,timeout=15,check=True)
            help_result=subprocess.run([config['cli'],'--help'],capture_output=True,text=True,timeout=15,check=True)
            out.update(cli_version=version.stdout.strip(),help_available=bool(help_result.stdout))
        except (OSError,subprocess.SubprocessError) as error:out['cli_check']=type(error).__name__
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if out['home_separation']=='ok' and 'cli_check' not in out else 1

if __name__=='__main__':raise SystemExit(main())
