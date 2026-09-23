"""Single-task entry with receipts. 'sample' and 'reindex' NEVER dispatch a task."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import hashlib
from contextlib import contextmanager

import usage_receipt as u

@contextmanager
def registry(root):
    path=Path(root)/"locks"/"registry.lock"
    path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    os.close(fd)
    try:yield
    finally:path.unlink()

TERMINAL={"completed","failed","blocked","not_started"}

def scope_key(path):
    path=Path(path).resolve()
    def digest(p):return hashlib.sha256(os.path.normcase(str(p)).encode()).hexdigest()
    return {"root":digest(path),"ancestors":[digest(p) for p in path.parents]}

def overlap(a,b):
    return a["root"]==b["root"] or a["root"] in b["ancestors"] or b["root"] in a["ancestors"]

def reserve(meta, root=u.ROOT, writable_scopes=()):
    root=u.plain_path(root)
    task=u.safe_id(meta["task_id"])
    u.receipt_dir(meta,root)
    locks=root/"locks"
    locks.mkdir(parents=True,exist_ok=True)
    # task + iteration spans run directories; abandoned/failed locks are NOT auto-reclaimed.
    lock=u.plain_path(locks/(task+"."+str(meta["iteration"])+".lock"))
    scopes=[scope_key(u.plain_path(x)) for x in writable_scopes]
    with registry(root):
        for candidate in locks.glob("*.lock"):
            if candidate.name=="registry.lock":continue
            row=json.loads(u.plain_path(candidate).read_text(encoding="utf-8"))
            if row.get("status") not in TERMINAL:
                for old in row.get("writable_scope_hashes",[]):
                    if any(overlap(old,new) for new in scopes):
                        raise FileExistsError("active or unknown task overlaps writable scope")
        with lock.open("x",encoding="utf-8") as f:
            json.dump({"task_id":task,"iteration":meta["iteration"],"run_id":meta["run_id"],
                       "reserved_at":u.now(),"status":"reserved","controller_pid":os.getpid(),
                       "writable_scope_hashes":scopes},f)
    return lock

def finish_lock(lock,status,root):
    with registry(root):
        row=json.loads(lock.read_text(encoding="utf-8"))
        row["status"]=status;row["finished_at"]=u.now()
        u.atomic_json(lock,row)

def capture_exec(argv, prompt, env, cwd, sink, on_start=None):
    events=[]
    flags=[]
    proc=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                          cwd=cwd,env=env,text=True,encoding="utf-8",errors="replace")
    if on_start is not None:
        try:on_start(proc.pid)
        except Exception:flags.append("startup_pid_persistence_failed")
    def send():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (OSError,ValueError):
            flags.append("stdin_delivery_uncertain")
    sender=threading.Thread(target=send,daemon=True)
    sender.start()
    for seq,line in enumerate(proc.stdout,1):
        try:
            native=json.loads(line)
            item=u.filter_event(native,seq) if isinstance(native,dict) else None
        except (ValueError,TypeError):
            flags.append("invalid_json_event")
            continue
        if item:
            events.append(item)
            try:
                sink.write(json.dumps(item,ensure_ascii=False)+"\n")
                sink.flush()
            except OSError:
                flags.append("usage_event_storage_failed")
    code=proc.wait()
    sender.join(timeout=3)
    return {"exit_code":code,"pid":proc.pid,"events":events,"flags":sorted(set(flags))}

def task_paths(root):
    root=u.plain_path(root)
    protected=[u.ROOT,u.PLUS_HOME,Path(u.settings()['controller_home']),Path.home()]
    for p in protected:
        p=p.resolve()
        if root==p or root in p.parents:
            raise ValueError("task root overlaps a protected parent")
        if p!=Path.home() and p in root.parents:
            raise ValueError("task root inside protected directory")
    if not root.is_dir() or (root/"control/dispatch.lock").exists():
        raise ValueError("missing root or existing legacy dispatch lock")
    for name in ("input","work","result"):
        if not u.plain_path(root/name).is_dir():
            raise ValueError("task root needs input/work/result directories")
    if any((root/"result").iterdir()):
        raise ValueError("result directory is not empty; no overwrite/re-dispatch")
    return root

def prompt_path_for(taskroot,path):
    control=u.plain_path(taskroot/"control")
    candidate=u.plain_path(path)
    if control not in candidate.parents or not candidate.is_file():
        raise ValueError("prompt must be an ordinary file under the authorized task control directory")
    return candidate

def argv_for(root):
    def path(p):return str(p).replace("\\","/")
    rules={":root":"deny",":minimal":"read",path(root/"input"):"read",path(root/"work"):"write",
           path(root/"result"):"write",path(u.ROOT):"deny",path(Path(u.settings()['controller_home'])):"deny",
           path(u.PLUS_HOME/"auth.json"):"deny",path(u.PLUS_HOME/".sandbox-secrets"):"deny",
           path(Path(sys.executable).parent):"read",
           str(Path(__import__("shutil").which("pwsh") or __import__("shutil").which("powershell") or sys.executable).parent):"read"}
    inline="{ filesystem = { "+", ".join(json.dumps(k)+" = "+json.dumps(v) for k,v in rules.items())+" }, network = { enabled = false } }"
    return [str(u.CLI),"--strict-config","-c",'default_permissions="plus-receipt-task"',
            "-c","permissions.plus-receipt-task="+inline,
            "-c",'approval_policy="never"',"-c",'forced_login_method="chatgpt"',
            "exec","--json","--ephemeral","--skip-git-repo-check","--color","never","-C",str(root/"work"),"-"]

def run(meta, cwd, argv, prompt, root=u.ROOT, sampler=u.sample, executor=capture_exec, version=None, scope_validator=None):
    directory=u.receipt_dir(meta,root)
    lock=reserve(meta,root,[cwd,Path(cwd).parent/"result"])
    directory.mkdir(parents=True,exist_ok=True)
    before={}
    after={}
    events=[]
    capture={"exit_code":None,"pid":None,"flags":[]}
    status="blocked"
    # Whole-function exceptions must never enter an exec retry loop.
    meta={"source_kind":"live",**meta,"started_at":u.now(),"cli_version":version if version is not None else u.cli_version(),
          "dispatch_intent":False,"startup_attempted":False,"cli_start_count":0,"backend_model_request_count":None}
    u.atomic_json(directory/"state.json",{**meta,"task_status":"sampling_before","dispatch_intent":False,"cli_start_count":0})
    scope_valid=True
    try:
        # This check is AFTER reservation. Other cooperating task entries cannot write these scopes now.
        (scope_validator or (lambda:task_paths(Path(cwd).parent)))()
    except (OSError,ValueError):
        scope_valid=False
        capture["flags"].append("scope_preflight_failed_no_dispatch")
    env=u.plus_env()
    for role,e in (("plus_executor",env),("pro_controller",u.controller_env())):
        before[role]=u.safe_sample(role,e,cwd,sampler)
    u.atomic_json(directory/"samples-before.json",before)
    a=before["plus_executor"].get("account") or {}
    pro=before["pro_controller"].get("account") or {}
    collision=bool(a.get("identity_sha256") and a.get("identity_sha256")==pro.get("identity_sha256"))
    identity_verified=bool(a.get("identity_sha256") and pro.get("identity_sha256"))
    if scope_valid and a.get("type")=="chatgpt" and a.get("planType")=="plus" and identity_verified and not collision:
        meta["dispatch_intent"]=True
        u.atomic_json(directory/"state.json",{**meta,"task_status":"dispatching","dispatch_intent":True,"startup_attempted":False,"cli_start_count":0})
        env["TMP"]=env["TEMP"]=str(cwd)
        def started(pid):
            capture["pid"]=pid
            meta.update(cli_start_count=1,process_id=pid,process_started_at=u.now())
            u.atomic_json(directory/"state.json",{**meta,"task_status":"running","dispatch_intent":True,
                          "startup_attempted":True,"cli_start_count":1,"process_id":pid,"process_started_at":u.now(),
                          "backend_model_request_count":None})
        try:
            # Opening the output sink precedes spawn; failure cannot trigger a second spawn.
            with (directory/"usage-events.jsonl").open("x",encoding="utf-8") as sink:
                meta["startup_attempted"]=True
                u.atomic_json(directory/"state.json",{**meta,"task_status":"starting","dispatch_intent":True,"startup_attempted":True,"cli_start_count":0})
                capture=executor(argv,prompt,env,cwd,sink,on_start=started)
            events=capture["events"]
            if capture.get("pid") is not None:meta["cli_start_count"]=1
            if capture["exit_code"]!=0 or any(e.get("type")=="turn.failed" for e in events):
                status="failed"
            elif any(e.get("type")=="turn.completed" for e in events):
                status="completed"
            else:
                status="unknown_after_dispatch"
        except Exception as exc:
            status="unknown_after_dispatch" if capture.get("pid") is not None else "not_started"
            capture["flags"].append("capture_error_"+type(exc).__name__)
            # Only previously persisted metadata is recovered. No process/model retry.
            path=directory/"usage-events.jsonl"
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:events.append(json.loads(line))
                    except ValueError:capture["flags"].append("incomplete_event_tail")
    else:
        if scope_valid:capture["flags"].append("plus_identity_not_verified_no_dispatch")
        if collision:
            capture["flags"].append("account_identity_collision")
    for role,e in (("plus_executor",env),("pro_controller",u.controller_env())):
        after[role]=u.safe_sample(role,e,cwd,sampler)
    u.atomic_json(directory/"samples-after.json",after)
    meta.update(task_status=status,finished_at=u.now(),process_id=capture.get("pid"),
                exit_code=capture.get("exit_code"),execution_entry="pinned official CLI via run_plus_task.py",
                original_owner="original Pro conversation")
    dispatch_count=1 if capture.get("pid") is not None else 0
    u.atomic_json(directory/"state.json",{**meta,"cli_start_count":dispatch_count,"backend_model_request_count":None,
                                          "capture_flags":capture["flags"]})
    receipt=u.build_receipt(meta,before,after,events,capture_flags=capture["flags"])
    receipt["flags"]=sorted(set(receipt["flags"]+capture["flags"]))
    if capture["flags"]:
        receipt["flags"]=sorted(set(receipt["flags"]+["statistics_incomplete"]))
    try:
        u.write_receipt(receipt,root)
    finally:
        finish_lock(lock,status,root)
    return receipt

def recover(meta, root=u.ROOT):
    """Recreate metadata receipts ONLY. Never sample accounts or start a process."""
    # Serialize against new reservations; active/unknown owners cannot be recovered over.
    with registry(root):
        return recover_locked(meta,root)

def recover_locked(meta,root):
    directory=u.receipt_dir(meta,root)
    lock=Path(root)/"locks"/(u.safe_id(meta["task_id"])+"."+str(meta["iteration"])+".lock")
    owner=json.loads(u.plain_path(lock).read_text(encoding="utf-8"))
    if any(owner.get(k)!=meta[k] for k in ("task_id","iteration","run_id")):
        raise ValueError("lock binding mismatch; preserve files")
    if owner.get("run_id")!=meta["run_id"] or owner.get("status") not in TERMINAL:
        raise RuntimeError("active or unknown controller; refuse receipt overwrite")
    state=json.loads((directory/"state.json").read_text(encoding="utf-8"))
    if any(state.get(k)!=meta[k] for k in ("task_id","iteration","run_id")):
        raise ValueError("state binding mismatch; preserve files")
    if state.get("task_status") not in TERMINAL:
        raise RuntimeError("unfinished state; refuse receipt overwrite")
    canonical=directory/"usage.json"
    if canonical.exists():
        existing=json.loads(canonical.read_text(encoding="utf-8"))
        if any(existing.get(k)!=meta[k] for k in ("task_id","iteration","run_id")):
            raise ValueError("canonical receipt binding conflict; preserve existing file")
        # Keep the canonical measurement and timestamp; only repair its derived MD/index.
        u.write_receipt(existing,root)
        return existing
    before=json.loads((directory/"samples-before.json").read_text(encoding="utf-8")) if (directory/"samples-before.json").exists() else {}
    after=json.loads((directory/"samples-after.json").read_text(encoding="utf-8")) if (directory/"samples-after.json").exists() else {}
    events=[]
    flags=state.get("capture_flags",[])[:]
    if "usage_event_storage_failed" in flags:
        flags.append("event_log_loss_possible")
    path=directory/"usage-events.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:events.append(json.loads(line))
            except ValueError:flags.append("incomplete_event_tail")
    receipt=u.build_receipt(state,before,after,events,capture_flags=flags)
    receipt["flags"]=sorted(set(receipt["flags"]+flags+["recovered_metadata_only"]))
    u.write_receipt(receipt,root)
    return receipt

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    sub=ap.add_subparsers(dest="command",required=True)
    sub.add_parser("sample",help="read-only live native API smoke test; no model task")
    sub.add_parser("reindex",help="rebuild persistent index only; never execute")
    recover_parser=sub.add_parser("recover",help="rebuild receipt from saved metadata; no RPC or execution")
    recover_parser.add_argument("--task-id",required=True)
    recover_parser.add_argument("--iteration",type=int,required=True)
    recover_parser.add_argument("--run-id",required=True)
    p=sub.add_parser("run",help="explicitly dispatch ONE newly authorized task")
    p.add_argument("--task-id",required=True)
    p.add_argument("--iteration",type=int,required=True)
    p.add_argument("--run-id",required=True)
    p.add_argument("--task-root",required=True)
    p.add_argument("--prompt-file",required=True)
    args=ap.parse_args()
    if args.command=="reindex":
        u.rebuild_index()
        print("Index rebuilt. No task dispatched.")
    elif args.command=="recover":
        recover({"task_id":args.task_id,"iteration":args.iteration,"run_id":args.run_id})
        print("Receipt regenerated from saved metadata. No task dispatched.")
    elif args.command=="sample":
        out={"kind":"native_read_only_smoke","time":u.now(),"cli_version":u.cli_version(),
             "model_dispatches":0,"samples":{}}
        for role,env in (("plus_executor",u.plus_env()),("pro_controller",u.controller_env())):
            out["samples"][role]=u.safe_sample(role,env,u.ROOT)
        dest=u.ROOT/"review"/("native-smoke-"+u.now().replace(":","").replace("+","_")+".json")
        u.atomic_json(dest,out)
        print(str(dest))
    else:
        taskroot=task_paths(args.task_root)
        prompt_path=prompt_path_for(taskroot,args.prompt_file)
        # Never store prompt or its path in the receipt.
        prompt=prompt_path.read_text(encoding="utf-8")
        meta={"task_id":args.task_id,"iteration":args.iteration,"run_id":args.run_id}
        receipt=run(meta,taskroot/"work",argv_for(taskroot),prompt)
        print(str(u.receipt_dir(meta)/"usage.md"))
        return 0 if receipt["task_status"]=="completed" else 2
    return 0

if __name__=="__main__":
    try:
        sys.exit(main())
    except (OSError,ValueError) as exc:
        print("Stopped safely; no automatic retry. Error type: "+type(exc).__name__,file=sys.stderr)
        sys.exit(3)
