"""Save non-secret paths only; no login, account switch, service or model call."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcc.paths import BASE, plain

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli',required=True)
    parser.add_argument('--plus-home',required=True)
    parser.add_argument('--controller-home',required=True)
    args=parser.parse_args()
    cli=plain(args.cli)
    plus=plain(args.plus_home)
    controller=plain(args.controller_home)
    if not cli.is_file():parser.error('CLI executable does not exist')
    if plus==controller or plus in controller.parents or controller in plus.parents:
        parser.error('Account homes overlap')
    if plus==Path.home() or plus==Path(plus.anchor):parser.error('Dedicated Plus home required')
    value={'cli':str(cli),'plus_home':str(plus),'controller_home':str(controller)}
    path=BASE/'config/runtime.local.json'
    with path.open('x',encoding='utf-8',newline='\n') as out:
        json.dump(value,out,indent=2);out.write('\n')
    print('Saved config/runtime.local.json. No account or global environment changed.')

if __name__=='__main__':main()
