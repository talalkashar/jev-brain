"""Command line for local computer use and closed questions."""

import argparse
import importlib.util
import json
import os
import shutil
import sys
import sysconfig
from pathlib import Path

from jev_brain import __version__, foreground, local
from jev_brain.client import JevBrainClient, JevBrainError
from jev_brain.computer_use import SETUP, UseRefused, perform
from jev_brain.engine import MockEngine
from jev_brain.install import AGENTS, InstallError, apply_install, plan_install
from jev_brain.questions import QUESTION_TYPES, validate_questions, validate_state
from jev_brain.server import DEFAULT_HOST, DEFAULT_PORT, DecisionServer

DEFAULT_URL = "http://127.0.0.1:18100"
_NO_WEIGHTS = "real weights are not bundled; this build requires --mock"


class _OrderedFlag(argparse.Action):
    """Record flags in argv order so criteria attach to the latest question."""

    def __call__(self, parser, namespace, values, option_string=None):
        sequence = getattr(namespace, "sequence", None)
        if sequence is None:
            sequence = []
            setattr(namespace, "sequence", sequence)
        sequence.append((self.dest, values))


def main(argv: list[str] | None = None) -> None:
    _configure_stdio()
    raise SystemExit(run(argv))


def run(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    if args.command == "decide":
        return _decide(args)
    if args.command == "health":
        return _health(args)
    if args.command == "install":
        return _install(args)
    if args.command == "doctor":
        return _doctor()
    if args.command == "consent":
        return _consent()
    if args.command == "use":
        return _use(args)
    parser.error(f"unknown command {args.command}")
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jev-brain",
        description=(
            "Hand one screen action from your agent to jev. Dry run by default."
        ),
    )
    parser.add_argument("--version", action="version", version=f"jev-brain {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="listen for POST /decide")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument(
        "--mock",
        action="store_true",
        help="serve fixed answers. This build has no model weights.",
    )
    serve.add_argument(
        "--allow-remote",
        action="store_true",
        help="bind 0.0.0.0. Off by default; the server otherwise stays on 127.0.0.1.",
    )

    decide = sub.add_parser("decide", help="ask a running server and print JSON")
    decide.add_argument("--url", default=None, help=f"default: JEV_BRAIN_URL or {DEFAULT_URL}")
    decide.add_argument("--state", default=None, help="context string. Omit for null. Do not put secrets here.")
    decide.add_argument(
        "--question",
        action=_OrderedFlag,
        required=True,
        help="NAME:TYPE:INSTRUCTIONS. TYPE is noul, choice, or score. Repeatable.",
    )
    decide.add_argument(
        "--criteria",
        action=_OrderedFlag,
        help="LABEL=TEXT for the most recent choice question. Repeatable.",
    )
    decide.add_argument(
        "--score-label",
        action=_OrderedFlag,
        dest="score_label",
        help="one rung, low to high, for the most recent score question. Repeatable.",
    )
    decide.set_defaults(sequence=None)

    health = sub.add_parser("health", help="print GET /health JSON")
    health.add_argument("--url", default=None, help=f"default: JEV_BRAIN_URL or {DEFAULT_URL}")

    install = sub.add_parser(
        "install",
        help="print the agent-config change. Write it only when --yes is passed.",
    )
    install.add_argument(
        "--agent",
        action="append",
        choices=AGENTS,
        help="which agent config to update. Repeatable. Default: claude-code, cursor, and codex.",
    )
    install.add_argument(
        "--yes",
        action="store_true",
        help="write the printed files under the current home. Without this, nothing is written.",
    )

    sub.add_parser("doctor", help="check the local computer-use setup")
    sub.add_parser("consent", help="enable act mode from your own interactive terminal")
    use = sub.add_parser("use", allow_abbrev=False, help="preview or perform one screen action")
    use.add_argument("--target", default="", help="exact control label, or 'focused field' for text")
    use.add_argument("--request", default="", help="one sub-goal for jev")
    use.add_argument("--expect", default="", help="frontmost supported native app name")
    use.add_argument("--text", help="literal text to enter")
    mode = use.add_mutually_exclusive_group()
    mode.add_argument("--arm", action="store_true", help="run one executor preview without input")
    mode.add_argument("--act", action="store_true", help="perform one step with local consent")
    return parser


def _serve(args: argparse.Namespace) -> int:
    if not args.mock:
        print(_NO_WEIGHTS, file=sys.stderr)
        return 2
    if args.port < 0 or args.port > 65535:
        print("port must be between 0 and 65535", file=sys.stderr)
        return 2
    host = "0.0.0.0" if args.allow_remote else DEFAULT_HOST
    try:
        server = DecisionServer(
            MockEngine(),
            host=host,
            port=args.port,
            allow_remote=args.allow_remote,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"could not bind {host}:{args.port}: {exc}", file=sys.stderr)
        return 2
    shown = DEFAULT_HOST if host == "0.0.0.0" else host
    print(
        f"listening on http://{shown}:{server.port} (bind {host}) model=mock",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.httpd.server_close()
    return 0


def _decide(args: argparse.Namespace) -> int:
    try:
        state = validate_state(args.state)
        questions = questions_from_flags(args.sequence or [])
        validate_questions(questions)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    client = JevBrainClient(_resolve_url(args.url))
    try:
        decision = client.system_one(state, questions)
    except JevBrainError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    json.dump(
        {"answers": decision.answers, "latency_s": decision.latency_s, "model": decision.model},
        sys.stdout,
        indent=2,
        ensure_ascii=False,
    )
    sys.stdout.write("\n")
    return 0


def _use(args: argparse.Namespace) -> int:
    try:
        code, body = perform(args.target, args.request, args.arm, act=args.act,
                             expect=args.expect, text=args.text)
    except (UseRefused, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    json.dump(body, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return code


def _consent() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Run jev-brain consent yourself in an interactive terminal.", file=sys.stderr)
        return 2
    try:
        if input("Allow agents with --act to control your Mac? Type ALLOW to continue: ") != "ALLOW":
            return 2
        local.grant()
    except (EOFError, KeyboardInterrupt, OSError) as exc:
        print(f"Consent was not granted. {exc}", file=sys.stderr)
        return 2
    print(f"Consent saved in {local.directory() / 'consent.json'}")
    return 0


def _doctor() -> int:
    print(f"Python {sys.version.split()[0]} (requires 3.11+)")
    runner = shutil.which("jev-run")
    print(f"jev-run: {runner or 'missing. ' + SETUP}")
    bundled = (runner and Path(runner).parent.resolve() == Path(sysconfig.get_path("scripts")).resolve()
               and importlib.util.find_spec("jev_run") is not None)
    backend = True
    if bundled:
        backend = importlib.util.find_spec("typesafe_computer_use") is not None
        print("typesafe-computer-use: " + ("installed" if backend else "missing. " + SETUP))
    elif runner:
        print("External jev-run: verify its backend in the runner's environment.")
    if sys.platform != "darwin":
        print("Desktop input requires macOS. Dry runs are available here.")
        return 1
    try:
        accessibility, recording = foreground.permissions()
    except (AttributeError, OSError):
        accessibility = recording = False
    print("Accessibility: " + ("granted" if accessibility else
          "missing or unavailable. Check System Settings > Privacy & Security for this host."))
    capture_note = "the bundled adapter uses Accessibility only" if bundled else "check whether your runner captures the screen"
    print(f"Screen Recording (informational): {'granted' if recording else 'unavailable'} ({capture_note}).")
    print("Act consent: " + ("present" if local.consented() else "absent. Run jev-brain consent to enable."))
    return 0 if runner and backend and accessibility else 1


def _install(args: argparse.Namespace) -> int:
    """Print the config change. Write only under Path.home(), and only with --yes."""
    home = Path.home()
    agents = tuple(args.agent) if args.agent else AGENTS
    try:
        planned = plan_install(home, agents)
        report = apply_install(home, planned, yes=bool(args.yes))
    except InstallError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    sys.stdout.write(report)
    return 0


def _health(args: argparse.Namespace) -> int:
    client = JevBrainClient(_resolve_url(args.url))
    try:
        payload = client.health()
    except JevBrainError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def questions_from_flags(sequence: list[tuple[str, str]]) -> dict:
    """Build a questions object. Criteria stick to the most recent matching question."""
    questions: dict = {}
    last: str | None = None
    for kind, value in sequence:
        if not isinstance(value, str):
            raise ValueError(f"{kind} must be a string")
        if kind == "question":
            name, qtype, instructions = _split_question(value)
            if name in questions:
                raise ValueError(f"duplicate question {name!r}")
            if qtype == "choice":
                criteria: dict | list | None = {}
            elif qtype == "score":
                criteria = []
            else:
                criteria = None
            questions[name] = {
                "type": qtype,
                "instructions": instructions,
                "criteria": criteria,
            }
            last = name
            continue
        if kind == "criteria":
            _require_recent(questions, last, "choice", "--criteria")
            if "=" not in value:
                raise ValueError("--criteria must be LABEL=TEXT")
            label, text = value.split("=", 1)
            if label == "":
                raise ValueError("--criteria label must not be empty")
            questions[last]["criteria"][label] = text
            continue
        if kind == "score_label":
            _require_recent(questions, last, "score", "--score-label")
            if value == "":
                raise ValueError("--score-label must not be empty")
            questions[last]["criteria"].append(value)
            continue
        raise ValueError(f"unknown decide flag {kind}")
    if not questions:
        raise ValueError("at least one --question is required")
    return questions


def _split_question(value: str) -> tuple[str, str, str]:
    parts = value.split(":", 2)
    if len(parts) != 3 or parts[0] == "":
        raise ValueError("--question must be NAME:TYPE:INSTRUCTIONS")
    name, qtype, instructions = parts
    if qtype not in QUESTION_TYPES:
        raise ValueError("--question TYPE must be noul, choice, or score")
    return name, qtype, instructions


def _require_recent(questions: dict, last: str | None, expected: str, flag: str) -> None:
    if last is None or questions[last]["type"] != expected:
        kind = "choice" if expected == "choice" else "score"
        raise ValueError(f"{flag} applies to the most recent {kind} question")


def _resolve_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    return os.environ.get("JEV_BRAIN_URL") or os.environ.get("SYSTEMONE_URL") or DEFAULT_URL


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                return


if __name__ == "__main__":
    main()
