"""Metadata-only receipts. No login/logout, credits/reset, or model dispatch here."""
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import stat
import subprocess
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
from pcc.runtime import settings, controller_env
_runtime = settings()
CLI = Path(_runtime["cli"])
PLUS_HOME = Path(_runtime["plus_home"])
CORE = ("input_tokens", "cached_input_tokens", "output_tokens")
ROLES = ("plus_executor", "pro_controller", "cwc_reviewer")
RPC_ALLOWED = {"initialize", "account/read", "account/rateLimits/read"}

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)

def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value) or value.endswith("."):
        raise ValueError("invalid metadata identifier")
    if value.split(".")[0].upper() in {"CON","PRN","AUX","NUL", *("COM"+str(i) for i in range(1,10)), *("LPT"+str(i) for i in range(1,10))}:
        raise ValueError("reserved metadata identifier")
    return value

def plain_path(path):
    p = Path(os.path.abspath(path))
    for q in (p, *p.parents):
        if q.exists() or q.is_symlink():
            s = q.lstat()
            if q.is_symlink() or getattr(s, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("reparse paths are not accepted")
    return p

def atomic_json(path, data):
    path = plain_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("x", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)

def plus_env():
    from pcc.runtime import validate_separation
    validate_separation(PLUS_HOME)
    e = os.environ.copy()
    for key in list(e):
        if (key.upper().startswith(("CODEX_", "OPENAI_", "CHATGPT_", "C2C_")) and key != "CODEX_CA_CERTIFICATE") or key in ("RUST_LOG", "RUST_LOG_STYLE"):
            e.pop(key, None)
    e["CODEX_HOME"] = str(PLUS_HOME)
    return e

def cli_version():
    try:
        value = subprocess.check_output([str(CLI), "--version"], text=True, encoding="utf-8", stderr=subprocess.DEVNULL, timeout=15).strip()
        return value if re.fullmatch(r"codex-cli [0-9A-Za-z.+-]+", value) else None
    except (OSError, subprocess.SubprocessError):
        return None

def numeric_tree(value):
    """Retain numeric/null usage data, not arbitrary string payloads."""
    if value is None or number(value) or isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return {k: numeric_tree(v) for k,v in value.items()
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,80}", k)
                and not re.search(r"password|secret|credential|authorization|api.?key", k, re.I)
                and (v is None or number(v) or isinstance(v, (bool,dict)))}
    return None

def sanitize_limits(result):
    def bucket(b):
        if not isinstance(b, dict):
            return None
        out = {}
        for key in ("primary", "secondary"):
            if key in b:
                out[key] = numeric_tree(b[key])
        for key in ("individualLimit", "spendControlReached"):
            if key in b:
                out[key] = numeric_tree(b[key])
        return out
    out = {}
    if "rateLimits" in result:
        b = result["rateLimits"]
        out["rateLimits"] = bucket(b)
        if isinstance(b,dict) and b.get("limitId") == "codex":
            out["legacy_is_codex"] = True
    if isinstance(result.get("rateLimitsByLimitId"),dict):
        out["rateLimitsByLimitId"] = {k:bucket(v) for k,v in result["rateLimitsByLimitId"].items()
                                     if re.fullmatch(r"[a-zA-Z0-9_.-]{1,96}",k)}
    return out

def sample(role, env, cwd, timeout=30):
    """Only allowlisted native read RPCs. Each call has a monotonic deadline."""
    out = {"role":role, "sample_started_at":now(), "account":None, "rate_limits":None,
           "account_status":"unavailable", "rate_limits_status":"unavailable"}
    proc = None
    try:
        trust='projects.'+json.dumps(os.path.normcase(os.path.abspath(cwd)))+'.trust_level="trusted"'
        proc = subprocess.Popen([str(CLI),"-c",trust,"app-server","--stdio"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, encoding="utf-8", cwd=cwd, env=env)
        messages = queue.Queue()
        def reader():
            for line in proc.stdout:
                try:
                    x=json.loads(line)
                    if x.get("id") in (1,2,3):
                        messages.put(x)
                except (ValueError,AttributeError):
                    pass
            messages.put(None)
        threading.Thread(target=reader,daemon=True).start()
        def call(i, method, params):
            if method not in RPC_ALLOWED:
                raise ValueError("RPC is not read-only allowlisted")
            proc.stdin.write(json.dumps({"id":i,"method":method,"params":params})+"\n")
            proc.stdin.flush()
            deadline=time.monotonic()+timeout
            while True:
                msg=messages.get(timeout=max(0.001, deadline-time.monotonic()))
                if msg is None:
                    raise EOFError()
                if msg.get("id")==i:
                    if "error" in msg:
                        raise RuntimeError("RPC rejected; raw error suppressed")
                    return msg.get("result",{})
                if time.monotonic()>=deadline:
                    raise TimeoutError()
        call(1,"initialize",{"clientInfo":{"name":"plus_task_usage","version":"1.0"}})
        proc.stdin.write('{"method":"initialized","params":{}}\n')
        proc.stdin.flush()
        try:
            a=call(2,"account/read",{"refreshToken":False}).get("account")
            out["account_sampled_at"]=now()
            if isinstance(a,dict):
                out["account"]={k:a.get(k) for k in ("type","planType")
                                if a.get(k) is None or re.fullmatch(r"[A-Za-z0-9_-]{1,48}",str(a[k]))}
                if isinstance(a.get("email"),str):
                    out["account"]["identity_sha256"]=hashlib.sha256(a["email"].strip().lower().encode()).hexdigest()
                out["account_status"]="ok"
            else:
                out["account_status"]="not_authenticated"
        except Exception as exc:
            out["account_error_type"]=type(exc).__name__
        try:
            out["rate_limits"]=sanitize_limits(call(3,"account/rateLimits/read",{}))
            out["rate_limits_status"]="ok"
            out["rate_limits_sampled_at"]=now()
        except Exception as exc:
            out["rate_limits_error_type"]=type(exc).__name__
    except Exception as exc:
        out["sample_error_type"]=type(exc).__name__
    finally:
        if proc is not None:
            try:
                proc.stdin.close()
                proc.wait(timeout=3)
            except Exception:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
        out["sample_finished_at"]=now()
    return out

def safe_sample(role, env, cwd, sampler=sample):
    try:
        return sampler(role,env,cwd)
    except Exception as exc:
        return {"role":role,"sample_started_at":now(),"sample_finished_at":now(),
                "account":None,"rate_limits":None,"account_status":"unavailable",
                "rate_limits_status":"unavailable","sample_error_type":type(exc).__name__}

def filter_event(event, seq):
    typ=event.get("type")
    if typ not in {"thread.started","turn.started","turn.completed","turn.failed","error"}:
        if isinstance(event.get("usage"),dict):
            typ="usage.scope.unhandled"
        else:
            return None
    out={"source_seq":seq,"sampled_at":now(),"type":typ}
    if typ=="thread.started":
        sid=event.get("thread_id")
        if isinstance(sid,str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}",sid):
            out["session_id"]=sid
    if "usage" in event and isinstance(event["usage"],dict):
        out["usage"]=numeric_tree(event["usage"])
        if out["usage"]!=event["usage"]:
            out["usage_fields_filtered"]=True
    for key in ("event_id","turn_id"):
        if isinstance(event.get(key),str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}",event[key]):
            out[key]=event[key]
    return out

def tokens(events, status):
    seen={}
    turns=[]
    session=None
    opened=False
    terminal=None
    flags=set()
    duplicate_count=0
    native_events={}
    completed_turns={}
    replay_turn=None
    active_turn=None
    for ev in events:
        if ev.get("usage_fields_filtered"):
            flags.add("usage_fields_filtered")
        seq=ev.get("source_seq")
        payload={k:v for k,v in ev.items() if k!="sampled_at"}
        if seq in seen:
            if seen[seq]!=payload:
                flags.add("duplicate_event_conflict")
            duplicate_count+=1
            continue
        seen[seq]=payload
        eid=ev.get("event_id")
        if eid:
            comparable={k:v for k,v in payload.items() if k!="source_seq"}
            if eid in native_events:
                if native_events[eid]!=comparable:flags.add("native_event_id_conflict")
                if ev.get("type") in ("turn.completed","turn.failed"):
                    end_tid=ev.get("turn_id")
                    if active_turn and end_tid in completed_turns and active_turn!=end_tid:
                        pass  # A replay must not close or replace another active turn.
                    elif active_turn and end_tid and active_turn!=end_tid:
                        flags.add("turn_binding_conflict")
                    elif active_turn is None or active_turn in completed_turns:
                        opened=False;replay_turn=None;active_turn=None
                    elif opened:
                        flags.add("native_event_binding_uncertain")
                duplicate_count+=1
                continue
            native_events[eid]=comparable
        typ=ev.get("type")
        if typ=="thread.started":
            if session is not None and ev.get("session_id")!=session:
                flags.add("multiple_sessions")
            session=ev.get("session_id")
        elif typ=="turn.started":
            start_tid=ev.get("turn_id")
            if start_tid and start_tid in completed_turns:
                duplicate_count+=1
                continue
            if opened:
                flags.add("duplicate_or_overlapping_turn_start")
            else:
                active_turn=start_tid
                opened=True
                terminal=None
        elif typ in ("turn.completed","turn.failed"):
            end_tid=ev.get("turn_id")
            tid=end_tid or active_turn
            if tid and tid in completed_turns:
                if completed_turns[tid]!=ev.get("usage",{}):flags.add("native_turn_id_conflict")
                duplicate_count+=1
                if active_turn is None or active_turn==tid:
                    opened=False;replay_turn=None;active_turn=None
                    terminal={k:v for k,v in ev.items() if k not in ("source_seq","sampled_at")}
                continue
            if active_turn and end_tid and active_turn!=end_tid:
                flags.add("turn_binding_conflict")
            if replay_turn:
                if completed_turns[replay_turn]!=ev.get("usage",{}):flags.add("native_turn_id_conflict")
                replay_turn=None;duplicate_count+=1
                continue
            if not opened:
                if terminal=={k:v for k,v in ev.items() if k not in ("source_seq","sampled_at")}:
                    duplicate_count+=1
                else:
                    flags.add("unbound_terminal_event")
                continue
            usage=ev.get("usage") if isinstance(ev.get("usage"),dict) else {}
            tid=ev.get("turn_id") or active_turn
            turns.append({"ordinal":len(turns)+1,"turn_id":tid,"state":typ,"usage":usage})
            if tid:completed_turns[tid]=usage
            terminal={k:v for k,v in ev.items() if k not in ("source_seq","sampled_at")}
            opened=False
        elif typ=="error":
            flags.add("exec_error_event")
        else:
            # Session snapshots/other counters are deliberately NOT added to turn totals.
            flags.add("unsupported_usage_scope")
    if opened:
        flags.add("unfinished_turn")
    if not turns or not session:
        flags.add("missing_terminal_or_session")
    total={}
    observed={}
    for field in CORE:
        values=[t["usage"].get(field) for t in turns]
        valid=[v for v in values if number(v) and v>=0]
        observed[field]=sum(valid) if valid else None
        total[field]=sum(valid) if values and len(valid)==len(values) else None
        if total[field] is None:
            flags.add("missing_"+field)
    # A failed/incomplete turn may contain an unreported suffix; never call a partial sum a full total.
    if opened or any(t["state"]=="turn.failed" for t in turns) or status not in ("completed","synthetic_completed"):
        flags.add("statistics_incomplete")
    total["input_plus_output_tokens"]=(total["input_tokens"]+total["output_tokens"]
                                      if total["input_tokens"] is not None and total["output_tokens"] is not None else None)
    if any(t["usage"].get("cached_input_tokens",0)>t["usage"].get("input_tokens",float("inf"))
           for t in turns if number(t["usage"].get("cached_input_tokens")) and number(t["usage"].get("input_tokens"))):
        flags.add("cached_exceeds_input")
    if flags:
        flags.add("statistics_incomplete")
    observed["input_plus_output_tokens"]=(observed["input_tokens"]+observed["output_tokens"]
        if observed["input_tokens"] is not None and observed["output_tokens"] is not None else None)
    if "statistics_incomplete" in flags:
        total={k:None for k in (*CORE,"input_plus_output_tokens")}
    return {"session_id":session,"totals":total,"observed_partial":observed,"turns":turns,
            "duplicate_events_ignored":duplicate_count,"flags":sorted(flags),
            "scope":"unique completed/failed turn records; no session cumulative snapshots added",
            "dedup_limit":"native identifiers used when available; without them, complete start/end groups replayed with new source sequence numbers cannot be distinguished from real equal-usage turns",
            "cached_input_is_not_added_again":True}

def window(sample_data, minutes):
    lim=(sample_data or {}).get("rate_limits") or {}
    mapping=lim.get("rateLimitsByLimitId")
    if isinstance(mapping,dict):
        bucket=mapping.get("codex") or {}
    else:
        bucket=(lim.get("rateLimits") or {}) if lim.get("legacy_is_codex") is True else {}
    matches=[v for k,v in bucket.items() if k in ("primary","secondary")
             and isinstance(v,dict) and v.get("windowDurationMins")==minutes]
    return matches[0] if len(matches)==1 else None

def epoch_time(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()
    except (ValueError,AttributeError):
        return None

def quota(before, after, minutes):
    b=window(before,minutes)
    a=window(after,minutes)
    used_b=(b or {}).get("usedPercent")
    used_a=(a or {}).get("usedPercent")
    rb=(b or {}).get("resetsAt")
    ra=(a or {}).get("resetsAt")
    flags=[]
    if b is None or a is None:
        flags.append("window_missing")
    valid_b=number(used_b) and 0<=used_b<=100
    valid_a=number(used_a) and 0<=used_a<=100
    if not valid_b or not valid_a or not number(rb) or not number(ra):
        flags.append("field_missing_or_invalid")
    after_epoch=epoch_time((after or {}).get("rate_limits_sampled_at"))
    cross=(number(rb) and number(ra) and rb!=ra) or (number(rb) and after_epoch is not None and after_epoch>=rb)
    if cross:
        flags.append("cross_reset")
    b_identity=((before or {}).get("account") or {}).get("identity_sha256")
    a_identity=((after or {}).get("account") or {}).get("identity_sha256")
    if not b_identity or not a_identity:
        flags.append("identity_unverified")
    elif b_identity!=a_identity:
        flags.append("identity_changed")
    raw=used_a-used_b if valid_a and valid_b else None
    if raw is not None and raw<0 and not cross:
        flags.append("nonmonotonic_reading")
    return {"window_minutes":minutes,"before_used_percent":used_b if valid_b else None,
            "after_used_percent":used_a if valid_a else None,
            "observed_change_pp":raw if not flags else None,
            "raw_reading_difference_pp":raw,
            "remaining_after_percent":100-used_a if valid_a else None,
            "reset_before_unix":rb,"reset_after_unix":ra,
            "reset_before_utc":dt.datetime.fromtimestamp(rb,dt.timezone.utc).isoformat() if number(rb) and 0<rb<32503680000 else None,
            "reset_after_utc":dt.datetime.fromtimestamp(ra,dt.timezone.utc).isoformat() if number(ra) and 0<ra<32503680000 else None,
            "flags":flags,"interpretation":"account snapshot change only; not exact task charge"}

def build_receipt(meta, before, after, events, parallel="unknown", capture_flags=()):
    tok=tokens(events,meta["task_status"])
    loss=[f for f in capture_flags if f in ("invalid_json_event","incomplete_event_tail","event_log_loss_possible") or f.startswith("capture_error_")]
    if loss:
        tok["flags"]=sorted(set(tok["flags"]+loss+["statistics_incomplete"]))
        tok["totals"]={k:None for k in (*CORE,"input_plus_output_tokens")}
    roles={}
    for role in ROLES:
        b=before.get(role)
        a=after.get(role)
        roles[role]={"before":b,"after":a,
                     "tokens":tok if role=="plus_executor" else None,
                     "token_status":"observed_exec_events" if role=="plus_executor" else "unknown_not_zero",
                     "five_hour":quota(b,a,300),"weekly":quota(b,a,10080),
                     "parallel_usage":parallel if role=="plus_executor" else "unknown",
                     "scope":"account-wide snapshot, not attributable to this role alone"}
    flags=set(tok["flags"])|set(capture_flags)
    if not meta.get("cli_version"):
        flags.add("cli_version_missing")
    for role,data in roles.items():
        if role!="cwc_reviewer":
            for win in ("five_hour","weekly"):
                flags.update(role+":"+f for f in data[win]["flags"])
        if data["before"] is None or data["after"] is None:
            flags.add(role+":sampling_missing")
    flags.add("parallel_usage_not_excluded")
    if any("missing" in f or "invalid" in f or "incomplete" in f or "unverified" in f for f in flags):
        flags.add("statistics_incomplete")
    return {"schema_version":1,**meta,"session_id":tok["session_id"],
            "generated_at":now(),"account_role":"plus_executor",
            "roles":roles,"flags":sorted(flags),
            "no_token_to_quota_conversion":True,"exact_task_charge":"unknown",
            "review_usage_note":"CWC reviewer separately unknown; do not duplicate controller account readings"}

def markdown(r):
    t=r["roles"]["plus_executor"]["tokens"]["totals"]
    def fmt(v):return "未知" if v is None else str(v)
    lines=["# 每任务用量回执",f"任务：{r['task_id']} / iteration {r['iteration']} / run {r['run_id']}",
           f"状态：{r['task_status']}；session：{fmt(r['session_id'])}；CLI：{fmt(r.get('cli_version'))}",
           f"生成时间：{r['generated_at']}（UTC）；角色：plus_executor",
           "", "|角色|输入|缓存输入（已包含在输入中）|输出|输入+输出|",
           "|---|---:|---:|---:|---:|",
           "|Plus 执行器|"+"|".join(fmt(t[k]) for k in (*CORE,"input_plus_output_tokens"))+"|",
           "|Pro 控制器|未知|未知|未知|未知|","|CWC 审查|未知|未知|未知|未知|","",
           "|角色/窗口|前已用%|后已用%|观察变化(百分点)|后剩余%|重置前/后 UTC|",
           "|---|---:|---:|---:|---:|---|"]
    for role in ROLES:
        for key in ("five_hour","weekly"):
            q=r["roles"][role][key]
            lines.append("|"+role+"/"+key+"|"+"|".join(fmt(q[k]) for k in ("before_used_percent","after_used_percent","observed_change_pp","remaining_after_percent"))+"|"+fmt(q["reset_before_utc"])+" / "+fmt(q["reset_after_utc"])+"|")
    lines += ["","仅为账号前后读数变化，不是精确任务扣费；不从 token 推算套餐百分比。缺失不填零。",
              "并行用量未排除；跨重置/身份不明时观察增量为未知，原始差值仅保留在 JSON。",
              "其他实际数值用量字段及采样时间保留在 usage.json / usage-events.jsonl。",
              "标记："+", ".join(r["flags"])]
    return "\n".join(lines)+"\n"

def receipt_dir(meta, root=ROOT):
    task=safe_id(meta["task_id"])
    run=safe_id(meta["run_id"])
    iteration=meta["iteration"]
    if type(iteration) is not int or iteration<0:
        raise ValueError("invalid iteration")
    return plain_path(Path(root)/"receipts"/task/str(iteration)/run)

def write_receipt(receipt, root=ROOT):
    root=plain_path(root)
    directory=receipt_dir(receipt,root)
    directory.mkdir(parents=True,exist_ok=True)
    atomic_json(directory/"usage.json",receipt)
    # JSON is canonical; interrupted MD/index writes are recoverable without execution.
    tmp=directory/("usage.md."+uuid.uuid4().hex+".tmp")
    tmp.write_text(markdown(receipt),encoding="utf-8")
    os.replace(tmp,directory/"usage.md")
    rebuild_index(root)
    return directory

def rebuild_index(root=ROOT):
    root=plain_path(root)
    lock=root/"index.lock"
    fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    os.close(fd)
    try:
        rows=[]
        for path in sorted((root/"receipts").glob("*/*/*/usage.json")):
            path=plain_path(path)
            data=json.loads(path.read_text(encoding="utf-8"))
            rows.append({"task_id":data["task_id"],"iteration":data["iteration"],"run_id":data["run_id"],
                         "session_id":data.get("session_id"),"task_status":data["task_status"],
                         "generated_at":data["generated_at"],"path":path.relative_to(root).as_posix(),
                         "source_kind":data.get("source_kind","unknown"),
                         "flags":data["flags"],
                         "token_statistics_complete":"statistics_incomplete" not in data["roles"]["plus_executor"]["tokens"]["flags"],
                         "usage_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
                         "input_plus_output_tokens":data["roles"]["plus_executor"]["tokens"]["totals"]["input_plus_output_tokens"]})
        atomic_json(root/"index.json",{"schema_version":1,"updated_at":now(),"items":rows})
    finally:
        lock.unlink()
