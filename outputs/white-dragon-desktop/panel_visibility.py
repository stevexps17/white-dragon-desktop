"""Visibility of the activity strip, independent from task state and video playback."""
from PySide6.QtCore import QObject,QTimer,QPropertyAnimation,QEasingCurve,QPoint,Qt
from PySide6.QtGui import QCursor,QRegion
from PySide6.QtWidgets import QApplication,QGraphicsOpacityEffect

# Verified in Codex 26.1027.3384.0 and 26.1103.4239.0.
DISMISS_MS=300
FADE_MS=200
PROXIMITY_ENTER=40
PROXIMITY_EXIT=56

def near_rect(point,rect,distance):
    dx=max(rect.left()-point.x(),0,point.x()-rect.right())
    dy=max(rect.top()-point.y(),0,point.y()-rect.bottom())
    return dx*dx+dy*dy<=distance*distance

class PanelVisibility(QObject):
    def __init__(self,panel,pet):
        super().__init__(panel);self.panel=panel;self.pet=pet
        self.has_activity=False;self.near=False;self.held=False;self.target_visible=False
        self.opacity=QGraphicsOpacityEffect(panel);self.opacity.setOpacity(0);panel.setGraphicsEffect(self.opacity)
        self.animation=QPropertyAnimation(self.opacity,b'opacity',self)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutQuad);self.animation.finished.connect(self.finished)
        self.dismiss=QTimer(self);self.dismiss.setSingleShot(True);self.dismiss.timeout.connect(self.fade_out)
        self.poll=QTimer(self);self.poll.setInterval(32);self.poll.timeout.connect(self.refresh);self.poll.start()
        self.panel.hide();self._interaction=self.read_interaction

    def read_interaction(self):
        cursor=QCursor.pos();origin=self.pet.mapToGlobal(QPoint(0,0))
        rect=self.pet.rect().translated(origin);rect.setTop(origin.y()+self.pet.canvas_top)
        if not (self.panel.isVisible() and self.opacity.opacity()>.01):rect.setHeight(self.pet.canvas_height)
        radius=PROXIMITY_EXIT if self.near else PROXIMITY_ENTER
        self.near=near_rect(cursor,rect,radius)
        popup=QApplication.activePopupWidget()
        focus=QApplication.focusWidget();active=QApplication.activeWindow()
        owner=focus
        while owner is not None and owner is not self.pet:owner=owner.parentWidget()
        # Merely being the last active tool window must not keep the strip alive.
        keyboard=owner is self.pet and active is not None and active.isActiveWindow()
        dragging=self.pet.drag_origin is not None
        return self.near or keyboard or dragging or (popup is not None and popup.isVisible())

    def set_activity(self,has_activity,changed=False):
        self.has_activity=has_activity
        if not has_activity:self.hide_now();return
        self.refresh()
        if changed and self.pet.isVisible():
            self.reveal()
            if not self.held:self.schedule_hide()

    def refresh(self):
        if not self.pet.isVisible():self.held=False;self.hide_now();return
        was_held=self.held;self.held=bool(self._interaction())
        if not self.has_activity:self.hide_now();return
        if self.held:
            self.dismiss.stop()
            if not self.target_visible:self.reveal()
        elif was_held:self.schedule_hide()

    def schedule_hide(self):
        if self.target_visible and not self.dismiss.isActive():self.dismiss.start(DISMISS_MS)

    def reveal(self):
        self.dismiss.stop();self.target_visible=True;self.panel.show()
        self.panel.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,False)
        self.update_mask();self.animate_to(1.)

    def fade_out(self):
        if self._interaction():self.held=True;self.reveal();return
        self.target_visible=False
        self.panel.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,True)
        self.animate_to(0.)

    def animate_to(self,value):
        self.animation.stop()
        if self.pet.os_reduced:
            self.opacity.setOpacity(value);self.finished();return
        self.animation.setDuration(FADE_MS)
        self.animation.setStartValue(self.opacity.opacity());self.animation.setEndValue(value);self.animation.start()

    def finished(self):
        if not self.target_visible:self.panel.hide();self.update_mask()

    def hide_now(self):
        self.dismiss.stop();self.animation.stop();self.target_visible=False
        if not self.panel.isHidden() or self.opacity.opacity()!=0:
            self.opacity.setOpacity(0);self.panel.hide()
        self.update_mask()

    def update_mask(self):
        # Include visible controls and the character in the click region.
        region=QRegion(0,self.pet.canvas_top,self.pet.width(),self.pet.canvas_height)
        if not self.panel.isHidden():region|=QRegion(self.panel.geometry())
        if not self.panel.status.isHidden():region|=QRegion(self.panel.status.geometry())
        if self.pet.mask()!=region:self.pet.setMask(region)

    def stop(self):self.poll.stop();self.dismiss.stop();self.animation.stop()
