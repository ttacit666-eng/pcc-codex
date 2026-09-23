"""One-shot, synthetic Pro CLI execution and metadata-only usage receipt.

This deliberately does not share PCC's Plus dispatcher or its idempotency key.
The caller must prepare an unused directory beneath ``synthetic`` containing
``input/input.csv``, ``control/task.txt``, and empty ``work``/``result`` folders.
No authentication file, raw model event, prompt, or stderr is persisted.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pcc.runtime import controller_env, plus_environment, settings, validate_cli, validate_separation
from pcc.sandbox_rpc import SandboxRPC
from vendor.tools import usage_receipt as usage

MODEL = "gpt-6-sol"
EFFORT = "medium"
EXPECTED = ("summary.json", "summary.md")


def sha256(path):
    return hashlib.sha256(usage.plain_path(path).read_bytes()).hexdigest()


def pro_env():
    """Keep the existing Pro CODEX_HOME, exclude inherited alternate billing/routes."""
    env = controller_env()
    for key in list(env):
        name = key.upper()
        if (name.startswith(("CODEX_", "OPENAI_", "CHATGPT_", "C2C_"))
                and name not in {"CODEX_HOME", "CODEX_CA_CERTIFICATE"}):
            env.pop(key, None)
    return env


def paths(run_dir):
    root = usage.plain_path(run_dir)
    synthetic = usage.plain_path(ROOT / "synthetic")
    if not root.is_relative_to(synthetic) or root == synthetic or not root.is_dir():
        raise ValueError("run directory must be a new synthetic PCC subdirectory")
    input_file = usage.plain_path(root / "input" / "input.csv")
    prompt_file = usage.plain_path(root / "control" / "task.txt")
    work = usage.plain_path(root / "work")
    result = usage.plain_path(root / "result")
    evidence = usage.plain_path(root / "pro-evidence")
    if not input_file.is_file() or not prompt_file.is_file() or not work.is_dir() or not result.is_dir():
        raise ValueError("synthetic input/input.csv, control/task.txt, work, and result are required")
    if any(result.iterdir()) or evidence.exists():
        raise FileExistsError("Pro result or receipt already exists; never dispatch twice")
    return root, input_file, prompt_file, work, result, evidence


def argv_for(cli, root):
    work = root / "work"
    result = root / "result"
    trust = "projects." + json.dumps(os.path.normcase(os.path.abspath(work))) + '.trust_level="trusted"'
    return [str(cli), "--strict-config", "-c", "model=" + json.dumps(MODEL),
            "-c", "model_reasoning_effort=" + json.dumps(EFFORT),
            "-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"',
            "-c", 'forced_login_method="chatgpt"',
            "-c", 'cli_auth_credentials_store="file"', "-c", trust,
            "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--color", "never",
            "-C", str(work), "--add-dir", str(result), "-"]


def effective_config(argv, cwd, env, rpc_factory=SandboxRPC):
    """App Server config/read only; mismatch blocks before the model can start."""
    rpc = rpc_factory(argv, cwd, env)
    try:
        reply = rpc.call("config/read", {"includeLayers": False, "cwd": str(cwd)})
        if "error" in reply:
            raise RuntimeError("effective configuration rejected")
        cfg = reply.get("result", {}).get("config", {})
        keys = ("model", "model_reasoning_effort", "sandbox_mode", "approval_policy",
                "forced_login_method", "cli_auth_credentials_store")
        effective = {key: cfg.get(key) for key in keys}
        expected = {"model": MODEL, "model_reasoning_effort": EFFORT,
                    "sandbox_mode": "workspace-write", "approval_policy": "never",
                    "forced_login_method": "chatgpt", "cli_auth_credentials_store": "file"}
        if effective != expected:
            raise PermissionError("Pro model, ChatGPT authentication, or sandbox effective configuration mismatch")
        return effective
    finally:
        rpc.close()


def capture_once(argv, prompt, env, cwd, sink, timeout_seconds, on_start):
    """Capture only usage events. One Popen, no retry, bounded deadline."""
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    try:
        on_start(proc.pid)
    except Exception:
        # No prompt has been sent. An unrecorded launch must not keep running.
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    lines = queue.Queue()
    flags = []
    events = []

    def readout():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    def drainerr():
        # Never persist native stderr; only fixed diagnostic categories survive.
        for line in proc.stderr:
            lower = line.lower()
            if "sandbox" in lower:
                flags.append("sandbox_diagnostic")
            if "approval" in lower:
                flags.append("approval_diagnostic")

    def send():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (OSError, ValueError):
            flags.append("stdin_delivery_uncertain")

    threading.Thread(target=readout, daemon=True).start()
    threading.Thread(target=drainerr, daemon=True).start()
    threading.Thread(target=send, daemon=True).start()
    deadline = time.monotonic() + timeout_seconds
    seq = 0
    timed_out = False
    while True:
        if time.monotonic() >= deadline:
            timed_out = True
            flags.append("timeout_process_tree_unknown")
            break
        try:
            line = lines.get(timeout=min(0.5, max(0.001, deadline - time.monotonic())))
        except queue.Empty:
            continue
        if line is None:
            break
        seq += 1
        try:
            native = json.loads(line)
            item = usage.filter_event(native, seq) if isinstance(native, dict) else None
        except (ValueError, TypeError, AttributeError):
            flags.append("invalid_json_event")
            continue
        if item:
            events.append(item)
            try:
                sink.write(json.dumps(item, ensure_ascii=False) + "\n")
                sink.flush()
            except OSError:
                flags.append("usage_event_storage_failed")
    if timed_out:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            flags.append("process_did_not_exit_after_terminate")
    else:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            flags.append("process_exit_uncertain")
    return {"pid": proc.pid, "exit_code": proc.poll(), "events": events,
            "flags": sorted(set(flags)), "timed_out": timed_out}


def _filtered_events(path):
    events = []
    flags = []
    if not path.exists():
        return events, ["event_log_missing"]
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                    raise ValueError("bad filtered event")
                events.append(item)
            except (ValueError, TypeError):
                flags.append("incomplete_event_tail")
    except (OSError, UnicodeError):
        flags.append("event_log_read_failed")
    return events, flags


def make_receipt(meta, before, after, events, flags):
    tok = usage.tokens(events, meta["task_status"])
    capture_loss = {"invalid_json_event", "incomplete_event_tail", "event_log_read_failed",
                    "event_log_missing", "event_log_loss_possible", "usage_event_storage_failed"}
    if any(flag in capture_loss or flag.startswith("capture_error_") for flag in flags):
        tok["flags"] = sorted(set(tok["flags"] + ["statistics_incomplete", *flags]))
        tok["totals"] = {key: None for key in (*usage.CORE, "input_plus_output_tokens")}
    five = usage.quota(before, after, 300)
    week = usage.quota(before, after, 10080)
    all_flags = sorted(set(flags + tok["flags"] + ["parallel_usage_not_excluded"] +
                           ["five_hour:" + f for f in five["flags"]] +
                           ["weekly:" + f for f in week["flags"]]))
    return {"schema_version": 1, **meta, "generated_at": usage.now(),
            "account_role": "pro_executor", "session_id": tok["session_id"],
            "roles": {"pro_executor": {"before": before, "after": after,
                                        "tokens": tok, "token_status": "observed_exec_events",
                                        "five_hour": five, "weekly": week,
                                        "parallel_usage": "unknown"},
                      "pro_web_planner_reviewer": {"tokens": None, "token_status": "unknown_not_zero"}},
            "flags": all_flags, "no_token_to_quota_conversion": True,
            "exact_task_charge": "unknown"}


def markdown(receipt):
    role = receipt["roles"]["pro_executor"]
    totals = role["tokens"]["totals"]
    def fmt(value):
        return "未取得" if value is None else str(value)
    lines = ["# Pro CLI 合成对照用量", "任务：" + receipt["task_id"] + " / " + receipt["run_id"],
             "状态：" + receipt["task_status"] + "；CLI：" + fmt(receipt.get("cli_version")) +
             "；session：" + fmt(receipt["session_id"]),
             "模型：" + MODEL + " / " + EFFORT + "；UTC：" + receipt["generated_at"],
             "", "|输入|缓存输入（包含在输入中）|输出|输入+输出|",
             "|---:|---:|---:|---:|",
             "|" + "|".join(fmt(totals.get(key)) for key in (*usage.CORE, "input_plus_output_tokens")) + "|",
             "", "Pro 网页规划与审查 token：未取得；不是零。",
             "额度前后读数仅是账号观察变化，不是精确任务扣费；并行用量未排除。",
             "输入 SHA-256：" + receipt["input_sha256_before"],
             "输出哈希见 pro-usage.json；标记：" + ", ".join(receipt["flags"])]
    for name, key in (("五小时", "five_hour"), ("周", "weekly")):
        window = role[key]
        lines.append(name + "：前已用 " + fmt(window["before_used_percent"]) +
                     "% / 后已用 " + fmt(window["after_used_percent"]) +
                     "% / 观察变化 " + fmt(window["observed_change_pp"]) +
                     " 个百分点 / 后剩余 " + fmt(window["remaining_after_percent"]) +
                     "% / 重置 UTC " + fmt(window["reset_after_utc"]))
    return "\n".join(lines) + "\n"


def run_once(run_dir, task_id, iteration, run_id, timeout_seconds=300,
             sampler=usage.safe_sample, capture=capture_once, config_probe=effective_config):
    task_id = usage.safe_id(task_id)
    run_id = usage.safe_id(run_id)
    if type(iteration) is not int or iteration < 1:
        raise ValueError("iteration must be a positive integer")
    if type(timeout_seconds) is not int or not 30 <= timeout_seconds <= 1800:
        raise ValueError("timeout must be between 30 and 1800 seconds")
    validate_separation()
    root, input_file, prompt_file, work, result, evidence = paths(run_dir)
    prompt = prompt_file.read_text(encoding="utf-8")
    if not prompt.strip():
        raise ValueError("empty synthetic prompt")
    input_hash = sha256(input_file)
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    cli = validate_cli()
    version = usage.cli_version()
    argv = argv_for(cli, root)
    env = pro_env()
    evidence.mkdir(exist_ok=False)
    state = {"task_id": task_id, "iteration": iteration, "run_id": run_id,
             "task_status": "preflight", "cli_start_count": 0,
             "created_at": usage.now(), "input_sha256_before": input_hash,
             "prompt_sha256": prompt_hash, "requested_model": MODEL,
             "requested_reasoning_effort": EFFORT, "cli_version": version,
             "argv": argv, "cwd": str(work)}
    usage.atomic_json(evidence / "pro-state.json", state)
    before = None
    after = None
    events = []
    flags = []
    capture_result = {"pid": None, "exit_code": None, "timed_out": False}
    status = "blocked"
    effective = None
    try:
        effective = config_probe(argv, work, env)
        state["effective_config"] = effective
        usage.atomic_json(evidence / "pro-state.json", state)
        before = sampler("pro_executor", env, work)
        plus_identity = sampler("plus_identity_check", plus_environment(), work)
        usage.atomic_json(evidence / "pro-sample-before.json", before)
        pro_account = (before or {}).get("account") or {}
        plus_account = (plus_identity or {}).get("account") or {}
        if (pro_account.get("type") != "chatgpt" or not pro_account.get("identity_sha256")
            or plus_account.get("type") != "chatgpt" or plus_account.get("planType") != "plus"
            or not plus_account.get("identity_sha256")
            or pro_account["identity_sha256"] == plus_account["identity_sha256"]):
            raise PermissionError("Pro/Plus ChatGPT identity separation not verified")
        state.update(task_status="dispatching", dispatch_intent=True)
        usage.atomic_json(evidence / "pro-state.json", state)
        def started(pid):
            state.update(task_status="running", process_id=pid, cli_start_count=1,
                         process_started_at=usage.now())
            usage.atomic_json(evidence / "pro-state.json", state)
        with (evidence / "pro-events.jsonl").open("x", encoding="utf-8") as sink:
            capture_result = capture(argv, prompt, env, work, sink, timeout_seconds, started)
        events = capture_result["events"]
        flags.extend(capture_result["flags"])
        ended = any(event["type"] == "turn.completed" for event in events)
        failed_turn = any(event["type"] == "turn.failed" for event in events)
        outputs_present = all((result / name).is_file() for name in EXPECTED)
        status = ("completed" if capture_result["exit_code"] == 0 and ended and not failed_turn
                  and outputs_present and not capture_result["timed_out"] else "failed")
        if not outputs_present:
            flags.append("expected_output_missing")
        if capture_result["timed_out"] or any(
                flag in capture_result["flags"] for flag in
                ("process_exit_uncertain", "process_did_not_exit_after_terminate")):
            status = "unknown_after_dispatch"
    except Exception as error:
        flags.append("capture_error_" + type(error).__name__ if state.get("cli_start_count") else
                     "preflight_error_" + type(error).__name__)
        if state.get("cli_start_count"):
            events, recovered = _filtered_events(evidence / "pro-events.jsonl")
            flags.extend(recovered)
            status = "unknown_after_dispatch"
        else:
            status = "not_started"
    finally:
        if before is not None:
            try:
                after = sampler("pro_executor", env, work)
                usage.atomic_json(evidence / "pro-sample-after.json", after)
            except Exception as error:
                flags.append("post_sample_error_" + type(error).__name__)
        try:
            input_after = sha256(input_file)
        except (OSError, ValueError) as error:
            input_after = None
            flags.append("input_recheck_error_" + type(error).__name__)
        if input_after != input_hash:
            flags.append("input_changed")
            if status == "completed":
                status = "failed"
        output_hashes = {}
        for name in EXPECTED:
            if (result / name).is_file():
                try:
                    output_hashes[name] = sha256(result / name)
                except (OSError, ValueError) as error:
                    flags.append("output_hash_error_" + type(error).__name__)
                    if status == "completed":
                        status = "failed"
        meta = {"task_id": task_id, "iteration": iteration, "run_id": run_id,
                "task_status": status, "source_kind": "live_pro_cli",
                "cli_version": version, "requested_model": MODEL,
                "requested_reasoning_effort": EFFORT,
                "effective_config": effective, "started_at": state["created_at"],
                "finished_at": usage.now(), "process_id": capture_result.get("pid") or state.get("process_id"),
                "exit_code": capture_result.get("exit_code"),
                "input_sha256_before": input_hash, "input_sha256_after": input_after,
                "prompt_sha256": prompt_hash, "outputs_sha256": output_hashes,
                "expected_outputs": list(EXPECTED), "cli_start_count": state["cli_start_count"]}
        receipt = make_receipt(meta, before, after, events, flags)
        usage.atomic_json(evidence / "pro-usage.json", receipt)
        temp = evidence / "pro-usage.md.tmp"
        temp.write_text(markdown(receipt), encoding="utf-8", newline="\n")
        os.replace(temp, evidence / "pro-usage.md")
        state.update(task_status=status, finished_at=meta["finished_at"],
                     exit_code=meta["exit_code"], flags=receipt["flags"])
        usage.atomic_json(evidence / "pro-state.json", state)
        usage.atomic_json(evidence / "pro-index.json",
                          {"task_id": task_id, "run_id": run_id, "usage_file": "pro-usage.json",
                           "usage_sha256": sha256(evidence / "pro-usage.json"),
                           "task_status": status, "session_id": receipt["session_id"]})
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    result = run_once(args.run_dir, args.task_id, args.iteration, args.run_id,
                      args.timeout_seconds)
    # Console output is metadata only, never prompt or native model text.
    print(json.dumps({"task_id": result["task_id"], "run_id": result["run_id"],
                      "status": result["task_status"], "session_id": result["session_id"],
                      "receipt": str(args.run_dir / "pro-evidence" / "pro-usage.json")},
                     ensure_ascii=True))
    return 0 if result["task_status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
