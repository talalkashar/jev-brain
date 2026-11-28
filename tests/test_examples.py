"""Keep the published sample inputs in sync with the actual CLI and mock."""

import json
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
import venv
from pathlib import Path

from test_cli import ROOT
from systemone.engine import MockEngine


class ExampleTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "requires Windows PowerShell")
    def test_windows_client_config_examples(self):
        guide = (ROOT / "docs" / "clients.md").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="client setup ") as directory:
            checkout = Path(directory)
            venv.EnvBuilder().create(checkout / ".venv")
            exe = str(checkout / ".venv" / "Scripts" / "systemone-mcp.exe")
            examples = [
                ("Claude Code and Cursor", json.loads, {"mcpServers": {
                    "systemone": {"type": "stdio", "command": exe, "args": []},
                }}),
                ("Codex", tomllib.loads, {"mcp_servers": {
                    "systemone": {"command": exe, "args": []},
                }}),
            ]
            for heading, parse, expected in examples:
                with self.subTest(client=heading):
                    section = guide.split(f"### {heading}\n", 1)[1].split("\n### ", 1)[0]
                    snippet = re.search(r"```powershell\n(.*?)\n```", section, re.DOTALL)
                    self.assertIsNotNone(snippet, "missing Windows PowerShell example")
                    script = checkout / "example.ps1"
                    script.write_text(snippet[1], encoding="utf-8-sig")
                    result = subprocess.run(
                        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                         "RemoteSigned", "-File", str(script)],
                        cwd=checkout, capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(parse(result.stdout), expected)

    def test_preview_reports_both_outcomes(self):
        demo = subprocess.run(
            [sys.executable, str(ROOT / "examples" / "preview.py")],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(demo.returncode, 0, demo.stderr)
        self.assertEqual(demo.stdout.splitlines(), [
            "Notes: dry-run, started=false", "exit 0",
            "Mail: refusing a sensitive surface: mail", "exit 2",
        ])

    def test_queue_sample_matches_documented_answer(self):
        fixture = json.loads((ROOT / "examples" / "queue.json").read_text())
        answers = MockEngine().decide(fixture["state"], fixture["questions"])
        self.assertEqual(answers, {"route": {"label": "ci"}})
