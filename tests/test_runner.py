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
        self.node = SimpleNamespace(label="New", ref=self.control)
        self.mac.actionable_elements.return_value = ([self.node], [], False)
        self.mac.focused_field.return_value = SimpleNamespace(is_text=True, label="",
                                                               placeholder="", ref=self.field)
        self.front = Window("TextEdit", "Untitled", "", "")
        self.observe = self.enterContext(patch.object(jev_run, "check_window", return_value=self.front))
        self.args = SimpleNamespace(expect="TextEdit", target="New", text=None, act=True)
        self.result = dict(clicked=None, typed=None, steps_taken=0)

    def run_step(self):
        return jev_run.perform(self.args, self.mac, self.result)

    def test_exact_control_and_ambiguous_target(self):
        self.assertEqual(self.run_step(), "target clicked")
        self.mac.ax_press.assert_called_once_with(self.control)
        self.mac.actionable_elements.return_value = ([self.node, self.node], [], False)
        self.assertEqual(self.run_step(), "low confidence")
        self.mac.actionable_elements.return_value = ([], [], False)
        self.assertEqual(self.run_step(), "target not found")
        self.assertEqual(self.mac.ax_press.call_count, 1)

    def test_focus_changes_between_lookup_and_input(self):
        self.observe.side_effect = [self.front, Window("Finder", "Untitled", "", "")]
        with self.assertRaises(UseRefused) as exc:
            self.run_step()
        self.assertEqual(exc.exception.outcome, "wrong window")
        self.mac.ax_press.assert_not_called()

    def test_type_only_into_empty_field_and_read_back(self):
        self.args.target, self.args.text = None, "Hello"
        self.mac.ax_value.side_effect = ["already occupied", "", "Hello"]
        self.assertEqual(self.run_step(), "text not typed")
        self.mac.ax_set_value.assert_not_called()
        self.assertEqual(self.run_step(), "text typed")
        self.mac.ax_set_value.assert_called_once_with(self.field, "Hello")
        self.assertEqual(self.result["typed"], "Hello")

    def test_preview_and_sensitive_field_never_type(self):
        self.args.text = "Hello"
        self.args.act = False
        self.assertEqual(self.run_step(), "dry run")
        self.mac.ax_press.assert_not_called()
        self.args.target = None
        self.args.act = True
        self.mac.focused_field.return_value.placeholder = "Password"
        self.assertEqual(self.run_step(), "sensitive")
        self.mac.ax_set_value.assert_not_called()
