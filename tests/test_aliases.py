import json
import subprocess
import sys
import tomllib
import unittest

from test_cli import ROOT, _env


class AliasTests(unittest.TestCase):
    def test_console_aliases_and_old_module_commands(self):
        entries = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
        for name, module, args in (("systemone", "cli", ["--version"]),
                                   ("systemone-mcp", "mcp_server", [])):
            target, function = entries[name].split(":")
            new_name = name.replace("systemone", "jev-brain")
            for invocation in (["-c", f"from {target} import {function}; {function}()"],
                               ["-m", "systemone." + module]):
                with self.subTest(alias=name, invocation=invocation):
                    proc = subprocess.run([sys.executable, *invocation, *args], env=_env(),
                                          input='{"jsonrpc":"2.0","id":1,"method":"ping"}\n',
                                          text=True, capture_output=True, check=True)
                    self.assertEqual(proc.stderr.splitlines(), [f"{name} is deprecated. Use {new_name}."])
                    if args:
                        self.assertTrue(proc.stdout.startswith("jev-brain "))
                    else:
                        self.assertEqual(json.loads(proc.stdout)["result"], {})
