import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
import usage_receipt as u
import run_plus_task as runner

def run_mock(*args,**kwargs):
    # Unit tests inject fake execution and scope validation; the real CLI uses strict defaults.
    kwargs.setdefault("scope_validator",lambda:None)
    return runner.run(*args,**kwargs)

def snapshot(used=10,weekly=20,reset=2000000000,identity="synthetic-plus"):
    return {"role":"plus_executor","sample_started_at":"2026-09-16T00:00:00+00:00",
            "rate_limits_sampled_at":"2026-09-16T00:00:01+00:00",
            "sample_finished_at":"2026-09-16T00:00:02+00:00",
            "account_status":"ok","rate_limits_status":"ok",
            "account":{"type":"chatgpt","planType":"plus","identity_sha256":identity},
            "rate_limits":{"rateLimitsByLimitId":{"codex":{
                "primary":{"usedPercent":used,"windowDurationMins":300,"resetsAt":reset},
                "secondary":{"usedPercent":weekly,"windowDurationMins":10080,"resetsAt":2100000000}}}}}
def events(usage=None):
    if usage is None:usage={"input_tokens":100,"cached_input_tokens":80,"output_tokens":20,"reasoning_output_tokens":4}
    return [u.filter_event({"type":"thread.started","thread_id":"synthetic-session"},1),
            u.filter_event({"type":"turn.started"},2),
            u.filter_event({"type":"turn.completed","usage":usage},3)]
def meta(status="synthetic_completed",run="run1"):
    return {"task_id":"synthetic_usage_test","iteration":1,"run_id":run,"cli_version":"synthetic","task_status":status,"source_kind":"synthetic"}
def receipt(b=None,a=None,ev=None,status="synthetic_completed",run="run1"):
    return u.build_receipt(meta(status,run),{"plus_executor":b or snapshot()},
                           {"plus_executor":a or snapshot(12,21)},ev if ev is not None else events())

class UsageTests(unittest.TestCase):
    def test_normal_cached_not_added_twice(self):
        r=receipt();p=r["roles"]["plus_executor"]
        self.assertEqual(p["tokens"]["totals"]["input_plus_output_tokens"],120)
        self.assertEqual(p["tokens"]["totals"]["cached_input_tokens"],80)
        self.assertEqual(p["five_hour"]["observed_change_pp"],2)
        self.assertEqual(p["five_hour"]["remaining_after_percent"],88)
        self.assertEqual(p["weekly"]["remaining_after_percent"],79)
        self.assertEqual(p["tokens"]["turns"][0]["usage"]["reasoning_output_tokens"],4)

    def test_missing_window_not_zero(self):
        a=snapshot();a["rate_limits"]["rateLimitsByLimitId"]["codex"]["primary"]=None
        q=receipt(a=a)["roles"]["plus_executor"]["five_hour"]
        self.assertIsNone(q["remaining_after_percent"]);self.assertIsNone(q["observed_change_pp"])
        self.assertIn("window_missing",q["flags"])

    def test_weekly_in_primary_is_not_five_hour(self):
        a=snapshot();b=a["rate_limits"]["rateLimitsByLimitId"]["codex"];b["primary"]=b.pop("secondary")
        self.assertIsNone(u.window(a,300));self.assertEqual(u.window(a,10080)["usedPercent"],20)

    def test_cross_reset(self):
        q=receipt(b=snapshot(90),a=snapshot(2,reset=2000010000))["roles"]["plus_executor"]["five_hour"]
        self.assertIsNone(q["observed_change_pp"]);self.assertEqual(q["raw_reading_difference_pp"],-88)
        self.assertIn("cross_reset",q["flags"])

    def test_elapsed_reset_detected_even_if_stale_reset_id(self):
        a=snapshot();a["rate_limits_sampled_at"]="2040-01-01T00:00:00+00:00"
        self.assertIn("cross_reset",u.quota(snapshot(),a,300)["flags"])

    def test_duplicate_source_and_terminal_not_counted(self):
        e=events();e+=[copy.deepcopy(e[-1]),{**e[-1],"source_seq":4}]
        t=u.tokens(e,"completed")
        self.assertEqual(t["totals"]["input_plus_output_tokens"],120)
        self.assertEqual(t["duplicate_events_ignored"],2)

    def test_same_usage_in_two_real_turns_counts_twice(self):
        e=events();e+=[{**e[1],"source_seq":4},{**e[2],"source_seq":5}]
        self.assertEqual(u.tokens(e,"completed")["totals"]["input_plus_output_tokens"],240)

    def test_cumulative_snapshots_never_added(self):
        e=events()+[{"source_seq":4,"type":"session_cumulative","usage":{"input_tokens":100,"output_tokens":20}},
                    {"source_seq":5,"type":"session_cumulative","usage":{"input_tokens":100,"output_tokens":20}}]
        t=u.tokens(e,"completed")
        self.assertIsNone(t["totals"]["input_plus_output_tokens"])
        self.assertEqual(t["observed_partial"]["input_plus_output_tokens"],120)
        self.assertIn("unsupported_usage_scope",t["flags"])

    def test_missing_token_no_zero(self):
        t=u.tokens(events({"input_tokens":20,"output_tokens":3,"new_tokens":7,"future_field":None}),"completed")
        self.assertIsNone(t["totals"]["cached_input_tokens"])
        self.assertEqual(t["turns"][0]["usage"]["new_tokens"],7)
        self.assertIsNone(t["turns"][0]["usage"]["future_field"])
        self.assertIn("statistics_incomplete",t["flags"])

    def test_failure_without_usage_unknown(self):
        e=events()[:2]+[u.filter_event({"type":"turn.failed","error":{"message":"PRIVATE BUSINESS"}},3)]
        r=receipt(ev=e,status="failed")
        self.assertIsNone(r["roles"]["plus_executor"]["tokens"]["totals"]["input_plus_output_tokens"])
        self.assertIn("statistics_incomplete",r["flags"])
        self.assertNotIn("PRIVATE BUSINESS",json.dumps(r))

    def test_failure_keeps_observed_usage(self):
        e=events();e[-1]["type"]="turn.failed"
        t=u.tokens(e,"failed")
        self.assertIsNone(t["totals"]["input_plus_output_tokens"])
        self.assertEqual(t["observed_partial"]["input_plus_output_tokens"],120)
        self.assertIn("statistics_incomplete",t["flags"])

    def test_controller_reviewer_unknown_separately(self):
        r=receipt()
        for role in ("pro_controller","cwc_reviewer"):
            self.assertIsNone(r["roles"][role]["tokens"])
            self.assertEqual(r["roles"][role]["token_status"],"unknown_not_zero")

    def test_identity_changed_suppresses_comparable_delta(self):
        q=receipt(a=snapshot(12,identity="different"))["roles"]["plus_executor"]["five_hour"]
        self.assertIsNone(q["observed_change_pp"]);self.assertIn("identity_changed",q["flags"])

    def test_percent_invalid(self):
        q=u.quota(snapshot(),snapshot(101),300)
        self.assertIsNone(q["remaining_after_percent"]);self.assertIsNone(q["after_used_percent"])

    def test_null_is_not_zero_and_multi_bucket_preferred(self):
        b=snapshot();b["rate_limits"]["rateLimits"]={"primary":{"usedPercent":99,"windowDurationMins":300}}
        b["rate_limits"]["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"]=None
        self.assertIsNone(u.quota(b,snapshot(),300)["before_used_percent"])

    def test_privacy_filter_excludes_messages_commands_errors(self):
        self.assertIsNone(u.filter_event({"type":"item.completed","item":{"text":"PROMPT","command":"SECRET"}},1))
        e=u.filter_event({"type":"error","message":"SECRET"},2)
        self.assertNotIn("SECRET",json.dumps(e))
        e=u.filter_event({"type":"turn.completed","usage":{"input_tokens":2,"api_key":"SECRET","password":99}},3)
        self.assertNotIn("password",json.dumps(e));self.assertNotIn("SECRET",json.dumps(e))

    def test_idempotent_index_and_rebuild_no_dispatch(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d);r=receipt()
            u.write_receipt(r,root);u.write_receipt(r,root);u.rebuild_index(root)
            items=json.loads((root/"index.json").read_text())["items"]
            self.assertEqual(len(items),1)
            self.assertEqual(items[0]["input_plus_output_tokens"],120)

    def test_atomic_task_iteration_lock_spans_run_ids(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            runner.reserve(meta(),Path(d))
            with self.assertRaises(FileExistsError):runner.reserve(meta(run="run2"),Path(d))

    def test_bad_identifiers(self):
        for v in ("../outside","CON","a/b","a.",".."):
            with self.assertRaises(ValueError):u.safe_id(v)

    def test_rate_query_failure_does_not_repeat_business(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d);calls=[];samples=[]
            def sample(role,env,cwd):
                samples.append(role)
                if len(samples)>2:raise TimeoutError("RAW SECRET")
                s=snapshot(identity=role);s["rate_limits"]=None;s["rate_limits_status"]="unavailable";return s
            def execute(argv,prompt,env,cwd,sink,on_start=None):
                if on_start:on_start(123)
                calls.append(1)
                for e in events():sink.write(json.dumps(e)+"\n")
                return {"exit_code":0,"pid":123,"events":events(),"flags":[]}
            r=run_mock(meta(),root,["synthetic-only"],"DO NOT PERSIST",root,sample,execute,version="synthetic")
            self.assertEqual(len(calls),1);self.assertEqual(r["task_status"],"completed")
            self.assertIn("statistics_incomplete",r["flags"])
            with self.assertRaises(FileExistsError):
                run_mock(meta(),root,[], "",root,sample,execute,version="synthetic")
            self.assertEqual(len(calls),1)
            alltext="\n".join(p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file())
            self.assertNotIn("DO NOT PERSIST",alltext);self.assertNotIn("RAW SECRET",alltext)

    def test_identity_query_failure_blocks_without_exec(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            ex=mock.Mock(side_effect=AssertionError("must not dispatch"))
            def fail(*args):raise TimeoutError()
            r=run_mock(meta(),Path(d),[],"",Path(d),fail,ex,version="synthetic")
            self.assertEqual(r["task_status"],"blocked");ex.assert_not_called()

    def test_failed_executor_never_retried(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            ex=mock.Mock(side_effect=OSError("raw business error"))
            r=run_mock(meta(),Path(d),[],"",Path(d),lambda role,*a:snapshot(identity=role),ex,version="synthetic")
            self.assertEqual(ex.call_count,1)
            self.assertEqual(r["task_status"],"not_started")
            self.assertNotIn("raw business error",json.dumps(r))

    def test_recover_only_metadata_no_rpc_or_exec(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d)
            def ex(argv,prompt,env,cwd,sink,on_start=None):
                if on_start:on_start(123)
                for e in events():sink.write(json.dumps(e)+"\n")
                return {"exit_code":0,"pid":123,"events":events(),"flags":[]}
            run_mock(meta(),root,[],"",root,lambda role,*a:snapshot(identity=role),ex,version="synthetic")
            with mock.patch.object(runner.subprocess,"Popen",side_effect=AssertionError("no process")):
                r=runner.recover(meta(),root)
            self.assertEqual(r["task_status"],"completed")
            self.assertEqual(r["roles"]["plus_executor"]["tokens"]["totals"]["input_plus_output_tokens"],120)

    def test_capture_stream_saves_only_usage(self):
        fake=mock.Mock()
        fake.pid=7
        fake.stdin=io.StringIO()
        native=[{"type":"thread.started","thread_id":"synthetic-session"},
                {"type":"turn.started"},{"type":"item.completed","item":{"text":"PRIVATE BODY"}},
                {"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":80,"output_tokens":20}}]
        fake.stdout=io.StringIO("\n".join(json.dumps(x) for x in native))
        fake.wait.return_value=0
        sink=io.StringIO()
        with mock.patch.object(runner.subprocess,"Popen",return_value=fake) as popen:
            result=runner.capture_exec([],"PRIVATE PROMPT",{},Path(__file__).parent,sink)
        self.assertEqual(popen.call_count,1)
        self.assertEqual(result["exit_code"],0)
        self.assertNotIn("PRIVATE",sink.getvalue())
        self.assertEqual(len(result["events"]),3)

    def test_index_lock_failure_does_not_erase_receipt(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d);(root/"index.lock").write_text("locked",encoding="utf-8")
            with self.assertRaises(FileExistsError):u.write_receipt(receipt(),root)
            self.assertTrue((u.receipt_dir(meta(),root)/"usage.json").is_file())

    def test_wrong_legacy_bucket_not_labeled_codex(self):
        s=snapshot();s["rate_limits"]={"rateLimits":{"primary":{"usedPercent":1,"windowDurationMins":300}}}
        self.assertIsNone(u.window(s,300))
        s["rate_limits"]["legacy_is_codex"]=True
        self.assertEqual(u.window(s,300)["usedPercent"],1)

    def test_same_identity_roles_blocks_execution(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            ex=mock.Mock(side_effect=AssertionError("must not dispatch"))
            r=run_mock(meta(),Path(d),[],"",Path(d),lambda *a:snapshot(),ex,version="synthetic")
            ex.assert_not_called();self.assertIn("account_identity_collision",r["flags"])

    def test_missing_identity_blocks_even_with_plus_plan(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            def sam(*args):
                s=snapshot();s["account"].pop("identity_sha256");return s
            ex=mock.Mock(side_effect=AssertionError("must not dispatch"))
            r=run_mock(meta(),Path(d),[],"",Path(d),sam,ex,version="synthetic")
            ex.assert_not_called();self.assertEqual(r["task_status"],"blocked")

    def test_unfinished_turn_partial_is_not_full_index_total(self):
        ev=events()+[u.filter_event({"type":"turn.started"},4)]
        r=receipt(ev=ev,status="unknown_after_dispatch")
        self.assertIsNone(r["roles"]["plus_executor"]["tokens"]["totals"]["input_plus_output_tokens"])
        self.assertEqual(r["roles"]["plus_executor"]["tokens"]["observed_partial"]["input_plus_output_tokens"],120)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            u.write_receipt(r,Path(d));idx=json.loads((Path(d)/"index.json").read_text(encoding="utf-8"))["items"][0]
            self.assertIsNone(idx["input_plus_output_tokens"])
            self.assertFalse(idx["token_statistics_complete"])
            self.assertIn("statistics_incomplete",idx["flags"])

    def test_different_task_overlapping_scope_refused(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d);sc=root/"work"
            runner.reserve(meta(),root,[sc])
            other={**meta(),"task_id":"another_task"}
            with self.assertRaises(FileExistsError):runner.reserve(other,root,[sc/"child"])
            with self.assertRaises(FileExistsError):runner.reserve(other,root,[sc.parent])

    def test_recover_refuses_active_owner_without_overwrite(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d);runner.reserve(meta(),root,[root/"work"])
            r=receipt();u.write_receipt(r,root)
            before=(u.receipt_dir(meta(),root)/"usage.json").read_bytes()
            with self.assertRaises(RuntimeError):runner.recover(meta(),root)
            self.assertEqual(before,(u.receipt_dir(meta(),root)/"usage.json").read_bytes())

    def test_completed_scope_releases_but_task_id_stays_locked(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d);lock=runner.reserve(meta(),root,[root/"work"])
            runner.finish_lock(lock,"completed",root)
            runner.reserve({**meta(),"task_id":"next_task"},root,[root/"work"])
            with self.assertRaises(FileExistsError):runner.reserve(meta(),root,[])

    def test_prompt_must_be_in_control_before_read(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d);(root/"control").mkdir()
            good=root/"control"/"task.txt";good.write_text("synthetic",encoding="utf-8")
            bad=root/"outside.txt";bad.write_text("must not read",encoding="utf-8")
            self.assertEqual(runner.prompt_path_for(root,good),good)
            with self.assertRaises(ValueError):runner.prompt_path_for(root,bad)

    def test_recover_preserves_canonical_when_event_log_truncated(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d)
            def ex(argv,prompt,env,cwd,sink,on_start=None):
                on_start(123)
                return {"exit_code":0,"pid":123,"events":events(),"flags":["usage_event_storage_failed"]}
            r=run_mock(meta(),root,[],"",root,lambda role,*a:snapshot(identity=role),ex,version="synthetic")
            path=u.receipt_dir(meta(),root)/"usage.json";before=path.read_bytes()
            recovered=runner.recover(meta(),root)
            self.assertEqual(recovered,r);self.assertEqual(path.read_bytes(),before)

    def test_pid_persisted_before_executor_finishes(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d)
            def ex(argv,prompt,env,cwd,sink,on_start=None):
                on_start(123)
                s=json.loads((u.receipt_dir(meta(),root)/"state.json").read_text(encoding="utf-8"))
                self.assertEqual(s["process_id"],123);self.assertEqual(s["cli_start_count"],1)
                raise OSError("capture broke after launch")
            r=run_mock(meta(),root,[],"",root,lambda role,*a:snapshot(identity=role),ex,version="synthetic")
            self.assertEqual(r["task_status"],"unknown_after_dispatch")
            self.assertEqual(r["process_id"],123)

    def test_log_open_failure_is_zero_starts(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d);directory=u.receipt_dir(meta(),root);directory.mkdir(parents=True)
            (directory/"usage-events.jsonl").write_text("",encoding="utf-8")
            ex=mock.Mock(side_effect=AssertionError("must not spawn"))
            r=run_mock(meta(),root,[],"",root,lambda role,*a:snapshot(identity=role),ex,version="synthetic")
            ex.assert_not_called();self.assertEqual(r["task_status"],"not_started")
            s=json.loads((directory/"state.json").read_text(encoding="utf-8"));self.assertEqual(s["cli_start_count"],0)

    def test_native_turn_id_deduplicates_replayed_group(self):
        e=events();e[1]["turn_id"]="t1";e[2]["turn_id"]="t1"
        e += [{**e[1],"source_seq":4},{**e[2],"source_seq":5}]
        self.assertEqual(u.tokens(e,"completed")["totals"]["input_plus_output_tokens"],120)

    def test_native_event_id_deduplicates_new_sequence(self):
        e=events();e[2]["event_id"]="terminal-1"
        e.append({**e[2],"source_seq":4})
        self.assertEqual(u.tokens(e,"completed")["totals"]["input_plus_output_tokens"],120)

    def test_cumulative_through_filter_storage_and_recover(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d)
            def ex(argv,prompt,env,cwd,sink,on_start=None):
                on_start(123)
                filtered=events()+[u.filter_event({"type":"session_cumulative","usage":{"input_tokens":100,"output_tokens":20}},4)]
                for e in filtered:sink.write(json.dumps(e)+"\n")
                return {"exit_code":0,"pid":123,"events":filtered,"flags":[]}
            r=run_mock(meta(),root,[],"",root,lambda role,*a:snapshot(identity=role),ex,version="synthetic")
            recovered=runner.recover(meta(),root)
            self.assertIn("unsupported_usage_scope",recovered["roles"]["plus_executor"]["tokens"]["flags"])
            self.assertEqual(recovered["roles"]["plus_executor"]["tokens"]["observed_partial"]["input_plus_output_tokens"],120)
            saved=(u.receipt_dir(meta(),root)/"usage-events.jsonl").read_text(encoding="utf-8")
            self.assertIn("usage.scope.unhandled",saved)

    def test_parse_loss_propagates_to_token_totals_and_index(self):
        r=u.build_receipt(meta(),{"plus_executor":snapshot()},{"plus_executor":snapshot(12)},events(),capture_flags=["invalid_json_event"])
        t=r["roles"]["plus_executor"]["tokens"]
        self.assertIsNone(t["totals"]["input_plus_output_tokens"])
        self.assertEqual(t["observed_partial"]["input_plus_output_tokens"],120)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            u.write_receipt(r,Path(d));item=json.loads((Path(d)/"index.json").read_text(encoding="utf-8"))["items"][0]
            self.assertFalse(item["token_statistics_complete"]);self.assertIsNone(item["input_plus_output_tokens"])

    def test_log_storage_only_does_not_erase_known_tokens(self):
        r=u.build_receipt(meta(),{"plus_executor":snapshot()},{"plus_executor":snapshot()},events(),capture_flags=["usage_event_storage_failed"])
        self.assertEqual(r["roles"]["plus_executor"]["tokens"]["totals"]["input_plus_output_tokens"],120)

    def test_only_terminal_has_native_turn_id_replay(self):
        e=events();e[2]["turn_id"]="t1"
        e += [{**e[1],"source_seq":4},{**e[2],"source_seq":5}]
        self.assertEqual(u.tokens(e,"completed")["totals"]["input_plus_output_tokens"],120)

    def test_turn_binding_conflict_suppresses_full_total(self):
        e=events();e[1]["turn_id"]="t1";e[2]["turn_id"]="t2"
        t=u.tokens(e,"completed")
        self.assertIn("turn_binding_conflict",t["flags"]);self.assertIsNone(t["totals"]["input_plus_output_tokens"])

    def test_native_event_and_turn_id_together_replay(self):
        e=events();e[2].update(turn_id="t1",event_id="e1")
        e += [{**e[1],"source_seq":4},{**e[2],"source_seq":5}]
        t=u.tokens(e,"completed")
        self.assertEqual(t["totals"]["input_plus_output_tokens"],120)

    def test_recover_missing_canonical_checks_state_binding(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d);lock=runner.reserve(meta(),root);runner.finish_lock(lock,"completed",root)
            directory=u.receipt_dir(meta(),root);directory.mkdir(parents=True)
            u.atomic_json(directory/"state.json",{**meta("completed"),"task_id":"WRONG"})
            with self.assertRaises(ValueError):runner.recover(meta(),root)
            self.assertFalse((root/"receipts/WRONG").exists());self.assertFalse((directory/"usage.json").exists())

    def test_recover_missing_canonical_keeps_not_started(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d);lock=runner.reserve(meta(),root);runner.finish_lock(lock,"not_started",root)
            directory=u.receipt_dir(meta(),root);directory.mkdir(parents=True)
            u.atomic_json(directory/"state.json",{**meta("not_started"),"cli_start_count":0})
            r=runner.recover(meta(),root)
            self.assertEqual(r["task_status"],"not_started");self.assertEqual(r["cli_start_count"],0)

    def test_missing_canonical_truncated_log_is_incomplete(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d);lock=runner.reserve(meta(),root);runner.finish_lock(lock,"completed",root)
            directory=u.receipt_dir(meta(),root);directory.mkdir(parents=True)
            u.atomic_json(directory/"state.json",meta("completed"))
            (directory/"usage-events.jsonl").write_text("\n".join(json.dumps(e) for e in events())+'\n{"incomplete":',encoding="utf-8")
            r=runner.recover(meta(),root)
            self.assertIsNone(r["roles"]["plus_executor"]["tokens"]["totals"]["input_plus_output_tokens"])
            self.assertIn("incomplete_event_tail",r["roles"]["plus_executor"]["tokens"]["flags"])

    def test_stale_empty_precheck_revalidated_under_reservation(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d);task=root/"task"
            for sub in ("input","work","result"):(task/sub).mkdir(parents=True)
            with mock.patch.object(u,"ROOT",root/"statistics"):
                runner.task_paths(task)  # B checks empty, then A completes a result before B reserves.
                target=task/"result/keep.txt";target.write_text("existing A result",encoding="utf-8")
                ex=mock.Mock(side_effect=AssertionError("must not launch B"))
                r=run_mock(meta(),task/"work",[],"",root,lambda role,*a:snapshot(identity=role),ex,version="synthetic",scope_validator=lambda:runner.task_paths(task))
                ex.assert_not_called();self.assertEqual(r["task_status"],"blocked")
                self.assertEqual(target.read_text(encoding="utf-8"),"existing A result")

    def test_missing_canonical_cumulative_log_recovery(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d);lock=runner.reserve(meta(),root);runner.finish_lock(lock,"completed",root)
            directory=u.receipt_dir(meta(),root);directory.mkdir(parents=True)
            u.atomic_json(directory/"state.json",meta("completed"))
            ev=events()+[u.filter_event({"type":"session_cumulative","usage":{"input_tokens":100,"output_tokens":20}},4)]
            (directory/"usage-events.jsonl").write_text("\n".join(json.dumps(x) for x in ev),encoding="utf-8")
            r=runner.recover(meta(),root)
            self.assertIn("unsupported_usage_scope",r["roles"]["plus_executor"]["tokens"]["flags"])
            self.assertIsNone(r["roles"]["plus_executor"]["tokens"]["totals"]["input_plus_output_tokens"])

    def test_old_replay_preserves_new_unfinished_turn(self):
        for native_event_id in (False,True):
            e=events();e[1]["turn_id"]="t1";e[2]["turn_id"]="t1"
            if native_event_id:e[2]["event_id"]="end-t1"
            e += [{"type":"turn.started","turn_id":"t2","source_seq":4},
                  {**e[1],"source_seq":5},{**e[2],"source_seq":6}]
            r=u.build_receipt(meta(),{}, {},e)
            t=r["roles"]["plus_executor"]["tokens"]
            self.assertEqual(t["observed_partial"]["input_plus_output_tokens"],120)
            self.assertIsNone(t["totals"]["input_plus_output_tokens"])
            self.assertIn("unfinished_turn",t["flags"])
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
                u.write_receipt(r,Path(d))
                self.assertFalse(json.loads((Path(d)/"index.json").read_text(encoding="utf-8"))["items"][0]["token_statistics_complete"])

    def test_old_replay_then_new_turn_completes_once(self):
        for native_event_id in (False,True):
            e=events();e[1]["turn_id"]="t1";e[2]["turn_id"]="t1"
            if native_event_id:e[2]["event_id"]="end-t1"
            e += [{"type":"turn.started","turn_id":"t2","source_seq":4},
                  {**e[1],"source_seq":5},{**e[2],"source_seq":6},
                  {"type":"turn.completed","turn_id":"t2","source_seq":7,"usage":e[2]["usage"]}]
            t=u.tokens(e,"completed")
            self.assertEqual(t["totals"]["input_plus_output_tokens"],240)
            self.assertEqual(len(t["turns"]),2);self.assertEqual(t["flags"],[])

if __name__=="__main__":
    unittest.main(verbosity=2)
