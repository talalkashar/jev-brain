"""Plan agent-config edits, then write them only when asked.

`plan_install` decides the paths and the file bytes. `apply_install` either prints
that plan or writes those files. Nothing is written unless `yes` is true.
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

AGENTS = ("claude-code", "cursor", "codex")

_SERVER = {"type": "stdio", "command": "jev-brain-mcp", "args": []}
_TOML_BLOCK = '[mcp_servers.jev-brain]\ncommand = "jev-brain-mcp"\nargs = []\n'


class InstallError(ValueError):
    """The home or an existing config cannot be updated safely."""


@dataclass(frozen=True)
class PlannedWrite:
    """One file under home, and the exact bytes that would replace it."""

    path: Path
    content: str
    existing: str | None


def plan_install(home: Path, agents: tuple[str, ...] | list[str] = AGENTS) -> list[PlannedWrite]:
    """Return the files install would write. Does not create or modify anything."""
    root = _home_dir(home)
    chosen = _agents(agents)
    planned: list[PlannedWrite] = []
    for name in chosen:
        path = _config_path(root, name)
        existing = _read(path)
        if name == "codex":
            content = _upsert_toml(existing or "")
        else:
            content = _upsert_json(existing)
        planned.append(PlannedWrite(path, content, existing))
    return planned


def apply_install(home: Path, planned: list[PlannedWrite], *, yes: bool) -> str:
    """Print the plan. Write the files only when `yes` is true."""
    root = _home_dir(home)
    lines = [f"home: {root}"]
    for item in planned:
        _require_inside(root, item.path)
        lines.append(f"agent: {_agent_label(root, item.path)}")
        lines.append(f"file: {item.path.relative_to(root).as_posix()}")
        lines.append("keep existing MCP configuration" if item.content == item.existing else
                     "add jev-brain with command jev-brain-mcp")
    if not yes:
        lines.append("dry run: no files written. Pass --yes to write them.")
        return "\n".join(lines) + "\n"
    for item in planned:
        if item.content != item.existing:
            if _read(item.path) != item.existing:
                raise InstallError("Agent configuration changed during installation. Run install again.")
            _write(root, item)
    lines.append("wrote the files above.")
    return "\n".join(lines) + "\n"


def _agents(agents: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if not agents:
        raise InstallError("at least one agent is required")
    unknown = [name for name in agents if name not in AGENTS]
    if unknown:
        raise InstallError(f"unknown agent {unknown[0]!r}")
    seen: list[str] = []
    for name in agents:
        if name not in seen:
            seen.append(name)
    return tuple(seen)


def _home_dir(home: Path) -> Path:
    root = Path(home).expanduser().resolve()
    if not root.is_dir():
        raise InstallError(f"home is not a directory: {root}")
    return root


_AGENT_LABELS = {
    ".claude.json": "Claude Code",
    ".cursor/mcp.json": "Cursor",
    ".codex/config.toml": "Codex",
}


def _agent_label(home: Path, path: Path) -> str:
    return _AGENT_LABELS.get(path.relative_to(home).as_posix(), path.name)


def _config_path(home: Path, agent: str) -> Path:
    if agent == "claude-code":
        return _require_inside(home, home / ".claude.json")
    if agent == "cursor":
        return _require_inside(home, home / ".cursor" / "mcp.json")
    if agent == "codex":
        return _require_inside(home, home / ".codex" / "config.toml")
    raise InstallError(f"unknown agent {agent!r}")


def _require_inside(home: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(home):
        raise InstallError(f"refusing to write outside home: {resolved}")
    return resolved


def _read(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise InstallError(f"refusing to replace {path}")
    return path.read_text(encoding="utf-8")


def _write(home: Path, item: PlannedWrite) -> None:
    path = _require_inside(home, item.path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise InstallError(f"refusing to replace {path}")
    parent = path.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise InstallError(f"refusing to replace {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise InstallError(f"refusing to replace {parent}")
    _require_inside(home, parent)
    temporary = path.with_name(path.name + ".jev-brain-tmp")
    # Do not resolve this path: resolve() would follow a symlink out of home.
    _refuse_before_write(temporary)
    fd = _open_exclusive(temporary)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InstallError(f"refusing to replace {temporary}")
        _write_all(fd, item.content.encode("utf-8"))
    except Exception:
        os.close(fd)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    os.close(fd)
    os.replace(temporary, path)


def _refuse_before_write(path: Path) -> None:
    """Refuse a symlink or a non-regular file before any byte is written."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise InstallError(f"refusing to replace {path}")


def _open_exclusive(path: Path) -> int:
    """Create a new regular file. O_EXCL fails on any existing name, a symlink included, and O_NOFOLLOW adds the
    same where the OS has it (Windows does not)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags, 0o600)
    except OSError as exc:
        raise InstallError(f"refusing to replace {path}") from exc


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        wrote = os.write(fd, view)
        if wrote <= 0:
            raise InstallError("could not write agent config")
        view = view[wrote:]


def _unique_keys(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise InstallError("Agent config contains duplicate JSON keys.")
        values[key] = value
    return values


def _upsert_json(existing: str | None) -> str:
    if existing is None or existing.strip() == "":
        data: dict = {}
    else:
        try:
            data = json.loads(existing, object_pairs_hook=_unique_keys)
        except json.JSONDecodeError as exc:
            raise InstallError(f"agent config is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise InstallError("agent config must be a JSON object")
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise InstallError("mcpServers must be a JSON object")
    if _has_server_policy(servers, data):
        return existing
    servers["jev-brain"] = dict(_SERVER)
    data["mcpServers"] = servers
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _has_server_policy(servers, config):
    if "jev-brain" in servers or "systemone" in servers:
        return True
    return _references_server(config)


def _references_server(value):
    # A disabled-server list or agent-specific policy can live outside the
    # server table. Preserve the server name those restrictions apply to.
    if isinstance(value, dict):
        return any(_references_server(k) or _references_server(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_references_server(item) for item in value)
    if not isinstance(value, str):
        return False
    value = value.casefold()
    return (value in ("systemone", "jev-brain", "systemone-mcp", "jev-brain-mcp")
            or "mcp__systemone__" in value or "mcp__jev-brain__" in value)


def _upsert_toml(existing: str) -> str:
    try:
        expected = tomllib.loads(existing)
        servers = expected.setdefault("mcp_servers", {})
        if not isinstance(servers, dict):
            raise InstallError("mcp_servers must be a table.")
        if _has_server_policy(servers, expected):
            return existing
        servers["jev-brain"] = {"command": "jev-brain-mcp", "args": []}
        proposed = existing + ("\n" if existing else "") + _TOML_BLOCK
        if tomllib.loads(proposed) != expected:
            raise InstallError("Could not verify the proposed MCP server table.")
        return proposed
    except tomllib.TOMLDecodeError as exc:
        raise InstallError("Cannot add the MCP server safely to this TOML configuration.") from exc
