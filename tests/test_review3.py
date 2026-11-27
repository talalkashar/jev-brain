"""Round-three regressions. Only marker processes and synthetic configuration are used."""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import srcpath
from jev_brain import cli, computer_use as gate, foreground, install, local


class InstallerTests(unittest.TestCase):
    def plans(self, command, args):
        entry = {"command": command, "args": args, "allowedTools": ["health"]}
        original = json.dumps({"mcpServers": {"library": entry}})
        yield original, install._upsert_json, lambda value: json.loads(value)["mcpServers"]
        original = (f'[mcp_servers.library]\ncommand={json.dumps(command)}\n'
                    f'args={json.dumps(args)}\ndisabled_tools=["use"]\n')
        yield original, install._upsert_toml, lambda value: tomllib.loads(value)["mcp_servers"]

    def test_wrapped_restricted_module_keeps_one_identity(self):
        for command, args in (
            ("uv", ["run", "python", "-m", "jev_brain.mcp_server"]),
            ("env", ["MODE=local", "uv", "run", "--project", "./project", "--",
                     "python3.12", "-I", "-W", "ignore", "-m", "systemone.mcp_server"]),
        ):
            for original, plan, _ in self.plans(command, args):
                with self.subTest(command=command, format=plan.__name__):
                    self.assertEqual(plan(original), original)

    def test_ambiguous_wrapper_names_entry_for_manual_review(self):
        for original, plan, _ in self.plans("uv", ["run", "--unknown-option", "private-value", "python",
                                                    "-m", "jev_brain.mcp_server"]):
            with self.subTest(format=plan.__name__):
                with self.assertRaises(install.InstallError) as raised:
                    plan(original)
                self.assertIn("library", str(raised.exception))
                self.assertNotIn("private-value", str(raised.exception))

    def test_python_application_arguments_do_not_suppress_installation(self):
        for command, invocation in (("python3", ["indexer.py"]), ("python3", ["--", "-indexer.py"]),
                                    ("python3", ["-X", "dev", "-m", "indexer"]), ("uv", ["run", "indexer.py"])):
            args = [*invocation, "-m", "jev_brain.mcp_server"]
            for original, plan, servers in self.plans(command, args):
                with self.subTest(invocation=invocation, format=plan.__name__):
                    updated = servers(plan(original))
                    self.assertEqual(set(updated), {"library", "jev-brain"})
                    self.assertEqual(updated["library"], servers(original)["library"])


class DoctorTests(unittest.TestCase):
    def test_unavailable_recording_probe_preserves_accessibility_status(self):
        def library(path):
            if path == foreground.CG:
                raise OSError("Screen Recording preflight unavailable")
            return object()

        with patch.object(sys, "platform", "darwin"), patch.object(foreground.C, "CDLL", side_effect=library), \
                patch.object(foreground, "_function", return_value=lambda: True):
            self.assertEqual(foreground.permissions(), (True, False))

    def test_external_runner_screen_recording_is_informational(self):
        with patch.object(sys, "platform", "darwin"), patch.object(cli.shutil, "which", return_value="/fake/jev-run"), \
                patch.object(cli.sysconfig, "get_path", return_value="/venv/bin"), \
                patch.object(foreground, "permissions", return_value=(True, False)), \
                patch.object(local, "consented", return_value=False), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli._doctor(), 0)
        line, = (line for line in output.getvalue().splitlines() if line.startswith("Screen Recording"))
        self.assertIn("informational", line.lower())
        self.assertNotIn("Check System Settings", line)


@unittest.skipUnless(os.name == "posix", "POSIX startup supervision")
class StartupTests(unittest.TestCase):
    approval = gate._Approval("Notes", None, foreground.Window("Notes", "Note", "", "AXTextArea"), None)

    def test_blocked_process_constructor_obeys_deadline(self):
        release = threading.Event()
        finished = threading.Event()

        def missing(*args, **kwargs):
            release.wait(1.5)
            finished.set()
            raise FileNotFoundError

        try:
            with patch.object(subprocess, "Popen", side_effect=missing):
                start = time.monotonic()
                with self.assertRaises(TimeoutError):
                    gate._capture(["/absent"], start + .1, {}, self.approval)
                self.assertLess(time.monotonic() - start, .8)
        finally:
            release.set()
            self.assertTrue(finished.wait(2))

    def test_late_child_cannot_run_executor_and_is_reaped(self):
        real_popen = subprocess.Popen
        release = threading.Event()
        created = threading.Event()
        children = []
        body = {"started": False, "steps": 0}

        def delayed_handle(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            children.append(proc)
            created.set()
            release.wait(1.5)
            return proc

        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "input"
            fake = [sys.executable, "-I", "-S", "-c", "import sys; open(sys.argv[1], 'xb').close()", str(marker)]
            try:
                with patch.object(subprocess, "Popen", side_effect=delayed_handle):
                    start = time.monotonic()
                    with self.assertRaises(TimeoutError):
                        gate._capture(fake, start + .2, body, self.approval)
                    elapsed = time.monotonic() - start
            finally:
                release.set()
            self.assertTrue(created.wait(2))
            until = time.monotonic() + 2
            while children[0].returncode is None and time.monotonic() < until:
                time.sleep(.01)
            self.assertFalse(marker.exists())
            self.assertLess(elapsed, .9)
            self.assertEqual(body, {"started": False, "steps": 0})
            self.assertIsNotNone(children[0].returncode, "supervisor did not reap the late child")
