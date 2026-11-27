"""Round-five reproductions. All desktop reads and mutations in these tests are synthetic."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_cli import ROOT, _env
from test_review4 import fake_adapter
from test_runner import jev_run
from jev_brain import computer_use as gate, foreground, install


class InputTests(unittest.TestCase):
    def exercise(self, mutation, inject=None, change=None, coherent=True):
        # Instrument the actual sequence, including repeated reads inside the gate.
        # Changing the simulated desktop also advances its synchronous revision.
        with fake_adapter() as f:
            f.args.target, f.args.text = ("New", None) if mutation == "press" else (None, "Hello")
            reads = []
            changed = False
            value = ""

            def watch(name, read):
                def sample(*args):
                    nonlocal changed
                    if len(reads) == inject:
                        changed = True
                    reads.append(name)
                    return read(*args)
                return sample

            def active(kind):
                return changed and change == kind

            def front(expect):
                if active("focus"):
                    return replace(f.front, app="Mail")
                if mutation == "write" and active("label"):
                    return replace(f.front, field_label="New label")
                if mutation == "write" and active("role"):
                    return replace(f.front, field_role="AXButton")
                return f.front

            def abort():
                if active("abort"):
                    raise gate.UseRefused("Operator stopped input.")

            attr = f.mac._ax_attr.side_effect
            def owner(ref, key):
                if active("window") and key == "AXFocusedWindow":
                    return f.control
                if active("focus") and key == "AXFocusedApplication":
                    return None
                if mutation == "write" and active("identity") and key == "AXFocusedUIElement":
                    return f.control
                return attr(ref, key)

            ax = f.mac.AS
            attribute = ax.AXUIElementCopyAttributeValue.side_effect
            def metadata(ref, key, out):
                if ref == f.control and key == "AXTitle" and active("label"):
                    return 0, "Archive"
                if ref == f.control and key == "AXRole" and active("role"):
                    return 0, "AXMenuItem"
                return attribute(ref, key, out)

            def read_value(ref):
                return "human draft" if active("value") else value

            def insert(ref, key, text):
                nonlocal value
                value = text
                return 0

            f.observe.side_effect = watch("foreground", front)
            ax.AXUIElementCreateSystemWide.side_effect = watch("system", lambda: f.app)
            ax.AXUIElementGetPid.side_effect = watch("pid", lambda *_: (0, 100))
            f.mac._ax_attr.side_effect = watch("ownership", owner)
            ax.AXUIElementCopyAttributeValue.side_effect = watch("metadata", metadata)
            f.mac._ax_actions.side_effect = watch("actions", lambda ref: [] if active("action") else ["AXPress"])
            f.mac._ax_frame.side_effect = watch("frame", lambda ref: (10, 10, 40, 20))
            ax.AXUIElementCopyElementAtPosition.side_effect = watch(
                "hit", lambda *_: (0, f.field if active("identity") else f.control))
            field = f.mac.focused_field.return_value
            f.mac.focused_field.side_effect = watch("field", lambda: field)
            f.mac.ax_value.side_effect = watch("value", read_value)
            f.mac.check_abort.side_effect = watch("abort", abort)
            f.mac.input_revision = watch("revision", lambda: int(changed)) if coherent else None
            # Record the exact prefix before the first mutation, excluding readback.
            before = []
            def mutation_called(*args):
                before.extend(reads)
                return insert(*args) if mutation == "write" else 0

            f.press.side_effect = f.write.side_effect = mutation_called
            try:
                outcome = f.run_step()
            except gate.UseRefused as exc:
                outcome = exc.outcome
            return before or reads, f.press.call_count + f.write.call_count, f.result, outcome

    def changes_at_each_read(self, mutation, changes):
        reads, calls, result, outcome = self.exercise(mutation)
        self.assertEqual((calls, result["steps_taken"]), (1, 1), outcome)
        for step, name in enumerate(reads):
            for change in changes:
                with self.subTest(mutation=mutation, step=step, read=name, change=change):
                    _, calls, result, _ = self.exercise(mutation, step, change)
                    self.assertEqual((calls, result["steps_taken"]), (0, 0))

    def test_press_refuses_changes_at_every_read(self):
        self.changes_at_each_read("press", ("focus", "window", "identity", "label", "role", "action", "abort"))

    def test_write_refuses_changes_at_every_read(self):
        self.changes_at_each_read("write", ("focus", "window", "identity", "label", "role", "value", "abort"))

    def test_press_reports_live_title_when_selector_is_a_description(self):
        with fake_adapter() as f:
            attribute = f.mac.AS.AXUIElementCopyAttributeValue.side_effect
            f.mac.AS.AXUIElementCopyAttributeValue.side_effect = lambda ref, key, out: (
                (0, "Create document") if ref == f.control and key == "AXDescription" else attribute(ref, key, out))
            f.args.target = "Create document"
            approval = gate._Approval("TextEdit", f.args.target, f.front,
                                      ("AXButton", "", "New", "Create document", ""))
            # Exercise the same result validation an executor reply goes through.
            outcome = jev_run.perform(f.args, f.mac, f.result, approval.stamp().split(":"),
                                      read_revision=f.mac.input_revision)
            body = {"target": f.args.target, "mode": "act"}
            with patch.object(gate, "_capture", return_value=(0, dict(f.result, outcome=outcome))):
                code = gate._run([], body, 0, None, approval)
            self.assertEqual((code, body["clicked"], f.press.call_count), (0, "New", 1))

    def test_same_reference_rename_during_foreground_read_sends_zero_presses(self):
        with fake_adapter() as f:
            attribute = f.mac.AS.AXUIElementCopyAttributeValue.side_effect
            title = "New"
            def front(expect):
                nonlocal title
                if f.mac.check_abort.called:
                    title = "Archive"
                return f.front
            f.observe.side_effect = front
            f.mac.AS.AXUIElementCopyAttributeValue.side_effect = lambda ref, key, out: (
                (0, title) if ref == f.control and key == "AXTitle" else attribute(ref, key, out))
            self.assertNotEqual(f.run_step(), "target clicked")
            self.assertEqual(f.result["steps_taken"], 0)
            f.press.assert_not_called()

    def test_draft_during_final_ownership_query_sends_zero_writes(self):
        with fake_adapter() as f:
            f.args.target, f.args.text = None, "Hello"
            f.mac.ax_value.return_value = ""
            attr = f.mac._ax_attr.side_effect
            def owner(ref, key):
                if key == "AXWindow" and f.mac.ax_value.call_count >= 2:
                    f.mac.ax_value.return_value = "human draft"
                return attr(ref, key)
            f.mac._ax_attr.side_effect = owner
            self.assertEqual(f.run_step(), "text not typed")
            self.assertEqual(f.result["steps_taken"], 0)
            f.write.assert_not_called()

    def test_abort_during_verification_stops_press_and_write(self):
        for mutation in ("press", "write"):
            with self.subTest(mutation=mutation), fake_adapter() as f:
                if mutation == "write":
                    f.args.target, f.args.text = None, "Hello"
                    f.mac.ax_value.return_value = ""
                stopped = False
                def front(expect):
                    nonlocal stopped
                    stopped |= f.mac.check_abort.called
                    return f.front
                def abort():
                    if stopped:
                        raise gate.UseRefused("Operator stopped input.")
                f.observe.side_effect = front
                f.mac.check_abort.side_effect = abort
                with self.assertRaises(gate.UseRefused):
                    f.run_step()
                self.assertEqual(f.result["steps_taken"], 0)
                f.press.assert_not_called()
                f.write.assert_not_called()

    def test_shipped_backend_refuses_live_input_even_with_approval(self):
        for target, text in (("New", None), (None, "Hello")):
            with self.subTest(target=target), fake_adapter() as f:
                f.args.target, f.args.text = target, text
                f.mac.ax_value.return_value = ""
                metadata = ("AXButton", "", "New", "", "") if target else None
                approval = gate._Approval("TextEdit", target, f.front, metadata).stamp()
                output = io.StringIO()
                with patch.dict(os.environ, {"JEV_BRAIN_APPROVAL": approval}), \
                        patch.dict(sys.modules, {"typesafe_computer_use": SimpleNamespace(macos=f.mac)}), \
                        patch.object(jev_run, "_arguments", return_value=f.args), redirect_stdout(output):
                    self.assertNotEqual(jev_run._backend(), 0)
                result = json.loads(output.getvalue())
                self.assertEqual((result["outcome"], result["steps_taken"]), ("unverifiable input", 0))
                f.press.assert_not_called()
                f.write.assert_not_called()

    def test_independent_ax_reads_without_coherent_revision_refuse_both_mutations(self):
        for mutation in ("write", "press"):
            with self.subTest(mutation=mutation):
                _, calls, result, outcome = self.exercise(mutation, coherent=False)
                self.assertEqual((calls, result["steps_taken"], outcome), (0, 0, "unverifiable input"))


class StartupTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX helper")
    def test_foreground_change_during_resolution_never_constructs_helper(self):
        front = foreground.Window("Notes", "Note", "", "AXTextArea", "com.apple.Notes", 12)
        with patch.object(foreground, "current", return_value=front) as current:
            def lookup():
                current.return_value = replace(front, app="Mail", bundle="com.apple.mail")
                return [sys.executable]
            with patch.object(sys, "platform", "darwin"), patch.object(gate, "_executor", side_effect=lookup), \
                    patch.object(subprocess, "Popen", side_effect=OSError) as constructor:
                with self.assertRaises(gate.UseRefused) as refusal:
                    gate.perform("Notes", "Count", armed=True)
                constructor.assert_not_called()
                self.assertEqual(refusal.exception.outcome, "sensitive")


    @unittest.skipUnless(os.name == "posix", "POSIX helper")
    def test_launch_worker_rechecks_after_building_child_environment(self):
        front = foreground.Window("Notes", "Note", "", "AXTextArea", "com.apple.Notes", 12)
        stamp = gate._Approval.stamp
        with patch.object(foreground, "current", return_value=front) as current:
            def prepare_environment(approval):
                current.return_value = replace(front, app="Mail", bundle="com.apple.mail")
                return stamp(approval)
            with patch.object(sys, "platform", "darwin"), patch.object(gate, "_executor", return_value=["/fake"]), \
                    patch.object(gate._Approval, "stamp", prepare_environment), \
                    patch.object(subprocess, "Popen", side_effect=OSError) as constructor:
                with self.assertRaises(gate.UseRefused) as refusal:
                    gate.perform("Notes", "Count", armed=True)
                constructor.assert_not_called()
                self.assertEqual(refusal.exception.outcome, "sensitive")


class InstallerTests(unittest.TestCase):
    def test_multicall_env_wrapper_preserves_positive_identity_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            suffix = ".exe" if os.name == "nt" else ""
            multicall = cwd / ("coreutils" + suffix)
            multicall.touch(mode=0o755)
            (cwd / ("indexer" + suffix)).touch(mode=0o755)
            for spelling, link in (("symlink", os.symlink), ("hardlink", os.link)):
                bindir = cwd / spelling
                bindir.mkdir()
                link(multicall, bindir / ("env" + suffix))
                search = os.pathsep.join((str(bindir), str(cwd)))
                self.check_entry("env", search, cwd, "preserve", ["python", "-m", "jev_brain.mcp_server"])
                self.check_entry("env", search, cwd, "refuse", ["indexer"])

    def test_python_module_alias_or_unresolved_module_never_adds_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            module = cwd / "indexer.py"
            (cwd / ("python.exe" if os.name == "nt" else "python")).touch(mode=0o755)
            module.symlink_to(ROOT / "src/jev_brain/mcp_server.py")
            self.check_entry("python", str(cwd), cwd, "preserve", ["-m", "indexer"])
            self.check_entry("python", str(cwd), cwd, "refuse", ["-m", "unknown_module"])

    def test_path_and_file_identity_preserve_restricted_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            bindir = cwd / "bin"
            bindir.mkdir()
            suffix = ".exe" if os.name == "nt" else ""
            console = bindir / ("jev-brain-mcp" + suffix)
            console.touch(mode=0o755)
            aliases = [("hard-alias" + suffix, os.link), ("sym-alias" + suffix, os.symlink)]
            for name, link in aliases:
                alias = bindir / name
                link(console, alias)
                for spelling in (str(alias), name, "./bin/" + name):
                    for search in (str(bindir), "bin"):
                        self.check_entry(spelling, search, cwd, "preserve")
            # The client overrides the installer's PATH. The same name can be a
            # distinct file. An unresolved path requires a diagnostic naming the entry.
            other = cwd / "other"
            other.mkdir()
            indexer = other / ("indexer" + suffix)
            indexer.touch(mode=0o755)
            with patch.dict(os.environ, {"PATH": str(bindir)}):
                self.check_entry(indexer.name, str(other), cwd, "add")
                self.check_entry(aliases[0][0], str(other), cwd, "refuse")

    def check_entry(self, command, search, cwd, expected, args=()):
        entry = {"command": command, "args": list(args), "cwd": str(cwd), "env": {"PATH": search}, "disabled": True}
        documents = (
            (install._upsert_json, json.dumps({"mcpServers": {"library": entry}})),
            (install._upsert_toml, '[mcp_servers.library]\nenabled=false\ncommand=' + json.dumps(command)
             + '\nargs=' + json.dumps(args) + '\ncwd=' + json.dumps(str(cwd)) + '\n[mcp_servers.library.env]\nPATH=' + json.dumps(search) + '\n'),
        )
        for plan, original in documents:
            with self.subTest(plan=plan.__name__, command=command, search=search):
                if expected == "refuse":
                    with self.assertRaisesRegex(install.InstallError, "library"):
                        plan(original)
                elif expected == "preserve":
                    self.assertEqual(plan(original), original)
                else:
                    self.assertNotEqual(plan(original), original)


class SkillTests(unittest.TestCase):
    def test_skill_explains_empty_stdout_preview_refusal_for_both_cli_names(self):
        for module in ("jev_brain.cli", "systemone.cli"):
            with self.subTest(module=module):
                run = subprocess.run([sys.executable, "-m", module, "use", "--target", "", "--request", "Count"],
                                     env=_env(), capture_output=True, text=True, timeout=5)
                self.assertEqual((run.returncode, run.stdout), (2, ""))
                self.assertIn("target", run.stderr)
        skill = (ROOT / "src/jev_brain/skill/SKILL.md").read_text(encoding="utf-8")
        for phrase in ("stderr", "stdout", "exit status"):
            self.assertIn(phrase, skill)
