"""Offline tests: the Pro comparison runner never launches Codex here."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import pro_cli_comparison as pro


def snapshot(identity, role, used=10, weekly=20, reset=2000000000):
    return {"role": role, "account_status": "ok", "rate_limits_status": "ok",
            "sample_started_at": "2026-09-23T00:00:00+00:00",
            "rate_limits_sampled_at": "2026-09-23T00:00:01+00:00",
            "sample_finished_at": "2026-09-23T00:00:02+00:00",
            "account": {"type": "chatgpt", "planType": "plus" if role == "plus_identity_check" else "prolite",
                        "identity_sha256": identity},
            "rate_limits": {"rateLimitsByLimitId": {"codex": {
                "primary": {"usedPercent": used, "windowDurationMins": 300, "resetsAt": reset},
                "secondary": {"usedPercent": weekly, "windowDurationMins": 10080,
                              "resetsAt": 2100000000}}}}}


def events():
    native = [{"type": "thread.started", "thread_id": "synthetic-pro-session"},
              {"type": "turn.started", "turn_id": "turn-1"},
              {"type": "turn.completed", "turn_id": "turn-1", "event_id": "end-1",
               "usage": {"input_tokens": 100, "cached_input_tokens": 80,
                         "output_tokens": 20, "reasoning_output_tokens": 4}}]
    return [pro.usage.filter_event(item, number) for number, item in enumerate(native, 1)]


class ProComparisonTests(unittest.TestCase):
    def test_pro_environment_keeps_only_home_and_ca_from_auth_prefixes(self):
        with mock.patch.object(pro, "controller_env", return_value={
                "CODEX_HOME": "C:/synthetic/pro", "CODEX_CA_CERTIFICATE": "C:/synthetic/ca.pem",
                "CODEX_API_KEY": "forbidden", "CODEX_BASE_URL": "forbidden",
                "OPENAI_API_KEY": "forbidden", "OPENAI_BASE_URL": "forbidden",
                "CHATGPT_TOKEN": "forbidden", "C2C_ENDPOINT": "forbidden",
                "HTTP_PROXY": "http://proxy.example"}):
            env = pro.pro_env()
        self.assertEqual(env["CODEX_HOME"], "C:/synthetic/pro")
        self.assertEqual(env["CODEX_CA_CERTIFICATE"], "C:/synthetic/ca.pem")
        self.assertEqual(env["HTTP_PROXY"], "http://proxy.example")
        self.assertFalse(set(env).intersection({"CODEX_API_KEY", "CODEX_BASE_URL",
                                                 "OPENAI_API_KEY", "OPENAI_BASE_URL",
                                                 "CHATGPT_TOKEN", "C2C_ENDPOINT"}))

    def setUp(self):
        (ROOT / "synthetic").mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix="pro-comparison-test-", dir=ROOT / "synthetic")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for sub in ("input", "control", "work", "result"):
            (self.root / sub).mkdir()
        (self.root / "input/input.csv").write_text("step,value\n1,7\n", encoding="utf-8", newline="\n")
        (self.root / "control/task.txt").write_text("Synthetic task only; no private data.", encoding="utf-8")

    def run_fake(self, capture, config_probe=None, sampler=None):
        calls = {"pro_executor": 0}
        def sample(role, env, cwd):
            if sampler:
                return sampler(role, env, cwd)
            if role == "plus_identity_check":
                return snapshot("other-account", role)
            calls[role] += 1
            return snapshot("pro-account", role, used=10 if calls[role] == 1 else 12)
        effective = {"model": pro.MODEL, "model_reasoning_effort": pro.EFFORT,
                     "sandbox_mode": "workspace-write", "approval_policy": "never",
                     "forced_login_method": "chatgpt", "cli_auth_credentials_store": "file"}
        with (mock.patch.object(pro, "validate_separation", return_value={}),
              mock.patch.object(pro, "validate_cli", return_value="C:/synthetic/codex.exe"),
              mock.patch.object(pro, "pro_env", return_value={"CODEX_HOME": "pro"}),
              mock.patch.object(pro, "plus_environment", return_value={"CODEX_HOME": "plus"}),
              mock.patch.object(pro.usage, "cli_version", return_value="codex-cli synthetic")):
            return pro.run_once(self.root, "pro_token_test", 1, "pro_run_1", 90,
                                sampler=sample, capture=capture,
                                config_probe=config_probe or (lambda *_: effective))

    def test_success_cached_tokens_dedup_and_no_prompt_persisted(self):
        launch = mock.Mock()
        def capture(argv, prompt, env, cwd, sink, timeout, on_start):
            launch()
            self.assertEqual(cwd, self.root / "work")
            self.assertIn("model=\"gpt-6-sol\"", argv)
            self.assertIn("model_reasoning_effort=\"medium\"", argv)
            self.assertIn("sandbox_mode=\"workspace-write\"", argv)
            self.assertIn("--add-dir", argv)
            self.assertEqual(argv[argv.index("--add-dir") + 1], str(self.root / "result"))
            on_start(123)
            (self.root / "result/summary.json").write_text('{"count":1}', encoding="utf-8")
            (self.root / "result/summary.md").write_text("one\n", encoding="utf-8")
            ev = events() + [{**events()[-1], "source_seq": 4}]
            for item in ev:
                sink.write(json.dumps(item) + "\n")
            return {"pid": 123, "exit_code": 0, "events": ev, "flags": [], "timed_out": False}
        receipt = self.run_fake(capture)
        launch.assert_called_once()
        self.assertEqual(receipt["task_status"], "completed")
        tok = receipt["roles"]["pro_executor"]["tokens"]
        self.assertEqual(tok["totals"]["input_plus_output_tokens"], 120)
        self.assertEqual(tok["totals"]["cached_input_tokens"], 80)
        self.assertEqual(tok["duplicate_events_ignored"], 1)
        self.assertEqual(receipt["roles"]["pro_executor"]["five_hour"]["observed_change_pp"], 2)
        self.assertIsNone(receipt["roles"]["pro_web_planner_reviewer"]["tokens"])
        saved = (self.root / "pro-evidence/pro-usage.json").read_text(encoding="utf-8")
        self.assertNotIn("Synthetic task only", saved)
        self.assertEqual(receipt["outputs_sha256"]["summary.json"],
                         pro.sha256(self.root / "result/summary.json"))
        with self.assertRaises(FileExistsError):
            self.run_fake(capture)
        launch.assert_called_once()

    def test_preflight_mismatch_does_not_dispatch(self):
        capture = mock.Mock(side_effect=AssertionError("must not dispatch"))
        receipt = self.run_fake(capture, config_probe=lambda *_: (_ for _ in ()).throw(PermissionError()))
        capture.assert_not_called()
        self.assertEqual(receipt["task_status"], "not_started")
        self.assertEqual(receipt["cli_start_count"], 0)
        self.assertIsNone(receipt["roles"]["pro_executor"]["tokens"]["totals"]["input_tokens"])

    def test_identity_collision_does_not_dispatch(self):
        capture = mock.Mock(side_effect=AssertionError("must not dispatch"))
        receipt = self.run_fake(capture,
            sampler=lambda role, env, cwd: snapshot("same-account", role))
        capture.assert_not_called()
        self.assertEqual(receipt["task_status"], "not_started")

    def test_capture_failure_retains_filtered_usage_without_retry(self):
        launch = mock.Mock()
        def capture(argv, prompt, env, cwd, sink, timeout, on_start):
            launch()
            on_start(456)
            for item in events():
                sink.write(json.dumps(item) + "\n")
            raise OSError("private native diagnostic")
        receipt = self.run_fake(capture)
        launch.assert_called_once()
        self.assertEqual(receipt["task_status"], "unknown_after_dispatch")
        tok = receipt["roles"]["pro_executor"]["tokens"]
        self.assertIsNone(tok["totals"]["input_plus_output_tokens"])
        self.assertEqual(tok["observed_partial"]["input_plus_output_tokens"], 120)
        self.assertIn("capture_error_OSError", receipt["flags"])
        self.assertNotIn("private native diagnostic", json.dumps(receipt))

    def test_process_exit_uncertain_is_not_reported_terminal(self):
        def capture(argv, prompt, env, cwd, sink, timeout, on_start):
            on_start(789)
            return {"pid": 789, "exit_code": None, "events": events(),
                    "flags": ["process_exit_uncertain"], "timed_out": False}
        receipt = self.run_fake(capture)
        self.assertEqual(receipt["task_status"], "unknown_after_dispatch")
        self.assertIn("process_exit_uncertain", receipt["flags"])

    def test_missing_window_and_cross_reset_are_not_zero(self):
        before = snapshot("same", "pro_executor", used=90)
        after = snapshot("same", "pro_executor", used=2, reset=2000010000)
        meta = {"task_id": "synthetic", "run_id": "one", "task_status": "completed",
                "cli_version": "synthetic"}
        result = pro.make_receipt(meta, before, after, events(), [])
        five = result["roles"]["pro_executor"]["five_hour"]
        self.assertIn("cross_reset", five["flags"])
        self.assertIsNone(five["observed_change_pp"])
        after["rate_limits"]["rateLimitsByLimitId"]["codex"].pop("secondary")
        result = pro.make_receipt(meta, before, after, events(), [])
        week = result["roles"]["pro_executor"]["weekly"]
        self.assertIsNone(week["remaining_after_percent"])
        self.assertIn("window_missing", week["flags"])

    def test_event_storage_failure_keeps_partial_but_not_complete_total(self):
        meta = {"task_id": "synthetic", "run_id": "one", "task_status": "completed",
                "cli_version": "synthetic"}
        receipt = pro.make_receipt(meta, snapshot("same", "pro_executor"),
                                   snapshot("same", "pro_executor"), events(),
                                   ["usage_event_storage_failed"])
        tok = receipt["roles"]["pro_executor"]["tokens"]
        self.assertIsNone(tok["totals"]["input_plus_output_tokens"])
        self.assertEqual(tok["observed_partial"]["input_plus_output_tokens"], 120)
        self.assertIn("statistics_incomplete", tok["flags"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
