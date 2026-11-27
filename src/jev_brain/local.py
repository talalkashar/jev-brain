"""User-owned local consent and append-only JSON Lines audit records."""

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path


def directory():
    return Path.home() / ".config" / "jev-brain"


def private_file(name, flags):
    root = directory()
    if flags & os.O_CREAT:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise OSError("Refusing a linked jev-brain directory.")
    fd = os.open(root / name, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            (os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077))):
        os.close(fd)
        raise OSError(f"{name} must be a private, user-owned regular file with no group or other permissions. New files use 0600.")
    return fd


def consented():
    try:
        with os.fdopen(private_file("consent.json", os.O_RDONLY), encoding="utf-8") as stream:
            raw = stream.read(1025)
            if len(raw) > 1024:
                return False
            config = json.loads(raw)
            return isinstance(config, dict) and config.get("allow_act") is True
    except (OSError, ValueError):
        return False


def grant():
    if consented():
        return
    with os.fdopen(private_file("consent.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY), "w") as stream:
        json.dump({"allow_act": True}, stream)


def audit(fd, record, outcome):
    line = {"time": datetime.now(timezone.utc).isoformat(), **record, "outcome": outcome}
    data = (json.dumps(line, ensure_ascii=True) + "\n").encode("ascii")
    if os.write(fd, data) != len(data):
        raise OSError("Incomplete audit write.")
    os.fsync(fd)


def append_audit(record, outcome, identity=None):
    root = directory()
    flags = os.O_APPEND | os.O_WRONLY
    if identity is None:
        flags |= os.O_CREAT
    fd = private_file("audit.jsonl", flags)
    try:
        info = os.fstat(fd)
        current = (info.st_dev, info.st_ino)
        if identity is not None and current != identity:
            raise OSError("The audit file changed during the call.")
        audit(fd, record, outcome)
        if identity is None and os.name == "posix":
            # Confirm the directory entry as well when the initial append created the file.
            parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        return current
    finally:
        os.close(fd)
