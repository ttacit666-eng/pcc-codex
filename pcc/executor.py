"""Exactly one CLI process per claimed task. No retry/fallback executor."""
import importlib.util
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from .paths import BASE,inside,plain,digest,save,load
from .controller import now
from .broker import Broker,validate_plan

spec=importlib.util.spec_from_file_location('pcc_native_usage',BASE/'vendor/tools/usage_receipt.py')
usage=importlib.util.module_from_spec(spec);spec.loader.exec_module(usage)

def arguments(job,control,python):
    def path(p):return str(p).replace('\\','/')
    rules={':root':'deny',':minimal':'read',path(job/'input'):'read',path(job/'work'):'write',path(job/'result'):'write',
           path(control):'deny',path(Path.home()/'.codex'):'deny',path(usage.PLUS_HOME/'auth.json'):'deny',
           path(usage.PLUS_HOME/'.sandbox-secrets'):'deny',path(Path(python).parent.parent):'read',
           path(Path(sys.base_prefix)):'read',
           str(Path(__import__("shutil").which("pwsh") or __import__("shutil").which("powershell") or sys.executable).parent):'read'}
    # Project env sits inside controller storage; only its immutable runtime is made readable.
    inline='{ filesystem = { '+', '.join(json.dumps(k)+' = '+json.dumps(v) for k,v in rules.items())+' }, network = { enabled = false } }'
    return [str(usage.CLI),'--strict-config','-c','default_permissions="pcc-task"','-c','permissions.pcc-task='+inline,
            '-c','projects.'+json.dumps(os.path.normcase(os.path.abspath(job/'work')))+'.trust_level="trusted"',
            '-c','approval_policy="on-request"','-c','forced_login_method="chatgpt"',
            'exec','--json','--ephemeral','--skip-git-repo-check','--color','never','-C',str(job/'work'),'-']

class LiveExecutor:
    execution_mode='PCC_STRICT'
    def arguments(self,job,control,python):return arguments(job,control,python)
    def permission_metadata(self):
        return {'execution_mode':self.execution_mode,'permission_scope':'CLI named permission profile; host authentication outside task sandbox','approval_policy':'on-request'}
    def preflight(self,c,r,job,run,python):
        from .isolation import probe
        return probe(job,run,python,arguments(job,c.root,python),usage.plus_env())
    def sample(self,cwd):
        return {role:usage.safe_sample(role,env,cwd) for role,env in
                [('plus_executor',usage.plus_env()),('pro_controller',usage.controller_env())]}
    def version(self):return usage.cli_version()
    def make_prompt(self,job,goal,plan,python,grant):
        return ('You are the independent Plus executor for a synthetic or authorized PCC task. Read only input; write only work/result. '
                'No network, installing dependencies, deleting project files, or modifying controller records. The host broker performs approved operations. '
                'If permissions require approval, stop and report it; never bypass. '
                f'Input directory: {job/"input"}. Output directory: {job/"result"}. Python: {python}. '
                f'Expected output paths: {json.dumps(plan["expected_outputs"])}. Task: '+goal)
    def run(self,c,r,job,run,goal,plan,python,grant):
        argv=self.arguments(job,c.root,python);save(run/'invocation.json',{'argv':argv,'cwd':str(job/'work'),**self.permission_metadata()})
        prompt=self.make_prompt(job,goal,plan,python,grant)
        env=usage.plus_env();env['TMP']=env['TEMP']=str(job/'work')
        # Sink creation must precede Popen.
        events=[];flags=[];lines=queue.Queue();code=None
        from .execution_evidence import ExecutionEvidence
        diagnostics=ExecutionEvidence(run)
        with (run/'usage-events.jsonl').open('x',encoding='utf-8') as sink:
            c.checkpoint(r)
            proc=subprocess.Popen(argv,cwd=job/'work',env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                  text=True,encoding='utf-8',errors='replace')
            c.update(r['id'],'RUNNING',proc.pid);save(run/'process.json',{'pid':proc.pid,'started':now(),'cli_start_count':1})
            def readout():
                for line in proc.stdout:lines.put(line)
                lines.put(None)
            def drainerr():
                # Diagnostics are classified only, no raw stderr/model text persisted.
                for line in proc.stderr:
                    if 'sandbox' in line.lower():flags.append('sandbox_diagnostic')
                    if 'approval' in line.lower():flags.append('approval_diagnostic')
            threading.Thread(target=readout,daemon=True).start();threading.Thread(target=drainerr,daemon=True).start()
            proc.stdin.write(prompt);proc.stdin.close();deadline=time.monotonic()+grant.get('timeout_seconds',120);seq=0
            timed=False
            while True:
                try:c.checkpoint(r)
                except (PermissionError,InterruptedError):timed=True;flags.append('cancel_or_revocation');break
                if time.monotonic()>deadline:timed=True;flags.append('timeout_process_tree_unknown');break
                try:line=lines.get(timeout=.5)
                except queue.Empty:continue
                if line is None:break
                seq+=1
                try:
                    native=json.loads(line)
                    try:diagnostics.record(native,seq)
                    except OSError:flags.append('diagnostic_storage_failed')
                    item=usage.filter_event(native,seq)
                except (ValueError,TypeError,AttributeError):flags.append('invalid_json_event');continue
                if item:
                    events.append(item);sink.write(json.dumps(item)+'\n');sink.flush()
            if timed:
                save(run/'execution-summary.json',diagnostics.summary())
                # Stop only our CLI; possible children mean lock stays RECOVERY_REQUIRED.
                proc.terminate()
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:pass
                return {'status':'RECOVERY_REQUIRED','events':events,'flags':flags,'pid':proc.pid,'exit_code':proc.poll()}
            code=proc.wait(timeout=10)
        save(run/'execution-summary.json',diagnostics.summary())
        status='completed' if code==0 and any(x['type']=='turn.completed' for x in events) and not any(x['type']=='turn.failed' for x in events) else 'failed'
        return {'status':status,'events':events,'flags':flags,'pid':proc.pid,'exit_code':code}

def receipt(c,r,run,before,after,events,status,version,flags,cap):
    meta={'task_id':r['id'],'iteration':1,'run_id':r['run_id'],'task_status':status,'source_kind':cap.get('source_kind','live'),
          'cli_version':version,'started_at':r['created'],'finished_at':now(),'process_id':cap.get('pid'),
          'exit_code':cap.get('exit_code'),'original_owner':'current ChatGPT conversation','grant_version':r['version']}
    out=usage.build_receipt(meta,before,after,events,capture_flags=flags)
    save(run/'usage.json',out);(run/'receipt.md').write_text(usage.markdown(out),encoding='utf-8')
    # SQLite journal serializes index rebuild; model execution is already over.
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE');items=[]
        for row in db.execute('SELECT id,run_id,status FROM tasks ORDER BY created'):
            p=c.root/'runs'/row['run_id']/'usage.json'
            if p.exists():
                u=load(p);t=u['roles']['plus_executor']['tokens']
                items.append({'task_id':row['id'],'run_id':row['run_id'],'task_status':u['task_status'],'source_kind':u['source_kind'],
                              'tokens':t['totals'],'token_statistics_complete':'statistics_incomplete' not in t['flags'],'usage_file':str(p.relative_to(c.root))})
        save(c.root/'usage-index.json',{'items':items})
    return out

def execute(c,task,executor=None):
    r=c.claim(task)
    e=executor or LiveExecutor()
    run=c.root/'runs'/r['run_id'];run.mkdir(parents=True,exist_ok=False)
    # Job data separate from controller-owned authority/receipts.
    job=c.root.parent/'jobs'/r['run_id']
    for sub in ('input','work','result'):(job/sub).mkdir(parents=True,exist_ok=False)
    before={};after={};events=[];flags=[];cap={};state='BLOCKED';artifacts=[];version='unavailable';broker=None
    save(run/'binding.json',{k:r[k] for k in ('id','run_id','subject','project','version','created')})
    try:
        c.checkpoint(r);g=c.authorized(r['subject'],r['project'],r['version']);body=json.loads(r['body']);plan=validate_plan(g,body['plan'])
        if executor is None and g.get('execution_mode')=='PCC_HOST_TRUSTED':
            from .host_trusted import HostTrustedExecutor
            e=HostTrustedExecutor()
        broker=Broker(c,r,g,run);inputs=broker.freeze_inputs(job,plan);python=broker.install(plan) or sys.executable
        version=e.version()
        if hasattr(e,'preflight'):e.preflight(c,r,job,run,python)
        c.update(task,'SAMPLING');before=e.sample(job/'work');save(run/'samples-before.json',before)
        plus=before.get('plus_executor',{}).get('account') or {};pro=before.get('pro_controller',{}).get('account') or {}
        if plus.get('type')!='chatgpt' or plus.get('planType')!='plus' or not plus.get('identity_sha256') or not pro.get('identity_sha256') or plus['identity_sha256']==pro['identity_sha256']:
            raise PermissionError('Plus independent identity not verified')
        c.checkpoint(r);cap=e.run(c,r,job,run,body['goal'],plan,python,g);events=cap['events'];flags=cap['flags']
        if cap['status']=='RECOVERY_REQUIRED':state='RECOVERY_REQUIRED'
        elif cap['status']!='completed':state='FAILED'
        else:
            c.update(task,'BROKER');c.checkpoint(r)
            if any(digest(inside(job/'input',x['path']))!=x['sha256'] for x in inputs):raise ValueError('input changed')
            artifacts=broker.freeze_outputs(job)
            if set(plan['expected_outputs'])-set(x['path'] for x in artifacts):raise ValueError('expected output missing')
            broker.apply(plan);state='LOCAL_CHECK'
    except Exception as ex:
        with c.db() as db: observed_pid=db.execute('SELECT pid FROM tasks WHERE id=?',(task,)).fetchone()[0]
        flags.append(type(ex).__name__)
        # A terminal guest response has no host Windows PID. Preserve failure rather
        # than misclassifying missing artifacts as a pre-execution block.
        state='FAILED' if cap.get('status') in ('completed','failed') else ('RECOVERY_REQUIRED' if observed_pid else 'BLOCKED')
        save(run/'failure.json',{'error_type':type(ex).__name__,'stage':'post_execution' if cap else 'pre_execution_or_capture','message':str(ex)[:300]})
        # Any uncertainty after spawn remains occupied, never automatically repeated.
    finally:
        cap.setdefault('source_kind',getattr(e,'source_kind','live'))
        try:after=e.sample(job/'work')
        except Exception:flags.append('post_sample_unavailable')
        save(run/'samples-after.json',after)
        save(run/'result.json',{'task_id':task,'run_id':r['run_id'],'status':state,'execution_mode':getattr(e,'execution_mode','injected_test_executor'),'local_check':state=='LOCAL_CHECK',
                              'artifacts':artifacts,'actions':broker.actions if broker else [],'flags':flags,'independent_review':'REVIEW_REQUIRED'})
        receipt(c,r,run,before,after,events,'completed' if state=='LOCAL_CHECK' else state.lower(),version,flags,cap)
        c.update(task,state)
    return load(run/'result.json')
