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


def _press_point(mac, match):
    frame = mac._ax_frame(match)
    actions = mac._ax_actions(match)
    if not frame or min(frame[2:]) <= 0 or "AXPress" not in actions:
        return None
    x, y, w, h = frame
    return x + w / 2, y + h / 2


def perform(args, mac, result, approval):
    if args.target and args.text is not None:
        return "refused"
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

    def check_owner(ref, *, focused=False):
        if (mac._ax_attr(ref, "AXWindow") != window_ref
                or mac._ax_attr(app_ref, "AXFocusedWindow") != window_ref
                or (focused and focused_ref != ref)
                or mac._ax_attr(app_ref, "AXFocusedUIElement") != focused_ref
                or mac._ax_attr(system, "AXFocusedApplication") != app_ref):
            check_front()
            raise UseRefused("The control or window changed.", "wrong window")

    if args.target:
        def attribute(ref, key):
            return _attribute(mac, ref, key)

        try:
            ref = foreground.unique_target(window_ref, args.target, lambda ref: attribute(ref, "AXChildren"), attribute)
            point = _press_point(mac, ref)
            if args.act:
                mac.check_abort()
            metadata = foreground.control_metadata(ref, args.target, attribute)
        except foreground.TargetUnavailable as exc:
            return exc.outcome
        if sensitive_control(*metadata):
            return "sensitive"
        if fingerprint(metadata) != approval[1]:
            return "wrong window"
        if point is None:
            return "low confidence"
        def visible():
            error, hit = mac.AS.AXUIElementCopyElementAtPosition(system, *point, None)
            return not error and hit == ref

        check_owner(ref)
        check_front()
        if not visible():
            return "low confidence"
        if args.act:
            check_front()
            if not visible():
                return "low confidence"
            check_owner(ref)
            result["steps_taken"] = None
            if mac.AS.AXUIElementPerformAction(ref, mac.AX_PRESS) != 0:
                return "input unconfirmed"
            result.update(clicked=args.target, steps_taken=1)
            return "target clicked"

    if args.text is not None:
        field = mac.focused_field()
        if not field or not field.is_text or field.ref is None:
            return "text not typed"
        if sensitive_surface(field.label, field.placeholder):
            return "sensitive"
        if mac.ax_value(field.ref) != "":
            return "text not typed"
        check_owner(field.ref, focused=True)
        check_front()
        if args.act:
            mac.check_abort()
        if mac._ax_attr(app_ref, "AXFocusedUIElement") != field.ref:
            return "wrong window"
        if mac.ax_value(field.ref) != "":
            return "text not typed"
        if args.act:
            check_front()
            if mac.ax_value(field.ref) != "":
                return "text not typed"
            check_owner(field.ref, focused=True)
            result["steps_taken"] = None
            if mac.AS.AXUIElementSetAttributeValue(field.ref, mac.AS.kAXValueAttribute, args.text) != 0:
                return "input unconfirmed"
            result["steps_taken"] = 1
            if mac.ax_value(field.ref) != args.text:
                return "text unconfirmed"
            result["typed"] = args.text
            return "text typed"
    return "refused" if args.act else "dry run"


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
