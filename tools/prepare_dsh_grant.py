"""Local-only bounded DSH grant. Never submits a task or reads authentication files."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pcc.controller import Controller
from pcc.paths import BASE,plain,digest,save
from pcc.broker import validate_grant

PACKAGES=[('auto-mode','dsh-auto-mode','0.1.1'),('graphflow','@roarpeng/graphflow','1.25.1'),('usage','dsh-usage-plugin','0.1.3')]

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('root','subject','node','dsh','pnpm'):p.add_argument('--'+name,required=True)
    p.add_argument('--external-idle-ack',action='store_true')
    p.add_argument('--apply',action='store_true')
    args=p.parse_args()
    if not args.external_idle_ack:p.error('Owner must explicitly confirm no conflicting plugin modification')
    root=plain(args.root)
    pin=lambda name:{'path':str(plain(name)),'sha256':digest(name)}
    runtime={'node':pin(args.node),'dsh':pin(args.dsh),'manager':pin(args.pnpm)}
    grant={'project':'dsh-web-plugins','subject':args.subject,'root':str(root),'execution_mode':'PCC_HOST_TRUSTED','host_trusted_ack':True,
           'external_idle_ack':True,'actions':['read','write','run','install'],'read_roots':['profiles/web/package.json'],
           'write_roots':['profiles/web'],'delete_roots':[],'wheels':[],'upload_targets':[],'download_targets':[],
           'timeout_seconds':300,'synthetic':False,'package_installs':[
               {'id':ident,'kind':'dsh','directory':'profiles/web','profile':'web','runtime':runtime,
                'registry':'https://registry.npmjs.org/','packages':[{'name':name,'version':version}],'timeout_seconds':600}
               for ident,name,version in PACKAGES]}
    grant=validate_grant(grant,BASE/'state')
    out=BASE/'config/dsh-install.local.json'
    if out.exists():raise FileExistsError('Existing DSH grant proposal retained; review before updating')
    save(out,grant)
    result={'proposal':str(out),'task_submitted':False}
    if args.apply:result.update(Controller().grant(grant))
    print(__import__('json').dumps(result))

if __name__=='__main__':main()
