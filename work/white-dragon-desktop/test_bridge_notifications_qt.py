"""Real Qt panel with synthetic controller; never constructs a CodexReader."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'outputs/white-dragon-desktop'))
from PySide6.QtCore import QObject,Signal
from PySide6.QtWidgets import QApplication,QWidget
from bridge_ui import ActivityPanel
from codex_bridge import Activity,Snapshot

class Tray(QObject):
    messageClicked=Signal()
    MessageIcon=SimpleNamespace(Information=1)
    def __init__(self):
        super().__init__();self.showMessage=Mock()

class Controller(QObject):
    updated=Signal(object)
    def __init__(self):
        super().__init__();self.reader=SimpleNamespace(acknowledge=Mock());self.poll=Mock();self.stop=Mock()

class Pet(QWidget):
    def __init__(self):
        super().__init__();self.tray=Tray();self.notifications=True;self.codex_activity_key=None
        self.dragged=True;self.portrait_mode=False;self.paused=False;self.os_reduced=True
        self.canvas_top=20;self.canvas_height=100;self.drag_origin=None
    def position_panel(self):pass

class QtNotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.pet=Pet();self.controller=Controller()
        self.panel=ActivityPanel(self.pet,controller=self.controller,opener=Mock(return_value=True))
    def tearDown(self):
        self.panel.stop();self.pet.close();self.pet.deleteLater();self.app.processEvents()
    def send(self,state='ready',connected=True):
        self.controller.updated.emit(Snapshot([Activity('synthetic','合成任务',state,'turn',time.time())] if connected else [],connected))
    def test_reconnect_retains_notifications_but_clears_disconnected_controls(self):
        self.send('running');self.send('needs_input')
        self.assertEqual(1,self.pet.tray.showMessage.call_count)
        self.send(connected=False)
        self.assertTrue(self.panel.status.isHidden());self.assertFalse(self.panel.live_tasks)
        self.send('needs_input')
        self.assertEqual(1,self.pet.tray.showMessage.call_count)
    def test_offline_first_then_first_success_is_silent(self):
        self.send(connected=False);self.send('ready')
        self.pet.tray.showMessage.assert_not_called()

if __name__=='__main__':unittest.main()
