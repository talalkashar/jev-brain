"""One local step using typesafe-computer-use's macOS accessibility backend."""

import argparse
import json
import os

from jev_brain import foreground
from jev_brain.computer_use import UseRefused, check_window, fingerprint, sensitive_control, sensitive_surface
from jev_brain.computer_use import perform as supervised


def _attribute(mac, ref, key):
    error, value = mac.AS.AXUIElementCopyAttributeValue(ref, key, None)
    if error in (-25205, -25212):  # unsupported attribute or no value
        return None
    if error or value is None:
        raise UseRefused("Incomplete accessibility tree.", "low confidence")
    return value


def _button(mac, system, window, label, approved_metadata):
    def attribute(ref, key):
        return _attribute(mac, ref, key)

    ref = foreground.unique_target(window, label, lambda ref: attribute(ref, "AXChildren"), attribute)
    metadata = foreground.control_metadata(ref, label, attribute)
    if sensitive_control(*metadata):
        raise foreground.TargetUnavailable("sensitive")
    if fingerprint(metadata) != approved_metadata:
        raise foreground.TargetUnavailable("wrong window")
    frame = mac._ax_frame(ref)
    actions = mac._ax_actions(ref)
    if not frame or min(frame[2:]) <= 0 or not actions or "AXPress" not in actions:
        raise foreground.TargetUnavailable()
    x, y, w, h = frame
    error, hit = mac.AS.AXUIElementCopyElementAtPosition(system, x + w / 2, y + h / 2, None)
    if error or hit != ref:
        raise foreground.TargetUnavailable()
    actual_label = metadata[2] or metadata[3]
    return ref, actual_label, tuple(actions)


def _field(mac, focused, window):
    field = mac.focused_field()
    if not field or not field.is_text or field.ref is None:
        raise foreground.TargetUnavailable("text not typed")
    if sensitive_surface(field.label, field.placeholder):
        raise foreground.TargetUnavailable("sensitive")
    if (field.ref != focused or field.role != window.field_role
            or field.label != (window.field_label or window.description) or field.placeholder != window.placeholder):
        raise foreground.TargetUnavailable("wrong window")
    if mac.ax_value(field.ref) != "":
        raise foreground.TargetUnavailable("text not typed")
    return field.ref


def perform(args, mac, result, approval, *, read_revision=None):
    if args.target and args.text is not None:
        return "refused"
    # A revision is evidence only from a source which synchronously invalidates
    # it for *every* guarded change. Separate native AX reads provide no such
    # evidence. The installed entry point therefore leaves this unavailable.
    revision = read_revision() if args.act and read_revision is not None else None
    window = check_window(args.expect)
    if fingerprint(window) != approval[0]:
        raise UseRefused("The supervisor approved a different foreground.", "wrong window")
    system = mac.AS.AXUIElementCreateSystemWide()
    app_ref = mac._ax_attr(system, "AXFocusedApplication")
    if app_ref is None or mac._ax_attr(app_ref, "AXTitle") != window.app:
        raise UseRefused("The front app changed.", "wrong window")
    error, pid = mac.AS.AXUIElementGetPid(app_ref, None)
    if error or pid != window.pid:
        raise UseRefused("Could not identify the front app.", "wrong window")
    window_ref = mac._ax_attr(app_ref, "AXFocusedWindow")
    focused_ref = mac._ax_attr(app_ref, "AXFocusedUIElement")
    if window_ref is None or focused_ref is None:
        raise UseRefused("The front window is unavailable.", "wrong window")

    def check_front():
        if check_window(args.expect) != window:
            raise UseRefused("The foreground changed.", "wrong window")

    def check_owner(ref):
        if (mac._ax_attr(ref, "AXWindow") != window_ref
                or mac._ax_attr(app_ref, "AXFocusedWindow") != window_ref
                or mac._ax_attr(app_ref, "AXFocusedUIElement") != focused_ref
                or mac._ax_attr(system, "AXFocusedApplication") != app_ref):
            check_front()
            raise UseRefused("The control or window changed.", "wrong window")

    def final_gate():
        if args.text is not None:
            _field(mac, focused_ref, window)
        check_front()
        if args.target:
            current = _button(mac, system, window_ref, args.target, approval[1])
            if current != button:
                raise foreground.TargetUnavailable("wrong window")
            actual_label = current[1]
        else:
            actual_label = None
        check_owner(ref)
        if args.text is not None:
            if mac.ax_value(ref) != "":
                raise foreground.TargetUnavailable("text not typed")
            if mac._ax_attr(system, "AXFocusedApplication") != app_ref:
                check_front()
                raise foreground.TargetUnavailable("wrong window")
        mac.check_abort()
        latest = read_revision() if read_revision is not None else None
        if type(revision) is not int or type(latest) is not int or latest != revision:
            raise foreground.TargetUnavailable("unverifiable input")
        return actual_label

    try:
        if args.target:
            button = _button(mac, system, window_ref, args.target, approval[1])
            ref = button[0]
            mutate, operands = mac.AS.AXUIElementPerformAction, (ref, mac.AX_PRESS)
        elif args.text is not None:
            ref = _field(mac, focused_ref, window)
            # Insert at the selection in an empty field. Never replace AXValue.
            mutate = mac.AS.AXUIElementSetAttributeValue
            operands = (ref, mac.AS.kAXSelectedTextAttribute, args.text)
        else:
            return "refused" if args.act else "dry run"
        if args.act:
            mac.check_abort()
        check_front()
        check_owner(ref)
        if not args.act:
            if args.text is not None and mac.ax_value(ref) != "":
                return "text not typed"
            return "dry run"
        label = final_gate()
    except foreground.TargetUnavailable as exc:
        return exc.outcome

    result["steps_taken"] = None
    if mutate(*operands) != 0:
        return "input unconfirmed"
    result["steps_taken"] = 1
    if args.target:
        result["clicked"] = label
        return "target clicked"
    if mac.ax_value(ref) != args.text:
        return "text unconfirmed"
    result["typed"] = args.text
    return "text typed"


def _arguments():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("goal")
    parser.add_argument("--expect", default="")
    parser.add_argument("--target")
    parser.add_argument("--text")
    parser.add_argument("--act", action="store_true")
    parser.add_argument("--steps", type=int, default=1)
    parser.add_argument("--handoffs", type=int, default=0)
    return parser.parse_args()


def _backend():
    args = _arguments()
    result = dict(clicked=None, typed=None, steps_taken=0, confidence=None)
    try:
        approval = os.environ.get("JEV_BRAIN_APPROVAL", "").split(":")
        if len(approval) != 2 or any(len(part) != 64 for part in approval):
            raise UseRefused("Missing supervisor approval.")
        from typesafe_computer_use import macos

        result["outcome"] = perform(args, macos, result, approval)
    except UseRefused as exc:
        result["outcome"] = exc.outcome
    except Exception:
        result["outcome"] = "runner error"
    print(json.dumps(result))
    return 0 if result["outcome"] in ("dry run", "target clicked", "text typed") else 3


def main():
    args = _arguments()
    target = args.target if args.target is not None else "focused field"
    try:
        code, result = supervised(target, args.goal, armed=not args.act, act=args.act,
                                  expect=args.expect, text=args.text, _bundled=True,
                                  steps=args.steps, handoffs=args.handoffs)
    except UseRefused as exc:
        code, result = 2, {"outcome": exc.outcome, "steps": 0, "error": str(exc)}
    result["steps_taken"] = result["steps"]
    print(json.dumps(result))
    return code
