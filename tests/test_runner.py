"""Probe the optional adapter using fake accessibility objects only."""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_cli import ROOT
from jev_brain.computer_use import UseRefused
from jev_brain.foreground import Window

sys.path.insert(0, str(ROOT / "runner"))
import jev_run
sys.path.pop(0)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.mac = Mock()
        self.app = object()
        self.window = object()
        self.control = object()
        self.field = object()
        values = {"AXFocusedApplication": self.app, "AXTitle": "TextEdit", "AXSubrole": "",
                  "AXFocusedUIElement": self.field, "AXFocusedWindow": self.window, "AXWindow": self.window}
        self.mac._ax_attr.side_effect = lambda ref, key: values[key]
        self.mac.AS.AXUIElementGetPid.return_value = 0, 100
        self.buttons = [self.control]
        def attribute(ref, key, _):
            if key == "AXChildren":
                return 0, self.buttons if ref == self.window else []
            if key == "AXRole":
                return 0, "AXButton" if ref in self.buttons else "AXWindow"
            if key == "AXTitle":
                return 0, "New" if ref in self.buttons else "Window"
            return -25205, None
        self.mac.AS.AXUIElementCopyAttributeValue.side_effect = attribute
        self.mac._ax_frame.return_value = (10, 10, 40, 20)
        self.mac._ax_actions.return_value = ["AXPress"]
        self.mac.AS.AXUIElementCopyElementAtPosition.return_value = (0, self.control)
        self.mac.focused_field.return_value = SimpleNamespace(is_text=True, label="",
                                                               placeholder="", ref=self.field)
        self.front = Window("TextEdit", "Untitled", "", "AXTextArea", "com.apple.TextEdit", 100)
        self.observe = self.enterContext(patch.object(jev_run, "check_window", return_value=self.front))
        self.args = SimpleNamespace(expect="TextEdit", target="New", text=None, act=True)
        self.result = dict(clicked=None, typed=None, steps_taken=0)

    def run_step(self):
        return jev_run.perform(self.args, self.mac, self.result)

    def test_exact_control_and_ambiguous_target(self):
        self.assertEqual(self.run_step(), "target clicked")
        self.mac.ax_press.assert_called_once_with(self.control)
        self.buttons.append(object())
        self.assertEqual(self.run_step(), "low confidence")
        self.buttons.clear()
        self.assertEqual(self.run_step(), "target not found")
        self.assertEqual(self.mac.ax_press.call_count, 1)

    def test_focus_changes_between_lookup_and_input(self):
        self.observe.side_effect = [self.front, Window("Finder", "Untitled", "", "")]
        with self.assertRaises(UseRefused) as exc:
            self.run_step()
        self.assertEqual(exc.exception.outcome, "wrong window")
        self.mac.ax_press.assert_not_called()

    def test_press_revalidates_focus_ownership_and_hit_after_final_metadata_read(self):
        read = self.mac.AS.AXUIElementCopyAttributeValue
        attribute = read.side_effect
        lookup = self.mac._ax_attr.side_effect
        for change in ("foreground", "ownership", "obscured"):
            with self.subTest(change=change):
                def final_read(ref, key, out):
                    if ref == self.control and key == "AXPlaceholderValue":
                        if change == "foreground":
                            self.observe.return_value = Window("Finder", "Untitled", "", "")
                        elif change == "ownership":
                            self.mac._ax_attr.side_effect = lambda ref, k: None if k == "AXWindow" else lookup(ref, k)
                        else:
                            self.mac.AS.AXUIElementCopyElementAtPosition.return_value = (0, self.field)
                    return attribute(ref, key, out)

                self.observe.return_value = self.front
                self.mac._ax_attr.side_effect = lookup
                self.mac.AS.AXUIElementCopyElementAtPosition.return_value = (0, self.control)
                read.side_effect = final_read
                try:
                    outcome = self.run_step()
                except UseRefused as exc:
                    outcome = exc.outcome
                self.assertEqual(outcome, "low confidence" if change == "obscured" else "wrong window")
                self.mac.ax_press.assert_not_called()
                self.assertEqual(self.result["steps_taken"], 0)

    def test_type_only_into_empty_field_and_read_back(self):
        self.args.target, self.args.text = None, "Hello"
        self.mac.ax_value.side_effect = ["already occupied", "", "", "Hello"]
        self.assertEqual(self.run_step(), "text not typed")
        self.mac.ax_set_value.assert_not_called()
        self.assertEqual(self.run_step(), "text typed")
        self.mac.ax_set_value.assert_called_once_with(self.field, "Hello")
        self.assertEqual(self.result["typed"], "Hello")

    def test_intervening_edit_or_unreadable_field_stops_setter_after_focus_recheck(self):
        self.args.target, self.args.text = None, "Hello"
        for act in (False, True):
            for changed in ("human draft", None):
                with self.subTest(act=act, changed=changed):
                    value = ""
                    def check(expect):
                        nonlocal value
                        if self.mac.ax_value.call_count:
                            value = changed
                        return self.front
                    self.observe.side_effect = check
                    self.mac.ax_value.reset_mock()
                    self.mac.ax_value.side_effect = lambda ref: value
                    self.args.act = act
                    self.assertEqual(self.run_step(), "text not typed")
                    self.assertEqual(self.result["steps_taken"], 0)
                    self.mac.ax_set_value.assert_not_called()

    def test_preview_and_sensitive_field_never_type(self):
        self.args.text = "Hello"
        self.args.target = None
        self.mac.ax_value.return_value = ""
        self.args.act = False
        self.assertEqual(self.run_step(), "dry run")
        self.mac.ax_press.assert_not_called()
        self.args.target = None
        self.args.act = True
        self.mac.focused_field.return_value.placeholder = "Password"
        self.assertEqual(self.run_step(), "sensitive")
        self.mac.ax_set_value.assert_not_called()

    def test_combined_press_and_text_refused_before_either_mutation(self):
        self.args.text = "Hello"
        self.mac.ax_value.side_effect = ["", "Hello"]
        self.assertEqual(self.run_step(), "refused")
        self.mac.ax_press.assert_not_called()
        self.mac.ax_set_value.assert_not_called()
        self.assertEqual(self.result["steps_taken"], 0)

    def test_overlapping_refs_hidden_by_upstream_deduplication(self):
        refs = [self.control, object()]
        self.mac.AS.AXUIElementCopyAttributeValue.side_effect = lambda ref, name, _: (
            (0, refs if ref == self.window else []) if name == "AXChildren" else
            (0, "New" if ref in refs else "Window") if name == "AXTitle" else (-25205, None))
        # The pinned walker returns just the first button for identical frames.
        self.mac.actionable_elements.return_value = ([SimpleNamespace(label="New", ref=self.control)], [], False)
        self.assertEqual(self.run_step(), "low confidence")
        self.mac.ax_press.assert_not_called()


    def test_capped_nonpressable_and_obscured_candidates_refused(self):
        self.buttons.extend(object() for _ in range(512))
        self.assertEqual(self.run_step(), "low confidence")
        self.buttons[:] = [self.control]
        self.mac._ax_actions.return_value = []
        self.assertEqual(self.run_step(), "low confidence")
        self.mac._ax_actions.return_value = ["AXPress"]
        self.mac.AS.AXUIElementCopyElementAtPosition.return_value = (0, self.field)
        self.assertEqual(self.run_step(), "low confidence")
        self.mac.ax_press.assert_not_called()

    def test_unreadable_children_cannot_prove_button_uniqueness(self):
        attribute = self.mac.AS.AXUIElementCopyAttributeValue.side_effect
        self.mac.AS.AXUIElementCopyAttributeValue.side_effect = lambda ref, key, out: (
            (0, "") if ref == self.control and key == "AXChildren" else attribute(ref, key, out))
        self.assertEqual(self.run_step(), "low confidence")
        self.mac.ax_press.assert_not_called()

    def test_pinned_walker_collapses_two_distinct_save_buttons(self):
        try:
            from typesafe_computer_use.ax_walk import walk_actionable
        except ModuleNotFoundError as exc:
            if exc.name != "typesafe_computer_use":
                raise
            self.skipTest("Pinned backend installed in the adapter job")
        self.buttons.append(object())
        def attrs(ref):
            if ref == self.window:
                return "AXWindow", "", (0, 0, 200, 200)
            return "AXButton", "Save", (10, 10, 40, 20)
        visible, offscreen, capped = walk_actionable(
            self.window, lambda ref: self.buttons if ref == self.window else [], attrs,
            lambda ref: ["AXPress"], 200, 200)
        self.assertEqual((len(visible), offscreen, capped), (1, [], False))
        self.args.target = "Save"
        self.mac.AS.AXUIElementCopyAttributeValue.side_effect = lambda ref, key, _: (
            (0, self.buttons if ref == self.window else []) if key == "AXChildren" else
            (0, attrs(ref)[1]) if key == "AXTitle" else (-25205, None))
        self.assertEqual(self.run_step(), "low confidence")
        self.mac.ax_press.assert_not_called()
