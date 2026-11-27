"""Validate one caller-selected step before delegating to a guarded jev-run."""

from __future__ import annotations

import hashlib
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
import threading
import time
import unicodedata
import uuid
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import astuple, dataclass
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
            r"\b(admin(?:istrator)?\s+consoles?|admin\b|(?:console|portal|dashboard|dash|management)\.[a-z0-9-]+\.|cloud\s+console|"
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


def sensitive_control(role, subrole, *labels):
    roles = unicodedata.normalize("NFKC", role + subrole).casefold()
    return "secure" in roles or sensitive_surface(role, subrole, *labels)


def check_window(expect, window=None):
    try:
        if window is None:
            window = foreground.current()
        if sensitive_control(window.field_role, window.field_subrole, window.app, window.bundle, window.title,
                             window.document, window.field_label, window.description, window.placeholder):
            raise UseRefused("The front window is sensitive.", "sensitive")
        if not window.matches(expect):
            raise UseRefused("The front window does not match --expect.", "wrong window")
        if window.bundle in foreground.BROWSERS:
            # AXDocument can establish equality but cannot attest a safe origin.
            raise UseRefused("Browser execution is disabled. No browser origins are approved.", "sensitive")
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


# A stuck OS read or storage call may finish later. Bound both waiting time
# and outstanding work in long-lived MCP hosts. These threads never issue input.
_io_slots = threading.BoundedSemaphore(4)


def _bounded(deadline, operation, *args):
    if not _io_slots.acquire(timeout=_remaining(deadline)):
        raise TimeoutError
    result = Future()

    def work():
        try:
            _remaining(deadline)
            result.set_result(operation(*args))
        except BaseException as exc:
            result.set_exception(exc)
        finally:
            _io_slots.release()

    try:
        threading.Thread(target=work, daemon=True).start()
    except BaseException:
        _io_slots.release()
        raise
    value = result.result(timeout=_remaining(deadline))
    _remaining(deadline)
    return value


# The initial process can announce readiness but cannot run executor code on its
# own. -I -S keeps site hooks and caller-controlled imports out of this handshake.
_LAUNCHER = """\
import os, select, sys, time
deadline = float(sys.argv[1])
# Keep the status channel private to this helper. dup is close-on-exec.
status = os.dup(2)
with open(os.devnull, "wb") as sink:
    os.dup2(sink.fileno(), 2)
for ready, grant in ((b"R", b"G"), (b"D", b"X")):
    os.write(status, ready)
    readable, _, _ = select.select([0], [], [], max(0, deadline - time.monotonic()))
    if not readable or os.read(0, 1) != grant or time.monotonic() >= deadline:
        os.write(status, b"T")
        sys.exit(1)
try:
    os.execv(sys.argv[2], sys.argv[2:])
except (OSError, ValueError):
    os.write(status, b"E")
    sys.exit(1)
"""
_launch_slots = threading.BoundedSemaphore(4)


@contextmanager
def _launch(argv, work_deadline, deadline, approval):
    """The worker owns the child through reaping, even if Popen returns late."""
    ready = Future()
    release = threading.Event()
    reaped = threading.Event()

    def start():
        if not _launch_slots.acquire(blocking=False):
            ready.set_exception(UseRefused("All executor startup slots are in use."))
            reaped.set()
            return
        try:
            _remaining(work_deadline)
            command = [sys.executable, "-I", "-S", "-c", _LAUNCHER, str(work_deadline), *argv]
            env = os.environ | {"JEV_BRAIN_EXECUTOR_CHILD": "1", "JEV_BRAIN_APPROVAL": approval.stamp()}
            proc = subprocess.Popen(command, env=env,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True)
        except BaseException as exc:
            ready.set_exception(exc)
        else:
            with proc:
                ready.set_result(proc)
                release.wait()
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # Darwin can return EPERM for a group whose leader is a zombie.
                    if proc.poll() is None:
                        proc.kill()
                # Popen's context closes pipes without draining, then reaps.
        finally:
            _launch_slots.release()
            reaped.set()

    try:
        threading.Thread(target=start, daemon=True).start()
        yield ready.result(timeout=_remaining(work_deadline))
    finally:
        release.set()
        if ready.done():
            reaped.wait(max(0, deadline - time.monotonic()))


class _ExecFailed(OSError):
    """The helper confirmed execv failed before the executor could start."""


def _capture(argv, deadline, body, approval):
    work_deadline = deadline - min(0.05, _remaining(deadline) / 10)
    with _launch(argv, work_deadline, deadline, approval) as proc:
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stderr, selectors.EVENT_READ)

            def status():
                if not selector.select(_remaining(work_deadline)):
                    raise TimeoutError
                return os.read(proc.stderr.fileno(), 1)

            for ready, grant in ((b"R", b"G"), (b"D", b"X")):
                if status() != ready:
                    raise OSError("Unexpected executor launcher status.")
                _bounded(work_deadline, approval.recheck, work_deadline)
                _remaining(work_deadline)
                if grant == b"X":
                    body.update(started=None, steps=None)
                try:
                    os.write(proc.stdin.fileno(), grant)
                except OSError:
                    body.update(started=False, steps=0)
                    raise
            proc.stdin.close()
            result = status()
            if result in (b"E", b"T"):
                body.update(started=False, steps=0)
                if result == b"T":
                    raise TimeoutError
                raise _ExecFailed
            if result:
                raise OSError("Unexpected executor launcher status.")
            selector.unregister(proc.stderr)
            selector.register(proc.stdout, selectors.EVENT_READ)
            body["started"] = True  # exec closed the helper's private status descriptor.
            while selector.select(_remaining(work_deadline)):
                chunk = os.read(proc.stdout.fileno(), min(8192, MAX_OUTPUT + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_OUTPUT:
                    raise ValueError("Process output exceeds the byte limit.")
            else:
                raise TimeoutError
        returncode = proc.wait(timeout=_remaining(work_deadline))
        if returncode < 0 and not output:
            # A signal could have closed the status descriptor just before exec.
            body["started"] = None
            raise OSError("Startup interrupted.")
        return returncode, json.loads(output)


def _run(argv, body, deadline, text, approval):
    try:
        status, data = _capture(argv, deadline, body, approval)
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
    except _ExecFailed:
        body.update(outcome="runner error", error=f"jev-run could not start. Check the executable and its interpreter. {SETUP}")
        return 2
    except OSError as exc:
        if isinstance(exc, TimeoutError):
            raise
        if body["started"] is not False:
            body.update(outcome="runner error", error="Lost contact with jev-run.")
            return 3
        raise UseRefused(f"jev-run could not be started. {SETUP}") from exc


def backend_command():
    return [sys.executable, "-I", "-c", "from jev_run import _backend; raise SystemExit(_backend())"]


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


def fingerprint(value):
    """Compare observations across exec without exporting raw desktop metadata."""
    if isinstance(value, foreground.Window):
        value = astuple(value)
    return hashlib.sha256(json.dumps(value, ensure_ascii=True).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class _Approval:
    expect: str
    label: str | None
    window: foreground.Window
    metadata: tuple | None

    def stamp(self):
        return fingerprint(self.window) + ":" + fingerprint(self.metadata)

    def recheck(self, deadline):
        if _observe(self.expect, self.label, deadline) != self:
            raise UseRefused("The approved window or target changed.", "wrong window")


def _observe(expect, label, deadline):
    _remaining(deadline)
    window = check_window(expect)
    metadata = None
    if label is not None:
        try:
            metadata = foreground.target_metadata(window, label, deadline)
        except TimeoutError:
            raise
        except foreground.TargetUnavailable as exc:
            raise UseRefused("Could not verify a unique target control.", exc.outcome) from exc
        except (OSError, TypeError, ValueError) as exc:
            raise UseRefused("Could not verify the target control.", "low confidence") from exc
        if sensitive_control(*metadata):
            raise UseRefused("The target control is sensitive.", "sensitive")
        if check_window(expect) != window:
            raise UseRefused("The foreground changed during target lookup.", "wrong window")
    _remaining(deadline)
    return _Approval(expect, label, window, metadata)


def _prepare(act, bundled, expect, label, deadline):
    if act and not local.consented():
        raise UseRefused("Run jev-brain consent yourself in a local terminal first.")
    if sys.platform != "darwin":
        raise UseRefused("Executor-backed calls require macOS.")
    if bundled and os.environ.get("JEV_BRAIN_EXECUTOR_CHILD"):
        raise UseRefused("Install both packages in the same environment to run the bundled adapter.")
    approval = _observe(expect, label, deadline)
    return (backend_command() if bundled else _executor()), approval


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
        raise UseRefused("Supply --expect with a supported native app name.")
    label = target if expect and target != "focused field" else None
    command, approval = _bounded(deadline, _prepare, act, bundled, expect or target, label, deadline)
    record["app"] = approval.window.bundle
    argv = [*command, request, "--expect", expect or target, "--steps", "1", "--handoffs", "0"]
    if label is not None:
        argv.extend(["--target", target])
    if text is not None:
        argv.extend(["--text", text])
    if act:
        argv.append("--act")
    code = _run(argv, body, deadline, text, approval)
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
    # Leave part of the same deadline available to confirm the final audit.
    work_deadline = deadline - min(.25, TIMEOUT / 10) if act else deadline
    if act:
        try:
            identity = _bounded(work_deadline, local.append_audit, record.copy(), "requested")
        except (TimeoutError, KeyboardInterrupt):
            body.update(outcome="audit indeterminate", error="Initial audit durability is unknown.")
            return 2, body
        except OSError:
            body.update(outcome="audit error", error="Could not record the act call.")
            return 2, body
    try:
        code = _execute(body, parts, act=act, armed=armed, text=text, deadline=work_deadline,
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
    if act:
        try:
            _bounded(deadline, local.append_audit, record.copy(), body["outcome"], identity)
        except (OSError, KeyboardInterrupt):
            body.update(step_outcome=body["outcome"], outcome="audit indeterminate",
                        error="Final audit durability is unknown. Inspect the result before retrying.")
            return 2, body
    return code, body
