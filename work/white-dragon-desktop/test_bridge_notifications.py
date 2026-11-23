"""Fixture-only regressions: execute actual snapshot handler without Qt/session I/O."""
import ast
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[2] / 'outputs/white-dragon-desktop/bridge_ui.py'
# Importing the UI is unnecessary for this synchronous state-machine test.
# Compile its unchanged methods, not a reimplementation, without readers/timers.
tree = ast.parse(SOURCE.read_text())
handler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ActivityPanel')
handler.body = [n for n in handler.body if isinstance(n, ast.FunctionDef) and n.name == 'accept_snapshot']
handler.bases = []
selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'visible_activities'] + [handler]
namespace = dict(time=time, ACTIVITY_TTL={'blocked':3600,'needs_input':86400,'ready':604800},
                 PRIORITY={'needs_input':0,'blocked':1,'ready':2,'running':3},
                 LABELS={'running':'运行','ready':'完成','needs_input':'确认','blocked':'失败'})
exec(compile(ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])), str(SOURCE), 'exec'), namespace)

class NotificationReconnectTests(unittest.TestCase):
    def setUp(self):
        self.panel = namespace['ActivityPanel']()
        p = self.panel
        p.previous = None
        p.pet = SimpleNamespace(notifications=True, tray=Mock(), codex_activity_key=None,
                                dragged=True, portrait_mode=False, paused=False)
        p.result_animations = set()
        p.activity_dialog = None
        p.update_status = Mock()
        p.presence = Mock()

    def snapshot(self, state='ready', turn='turn-1', connected=True):
        item = SimpleNamespace(id='synthetic-task', title='合成任务', status=state,
                               turn_id=turn, updated=time.time())
        self.panel.accept_snapshot(SimpleNamespace(connected=connected, activities=[item] if connected else []))

    def test_reconnection_does_not_repeat_existing_notice(self):
        for state in ['ready', 'needs_input', 'blocked']:
            with self.subTest(state=state):
                self.setUp()
                self.snapshot('running')
                self.snapshot(state)
                self.assertEqual(1, self.panel.pet.tray.showMessage.call_count)
                self.snapshot(connected=False)
                self.panel.update_status.assert_called_with([])
                self.assertFalse(self.panel.presence.set_activity.call_args.args[0])
                self.snapshot(state)
                self.assertEqual(1, self.panel.pet.tray.showMessage.call_count)

    def test_new_result_during_disconnection_is_announced_once(self):
        self.snapshot('running')
        self.snapshot(connected=False)
        self.snapshot('ready')
        self.snapshot('ready')
        self.assertEqual(1, self.panel.pet.tray.showMessage.call_count)

    def test_first_success_after_offline_start_is_baseline_not_spam(self):
        self.snapshot(connected=False)
        self.snapshot('ready')
        self.panel.pet.tray.showMessage.assert_not_called()

    def test_new_turn_still_generates_notice(self):
        self.snapshot('ready')
        self.snapshot(connected=False)
        self.snapshot('ready', turn='turn-2')
        self.assertEqual(1, self.panel.pet.tray.showMessage.call_count)

    def test_connected_empty_scan_still_clears_baseline(self):
        self.snapshot('ready')
        self.panel.accept_snapshot(SimpleNamespace(connected=True, activities=[]))
        self.snapshot('ready')
        self.assertEqual(1, self.panel.pet.tray.showMessage.call_count)

if __name__ == '__main__':
    unittest.main()
