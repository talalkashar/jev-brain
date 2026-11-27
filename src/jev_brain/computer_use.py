"""Validate one caller-selected step before delegating to a guarded jev-run."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import uuid
from urllib.parse import unquote

from jev_brain import foreground, local

SETUP = "See https://github.com/talalkashar/jev-brain#install-the-macos-runner"
TIMEOUT = 30


class UseRefused(Exception):
    def __init__(self, message, outcome="refused"):
        super().__init__(message)
        self.outcome = outcome


_SURFACES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "banking",
        re.compile(
            r"\b(bank|banking|credit\s+card|debit\s+card|bank\s*of\s*america|onlinebanking|chase|wells\s*fargo|capital\s*one|"
            r"citi\s*bank|navy\s+federal|paypal|venmo|zelle|cash\s+app|wallet|robinhood|coinbase|fidelity|"
            r"schwab|vanguard)\b",
            re.I,
        ),
    ),
    ("mail", re.compile(r"\b(mail|e-mail|email|gmail|outlook)\b", re.I)),
    ("messages", re.compile(r"\b(messages|imessage|sms|signal|whatsapp|telegram)\b", re.I)),
    (
        "password managers",
        re.compile(
            r"\b(password\s+managers?|passwords?|1password|bitwarden|lastpass|keychain|dashlane)\b", re.I
        ),
    ),
    (
        "Mason or student records",
        re.compile(
            r"\b(mason|gmu|patriot\s*web|student\s+(records?|accounts?)|ellucian|degree\s+evaluation|"
            r"financial\s+aid|tuition)\b",
            re.I,
        ),
    ),
    (
        "admin consoles",
        re.compile(
            r"\b(admin(?:istrator)?\s+consoles?|admin\b|console\.cloud\.google|cloud\s+console|"
            r"supabase\.com/dashboard|dashboard\.render\.com|console\.anthropic|platform\.openai|"
            r"console\.typesafe)",
            re.I,
        ),
    ),
    (
        "login or MFA",
        re.compile(
            r"\b(mfa|2fa|two-factor|multi-factor|one[- ]time\s+(?:code|password)|passcode|otp|verification\s+code|duo\s+security|"
            r"log[\s-]*in|login|sign[\s-]?in|accounts\.google)\b",
            re.I,
        ),
    ),
)


def sensitive_surface(*parts: str) -> str | None:
    """Return the refused surface name, or None when the text is allowed."""
    text = unquote("\n".join(parts)).replace("_", " ")
    for name, pattern in _SURFACES:
        if pattern.search(text):
            return name
    return None


def check_window(expect):
    try:
        window = foreground.current()
    except OSError as exc:
        raise UseRefused(str(exc), "wrong window") from exc
    if ("secure" in window.field_role.casefold() or
            sensitive_surface(window.app, window.title, window.document)):
        raise UseRefused("The front window is sensitive.", "sensitive")
    if not window.matches(expect):
        raise UseRefused("The front window does not match --expect.", "wrong window")
    return window


def _run(argv, body):
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=os.name == "posix")
    except OSError as exc:
        raise UseRefused(f"jev-run could not be started. {SETUP}") from exc
    body.update(started=True, steps=None)
    try:
        stdout, _ = proc.communicate(timeout=TIMEOUT)
    except (KeyboardInterrupt, subprocess.TimeoutExpired) as exc:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            proc.kill()
        proc.communicate()
        body["outcome"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "timeout"
        return 130 if isinstance(exc, KeyboardInterrupt) else 124

    try:
        data = json.loads(stdout)
        if isinstance(data, dict) and "error" in data and not data.get("outcome"):
            body.update(outcome="runner error", error=f"jev-run could not complete its backend call. {SETUP}")
            return 3
        if not isinstance(data, dict) or not isinstance(data.get("outcome"), str):
            raise ValueError
    except (ValueError, UnicodeError):
        body.update(outcome="runner error", error=f"jev-run returned no valid outcome. {SETUP}")
        return 3
    body.update(outcome=data["outcome"], clicked=data.get("clicked"), typed=data.get("typed"),
                confidence=data.get("confidence"), steps=data.get("steps_taken", data.get("steps")))
    if isinstance(data.get("error"), str):
        body["error"] = data["error"]
    if body["outcome"] in ("done", "dry run", "target clicked", "text typed"):
        if proc.returncode != 0:
            body.update(outcome="runner error", error=f"jev-run exited {proc.returncode}.")
            return 3
        return 0
    return 2 if body["outcome"] == "sensitive" else 3


def _execute(body, parts, surface, *, act, armed, text):
    target, request, expect, _ = parts
    if surface:
        raise UseRefused(f"refusing a sensitive surface: {surface}", "sensitive")
    if not target.strip():
        raise UseRefused("a named target is required")
    if not request.strip():
        raise UseRefused("a request is required")
    if any("--act" in part.split() for part in parts):
        raise UseRefused("refusing to pass --act as data")
    if any(part.strip().startswith("-") or "\x00" in part for part in parts):
        raise UseRefused("refusing an option-like or invalid argument")
    if not (act or armed):
        return 0
    if act and target == "focused field" and text is None:
        raise UseRefused("A focused field requires --text.")
    if act and not expect.strip():
        raise UseRefused("Act requires --expect with an app name or exact URL.")
    if act and not local.consented():
        raise UseRefused("Run jev-brain consent yourself in a local terminal first.")
    executable = shutil.which("jev-run")
    if not executable:
        raise UseRefused(f"jev-run is not on PATH. Nothing was run. {SETUP}")
    if act:
        check_window(expect)
    argv = [executable, request, "--expect", expect or target, "--steps", "1", "--handoffs", "0"]
    if expect and target != "focused field":
        argv.extend(["--target", target])
    if text is not None:
        argv.extend(["--text", text])
    if act:
        argv.append("--act")
    code = _run(argv, body)
    # Older guarded runners report confirmation as an outcome without separate action fields.
    if code == 0 and body["outcome"] == "target clicked" and body["clicked"] is None:
        body["clicked"] = target
    if code == 0 and body["outcome"] == "text typed" and body["typed"] is None:
        body["typed"] = text
    return code


def perform(target="", request="", armed=False, *, act=False, expect="", text=None):
    mode = "act" if act else "armed" if armed else "dry-run"
    body = dict(mode=mode, decision_maker="caller", hands="jev", target=target, request=request,
                started=False, outcome="dry run", clicked=None, typed=None, confidence=None, steps=0)
    parts = [target, request, expect, text or ""]
    surface = sensitive_surface(*parts)
    if not act:
        return _execute(body, parts, surface, act=False, armed=armed, text=text), body

    record = {"call": uuid.uuid4().hex, "target": target, "request": request}
    if surface:
        record.update(target="[redacted]", request="[redacted]")
    try:
        fd = local.private_file("audit.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY)
        try:
            local.audit(fd, record, "requested")
            try:
                code = _execute(body, parts, surface, act=True, armed=False, text=text)
            except UseRefused as exc:
                code = 2
                body.update(outcome=exc.outcome, error=str(exc))
            except KeyboardInterrupt:
                code = 130
                body.update(outcome="interrupted")
            local.audit(fd, record, body["outcome"])
        finally:
            os.close(fd)
    except OSError as exc:
        if body["started"]:
            body["runner_outcome"] = body["outcome"]
        body.update(outcome="audit error", error=f"Could not record the act call: {exc}")
        code = 2
    return code, body
