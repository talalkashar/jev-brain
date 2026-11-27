"""Validate one caller-selected step before delegating to a guarded jev-run."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import sysconfig
import time
import unicodedata
import uuid
from pathlib import Path
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
            r"schwab|vanguard|hsbc|barclays|santander|monzo|revolut|wise|usbank|citibank|bofa)\b",
            re.I,
        ),
    ),
    ("mail", re.compile(r"\b(mail|e-mail|email|gmail|outlook|thunderbird|webmail|spark|fastmail|proton\s?mail|tuta(?:nota)?|airmail|postbox|hotmail|courriel)\b", re.I)),
    ("messages", re.compile(r"\b(messages|imessage|sms|signal|whatsapp|telegram|messenger|discord|slack|wechat|viber|teams)\b", re.I)),
    (
        "password managers",
        re.compile(
            r"\b(password\s+managers?|passwords?|1password|bitwarden|lastpass|keychain|dashlane|keepass(?:xc)?|keeper|nordpass|enpass|strongbox|axsecuretextfield)\b", re.I
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
            r"connexion|anmelden|iniciar sesión|登录|mot de passe|log[\s-]*in|login|sign[\s-]?in|accounts\.google)\b",
            re.I,
        ),
    ),
)


def sensitive_surface(*parts: str) -> str | None:
    """Return the refused surface name, or None when the text is allowed."""
    # Some sensitive bundle identifiers do not contain the visible product name.
    for part in parts:
        bundle = unicodedata.normalize("NFKC", part).casefold()
        if bundle in ("com.readdle.smartemail-mac", "com.readdle.sparkdesktop"):
            return "mail"
        if bundle in ("com.apple.ichat", "com.apple.mobilesms", "org.whispersystems.signal-desktop"):
            return "messages"
    text = "\n".join(parts)
    for _ in range(4):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    text = unicodedata.normalize("NFKC", text).casefold().replace("_", " ")
    for name, pattern in _SURFACES:
        if pattern.search(text):
            return name
    # Unknown scripts, invisible controls and residual encodings can conceal labels.
    if not text.isascii() or re.search(r"%[0-9a-f]{2}|xn--|[\x00-\x08\x0b-\x1f\x7f]", text):
        return "unverified text"
    return None


def check_window(expect, window=None):
    try:
        if window is None:
            window = foreground.current()
        roles = unicodedata.normalize("NFKC", window.field_role + window.field_subrole).casefold()
        if ("secure" in roles or sensitive_surface(
                window.app, window.bundle, window.title, window.document, window.field_role,
                window.field_subrole, window.field_label, window.description, window.placeholder)):
            raise UseRefused("The front window is sensitive.", "sensitive")
        if not window.matches(expect):
            raise UseRefused("The front window does not match --expect.", "wrong window")
    except (OSError, ValueError, TypeError) as exc:
        raise UseRefused("Could not verify the front window.", "wrong window") from exc
    return window


MAX_OUTPUT = 65536
MAX_FIELD = 4096
OUTCOMES = {"done", "dry run", "target clicked", "text typed", "target not clicked", "text not typed",
            "text unconfirmed", "input unconfirmed", "sensitive", "wrong window", "low confidence", "target not found",
            "stalled", "refused", "runner error"}


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError
    return left


def _capture(argv, deadline, body=None):
    # Both the read-only probe and the executor use this absolute deadline.
    work_deadline = deadline - min(0.05, _remaining(deadline) / 10)
    env = None if body is None else {**os.environ, "JEV_BRAIN_EXECUTOR_CHILD": "1"}
    proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    if body is not None:
        body.update(started=True, steps=None)
    try:
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.select(_remaining(work_deadline)):
                chunk = os.read(proc.stdout.fileno(), min(8192, MAX_OUTPUT + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_OUTPUT:
                    raise ValueError("Process output exceeds the byte limit.")
            else:
                raise TimeoutError
        return proc.wait(timeout=_remaining(work_deadline)), json.loads(output)
    finally:
        # An inherited pipe may outlive the child. Never drain it during cleanup.
        proc.stdout.close()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Darwin can return EPERM for a group whose leader is a zombie.
            if proc.poll() is None:
                proc.kill()
        try:
            proc.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.poll()  # Popen also reaps outstanding children on later launches.


def _observe(deadline):
    # A native AX call can block in C. Only this read-only helper sees the
    # desktop before the parent has approved an executor, and it can be killed.
    probe = ("from dataclasses import asdict; import json; from jev_brain.foreground import current; "
             "print(json.dumps(asdict(current())))")
    try:
        status, data = _capture([sys.executable, "-c", probe], deadline)
        if status or not isinstance(data, dict):
            raise ValueError
        return foreground.Window(**data)
    except (ValueError, TypeError, OSError) as exc:
        if isinstance(exc, TimeoutError):
            raise
        raise UseRefused("Could not read the front window.", "wrong window") from exc


def _run(argv, body, deadline, text):
    try:
        status, data = _capture(argv, deadline, body)
        if not isinstance(data, dict) or data.get("outcome") not in OUTCOMES:
            raise ValueError
        outcome = data["outcome"]
        steps = data.get("steps_taken", data.get("steps"))
        act = body["mode"] == "act"
        success = outcome in ("done", "dry run", "target clicked", "text typed")
        expected = "text typed" if text is not None else "target clicked"
        if success:
            valid = outcome in ("done", expected) if act else outcome == "dry run"
            if status or not valid or type(steps) is not int or steps != int(act):
                raise ValueError
        elif act and outcome in ("input unconfirmed", "runner error") and steps is None:
            pass  # The backend may have lost contact during the one permitted mutation.
        elif type(steps) is not int or steps != int(act and outcome == "text unconfirmed"):
            raise ValueError
        clicked, typed = data.get("clicked"), data.get("typed")
        if clicked is not None and (not act or not success or text is not None or clicked != body["target"]):
            raise ValueError
        if typed is not None and (not act or not success or text is None or typed != text):
            raise ValueError
        confidence = data.get("confidence")
        if confidence is not None and (type(confidence) not in (int, float) or not 0 <= confidence <= 1):
            raise ValueError
        body.update(outcome=outcome, steps=steps, confidence=confidence, clicked=clicked, typed=typed)
        if outcome == "runner error":
            body["error"] = f"jev-run could not complete the backend call. {SETUP}"
        if success:
            return 0
        return 2 if outcome in ("refused", "sensitive") else 3
    except (ValueError, TypeError, RecursionError):
        body.update(outcome="runner error", error=f"Invalid or oversized jev-run result. {SETUP}")
        return 3
    except OSError as exc:
        if isinstance(exc, TimeoutError):
            raise
        if body["started"]:
            body.update(outcome="runner error", error="Lost contact with jev-run.")
            return 3
        raise UseRefused(f"jev-run could not be started. {SETUP}") from exc


def backend_command():
    return [sys.executable, "-c", "from jev_run import _backend; raise SystemExit(_backend())"]


def _executor():
    executable = shutil.which("jev-run")
    if not executable:
        raise UseRefused(f"jev-run is not on PATH. Nothing was run. {SETUP}")
    path = Path(executable)
    if path.suffix.lower() in (".cmd", ".bat"):
        raise UseRefused("A batch file cannot be used as an executor.")
    if (path.parent.resolve() == Path(sysconfig.get_path("scripts")).resolve()
            and importlib.util.find_spec("jev_run") is not None):
        return backend_command()
    return [executable]


def _execute(body, parts, *, act, armed, text, deadline, record, bundled, limits):
    target, request, expect, _ = parts
    if limits != (1, 0):
        raise UseRefused("A call permits exactly one step and zero handoffs.")
    if any(len(part) > MAX_FIELD for part in parts):
        raise UseRefused(f"Each argument is limited to {MAX_FIELD} characters.")
    surface = sensitive_surface(*parts)
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
    if text is not None and target != "focused field":
        raise UseRefused("A text write requires the single target 'focused field'.")
    if act and target == "focused field" and text is None:
        raise UseRefused("A focused field requires --text.")
    if (act or bundled) and not expect.strip():
        raise UseRefused("Supply --expect with an app name or exact URL.")
    if act and not local.consented():
        raise UseRefused("Run jev-brain consent yourself in a local terminal first.")
    if sys.platform != "darwin":
        raise UseRefused("Executor-backed calls require macOS.")
    if bundled and os.environ.get("JEV_BRAIN_EXECUTOR_CHILD"):
        raise UseRefused("Install both packages in the same environment to run the bundled adapter.")
    command = backend_command() if bundled else _executor()
    window = check_window(expect or target, _observe(deadline))
    record["app"] = window.bundle  # matches() accepted a known, fixed bundle identifier.
    argv = [*command, request, "--expect", expect or target, "--steps", "1", "--handoffs", "0"]
    if expect and target != "focused field":
        argv.extend(["--target", target])
    if text is not None:
        argv.extend(["--text", text])
    if act:
        argv.append("--act")
    code = _run(argv, body, deadline, text)
    if code == 0 and body["outcome"] == "target clicked":
        body["clicked"] = target
    if code == 0 and body["outcome"] == "text typed":
        body["typed"] = text
    return code


def perform(target="", request="", armed=False, *, act=False, expect="", text=None, steps=1, handoffs=0, _bundled=False):
    deadline = time.monotonic() + TIMEOUT
    mode = "act" if act else "armed" if armed else "dry-run"
    body = dict(mode=mode, decision_maker="caller", hands="jev", target=target, request=request,
                started=False, outcome="dry run", clicked=None, typed=None, confidence=None, steps=0)
    parts = [target, request, expect, text or ""]
    record = {"call": uuid.uuid4().hex[:16], "app": None, "length": sum(map(len, parts))}
    fd = None
    try:
        if act:
            fd = local.private_file("audit.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY)
            local.audit(fd, record, "requested")
        try:
            code = _execute(body, parts, act=act, armed=armed, text=text, deadline=deadline,
                            record=record, bundled=_bundled, limits=(steps, handoffs))
        except UseRefused as exc:
            if not act:
                raise
            body.update(outcome=exc.outcome, error=str(exc))
            code = 2
        except (TimeoutError, subprocess.TimeoutExpired):
            body.update(outcome="timeout")
            code = 124
        except KeyboardInterrupt:
            body.update(outcome="interrupted")
            code = 130
        if fd is not None:
            local.audit(fd, record, body["outcome"])
    except OSError as exc:
        body.update(outcome="audit error", error=f"Could not record the act call: {exc}")
        code = 2
    finally:
        if fd is not None:
            os.close(fd)
    return code, body
