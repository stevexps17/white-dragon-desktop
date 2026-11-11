"""Local activity controls; Codex receives only explicit user-initiated deep links."""
from concurrent.futures import ThreadPoolExecutor
import ctypes,sys
from ctypes import wintypes
from PySide6.QtCore import QObject,Signal,QTimer,Qt,QUrl,QAbstractNativeEventFilter
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication,QFrame,QHBoxLayout,QPushButton,QDialog,QVBoxLayout,QListWidget,QListWidgetItem,QLabel,QPlainTextEdit
from codex_bridge import CodexReader,Snapshot,LABELS,chat_url,PRIORITY
from panel_visibility import PanelVisibility
import time

COLORS={'running':'#7355ab','needs_input':'#986300','ready':'#247a4c','blocked':'#b04450','idle':'#60566e','offline':'#747078'}
ACTIVITY_TTL={'blocked':3600,'needs_input':86400,'ready':604800}

def visible_activities(snapshot,now=None):
    now=time.time() if now is None else now
    return sorted((a for a in snapshot.activities if a.status in {'running','needs_input','ready','blocked'} and (a.status not in ACTIVITY_TTL or now<a.updated+ACTIVITY_TTL[a.status])),key=lambda a:(PRIORITY[a.status],-a.updated)) if snapshot.connected else []

class BridgeController(QObject):
    updated=Signal(object)
    def __init__(self,parent=None,reader=None):
        super().__init__(parent);self.reader=reader or CodexReader();self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='codex-events');self.future=None
        self.timer=QTimer(self);self.timer.timeout.connect(self.poll);self.timer.start(1200)
        self.result_timer=QTimer(self);self.result_timer.timeout.connect(self.collect);self.result_timer.start(100)
        self.poll()
    def poll(self):
        if self.future is None:self.future=self.pool.submit(self.reader.scan)
    def collect(self):
        if self.future is not None and self.future.done():
            try:snapshot=self.future.result()
            except Exception:snapshot=Snapshot(error='本地事件读取失败')
            self.future=None;self.updated.emit(snapshot)
    def stop(self):self.timer.stop();self.result_timer.stop();self.pool.shutdown(wait=False,cancel_futures=True)

class Hotkey(QAbstractNativeEventFilter):
    ID=0x573D
    def __init__(self,app,callback):
        super().__init__();self.callback=callback;self.registered=False;self.app=app
        if sys.platform=='win32':
            self.registered=bool(ctypes.windll.user32.RegisterHotKey(None,self.ID,0x4007,0x50))
            if self.registered:app.installNativeEventFilter(self)
    def nativeEventFilter(self,event_type,message):
        if self.registered:
            msg=ctypes.cast(int(message),ctypes.POINTER(wintypes.MSG)).contents
            if msg.message==0x0312 and msg.wParam==self.ID:self.callback();return True,0
        return False,0
    def stop(self):
        if self.registered:
            ctypes.windll.user32.UnregisterHotKey(None,self.ID);self.app.removeNativeEventFilter(self);self.registered=False

class ActivityPanel(QFrame):
    def __init__(self,pet,controller=None,opener=None):
        super().__init__(pet);self.pet=pet;self.controller=controller or BridgeController(self)
        self.opener=opener or (lambda url:QDesktopServices.openUrl(QUrl(url)))
        self.snapshot=Snapshot();self.previous=None;self.notice_thread=None;self.notice_threads=[];self.activity_dialog=None;self.composer=None
        self.result_animations=set()
        self.setObjectName('activityPanel');self.setStyleSheet('QFrame#activityPanel{background:rgba(248,245,255,235);border:1px solid #d7cde7;border-radius:9px;} QPushButton{border:0;background:transparent;padding:3px;font-family:Microsoft YaHei UI;font-size:11px;color:#55446c;} QPushButton:hover{background:#e9e0f5;border-radius:5px;}')
        row=QHBoxLayout(self);row.setContentsMargins(4,3,4,3);row.setSpacing(0)
        for text,tip,callback in [('任务','查看 Codex 本机任务与提醒',self.show_tasks),('聊','打开 Codex 新聊天草稿',self.show_composer),('音','打开原生聊天后，点击 Codex 的麦克风开始语音',self.open_voice_chat)]:
            b=QPushButton(text,self);b.setToolTip(tip);b.clicked.connect(callback);row.addWidget(b,1)
        self.status=QPushButton(pet)
        self.status_full_text=''
        self.status.setCursor(Qt.CursorShape.PointingHandCursor)
        self.status.clicked.connect(self.open_status)
        self.status.hide()
        self.live_tasks=[]
        self.controller.updated.connect(self.accept_snapshot)
        self.presence=PanelVisibility(self,pet)
        self.hotkey=Hotkey(QApplication.instance(),self.show_composer)
        self.pet.tray.messageClicked.connect(self.open_notice)

    def accept_snapshot(self,snapshot):
        previous=self.previous;self.snapshot=snapshot
        activities=visible_activities(snapshot)
        # Character and overhead badge share one activity rule. Old unread
        # results remain in the task list without overriding active work.
        live=[a for a in activities if a.status in {'running','needs_input'}]
        primary=next((a for a in live if a.status=='running'),live[0] if live else None)
        state=primary.status if primary else 'idle'
        self.update_status(activities)
        current={a.id:(a.status,a.turn_id) for a in activities}
        if previous is not None and self.pet.notifications:
            notices=[a for a in activities if a.status in {'needs_input','ready','blocked'} and previous.get(a.id)!=(a.status,a.turn_id)]
            if notices:
                self.notice_threads=[a.id for a in notices];self.notice_thread=self.notice_threads[0]
                title=LABELS[notices[0].status] if len(notices)==1 else f'{len(notices)} 项新提醒'
                body=notices[0].title[:100] if len(notices)==1 else '\n'.join(LABELS[a.status]+' · '+a.title[:50] for a in notices[:3])+'\n点击查看全部提醒及对应聊天'
                self.pet.tray.showMessage('小白龙 · '+title,body,self.pet.tray.MessageIcon.Information,6500)
        old_primary=self.pet.codex_activity_key
        self.previous=current;self.pet.codex_activity_key=(primary.id,primary.status,primary.turn_id) if primary else None
        self.pet.codex_status=state
        self.presence.set_activity(bool(activities),changed=current!=previous)
        results=[a for a in activities if a.status in {'ready','blocked'}]
        new_results=[a for a in results if previous is not None and previous.get(a.id)!=(a.status,a.turn_id) and (a.id,a.turn_id,a.status) not in self.result_animations]
        self.result_animations.update((a.id,a.turn_id,a.status) for a in results)
        if len(self.result_animations)>4096:self.result_animations={(a.id,a.turn_id,a.status) for a in results}
        if not self.pet.dragged and not self.pet.portrait_mode and not self.pet.paused:
            if not live and new_results:
                animation='success' if new_results[0].status=='ready' else 'failed'
                self.pet.play(animation if animation in self.pet.manifest['states'] else 'idle')
            elif old_primary!=self.pet.codex_activity_key:self.pet.play(self.pet.status_animation())
        if self.activity_dialog is not None and self.activity_dialog.isVisible():self.populate_tasks()

    def update_status(self,activities):
        self.live_tasks=[task for task in activities if task.status in {'running','needs_input'}]
        if not self.live_tasks:
            if not self.status.isHidden():
                self.status.hide()
                self.presence.update_mask()
            return

        running=sum(task.status=='running' for task in self.live_tasks)
        state='running' if running else 'needs_input'
        if len(self.live_tasks)==1:
            task=self.live_tasks[0]
            label=(task.progress or LABELS[state]) if state=='running' else LABELS[state]
        elif running:
            label=f'{running} 个任务进行中'
        else:
            label=f'{len(self.live_tasks)} 个任务待确认'
        if self.status_full_text!=label:
            self.status_full_text=label
            self.status.setStyleSheet(
                'QPushButton {background:#f8f5ff;border:1px solid #d7cde7;border-radius:13px;'
                f'color:{COLORS[state]};font:600 12px "Microsoft YaHei UI";padding:2px 10px;}}'
                'QPushButton:hover {background:#e9e0f5;}'
            )
        self.status.setToolTip('\n'.join(
            f'{LABELS[task.status]} · {task.title}'+(f'\n{task.progress}' if task.status=='running' and task.progress else '')
            for task in self.live_tasks[:5]
        ))
        self.status.show()
        self.pet.position_panel()

    def open_status(self):
        if len(self.live_tasks)==1:
            self.open_thread(self.live_tasks[0].id)
        elif self.live_tasks:
            self.show_tasks()

    def open_thread(self,thread_id):
        if self.opener(chat_url(thread_id)):
            item=next((a for a in self.snapshot.activities if a.id==thread_id),None)
            if item:self.controller.reader.acknowledge(item.id,item.turn_id)
            self.controller.poll()
    def open_primary(self):
        activities=visible_activities(self.snapshot)
        if activities:self.open_thread(activities[0].id)
        else:self.opener(chat_url())
    def open_notice(self):
        if len(self.notice_threads)>1:self.show_tasks()
        elif self.notice_thread:self.open_thread(self.notice_thread)
    def open_voice_chat(self):self.open_primary()

    def show_tasks(self):
        self.pet.show_pet()
        if self.activity_dialog is None:
            self.activity_dialog=QDialog(self.pet);self.activity_dialog.setWindowTitle('小白龙 · Codex 活动');self.activity_dialog.resize(560,340)
            layout=QVBoxLayout(self.activity_dialog)
            note=QLabel('双击任务打开对应聊天。读取本机 Codex 活动，不会启动模型。');note.setWordWrap(True);layout.addWidget(note)
            self.tasks=QListWidget();self.tasks.itemDoubleClicked.connect(lambda item:self.open_thread(item.data(Qt.ItemDataRole.UserRole)));layout.addWidget(self.tasks)
            b=QPushButton('在 Codex 打开所选任务');b.clicked.connect(lambda:self.open_thread(self.tasks.currentItem().data(Qt.ItemDataRole.UserRole)) if self.tasks.currentItem() else None);layout.addWidget(b)
        self.populate_tasks();self.activity_dialog.show();self.activity_dialog.raise_();self.activity_dialog.activateWindow()
    def populate_tasks(self):
        selected=self.tasks.currentItem().data(Qt.ItemDataRole.UserRole) if self.tasks.currentItem() else None
        self.tasks.clear()
        for a in visible_activities(self.snapshot):
            item=QListWidgetItem('【'+LABELS[a.status]+'】 '+a.title);item.setData(Qt.ItemDataRole.UserRole,a.id);self.tasks.addItem(item)
            if a.id==selected:self.tasks.setCurrentItem(item)
        if not self.snapshot.connected:
            item=QListWidgetItem(self.snapshot.error or '尚未连接');item.setFlags(Qt.ItemFlag.NoItemFlags);self.tasks.addItem(item)
        elif self.tasks.count()==0:
            item=QListWidgetItem('当前没有活动任务');item.setFlags(Qt.ItemFlag.NoItemFlags);self.tasks.addItem(item)

    def show_composer(self):
        self.pet.show_pet()
        if self.composer is None:
            self.composer=QDialog(self.pet);self.composer.setWindowTitle('小白龙 · 新聊天');self.composer.resize(440,240)
            layout=QVBoxLayout(self.composer)
            note=QLabel('输入请求后带到 Codex 草稿；在 Codex 中确认发送。语音请使用 Codex 原生麦克风。');note.setWordWrap(True);layout.addWidget(note)
            self.prompt=QPlainTextEdit();self.prompt.setPlaceholderText('想让 Codex 做什么？');layout.addWidget(self.prompt)
            button=QPushButton('带到 Codex 输入框');button.clicked.connect(self.open_draft);layout.addWidget(button)
        self.composer.show();self.composer.raise_();self.composer.activateWindow();self.prompt.setFocus()
    def open_draft(self):
        prompt=self.prompt.toPlainText().strip()
        if prompt and self.opener(chat_url(prompt=prompt)):self.composer.hide()
    def stop(self):self.presence.stop();self.controller.stop();self.hotkey.stop()
