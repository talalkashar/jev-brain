"""Round-two reproductions using synthetic windows, controls, storage and stdio."""

import ctypes as C
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import patch

import srcpath
from jev_brain import computer_use as gate, foreground, install, local, mcp_server


class FakeAX:
    """Integer handles backed by Python values for the parent's native read path."""

    def __init__(self):
        self.values = {1: "desktop", 2: "app", 3: "window", 4: "field", 5: "button", 6: [5]}
        self.attrs = {
            (1, "AXFocusedApplication"): 2, (2, "AXFocusedWindow"): 3, (2, "AXFocusedUIElement"): 4,
            (2, "AXTitle"): "TextEdit", (3, "AXTitle"): "Untitled", (4, "AXRole"): "AXTextArea",
            (5, "AXTitle"): "New", (5, "AXDescription"): "Login", (5, "AXRole"): "AXButton", (5, "AXWindow"): 3,
        }

    def string(self, value):
        handle = len(self.values) + 1
        self.values[handle] = value
        return handle

    def copy_attr(self, ref, key, out):
        value = self.attrs.get((ref, self.values[key]))
        if value is None:
            return -25212
        out._obj.value = self.string(value) if isinstance(value, str) else value
        return 0

    def children(self, ref, key, start, limit, out):
        assert self.values[key] == "AXChildren" and start == 0 and limit == 513
        if ref != 3:
            return -25205
        out._obj.value = 6
        return 0

    def function(self, lib, name, *signature):
        # ctypes normally unwraps c_void_p before dispatching to a C function.
        functions = {
            "AXUIElementCreateSystemWide": lambda: 1,
            "AXUIElementCopyAttributeValue": self.copy_attr, "AXUIElementCopyAttributeValues": self.children,
            "AXUIElementGetPid": lambda ref, out: setattr(out._obj, "value", 123) or 0,
            "AXUIElementGetTypeID": lambda: 3, "CFEqual": lambda a, b: a == b, "CFRelease": lambda ref: None,
            "CFGetTypeID": lambda ref: 3 if ref <= 5 else 2 if ref == 6 else 1,
            "CFStringGetTypeID": lambda: 1, "CFArrayGetTypeID": lambda: 2, "CFURLGetTypeID": lambda: 4,
            "CFURLGetString": lambda ref: ref, "CFStringGetLength": lambda ref: len(self.values[ref]),
            "CFStringCreateWithCString": lambda alloc, raw, encoding: self.string(raw.decode("ascii")),
            "CFStringGetCString": lambda ref, buf, size, encoding: setattr(buf, "value", self.values[ref].encode()) or True,
            "CFArrayGetCount": lambda ref: len(self.values[ref]),
            "CFArrayGetValueAtIndex": lambda ref, n: self.values[ref][n],
        }
        fn = functions[name]
        return lambda *args: fn(*(a.value if isinstance(a, C.c_void_p) else a for a in args))


class ParentReviewTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(local, "directory", return_value=self.home))
        self.front = foreground.Window("TextEdit", "Untitled", "", "AXTextArea", "com.apple.TextEdit", 123)
        self.current = self.enterContext(patch.object(foreground, "current", return_value=self.front))
        self.enterContext(patch.object(gate, "_executor", return_value=["/fake/jev-run"]))
        self.enterContext(patch.object(gate, "sys", SimpleNamespace(platform="darwin", executable=sys.executable)))
        self.executor = self.enterContext(patch.object(gate, "_run", return_value=0))
        # Intercept the old read-only process separately from the executor.
        self.probe = self.enterContext(patch.object(gate, "_capture",
                                                    side_effect=lambda *args: (0, asdict(self.current.return_value))))

    def test_exact_browser_url_does_not_approve_management_or_unknown_origin(self):
        for host in ("console.aws.amazon.com", "eu-west-2.console.aws.amazon.com", "portal.azure.com",
                     "portal.azure.us", "console.cloud.google.com", "dash.cloudflare.com",
                     "cloud.digitalocean.com", "vercel.com", "console.example.test", "example.org"):
            url = f"https://{host}/"
            win = replace(self.front, app="Safari", bundle="com.apple.Safari", title="Console Home", document=url)
            with self.subTest(host=host):
                self.current.return_value = win
                with self.assertRaises(gate.UseRefused):
                    gate.check_window(url, win)
                with self.assertRaises(gate.UseRefused) as raised:
                    gate.perform("New", "Review", armed=True, expect=url)
                self.assertEqual(raised.exception.outcome, "sensitive")
                self.executor.assert_not_called()
                self.probe.assert_not_called()

    def test_unreadable_browser_url_fails_expect_even_when_app_name_matches(self):
        for bundle in foreground.BROWSERS:
            self.current.return_value = replace(self.front, app="Browser", bundle=bundle)
            with self.subTest(bundle=bundle), self.assertRaises(gate.UseRefused) as raised:
                gate.perform("Browser", "Review", armed=True)
            self.assertEqual(raised.exception.outcome, "wrong window")
        self.executor.assert_not_called()

    def test_sensitive_foreground_starts_zero_probes_and_zero_executors(self):
        local.grant()
        spawn = self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected child")))
        self.current.return_value = replace(self.front, app="Mail", bundle="com.apple.mail")
        for act in (False, True):
            with self.subTest(act=act):
                if act:
                    code, body = gate.perform("New", "Review", act=True, expect="TextEdit")
                    self.assertEqual((code, body["outcome"]), (2, "sensitive"))
                else:
                    with self.assertRaises(gate.UseRefused):
                        gate.perform("Notes", "Count windows", armed=True)
                self.probe.assert_not_called()
                self.executor.assert_not_called()
                spawn.assert_not_called()
                self.current.assert_called()

    def test_parent_resolves_sensitive_button_before_external_runner(self):
        local.grant()
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected child")))
        native = FakeAX()
        with patch.object(foreground.C, "CDLL", return_value=object()), \
                patch.object(foreground, "_function", side_effect=native.function), \
                patch.object(foreground, "_bundle_id", return_value=self.front.bundle), \
                patch.object(foreground, "sys", SimpleNamespace(platform="darwin")):
            for act in (True, False):
                with self.subTest(act=act):
                    if act:
                        code, body = gate.perform("New", "Review", act=True, expect="TextEdit")
                        self.assertEqual((code, body["outcome"]), (2, "sensitive"))
                    else:
                        with self.assertRaises(gate.UseRefused) as raised:
                            gate.perform("New", "Review", armed=True, expect="TextEdit")
                        self.assertEqual(raised.exception.outcome, "sensitive")
                    self.executor.assert_not_called()
                    self.probe.assert_not_called()
            # Verify the same transport permits a unique ordinary button, and
            # rejects an incomplete or ambiguous walk before executor launch.
            native.attrs[(5, "AXDescription")] = "New document"
            self.assertEqual(gate.perform("New", "Review", armed=True, expect="TextEdit")[0], 0)
            self.executor.assert_called_once()
            for refs in ([5, 5], [5] * 513):
                native.values[6] = refs
                with self.assertRaises(gate.UseRefused) as raised:
                    gate.perform("New", "Review", armed=True, expect="TextEdit")
                self.assertEqual(raised.exception.outcome, "low confidence")
                self.executor.assert_called_once()

    def test_slow_initial_audit_is_bounded_and_never_authorizes_late_input(self):
        local.grant()
        self._stalled_sync(final=False)

    def test_slow_final_audit_reports_indeterminate_and_preserves_step_outcome(self):
        self._stalled_sync(final=True)

    def test_slow_final_audit_after_input_keeps_confirmed_step(self):
        local.grant()

        def fake_input(argv, body, deadline, text):
            body.update(started=True, outcome="text typed", steps=1, typed=text)
            return 0

        self.executor.side_effect = fake_input
        self._stalled_sync(final=True, input_sent=True)

    def _stalled_sync(self, final, input_sent=False):
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        count = 0

        def sync(fd):
            nonlocal count
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                return
            count += 1
            if count == (2 if final else 1):
                entered.set()
                release.wait(0.8)
                completed.set()

        try:
            with patch.object(gate, "TIMEOUT", .12), patch.object(local.os, "fsync", side_effect=sync):
                start = time.monotonic()
                code, body = gate.perform("focused field", "Review", text="Hello", act=True, expect="TextEdit")
                elapsed = time.monotonic() - start
            self.assertTrue(entered.is_set())
            self.assertLess(elapsed, .45)
            self.assertNotEqual(code, 0)
            self.assertEqual(body["outcome"], "audit indeterminate")
            self.assertEqual(body["started"], input_sent)
            self.assertEqual(body["steps"], int(input_sent))
            if final:
                self.assertEqual(body["step_outcome"], "text typed" if input_sent else "refused")
        finally:
            release.set()
            if entered.is_set():
                self.assertTrue(completed.wait(2))
        self.assertEqual(self.executor.call_count, int(input_sent))

    def test_backend_import_search_excludes_calling_project_and_pythonpath(self):
        # Introspect origins without importing the backend or exercising input.
        with tempfile.TemporaryDirectory() as other:
            for root in (self.home, Path(other)):
                (root / "jev_run.py").touch()
                (root / "jev_brain").mkdir()
                (root / "jev_brain/__init__.py").touch()
            command = gate.backend_command()
            script = ("import importlib.util as u, json, sys; "
                      "print(json.dumps([sys.flags.isolated, "
                      "{n: s.origin if (s := u.find_spec(n)) else None for n in ('jev_brain', 'jev_run')}]))")
            command[command.index("-c") + 1] = script
            proc = subprocess.run(command, cwd=self.home, env=os.environ | {"PYTHONPATH": other},
                                  capture_output=True, check=True, text=True)
            isolated, origins = json.loads(proc.stdout)
            self.assertEqual(isolated, 1)
            trusted = subprocess.check_output([sys.executable, "-I", "-c", script], text=True)
            self.assertEqual(origins, json.loads(trusted)[1])
            if os.getenv("JEV_TEST_RUNNER_DIST"):  # Installed-adapter CI job.
                self.assertTrue(all(origins.values()))
            for origin in filter(None, origins.values()):
                self.assertFalse(any(Path(origin).resolve().is_relative_to(p) for p in (self.home, Path(other))))


class ConfigReviewTests(unittest.TestCase):
    def test_unrelated_server_arguments_do_not_suppress_installation(self):
        docs = {"command": "indexer", "args": ["systemone"], "env": {"TOOL": "jev-brain-mcp"}}
        original = {"mcpServers": {"docs": docs}}
        updated = json.loads(install._upsert_json(json.dumps(original)))
        self.assertEqual(updated["mcpServers"], {"docs": docs, "jev-brain": install._SERVER})
        original = '[mcp_servers.docs]\ncommand="indexer"\nargs=["systemone"]\n'
        self.assertIn("jev-brain", tomllib.loads(install._upsert_toml(original))["mcp_servers"])

    def test_standalone_disable_policy_explains_refusal_without_disclosing_values(self):
        for config in ({"disabledMcpServers": ["systemone", "PRIVATE"]},
                       {"allowedMcpServers": [{"serverName": "PRIVATE"}]},
                       {"projects": {"PRIVATE": {"permissions": {"deny": ["mcp__systemone__use"]}}}}):
            with self.subTest(config=config):
                with self.assertRaises(install.InstallError) as raised:
                    install._upsert_json(json.dumps(config))
                self.assertIn("policy", str(raised.exception).lower())
                self.assertNotIn("PRIVATE", str(raised.exception))

    def test_restricted_aliased_entry_points_do_not_gain_an_unrestricted_server(self):
        for command, args in (("C:/bin/SYSTEMONE-MCP.exe", []), ("python3.12", ["-m", "jev_brain.mcp_server"])):
            config = json.dumps({"mcpServers": {"legacy": {"command": command, "args": args, "disabled": True}}})
            with self.subTest(command=command):
                self.assertEqual(install._upsert_json(config), config)
        config = {"mcpServers": {"legacy": {"command": "uv", "args": ["run", "systemone-mcp"], "disabled": True}}}
        with self.assertRaisesRegex(install.InstallError, "wrapped MCP server"):
            install._upsert_json(json.dumps(config))

    def test_strict_utf8_mcp_writer_survives_surrogate_id_then_ping(self):
        messages = [{"jsonrpc": "2.0", "id": n, "method": "ping"} for n in ("\ud800", 2)]
        data = "".join(json.dumps(m) + "\n" for m in messages).encode("ascii")
        with io.TextIOWrapper(io.BytesIO(data)) as stdin, io.TextIOWrapper(io.BytesIO(), encoding="utf-8",
                errors="strict") as stdout, patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout):
            self.assertEqual(mcp_server.serve_stdio(), 0)
            stdout.seek(0)
            self.assertEqual([json.loads(line)["id"] for line in stdout], ["\ud800", 2])

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_consent_diagnostic_matches_accepted_private_modes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(local, "directory", return_value=Path(tmp)):
            local.grant()
            consent = Path(tmp) / "consent.json"
            self.assertEqual(stat.S_IMODE(consent.stat().st_mode), 0o600)
            for mode in (0o400, 0o600):
                consent.chmod(mode)
                self.assertTrue(local.consented())
            for mode in (0o644, 0o660):
                consent.chmod(mode)
                with self.assertRaises(OSError) as raised:
                    local.private_file("consent.json", os.O_RDONLY)
                self.assertIn("group", str(raised.exception))
                self.assertIn("other", str(raised.exception))
                self.assertIn("0600", str(raised.exception))
