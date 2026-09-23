"""Explicit local-owner recovery of the legacy pnpm hardlink validation failure."""
import json
from .paths import load,save,inside
from .broker import Broker
from .package_install import verify_installed,validate_targets,install

def recover(c,task,reason):
    if not reason.strip():raise ValueError('local recovery reason required')
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        r=db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
        if not r:raise ValueError('unknown task')
        r=dict(r);run=c.root/'runs'/r['run_id'];recovery=run/'install-recovery'
        if recovery.exists():raise RuntimeError('recovery already attempted; inspect receipt, no automatic retry')
        if r['status']!='FAILED' or r['cancel']:raise RuntimeError('not an eligible failed task')
        if db.execute("SELECT id FROM tasks WHERE status NOT IN ('LOCAL_CHECK','FAILED','BLOCKED','CANCELLED')").fetchone():raise RuntimeError('account occupied')
        g=c.authorized(r['subject'],r['project'],r['version']);validate_targets(g)
        actions=load(run/'actions.json')
        failures=[a for a in actions if a['operation']=='package_install_failure']
        if len(failures)!=1 or failures[0].get('exit_code')!=0 or failures[0].get('message')!='hard-linked file rejected':
            raise RuntimeError('not the known post-install hardlink validation failure')
        requested=json.loads(r['body'])['plan']['package_installs']
        attempted=[a['id'] for a in actions if a['operation']=='package_install_intent']
        if attempted!=requested[:len(attempted)] or not attempted:raise RuntimeError('unexpected install history')
        targets={t['id']:t for t in g['package_installs']}
        for ident in attempted:
            verify_installed(inside(g['root'],targets[ident]['directory']),targets[ident]['packages'])
        recovery.mkdir()
        save(recovery/'authorization.json',{'reason':reason,'original_task':task,'reconciled':attempted,'remaining':requested[len(attempted):]})
        db.execute("UPDATE tasks SET status='BROKER' WHERE id=?",(task,))
    b=Broker(c,r,g,recovery)
    try:
        for ident in attempted:b.record('package_install_reconciled',{'id':ident,'status':'VERIFIED_INSTALLED','reinstalled':False})
        install(b,{'package_installs':requested[len(attempted):]})
        result={'status':'VERIFIED_INSTALLED','activation':'NOT_VERIFIED','original_failure_preserved':True,'actions':b.actions}
        save(recovery/'result.json',result)
        c.audit(task,'manual_install_recovery_completed',result)
        c.update(task,'FAILED') # Original frozen result remains a truthful historical failure.
        return result
    except BaseException:
        c.update(task,'RECOVERY_REQUIRED')
        raise
