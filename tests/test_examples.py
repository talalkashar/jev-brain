"""Keep the published sample inputs in sync with the actual CLI and mock."""

import json
import subprocess
import sys
import unittest

from test_cli import ROOT
from systemone.engine import MockEngine


class ExampleTests(unittest.TestCase):
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
