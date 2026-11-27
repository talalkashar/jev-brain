"""One local step using typesafe-computer-use's macOS accessibility backend."""

import argparse
import json
import sys

from jev_brain import local
from jev_brain.computer_use import SETUP, UseRefused, check_window, sensitive_surface


def perform(args, mac, result):
    window = check_window(args.expect)
    app_ref = mac._ax_attr(mac.AS.AXUIElementCreateSystemWide(), "AXFocusedApplication")
    if app_ref is None or mac._ax_attr(app_ref, "AXTitle") != window.app:
        raise UseRefused("The front app changed.", "wrong window")
    error, pid = mac.AS.AXUIElementGetPid(app_ref, None)
    if error:
        raise UseRefused("Could not identify the front app.", "wrong window")
    window_ref = mac._ax_attr(app_ref, "AXFocusedWindow")
    if window_ref is None:
        raise UseRefused("The front window is unavailable.", "wrong window")

    def recheck(ref):
        if (check_window(args.expect) != window or mac._ax_attr(app_ref, "AXFocusedWindow") != window_ref
                or mac._ax_attr(ref, "AXWindow") != window_ref):
            raise UseRefused("The control or window changed.", "wrong window")

    if args.target:
        display = mac.Quartz.CGMainDisplayID()
        bounds = mac.Quartz.CGDisplayBounds(display)
        nodes, _, capped = mac.actionable_elements(pid, bounds.size.width, bounds.size.height)
        matches = [n for n in nodes if n.label == args.target and n.ref is not None
                   and mac._ax_attr(n.ref, "AXWindow") == window_ref]
        if capped or len(matches) > 1:
            return "low confidence"
        if not matches:
            return "target not found"
        node = matches[0]
        recheck(node.ref)
        if args.act:
            if not mac.ax_press(node.ref):
                return "target not clicked"
            result.update(clicked=args.target, steps_taken=1)
    if args.text is not None:
        field = mac.focused_field()
        if not field or not field.is_text or field.ref is None:
            return "text not typed"
        if (sensitive_surface(field.label, field.placeholder) or
                "secure" in str(mac._ax_attr(field.ref, "AXSubrole")).casefold()):
            return "sensitive"
        recheck(field.ref)
        if mac._ax_attr(app_ref, "AXFocusedUIElement") != field.ref:
            return "wrong window"
        if args.act:
            # Never replace an existing document or send keystrokes to a different field.
            if mac.ax_value(field.ref) != "" or not mac.ax_set_value(field.ref, args.text):
                return "text not typed"
            result["steps_taken"] = 1
            if mac.ax_value(field.ref) != args.text:
                return "text unconfirmed"
            result["typed"] = args.text
    if not args.act:
        return "dry run"
    return "text typed" if args.text is not None else "target clicked"


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("goal")
    parser.add_argument("--expect", required=True)
    parser.add_argument("--target")
    parser.add_argument("--text")
    parser.add_argument("--act", action="store_true")
    parser.add_argument("--steps", type=int, choices=[1], default=1)
    parser.add_argument("--handoffs", type=int, choices=[0], default=0)
    args = parser.parse_args()
    result = dict(clicked=None, typed=None, steps_taken=0, confidence=None)
    try:
        if sensitive_surface(args.goal, args.expect, args.target or "", args.text or ""):
            raise UseRefused("Sensitive surface.", "sensitive")
        if args.act and not local.consented():
            raise UseRefused("Run jev-brain consent yourself in a local terminal first.")
        if args.act and not args.target and args.text is None:
            raise UseRefused("Select a control or supply literal text.")
        if sys.platform != "darwin":
            raise UseRefused("This runner requires macOS.")
        from typesafe_computer_use import macos

        result["outcome"] = perform(args, macos, result)
    except UseRefused as exc:
        result.update(outcome=exc.outcome, error=str(exc))
    except ImportError:
        result.update(outcome="runner error", error=f"Install the macOS runner and its upstream dependency. {SETUP}")
    except Exception:
        result.update(outcome="runner error", error="Could not access the window. Run jev-brain doctor.")
    print(json.dumps(result))
    return 0 if result["outcome"] in ("dry run", "target clicked", "text typed") else 2 if result["outcome"] == "sensitive" else 3
