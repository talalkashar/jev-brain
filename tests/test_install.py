"""Drive the real `jev-brain install` command against a temporary HOME.

The test process keeps the real HOME. Only the child process sees the temp one.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

import srcpath  # side effect: put src/ on sys.path
from test_cli import _env, _run

class InstallCommandTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.home = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)

    def _child_env(self):
        # Path.home() reads USERPROFILE on Windows and HOME elsewhere.
        return _env(HOME=str(self.home), USERPROFILE=str(self.home),
                    PATH=str(self.home) + os.pathsep + os.environ.get("PATH", ""))

    def _assert_targeted_temp_home(self, stdout: str):
        home_line = stdout.splitlines()[0]
        self.assertTrue(home_line.startswith("home: "))
        reported = Path(home_line.removeprefix("home: "))
        self.assertEqual(reported.resolve(), self.home.resolve())
        self.assertNotEqual(reported.resolve(), Path.home().resolve())

    def test_without_yes_prints_the_change_and_writes_nothing(self):
        self._seed_existing()
        before = self._seed_bytes()
        result = _run(["install"], env=self._child_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_targeted_temp_home(result.stdout)
        self.assertIn("dry run: no files written. Pass --yes to write them.", result.stdout)
        self.assertIn("jev-brain-mcp", result.stdout)
        self.assertIn(".claude.json", result.stdout)
        self.assertIn(".cursor/mcp.json", result.stdout)
        self.assertIn(".codex/config.toml", result.stdout)
        self.assertEqual(self._seed_bytes(), before)
        self.assertEqual(list(self.home.rglob("*.jev-brain-tmp")), [])
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        print("install without --yes: Claude Code, Cursor, and Codex config printed; nothing written")

    def test_yes_writes_only_inside_the_temp_home(self):
        self._seed_existing()
        result = _run(["install", "--yes"], env=self._child_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_targeted_temp_home(result.stdout)
        self.assertIn("wrote the files above.", result.stdout)
        self.assertNotIn("Pass --yes", result.stdout)

        claude = json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))
        self.assertEqual(claude["userID"], "kept")
        self.assertEqual(claude["mcpServers"]["other"]["command"], "keep")
        self.assertEqual(claude["mcpServers"]["jev-brain"]["command"], "jev-brain-mcp")
        self.assertEqual(claude["mcpServers"]["jev-brain"]["args"], [])
        self.assertEqual(claude["mcpServers"]["jev-brain"]["type"], "stdio")

        cursor = json.loads((self.home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(cursor["mcpServers"]["other"]["url"], "http://127.0.0.1:9")
        self.assertEqual(cursor["mcpServers"]["jev-brain"]["command"], "jev-brain-mcp")

        codex = (self.home / ".codex" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('[mcp_servers.other]\ncommand = "keep"', codex)
        self.assertIn("[mcp_servers.other.env]", codex)
        self.assertEqual(codex.count("[mcp_servers.jev-brain]"), 1)
        self.assertIn('command = "old"', codex)
        self.assertIn('args = ["stale"]', codex)

        for path in (
            self.home / ".claude.json",
            self.home / ".cursor" / "mcp.json",
            self.home / ".codex" / "config.toml",
        ):
            self.assertTrue(path.resolve().is_relative_to(self.home.resolve()))
            self.assertFalse(path.is_symlink())
        self.assertEqual(list(self.home.rglob("*.jev-brain-tmp")), [])
        print("install --yes: writes stayed inside the temp home")

    def test_yes_one_agent_does_not_write_the_others(self):
        result = _run(["install", "--agent", "cursor", "--yes"], env=self._child_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        written = self.home / ".cursor" / "mcp.json"
        self.assertTrue(written.is_file())
        self.assertTrue(written.resolve().is_relative_to(self.home.resolve()))
        self.assertFalse((self.home / ".claude.json").exists())
        self.assertFalse((self.home / ".codex" / "config.toml").exists())
        data = json.loads(written.read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["jev-brain"]["command"], "jev-brain-mcp")

    def test_invalid_json_exits_2_and_leaves_bytes_unchanged(self):
        path = self.home / ".claude.json"
        path.write_text("{", encoding="utf-8")
        before = path.read_bytes()
        result = _run(["install", "--yes"], env=self._child_env())
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("not valid JSON", result.stderr)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.home / ".cursor").exists())
        self.assertFalse((self.home / ".codex").exists())

    def test_symlink_outside_the_temp_home_is_refused(self):
        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir) / "claude.json"
            outside.write_text('{"keep": true}\n', encoding="utf-8")
            before = outside.read_bytes()
            (self.home / ".claude.json").symlink_to(outside)
            result = _run(["install", "--yes"], env=self._child_env())
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn("outside home", result.stderr)
            self.assertEqual(outside.read_bytes(), before)
            self.assertFalse((self.home / ".cursor").exists())
            self.assertFalse((self.home / ".codex").exists())
            self.assertFalse((Path(outside_dir) / ".cursor").exists())

    def test_yes_does_not_follow_a_temp_symlink_outside_home(self):
        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir) / "payload"
            outside.write_bytes(b"OUTSIDE-SENTINEL")
            before = outside.read_bytes()
            link = self.home / ".claude.json.jev-brain-tmp"
            link.symlink_to(outside)
            result = _run(["install", "--yes"], env=self._child_env())
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("refusing to replace", result.stderr)
            self.assertEqual(outside.read_bytes(), before)
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link).removeprefix("\\\\?\\"), str(outside))  # Windows adds \\?\
            self.assertFalse((self.home / ".claude.json").exists())
            self.assertFalse((self.home / ".cursor" / "mcp.json").exists())
            self.assertFalse((self.home / ".codex" / "config.toml").exists())
            self.assertEqual(list(Path(outside_dir).iterdir()), [outside])
            print("pre-existing *.jev-brain-tmp symlink left the outside file unchanged")

    def test_preserves_legacy_server_identity_and_other_servers(self):
        cursor = self.home / ".cursor"
        cursor.mkdir()
        (cursor / "mcp.json").write_text(json.dumps({"mcpServers": {
            "systemone": {"type": "stdio", "command": "systemone-mcp", "args": []},
            "other": {"command": "unrelated"}}}))
        codex = self.home / ".codex"
        codex.mkdir()
        path = codex / "config.toml"
        path.write_text('[mcp_servers.systemone]\ncommand = "systemone-mcp"\nargs = []\n'
                        '[mcp_servers.jev-brain-extra]\ncommand = "keep"\n')
        result = _run(["install", "--yes"], env=self._child_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(json.loads((cursor / "mcp.json").read_text())["mcpServers"]),
                         {"other", "systemone"})
        self.assertNotIn("[mcp_servers.jev-brain]", path.read_text())
        self.assertIn('[mcp_servers.jev-brain-extra]\ncommand = "keep"', path.read_text())

    def _seed_existing(self):
        (self.home / ("keep.exe" if os.name == "nt" else "keep")).touch(mode=0o755)
        (self.home / ".claude.json").write_text(
            json.dumps(
                {
                    "userID": "kept",
                    "mcpServers": {"other": {"type": "stdio", "command": "keep", "cwd": str(self.home), "args": ["--a"]}},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        cursor = self.home / ".cursor"
        cursor.mkdir()
        (cursor / "mcp.json").write_text(
            json.dumps({"mcpServers": {"other": {"url": "http://127.0.0.1:9"}}}) + "\n",
            encoding="utf-8",
        )
        codex = self.home / ".codex"
        codex.mkdir()
        (codex / "config.toml").write_text(
            '[mcp_servers.other]\ncommand = "keep"\n\n'
            '[mcp_servers.jev-brain]\ncommand = "old"\nargs = ["stale"]\n\n'
            "[mcp_servers.other.env]\nVALUE = \"kept\"\n",
            encoding="utf-8",
        )

    def _seed_bytes(self):
        return {
            path: path.read_bytes()
            for path in (
                self.home / ".claude.json",
                self.home / ".cursor" / "mcp.json",
                self.home / ".codex" / "config.toml",
            )
        }
