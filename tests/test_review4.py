"""Round-four regressions. Only synthetic foregrounds and recording executors are used."""

import json
import os
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_runner
from test_cli import ROOT, _env
import test_review3
from jev_brain import computer_use as gate, foreground, install


class InstallerTests(unittest.TestCase):
    def test_restricted_alias_in_json_and_toml_by_effective_entry_point(self):
        plans = test_review3.InstallerTests().plans
        for package, console in (("jev_brain", "jev-brain-mcp"), ("systemone", "systemone-mcp")):
            path = f"src/{package}/mcp_server.py"
            known = (
                [console], ["python3.12", "-Im" + package + ".mcp_server"],
                [sys.executable, str(ROOT / path)], ["python", "--", "./" + path],
                ["py", "-3", path.replace("/", "\\")], [path], ["uv", "run", path],
                ["env", "MODE=local", "uv", "run", "--locked", "--", "python", "-I", path],
                ["env", "python", str(ROOT / path)], ["env", "MODE=local", console],
                ["uv", "run", "--", "/tool/bin/" + console],
                ["uv", "run", "--directory", str((ROOT / path).parent), "python", "./mcp_server.py"],
            )
            uncertain = (["uv", "run", "--", console], ["env", "PATH=/another/bin", console],
                         ["python", "mcp_server.py"], ["unknown-wrapper", path])
            for invocation in (*known, *uncertain):
                for original, plan, _ in plans(invocation[0], invocation[1:]):
                    with self.subTest(invocation=invocation, format=plan.__name__):
                        if invocation in uncertain:
                            with self.assertRaisesRegex(install.InstallError, "library"):
                                plan(original)
                        else:
                            self.assertEqual(plan(original), original)


@contextmanager
def fake_adapter():
    fixture = test_runner.RunnerTests()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.doCleanups()


@unittest.skipUnless(os.name == "posix", "POSIX launcher with fake AX")
class ForegroundTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.front = foreground.Window("TextEdit", "Untitled", "", "AXTextArea", "com.apple.TextEdit", 100)
        self.current = self.enterContext(patch.object(foreground, "current", return_value=self.front))
        self.metadata = self.enterContext(patch.object(foreground, "target_metadata",
                                          return_value=("AXButton", "", "New", "", "")))
        self.enterContext(patch.object(gate, "sys", SimpleNamespace(platform="darwin", executable=sys.executable)))
        self.marker = self.root / "dispatch"
        recorder = [sys.executable, "-I", "-S", "-c",
                    "import sys; open(sys.argv[1], 'a').close(); print('{\"outcome\":\"dry run\",\"steps\":0}')",
                    str(self.marker)]
        self.executor = self.enterContext(patch.object(gate, "_executor", return_value=recorder))

    def test_focus_switch_at_each_authorization_boundary_sends_zero_input(self):
        finder = replace(self.front, app="Finder", bundle="com.apple.finder", pid=101)
        mail = replace(self.front, app="Mail", bundle="com.apple.mail", pid=102)
        for boundary in ("preflight", "helper grant", "executor dispatch", "adapter entry", "press", "write",
                         "press abort check", "write abort check", "last centre hit", "last empty read"):
            changes = [(finder, "wrong window"), (mail, "sensitive")]
            if boundary != "preflight":  # Only a prior approval can pin a particular TextEdit PID.
                changes.append((replace(self.front, pid=101), "wrong window"))
            for changed, expected in changes:
                with self.subTest(boundary=boundary, changed=changed):
                    self.current.return_value = self.front
                    if boundary in ("preflight", "helper grant", "executor dispatch"):
                        self.check_startup_switch(boundary, changed, expected)
                    else:
                        self.check_input_switch(boundary, changed, expected)

    def check_startup_switch(self, boundary, changed, expected):
        grants = []
        read, write = os.read, os.write
        signals = {"helper grant": b"R", "executor dispatch": b"D"}

        def receive(fd, count):
            value = read(fd, count)
            if value == signals.get(boundary):
                self.current.return_value = changed
            return value

        def send(fd, value):
            if value in (b"G", b"X"):
                grants.append(value)
            return write(fd, value)

        for act in (False, True):
            grants.clear()
            self.current.return_value = changed if boundary == "preflight" else self.front
            with patch.object(os, "read", side_effect=receive), patch.object(os, "write", side_effect=send), \
                    patch.object(gate.local, "consented", return_value=True), patch.object(gate.local, "append_audit"):
                if act:
                    code, body = gate.perform("New", "Review", act=True, expect="TextEdit")
                    self.assertEqual((code, body["outcome"], body["started"], body["steps"]), (2, expected, False, 0))
                else:
                    with self.assertRaises(gate.UseRefused) as refusal:
                        gate.perform("New", "Review", armed=True, expect="TextEdit")
                    self.assertEqual(refusal.exception.outcome, expected)
            self.assertEqual(grants, [b"G"] if boundary == "executor dispatch" else [])
            self.assertFalse(self.marker.exists())

    def check_input_switch(self, boundary, changed, expected):
        with fake_adapter() as fixture:
            fixture.observe.side_effect = gate.check_window
            original = fixture.mac._ax_attr.side_effect

            def switch():
                self.current.return_value = changed
                fixture.mac._ax_attr.side_effect = lambda ref, key: (
                    None if key == "AXFocusedApplication" else original(ref, key))

            hit = fixture.mac.AS.AXUIElementCopyElementAtPosition
            value = fixture.mac.ax_value
            if boundary == "adapter entry":
                switch()
            elif boundary.endswith("abort check"):
                fixture.mac.check_abort.side_effect = switch
            if boundary in ("press", "last centre hit"):
                def hit_test(*args):
                    if hit.call_count == (1 if boundary == "press" else 2):
                        switch()
                    return 0, fixture.control
                hit.side_effect = hit_test
            if boundary in ("write", "write abort check", "last empty read"):
                fixture.args.target, fixture.args.text = None, "Hello"
                def read_value(ref):
                    if ((boundary == "write" and value.call_count == 2)
                            or (boundary == "last empty read" and value.call_count == 3)):
                        switch()
                    return ""
                value.side_effect = read_value
            with self.assertRaises(gate.UseRefused) as refusal:
                fixture.run_step()
            self.assertEqual(refusal.exception.outcome, expected)
            self.assertEqual(fixture.result["steps_taken"], 0)
            fixture.mac.AS.AXUIElementPerformAction.assert_not_called()
            fixture.mac.AS.AXUIElementSetAttributeValue.assert_not_called()

    def test_target_change_during_startup_withholds_grant(self):
        for new_description, outcome in (("Login", "sensitive"), ("Another action", "wrong window")):
            before = ("AXButton", "", "New", "", "")
            self.metadata.side_effect = [before, (*before[:3], new_description, "")]
            with self.subTest(outcome=outcome):
                with self.assertRaises(gate.UseRefused) as refusal:
                    gate.perform("New", "Review", armed=True, expect="TextEdit")
                self.assertEqual(refusal.exception.outcome, outcome)
                self.assertFalse(self.marker.exists())

    def test_final_foreground_read_cannot_hide_a_new_overlay_or_field_edit(self):
        for action in ("press", "write"):
            with self.subTest(action=action), fake_adapter() as fixture:
                hit = fixture.mac.AS.AXUIElementCopyElementAtPosition
                value = fixture.mac.ax_value
                value.return_value = ""
                def observe(expect):
                    if hit.call_count:
                        hit.return_value = (0, fixture.field)
                    if value.call_count >= 2:
                        value.return_value = "human draft"
                    return fixture.front
                fixture.observe.side_effect = observe
                if action == "write":
                    fixture.args.target, fixture.args.text = None, "Hello"
                self.assertEqual(fixture.run_step(), "low confidence" if action == "press" else "text not typed")
                self.assertEqual(fixture.result["steps_taken"], 0)
                fixture.mac.AS.AXUIElementPerformAction.assert_not_called()
                fixture.mac.AS.AXUIElementSetAttributeValue.assert_not_called()

    def test_missing_shebang_interpreter_reports_executor_never_started(self):
        executable = self.root / "jev-run"
        executable.write_text("#!/absent-jev-interpreter/bin/python\n")
        executable.chmod(0o700)
        self.executor.return_value = [str(executable)]
        code, body = gate.perform("TextEdit", "Count windows", armed=True)
        self.assertNotEqual(code, 0)
        self.assertEqual((body["started"], body["steps"]), (False, 0))
        self.assertIn("install-the-macos-runner", body["error"])
        self.assertIn("start", body["error"])

    def test_executor_start_is_indeterminate_if_dispatch_acknowledgement_stalls(self):
        # Speak the launcher protocol on pipes, then stall with the status pipe
        # still open. There is no evidence of an exec or of its failure.
        launcher = ("import os,time\nos.write(1, b'R'); os.write(2, b'R')\n"
                    "os.read(0, 1); os.write(2, b'D'); os.read(0, 1); time.sleep(10)")
        with patch.object(gate, "_LAUNCHER", launcher), patch.object(gate, "TIMEOUT", .8):
            code, body = gate.perform("TextEdit", "Count windows", armed=True)
        self.assertEqual(code, 124)
        self.assertIsNone(body["started"])
        self.assertIsNone(body["steps"])

    def test_backend_rejects_a_different_window_or_target_after_exec(self):
        for change in ("window", "target"):
            # Start the actual backend entry with synthetic macOS and AX modules.
            self.executor.return_value = [sys.executable, "-c",
                f"import sys; sys.path.insert(0, {str(ROOT / 'tests')!r}); "
                f"from test_review4 import fake_backend; fake_backend({change!r})"]
            with self.subTest(change=change):
                code, body = gate.perform("New", "Review", armed=True, expect="TextEdit")
                self.assertNotEqual(code, 0)
                self.assertEqual((body["outcome"], body["steps"]), ("wrong window", 0))


def fake_backend(change):
    with fake_adapter() as fixture:
        if change == "window":
            fixture.observe.return_value = replace(fixture.front, title="Another document")
        else:
            attribute = fixture.mac.AS.AXUIElementCopyAttributeValue.side_effect
            fixture.mac.AS.AXUIElementCopyAttributeValue.side_effect = lambda ref, key, out: (
                (0, "Another action") if key == "AXDescription" else attribute(ref, key, out))
        with patch.dict(sys.modules, {"typesafe_computer_use": SimpleNamespace(macos=fixture.mac)}):
            raise SystemExit(test_runner.jev_run._backend())


class MCPTests(unittest.TestCase):
    def test_both_mcp_names_return_structured_preview_refusal(self):
        for name in ("jev-brain-mcp", "systemone-mcp"):
            script = Path(sysconfig.get_path("scripts")) / (name + (".exe" if os.name == "nt" else ""))
            module = "jev_brain.mcp_server" if name == "jev-brain-mcp" else "systemone.mcp_server"
            command = [str(script)] if script.is_file() else [sys.executable, "-m", module]
            message = {"jsonrpc": "2.0", "method": "tools/call", "id": 1,
                       "params": {"name": "use", "arguments": {"target": "", "request": "Count windows"}}}
            with self.subTest(name=name):
                proc = subprocess.run(command, input=json.dumps(message) + "\n", text=True, capture_output=True,
                                      check=True, timeout=5, env=_env())
                result = json.loads(proc.stdout)["result"]
                self.assertTrue(result["isError"])
                self.assertIn("structuredContent", result)
                body = result["structuredContent"]
                self.assertEqual(json.loads(result["content"][0]["text"]), body)
                self.assertEqual((body["started"], body["outcome"], body["steps"]), (False, "refused", 0))
                self.assertIn("target", body["error"])
