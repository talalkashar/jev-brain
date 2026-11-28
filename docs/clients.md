# Connect a local MCP client

Install the CLI in a virtual environment as shown in the [README](../README.md#install-the-cli). In a terminal, start the mock and leave it running.

```sh
.venv/bin/systemone serve --mock
```

In Windows PowerShell, start the mock with `.\.venv\Scripts\systemone.exe serve --mock`.

The client starts a separate stdio process, `systemone-mcp`. Its two tools call the mock at `http://127.0.0.1:18100`. Use the installed executable's **absolute path** so a client opened from the Dock can find it.

### Claude Code and Cursor

From the checkout, run the command for your shell in a second terminal. Copy the JSON it prints into your MCP configuration. Claude Code supports project-level `.mcp.json`; Cursor supports project-level `.cursor/mcp.json`. If the file already has servers, add just the `systemone` entry under `mcpServers`. Keep machine-specific paths out of shared configs.

**macOS and Linux**

```sh
.venv/bin/python -c 'import json, sysconfig
from pathlib import Path
exe = Path(sysconfig.get_path("scripts")) / "systemone-mcp"
entry = {"type": "stdio", "command": str(exe), "args": []}
print(json.dumps({"mcpServers": {"systemone": entry}}, indent=2))'
```

**Windows PowerShell**

```powershell
@'
import json, sysconfig
from pathlib import Path
exe = Path(sysconfig.get_path("scripts")) / "systemone-mcp.exe"
entry = {"type": "stdio", "command": str(exe), "args": []}
print(json.dumps({"mcpServers": {"systemone": entry}}, indent=2))
'@ | .\.venv\Scripts\python.exe -
```

### Codex

From the checkout, run the command for your shell. Copy its output into your Codex `config.toml`. If a `[mcp_servers.systemone]` table exists, edit that table in place.

**macOS and Linux**

```sh
.venv/bin/python -c 'import json, sysconfig
from pathlib import Path
exe = Path(sysconfig.get_path("scripts")) / "systemone-mcp"
print("[mcp_servers.systemone]")
print("command =", json.dumps(str(exe), ensure_ascii=False))
print("args = []")'
```

**Windows PowerShell**

```powershell
@'
import json, sysconfig
from pathlib import Path
exe = Path(sysconfig.get_path("scripts")) / "systemone-mcp.exe"
print("[mcp_servers.systemone]")
print("command =", json.dumps(str(exe), ensure_ascii=False))
print("args = []")
'@ | .\.venv\Scripts\python.exe -
```

### Verify the connection

Reload the client, list this server's tools and call `health` with `{}`. Expect `status` equal to `ok` and `mock` equal to `true`. If the client lists tools but says SystemOne is unreachable, check that the HTTP mock is still running. If port 18100 is busy, start the mock with `--port 18101` and set `SYSTEMONE_URL=http://127.0.0.1:18101` in the stdio process environment. If the client cannot launch the stdio process, check that the configured file exists. Moving or recreating the venv can change that path.

`systemone install` also previews the built-in user-level configuration for each client. It emits a bare `systemone-mcp` command and needs the client's PATH to include the installed scripts directory. Review its plan locally. Passing `--yes` writes the proposed config. A CLI-capable agent can use the [bundled skill](../src/systemone/skill/SKILL.md) for handoff previews.
