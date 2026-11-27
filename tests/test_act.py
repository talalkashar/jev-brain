"""Exercise the CLI and MCP over pipes. No test has a path to a live executor."""

import json
import os
from pathlib import Path
import signal
import subprocess
import hashlib
from importlib.metadata import PackageNotFoundError, distribution
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import srcpath
from jev_brain import foreground, local
from jev_brain.cli import _consent, _doctor
from test_cli import ROOT, SRC, _env, _run
from test_use import _SURFACES


class ActTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.env = _env(PATH=str(self.bin), HOME=temp.name, USERPROFILE=temp.name,
                        PYTHONPATH=os.pathsep.join([temp.name, str(SRC)]))
        # Inject only the OS observation. Both entry points run their real gates.
        (self.home / "sitecustomize.py").write_text(
            "import os, sys\nfrom types import SimpleNamespace\nfrom jev_brain import foreground, computer_use\n"
            "if os.name == 'posix': computer_use.sys = SimpleNamespace(platform='darwin', executable=sys.executable)\n"
            # Isolate short AX and pipe-drain deadlines from filesystem latency.
            # Dedicated audit tests cover stalled initial and final writes.
            "if os.getenv('SLOW_PROBE') or os.getenv('HELD_PIPE'):\n"
            "    computer_use.local.append_audit = lambda *args: (1, 1)\n"
            "def current():\n"
            "    if os.getenv('SLOW_PROBE'):\n"
            "        from pathlib import Path\n"
            "        (Path(os.environ['HOME']) / 'observing').touch()\n"
            "        __import__('time').sleep(5)\n"
            "    if os.getenv('NO_PROBE'): raise AssertionError('unexpected desktop access')\n"
            "    return foreground.Window(os.getenv('APP', 'TextEdit'), os.getenv('TITLE', 'Untitled'),"
            " os.getenv('URL', ''), os.getenv('ROLE', 'AXTextArea'), 'com.apple.TextEdit', 100, placeholder=os.getenv('HINT',''))\n"
            "foreground.current = current\n"
            "foreground.target_metadata = lambda window, label, deadline: ('AXButton', '', label, '', '')\n"
            "if 'SHORT_TIMEOUT' in os.environ: computer_use.TIMEOUT = float(os.environ['SHORT_TIMEOUT'])\n",
            encoding="utf-8")
        stub = self.bin / "jev-run"
        stub.write_bytes(f"#!{sys.executable}\n".encode() + (ROOT / "tests/stubs/jev-run").read_bytes())
        stub.chmod(0o700)
        self.args = ["use", "--act", "--expect", "TextEdit", "--target", "focused field",
                     "--request", "Write a greeting", "--text", "Hello there!"]
        self.state = self.home / ".config/jev-brain"

    def allow(self):
        self.state.mkdir(parents=True, mode=0o700, exist_ok=True)
        consent = self.state / "consent.json"
        consent.write_text('{"allow_act": true}')
        consent.chmod(0o600)

    def call(self, args=None, **env):
        return _run(self.args if args is None else args, env=self.env | env)

    def audit(self):
        return [json.loads(line) for line in (self.state / "audit.jsonl").read_text().splitlines()]

    def test_requires_consent_and_rejects_piped_grant(self):
        result = self.call(NO_PROBE="1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("jev-brain consent", json.loads(result.stdout)["error"])
        refused = subprocess.run([sys.executable, "-m", "jev_brain.cli", "consent"],
                                 input="ALLOW\n", capture_output=True, text=True, env=self.env)
        self.assertEqual(refused.returncode, 2)
        self.assertFalse((self.state / "consent.json").exists())
        self.assertFalse((self.home / "calls").exists())
        self.assertEqual(self.audit()[-1]["outcome"], "refused")

    @unittest.skipUnless(os.name == "posix", "fake macOS executor")
    def test_expect_required_and_checks_foreground_including_actual_sensitive_window(self):
        self.allow()
        for args, env, outcome in (
            (self.args[:2] + self.args[4:], {"NO_PROBE": "1"}, "refused"),
            (self.args, {"APP": "Finder"}, "wrong window"),
            (self.args, {"APP": "Mail"}, "sensitive"),
            (self.args, {"TITLE": "Banking"}, "sensitive"),
            (self.args, {"ROLE": "AXSecureTextField"}, "sensitive"),
            (self.args, {"APP": "Google Chrome", "URL": "https://example.org"}, "wrong window"),
        ):
            with self.subTest(outcome=outcome, env=env):
                result = self.call(args, **env)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(json.loads(result.stdout)["outcome"], outcome)
        self.assertFalse((self.home / "calls").exists())

    @unittest.skipUnless(os.name == "posix", "fake macOS executor")
    def test_armed_preview_checks_real_probe_before_executor(self):
        for env in ({"APP": "Mail"}, {"HINT": "Password"}, {"ROLE": ""}):
            result = self.call(["use", "--arm", "--target", "TextEdit", "--request", "Count windows"], **env)
            self.assertEqual(result.returncode, 2)
            self.assertFalse((self.home / "calls").exists())

    @unittest.skipUnless(os.name == "posix", "fake macOS executor")
    def test_success_writes_no_argument_canaries(self):
        self.allow()
        for action in (["--target", "CONTROL-CANARY"], ["--target", "focused field", "--text", "TYPED-CANARY"]):
            result = self.call(["use", "--act", "--expect", "TextEdit", "--request", "REQUEST-CANARY", *action])
            self.assertEqual(result.returncode, 0, result.stderr)
        for path in (*self.state.iterdir(), self.home / "calls"):
            self.assertNotIn("CANARY", path.read_text())

    def test_sensitive_inputs_refused_before_observing_or_executing(self):
        self.allow()
        for _, phrase in _SURFACES[:7]:
            for flag in ("--target", "--request", "--expect", "--text"):
                args = self.args.copy()
                args[args.index(flag) + 1] = phrase
                result = self.call(args, NO_PROBE="1")
                self.assertEqual(result.returncode, 2, (flag, phrase, result.stderr))
                self.assertEqual(json.loads(result.stdout)["outcome"], "sensitive")
        self.assertFalse((self.home / "calls").exists())
        records = self.audit()
        self.assertEqual(len(records), 7 * 4 * 2)
        self.assertTrue(all("request" not in r and "target" not in r for r in records))

    @unittest.skipUnless(os.name == "posix", "fake macOS executor")
    def test_calls_exact_runner_and_appends_outcomes(self):
        self.allow()
        for outcome, code in (("text typed", 0), ("wrong window", 3),
                              ("target not found", 3), ("low confidence", 3), ("stalled", 3)):
            result = self.call(OUTCOME=outcome)
            self.assertEqual(result.returncode, code, result.stderr)
            body = json.loads(result.stdout)
            self.assertEqual(body["outcome"], outcome)
            self.assertEqual((body["confidence"], body["steps"]), (0.92, int(not code)))
            if not code:
                self.assertEqual((body["clicked"], body["typed"]), (None, "Hello there!"))
        calls = [json.loads(s) for s in (self.home / "calls").read_text().splitlines()]
        self.assertEqual(len(calls), 5)
        args = dict(goal="Write a greeting", expect="TextEdit", target=None,
                    text="Hello there!", act=True, steps="1", handoffs="0")
        self.assertEqual(calls[0], hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest())
        records = self.audit()
        self.assertEqual(len(records), 10)
        self.assertEqual(records[0]["call"], records[1]["call"])
        self.assertEqual(records[-1]["outcome"], "stalled")
        self.assertNotIn("request", records[-1])
        self.assertEqual(records[-1]["app"], "com.apple.TextEdit")
        self.assertIn("time", records[-1])
        self.assertNotIn("Hello there!", (self.state / "audit.jsonl").read_text())
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["audit.jsonl", "consent.json"])

    @unittest.skipUnless(os.name == "posix", "fake macOS executor")
    def test_step_cap_and_untrusted_outcome_never_enter_audit(self):
        self.allow()
        for env in ({"STEPS": "2"}, {"OUTCOME": "UNTRUSTED-CANARY"}):
            with self.subTest(env=env):
                result = self.call(["use", "--act", "--expect", "TextEdit", "--target", "New",
                                    "--request", "Review REQUEST-CANARY"], **env)
                self.assertEqual(json.loads(result.stdout)["outcome"], "runner error")
                self.assertNotIn("CANARY", (self.state / "audit.jsonl").read_text())

    @unittest.skipUnless(os.name == "posix", "POSIX subprocess supervisor")
    def test_deadline_includes_foreground_probe(self):
        self.allow()
        start = time.monotonic()
        result = self.call(["use", "--act", "--expect", "TextEdit", "--target", "New",
                            "--request", "Review"], SLOW_PROBE="1", SHORT_TIMEOUT="1")
        self.assertEqual(result.returncode, 124, result.stdout)
        self.assertLess(time.monotonic() - start, 3)
        self.assertTrue((self.home / "observing").exists())
        self.assertFalse(json.loads(result.stdout)["started"])
        self.assertFalse((self.home / "calls").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX subprocess supervisor")
    def test_direct_runner_uses_audited_supervisor(self):
        # Replace only the supervisor's backend command in the fixture process.
        # The real isolated interpreter must never reach the desktop in this test.
        fake = self.home / "test_backend.py"
        fake.write_text("import os, time, json\nfrom pathlib import Path\n"
                        "if os.getenv('WAIT'):\n"
                        "    (Path(os.environ['HOME']) / ('ready-' + os.environ['WAIT'])).touch()\n"
                        "    time.sleep(5)\n"
                        "print(json.dumps(dict(clicked='New', steps_taken=1, outcome='target clicked')))\n")
        with (self.home / "sitecustomize.py").open("a") as f:
            f.write(f"computer_use.backend_command = lambda: [sys.executable, {str(fake)!r}]\n")
        env = self.env.copy()
        env["PYTHONPATH"] += os.pathsep + str(ROOT / "runner")
        try:
            dist = distribution("jev-brain-macos-runner")
        except PackageNotFoundError:
            entry = [sys.executable, "-c", "import jev_run; raise SystemExit(jev_run.main())"]
        else:
            script = next(f for f in dist.files if f.name == "jev-run")
            entry = [str(dist.locate_file(script))]
        cmd = [*entry, "Review", "--act", "--target", "New", "--expect", "TextEdit"]
        for mode, outcome, code in (("deny", "refused", 2), ("allow", "target clicked", 0),
                                    ("wait", "timeout", 124)):
            if mode == "allow":
                self.allow()
            extra = {"SHORT_TIMEOUT": "2", "WAIT": "timeout"} if mode == "wait" else {}
            result = subprocess.run(cmd, capture_output=True, text=True, env=env | extra, timeout=5)
            self.assertEqual(json.loads(result.stdout)["outcome"], outcome)
            self.assertEqual(result.returncode, code)
            self.assertEqual(self.audit()[-1]["outcome"], outcome)

        for extra_args in (["--steps", "2"], ["--handoffs", "1"]):
            before = self.audit()
            result = subprocess.run([*cmd, *extra_args], capture_output=True, text=True, env=env, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(json.loads(result.stdout)["started"])
            self.assertEqual(len(self.audit()), len(before) + 2)
            self.assertEqual(self.audit()[-1]["outcome"], "refused")

        self.assertTrue((self.home / "ready-timeout").exists())
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env | {"WAIT": "interrupt"}, text=True) as proc:
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and not (self.home / "ready-interrupt").exists():
                time.sleep(.02)
            self.assertTrue((self.home / "ready-interrupt").exists())
            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=3)
            self.assertEqual(proc.returncode, 130, stderr)
            self.assertEqual(json.loads(stdout)["outcome"], "interrupted")
            self.assertEqual(self.audit()[-1]["outcome"], "interrupted")

    @unittest.skipUnless(os.name == "posix", "fake macOS executor")
    def test_oversized_runner_output_and_inherited_pipe(self):
        self.allow()
        huge = self.call(HUGE="1")
        self.assertEqual(json.loads(huge.stdout)["outcome"], "runner error")
        start = time.monotonic()
        escaped = self.call(HELD_PIPE="1", SHORT_TIMEOUT="1")
        self.assertEqual(json.loads(escaped.stdout)["outcome"], "timeout")
        self.assertTrue(json.loads(escaped.stdout)["started"])
        self.assertLess(time.monotonic() - start, 2.4)

    @unittest.skipUnless(os.name == "posix", "fake macOS executor")
    def test_runner_failures(self):
        self.allow()
        result = self.call(BROKEN="1")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self.audit()[-1]["outcome"], "runner error")
        result = self.call(PATH=str(self.state))
        self.assertEqual(result.returncode, 2)
        self.assertIn("#install-the-macos-runner", json.loads(result.stdout)["error"])

    def test_unwritable_audit_refuses_before_runner(self):
        self.allow()
        (self.state / "audit.jsonl").mkdir()
        result = self.call(NO_PROBE="1")
        self.assertEqual(json.loads(result.stdout)["outcome"], "audit error")
        self.assertFalse((self.home / "calls").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX live process tree")
    def test_timeout_and_ctrl_c_stop_the_process_group_and_audit(self):
        self.allow()
        result = self.call(SLEEP="1", SHORT_TIMEOUT="2")
        self.assertEqual(result.returncode, 124)
        self.assertEqual(self.audit()[-1]["outcome"], "timeout")
        (self.home / "ready").unlink()
        proc = subprocess.Popen([sys.executable, "-m", "jev_brain.cli", *self.args],
                                env=self.env | {"SLEEP": "1"}, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            until = time.monotonic() + 5
            while time.monotonic() < until and not (self.home / "ready").exists():
                time.sleep(.02)
            self.assertTrue((self.home / "ready").exists())
            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 130, stderr)
            self.assertEqual(json.loads(stdout)["outcome"], "interrupted")
            self.assertEqual(self.audit()[-1]["outcome"], "interrupted")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    @unittest.skipUnless(os.name == "posix", "fake macOS executor")
    def test_mcp_uses_same_gate_and_cannot_grant_consent(self):
        def tool(name, args):
            msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": args}}
            proc = subprocess.run([sys.executable, "-m", "jev_brain.mcp_server"],
                                  input=json.dumps(msg) + "\n", env=self.env, timeout=5,
                                  text=True, capture_output=True, check=True)
            return json.loads(proc.stdout)
        args = dict(target="focused field", request="Write a greeting", expect="TextEdit", text="Hello!")
        self.assertFalse(tool("use", args)["result"]["structuredContent"]["started"])
        self.assertIn("error", tool("consent", {}))
        self.assertIn("error", tool("use", args | {"act": "true"}))
        self.assertIn("error", tool("use", args | {"consent": True}))
        self.assertTrue(tool("use", args | {"act": True})["result"]["isError"])
        self.assertFalse((self.home / "calls").exists())
        self.allow()
        result = tool("use", args | {"act": True})["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["typed"], "Hello!")
        self.assertEqual(len((self.home / "calls").read_text().splitlines()), 1)


class LocalTests(unittest.TestCase):
    def test_consent_interactive_only_with_exact_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(Path, "home", return_value=Path(tmp)), \
                patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True):
            self.assertFalse(local.consented())
            with patch("builtins.input", return_value="no"):
                self.assertEqual(_consent(), 2)
                self.assertFalse(local.consented())
            with patch("builtins.input", return_value="ALLOW"):
                self.assertEqual(_consent(), 0)
                self.assertTrue(local.consented())
                self.assertEqual(_consent(), 0)
            consent = local.directory() / "consent.json"
            if os.name == "posix":
                self.assertEqual(consent.stat().st_mode & 0o777, 0o600)
                consent.chmod(0o644)
                self.assertFalse(local.consented())

    def test_browser_requires_exact_document_url(self):
        window = foreground.Window("Google Chrome", "Demo", "https://example.org/demo", "AXWebArea",
                                   "com.google.Chrome", 123)
        self.assertTrue(window.matches("https://example.org/demo"))
        for other in ("Google Chrome", "https://example.org", "https://example.org/Demo", "example"):
            self.assertFalse(window.matches(other))

    def test_doctor_checks_permissions_without_requesting_them(self):
        with patch("sys.platform", "darwin"), patch("shutil.which", return_value="/fake/jev-run"), \
                patch("sysconfig.get_path", return_value="/test/venv/bin"), \
                patch.object(local, "consented", return_value=False):
            with patch.object(foreground, "permissions", return_value=(True, True)):
                self.assertEqual(_doctor(), 0)
            with patch.object(foreground, "permissions", return_value=(False, True)):
                self.assertEqual(_doctor(), 1)
