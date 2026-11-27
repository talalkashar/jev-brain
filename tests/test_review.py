"""Regression fixtures from the PR review. All foreground and executor I/O is fake."""

import io
import json
import os
import tarfile
import zipfile
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_cli import ROOT
from jev_brain import computer_use as gate, foreground, install, local, mcp_server


class GateReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(local, "directory", return_value=Path(self.temp)))
        self.enterContext(patch.object(gate.shutil, "which", return_value="/fake/jev-run"))
        self.run_child = self.enterContext(patch.object(gate, "_run", return_value=0))
        self.front = foreground.Window("TextEdit", "Untitled", "", "AXTextArea")
        self.observe = self.enterContext(patch.object(foreground, "current", return_value=self.front))
        self.enterContext(patch.object(gate, "_observe", side_effect=lambda deadline: foreground.current(),
                                      create=True))
        self.enterContext(patch.object(gate, "sys", SimpleNamespace(platform="darwin", executable=sys.executable)))

    def test_arm_preflights_actual_mail_and_focused_metadata(self):
        for field, value in [("app", "Mail"), ("field_role", "AXSecureTextField"),
                             ("field_label", "Login"), ("placeholder", "Password"),
                             ("field_subrole", "AXSecureTextField")]:
            with self.subTest(field=field):
                self.observe.return_value = replace(self.front, **{field: value})
                with self.assertRaises(gate.UseRefused) as exc:
                    gate.perform("Notes", "Count windows", armed=True)
                self.assertEqual(exc.exception.outcome, "sensitive")
                self.run_child.assert_not_called()

    def test_sensitive_clients_unicode_and_unknown_identity(self):
        for bundle in ("com.apple.mail", "org.mozilla.thunderbird", "com.microsoft.Outlook",
                       "com.readdle.smartemail-Mac", "com.1password.1password", "com.apple.MobileSMS"):
            with self.subTest(bundle=bundle), self.assertRaises(gate.UseRefused) as exc:
                gate.check_window("TextEdit", replace(self.front, bundle=bundle, pid=123))
            self.assertEqual(exc.exception.outcome, "sensitive")
        for label in ("Thunderbird", "Webmail", "Outlook", "Spark", "ProtonMail", "HSBC",
                      "KeePassXC", "Messenger", "ｌｏｇｉｎ", "lоgin", "Connexion", "登录"):
            with self.subTest(label=label):
                self.assertIsNotNone(gate.sensitive_surface(label))
        self.assertFalse(self.front.matches("TextEdit"))
        approved = replace(self.front, bundle="com.apple.TextEdit", pid=123)
        for key, missing in (("bundle", ""), ("title", " "), ("app", ""), ("pid", 0),
                             ("field_role", ""), ("bundle", "org.unknown.app")):
            self.assertFalse(replace(approved, **{key: missing}).matches("TextEdit"))

    def test_browser_variants_need_verified_url_and_bundle(self):
        for app in ("Vivaldi", "Firefox Developer Edition", "Unknown Browser"):
            self.assertFalse(foreground.Window(app, "Page", "", "AXTextField").matches(app))
        for bundle in ("com.vivaldi.Vivaldi", "org.mozilla.firefoxdeveloperedition"):
            win = replace(self.front, app="Browser", bundle=bundle, pid=123)
            self.assertFalse(win.matches("Browser"))
            self.assertTrue(replace(win, document="https://example.org/a").matches("https://example.org/a"))

    def test_no_freeform_audit_data_even_on_refusal(self):
        code, _ = gate.perform("CONTROL-CANARY", "Review REQUEST-CANARY", act=True, expect="TextEdit")
        self.assertEqual(code, 2)
        audit = (Path(self.temp) / "audit.jsonl").read_text()
        self.assertNotIn("CANARY", audit)
        for line in audit.splitlines():
            self.assertEqual(set(json.loads(line)), {"time", "app", "outcome", "call", "length"})

    def test_windows_batch_is_refused_before_lookup(self):
        with patch.object(gate.sys, "platform", "win32"), patch.object(gate.shutil, "which") as lookup:
            lookup.return_value = "record.cmd"
            with self.assertRaises(gate.UseRefused):
                gate.perform("Notes", "Review a & b", armed=True)
            lookup.assert_not_called()
            self.run_child.assert_not_called()

    def test_oversized_field_refused_before_launch_and_audit_is_bounded(self):
        local.grant()
        code, _ = gate.perform("x" * 100000, "Review", act=True, expect="TextEdit")
        self.assertNotEqual(code, 0)
        self.assertLess((Path(self.temp) / "audit.jsonl").stat().st_size, 1024)
        self.run_child.assert_not_called()

    def test_mcp_bounds_frame_before_read_and_recovers_at_newline(self):
        class Bounded(io.BytesIO):
            def readline(self, size=-1):
                if not 0 < size <= 257:
                    raise AssertionError("unbounded readline")
                return super().readline(size)

        class Stdin(io.TextIOWrapper):
            def __iter__(self):
                raise AssertionError("unbounded iteration")

        stream = Stdin(Bounded(b"x" * 4096 + b'\n{"jsonrpc":"2.0","id":5,"method":"ping"}\n'))
        with stream, patch.object(sys, "stdin", stream), patch.object(mcp_server, "MAX_LINE_CHARS", 256), \
                redirect_stdout(io.StringIO()) as out:
            mcp_server.serve_stdio()
        self.assertEqual([json.loads(s)["id"] for s in out.getvalue().splitlines()], [None, 5])


class InstallReviewTests(unittest.TestCase):
    def test_installer_preserves_restrictions_and_hides_unrelated_config(self):
        for server in ("systemone", "jev-brain"):
            for agent in ("codex", "cursor"):
                with self.subTest(server=server, agent=agent), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp).resolve()
                    path = install._config_path(root, agent)
                    path.parent.mkdir()
                    if agent == "codex":
                        original = (f'[mcp_servers."{server}"] # local policy\nenabled=false\n'
                                    'disabled_tools=["use"]\n[mcp_servers.other.env]\nTOKEN="CANARY"\n')
                    else:
                        original = json.dumps({"mcpServers": {server: {"disabled": True, "allowedTools": []},
                                                              "other": {"env": {"TOKEN": "CANARY"}}}})
                    path.write_text(original)
                    for yes in (False, True):
                        report = install.apply_install(root, install.plan_install(root, [agent]), yes=yes)
                        self.assertNotIn("CANARY", report)
                        self.assertEqual(path.read_text(), original)

    def test_toml_uses_parsed_key_path_and_reparses(self):
        for header in ('[mcp_servers."jev-brain"] # note', "['mcp_servers'.'jev-brain']",
                       '[mcp_servers.jev-brain] # trailing comment'):
            original = header + '\nenabled=false\n'
            self.assertEqual(tomllib.loads(install._upsert_toml(original)), tomllib.loads(original))
        with self.assertRaises(install.InstallError):
            install._upsert_toml("mcp_servers = {}\n")


class DistributionReviewTests(unittest.TestCase):
    def test_ci_installs_and_tests_adapter_and_lints(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        for command in ("-e ./runner", "pip check", "ruff check", "jev-run --help",
                        "from typesafe_computer_use import macos"):
            self.assertIn(command, workflow)
        self.assertIn("ruff", tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"])

    def test_runner_declares_and_ships_mit_and_credit(self):
        project = tomllib.loads((ROOT / "runner/pyproject.toml").read_text())["project"]
        self.assertEqual(project.get("license"), "MIT")
        self.assertEqual(set(project["license-files"]), {"LICENSE", "NOTICE"})
        self.assertEqual((ROOT / "runner/LICENSE").read_bytes(), (ROOT / "LICENSE").read_bytes())
        self.assertIn("Aaron Levin", (ROOT / "runner/NOTICE").read_text())


    @unittest.skipUnless(os.getenv("JEV_TEST_RUNNER_DIST"), "adapter distribution build")
    def test_built_runner_archives_include_license_and_notice(self):
        directory = Path(os.environ["JEV_TEST_RUNNER_DIST"])
        with zipfile.ZipFile(next(directory.glob("*.whl"))) as wheel:
            for name in ("LICENSE", "NOTICE"):
                path, = (n for n in wheel.namelist() if n.endswith(".dist-info/licenses/" + name))
                self.assertEqual(wheel.read(path), (ROOT / "runner" / name).read_bytes())
        with tarfile.open(next(directory.glob("*.tar.gz"))) as sdist:
            for name in ("LICENSE", "NOTICE"):
                path, = (n for n in sdist.getnames() if n.count("/") == 1 and n.endswith("/" + name))
                self.assertEqual(sdist.extractfile(path).read(), (ROOT / "runner" / name).read_bytes())
