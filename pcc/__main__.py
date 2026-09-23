import argparse
import json
from .controller import Controller
from .paths import load

def main():
    p=argparse.ArgumentParser(description='PCC: local admin and explicit controller entry; no global changes')
    s=p.add_subparsers(dest='cmd',required=True)
    for action in ('grant','serve'):
        a=s.add_parser(action);a.add_argument('file')
    a=s.add_parser('revoke');a.add_argument('project')
    a=s.add_parser('status');a.add_argument('subject');a.add_argument('task')
    a=s.add_parser('restore');a.add_argument('subject');a.add_argument('task');a.add_argument('path')
    s.add_parser('capabilities').add_argument('subject')
    s.add_parser('pause')
    s.add_parser('resume')
    a=s.add_parser('retry-prestart');a.add_argument('task');a.add_argument('--reason',required=True)
    a=s.add_parser('recover-install');a.add_argument('task');a.add_argument('--reason',required=True)
    args=p.parse_args();c=Controller()
    if args.cmd=='grant':result=c.grant(load(args.file))
    elif args.cmd=='retry-prestart':result=c.retry_prestart(args.task,args.reason)
    elif args.cmd=='recover-install':
        from .install_recovery import recover
        result=recover(c,args.task,args.reason)
    elif args.cmd=='revoke':c.revoke(args.project);result={'revoked':args.project,'in_flight':'cancellation/revocation checked by running executor; not all effects undone'}
    elif args.cmd=='status':result=c.status(args.subject,args.task)
    elif args.cmd=='capabilities':result=c.capabilities(args.subject)
    elif args.cmd=='pause':
        (c.root/'disabled.flag').write_text('dispatch paused by local administrator',encoding='utf-8')
        result={'dispatch':'paused','in_flight':'not automatically stopped; query and cancel separately'}
    elif args.cmd=='resume':
        (c.root/'disabled.flag').unlink(missing_ok=True);result={'dispatch':'resumed; saved project grant selects PCC_STRICT or explicitly acknowledged PCC_HOST_TRUSTED'}
    elif args.cmd=='serve':
        from .server import serve
        serve(args.file);return
    elif args.cmd=='restore':
        from .broker import Broker
        r=c.row(args.subject,args.task)
        if r['status'] not in ('LOCAL_CHECK','FAILED','BLOCKED','CANCELLED'):raise RuntimeError('task still occupied/unknown')
        b=Broker(c,r,c.authorized(args.subject,r['project'],r['version']),c.root/'runs'/r['run_id']);b.restore(args.path);result={'restored':args.path}
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
