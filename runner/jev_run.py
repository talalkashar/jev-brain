"""One local step using typesafe-computer-use's macOS accessibility backend."""

import argparse
import json

from jev_brain import foreground
from jev_brain.computer_use import UseRefused, check_window, sensitive_control, sensitive_surface
from jev_brain.computer_use import perform as supervised


def _attribute(mac, ref, key):
    error, value = mac.AS.AXUIElementCopyAttributeValue(ref, key, None)
    if error in (-25205, -25212):  # unsupported attribute or no value
        return None
    if error or value is None:
        raise UseRefused("Incomplete accessibility tree.", "low confidence")
    return value


def _unique_button(mac, window, label):
    try:
        match = foreground.unique_target(window, label,
                                         lambda ref: _attribute(mac, ref, "AXChildren"),
                                         lambda ref, key: _attribute(mac, ref, key))
    except foreground.TargetUnavailable as exc:
        return None, exc.outcome
    frame = mac._ax_frame(match)
    actions = mac._ax_actions(match)
    if not frame or min(frame[2:]) <= 0 or "AXPress" not in actions:
        return None, "low confidence"
    # Hit-test the centre to exclude clipped, covered and off-display controls.
    x, y, w, h = frame
    error, visible = mac.AS.AXUIElementCopyElementAtPosition(
        mac.AS.AXUIElementCreateSystemWide(), x + w / 2, y + h / 2, None)
    if error or visible != match:
        return None, "low confidence"
    return match, None


def perform(args, mac, result):
    if args.target and args.text is not None:
        return "refused"
    window = check_window(args.expect)
    app_ref = mac._ax_attr(mac.AS.AXUIElementCreateSystemWide(), "AXFocusedApplication")
    if app_ref is None or mac._ax_attr(app_ref, "AXTitle") != window.app:
        raise UseRefused("The front app changed.", "wrong window")
    error, pid = mac.AS.AXUIElementGetPid(app_ref, None)
    if error or pid != window.pid:
        raise UseRefused("Could not identify the front app.", "wrong window")
    window_ref = mac._ax_attr(app_ref, "AXFocusedWindow")
    if window_ref is None:
        raise UseRefused("The front window is unavailable.", "wrong window")

    def recheck(ref):
        if (check_window(args.expect) != window or mac._ax_attr(app_ref, "AXFocusedWindow") != window_ref
                or mac._ax_attr(ref, "AXWindow") != window_ref):
            raise UseRefused("The control or window changed.", "wrong window")

    if args.target:
        ref, refusal = _unique_button(mac, window_ref, args.target)
        if refusal:
            return refusal
        recheck(ref)
        try:
            metadata = foreground.control_metadata(ref, args.target, lambda ref, key: _attribute(mac, ref, key))
        except foreground.TargetUnavailable as exc:
            return exc.outcome
        if sensitive_control(*metadata):
            return "sensitive"
        if args.act:
            result["steps_taken"] = None
            if not mac.ax_press(ref):
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
        recheck(field.ref)
        if mac._ax_attr(app_ref, "AXFocusedUIElement") != field.ref:
            return "wrong window"
        if mac.ax_value(field.ref) != "":
            return "text not typed"
        if args.act:
            result["steps_taken"] = None
            if not mac.ax_set_value(field.ref, args.text):
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
        from typesafe_computer_use import macos

        result["outcome"] = perform(args, macos, result)
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
