"""Qt regression tests that never read local Codex data."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / "outputs" / "white-dragon-desktop"
sys.path.insert(0, str(SOURCE))

from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QApplication
from desktop import ASSETS, Dragon, nearest_work_area, portrait_motion


class DesktopBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.manifest = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch("desktop.reduced_motion", return_value=False):
            self.pet = Dragon(self.manifest, Path(self.tmp.name) / "settings.json")
        self.addCleanup(self.dispose)
        self.pet.show()
        self.app.processEvents()

    def dispose(self):
        self.pet.hide()
        self.pet.cleanup()
        self.pet.deleteLater()

    def test_native_idle_rate_and_interaction_refresh(self):
        self.pet.set_fps(60)
        self.assertEqual(self.pet.timer.interval(), 42)
        self.pet.play("jumping")
        self.assertEqual(self.pet.timer.interval(), 17)
        self.pet.play("idle")
        self.pet.set_fps(15)
        self.assertEqual(self.pet.timer.interval(), 67)

    def test_hidden_paused_static_and_reduced_motion_stop_paint_timer(self):
        self.assertTrue(self.pet.timer.isActive())
        self.pet.hide()
        self.assertFalse(self.pet.timer.isActive())
        self.pet.show()
        self.assertTrue(self.pet.timer.isActive())
        self.pet.toggle_pause()
        self.assertFalse(self.pet.timer.isActive())
        self.pet.interaction_play("jumping")
        self.assertEqual(self.pet.state, "idle")
        self.pet.toggle_pause()
        self.pet.toggle_portrait()
        self.assertFalse(self.pet.timer.isActive())
        self.pet.interaction_play("waving")
        self.assertTrue(self.pet.portrait_mode)
        self.pet.toggle_portrait()
        with patch("desktop.reduced_motion", return_value=True):
            self.pet.refresh_motion()
        self.assertFalse(self.pet.timer.isActive())
        with patch("desktop.reduced_motion", return_value=False):
            self.pet.refresh_motion()
        self.assertTrue(self.pet.timer.isActive())

    def test_activity_feedback_reuses_own_art_and_one_shots_return_to_idle(self):
        for status, state in (("running", "running"), ("needs_input", "waiting"),
                              ("ready", "success"), ("blocked", "failed")):
            with self.subTest(status=status):
                self.pet.codex_status = status
                self.assertEqual(self.pet.status_animation(), state)
                self.pet.play(state)
                self.assertEqual(self.pet.render_source(), "idle")
                self.assertNotEqual(portrait_motion(state, 0.7), (0., 0., 0.))
                if state in {"success", "failed"}:
                    self.pet.started -= 3
                    self.pet.tick()
                    self.assertEqual(self.pet.state, "idle")

    def test_nearest_work_area_handles_negative_and_gapped_coordinates(self):
        left = QRect(-1920, 0, 1920, 1040)
        right = QRect(240, 150, 1600, 900)
        self.assertEqual(nearest_work_area([left, right], QPoint(199, 500)), right)
        self.assertEqual(nearest_work_area([left, right], QPoint(20, 500)), left)
        self.assertEqual(nearest_work_area([left, right], QPoint(-1440, 500)), left)


if __name__ == "__main__":
    unittest.main()
