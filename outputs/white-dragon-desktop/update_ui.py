"""Qt update controls: background reads; download/install only after consent."""
from concurrent.futures import Future
from threading import Thread
import json,logging
from pathlib import Path
from PySide6.QtCore import QObject,QTimer,QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox
from updates import APP_VERSION,RELEASES_URL,UpdateError,atomic_json,fetch_latest,version_tuple,prepare_update,launch_helper


class BackgroundWorker:
    """Network checks must never keep a closed pet alive at interpreter exit."""
    def __init__(self):self.closed=False
    def submit(self,fn,*args):
        future=Future()
        if self.closed:future.cancel();return future
        def run():
            if not future.set_running_or_notify_cancel():return
            try:future.set_result(fn(*args))
            except BaseException as exc:future.set_exception(exc)
        Thread(target=run,name='release-update',daemon=True).start();return future
    def shutdown(self,**_):self.closed=True


class UpdateController(QObject):
    def __init__(self,pet,base,scope):
        super().__init__(pet);self.pet=pet;self.base=Path(base);self.scope=scope
        self.pool=BackgroundWorker()
        self.future=None;self.operation=None;self.manual=False;self.latest=None;self.dialog=None;self.notes_dialog=None;self.helper=None;self.request=None
        self.settings_path=self.base/'update-settings.json';self.prompted=set();self.stopped=False
        try:
            self.settings=json.loads(self.settings_path.read_text(encoding='utf-8'))
            if not isinstance(self.settings,dict):self.settings={}
        except (OSError,ValueError):self.settings={}
        self.timer=QTimer(self);self.timer.timeout.connect(self.collect);self.timer.start(150)
        self.auto_timer=QTimer(self);self.auto_timer.timeout.connect(lambda:self.check(False));self.auto_timer.start(6*60*60*1000)
        QTimer.singleShot(12000,lambda:self.check(False) if not self.stopped else None)

    def add_menu(self,menu):
        menu.addSeparator()
        checking=self.future is not None
        label='正在检查/准备更新…' if checking else ('发现新版本 v'+self.latest.version if self.latest and version_tuple(self.latest.version)>version_tuple(APP_VERSION) else '检查更新…')
        action=menu.addAction(label,lambda:self.offer(self.latest) if self.latest and version_tuple(self.latest.version)>version_tuple(APP_VERSION) else self.check(True));action.setEnabled(not checking)
        menu.addAction('版本说明 · v'+APP_VERSION,self.notes)
        automatic=menu.addAction('自动检查更新');automatic.setCheckable(True);automatic.setChecked(self.settings.get('automatic',True));automatic.triggered.connect(self.toggle_automatic)

    def toggle_automatic(self,enabled):
        self.settings['automatic']=bool(enabled)
        try:atomic_json(self.settings_path,self.settings)
        except OSError:logging.exception('Could not save update preference')

    def check(self,manual=True):
        if self.stopped or self.future is not None or self.helper is not None:return
        if not manual and not self.settings.get('automatic',True):return
        self.manual=manual;self.operation='check';self.future=self.pool.submit(fetch_latest)

    def notes(self,release=None):
        if self.notes_dialog is not None:self.notes_dialog.raise_();return
        is_release=hasattr(release,'version')
        version=release.version if is_release else APP_VERSION
        url=release.page_url if is_release else RELEASES_URL+'/tag/v'+APP_VERSION
        if is_release:content=release.notes
        else:
            try:content=(self.base/'CHANGELOG.md').read_text(encoding='utf-8')[:100000]
            except OSError:content='当前安装未附版本说明，可打开GitHub完整版本页。'
        box=QMessageBox(self.pet);self.notes_dialog=box
        box.setWindowTitle('小白龙 · 版本说明');box.setText('版本 v'+version)
        box.setInformativeText('展开详情查看此版本的更新内容。');box.setDetailedText(content or '此版本未填写说明。')
        github=box.addButton('GitHub完整版本页',QMessageBox.ButtonRole.ActionRole);box.addButton('关闭',QMessageBox.ButtonRole.RejectRole)
        def finished(_):
            self.notes_dialog=None
            if box.clickedButton() is github:QDesktopServices.openUrl(QUrl(url))
            box.deleteLater()
        box.finished.connect(finished);box.open()

    def offer(self,release):
        if release is None or self.future is not None or self.helper is not None:return
        if self.dialog is not None:self.dialog.raise_();return
        box=QMessageBox(self.pet);self.dialog=box
        box.setWindowTitle('小白龙 · 更新到 v'+release.version)
        box.setText(f'当前 v{APP_VERSION}，可更新到 v{release.version}。')
        box.setInformativeText('同意后才下载并校验更新包。安装会正常退出小白龙及联动监视器，完成后恢复；个人设置与日志保留。本地源码或Git改动会阻止覆盖。')
        box.setDetailedText(release.notes or '此版本未填写说明。')
        install=box.addButton('下载并安装',QMessageBox.ButtonRole.AcceptRole)
        notes=box.addButton('查看版本说明',QMessageBox.ButtonRole.ActionRole)
        box.addButton('稍后',QMessageBox.ButtonRole.RejectRole)
        def finished(_):
            self.dialog=None
            if box.clickedButton() is install:
                self.operation='prepare';self.future=self.pool.submit(prepare_update,self.base,release,self.scope,getattr(self.pet,'managed_launch',False))
            elif box.clickedButton() is notes:self.notes(release)
            box.deleteLater()
        box.finished.connect(finished);box.setModal(False);box.open()

    def collect(self):
        if self.stopped:return
        if self.future is not None and self.future.done():
            future=self.future;operation=self.operation;self.future=None
            try:
                value=future.result()
                if operation=='check':
                    self.latest=value
                    if version_tuple(value.version)>version_tuple(APP_VERSION):
                        if self.manual or value.version not in self.prompted:
                            self.prompted.add(value.version);self.offer(value)
                    elif self.manual:QMessageBox.information(self.pet,'小白龙 · 检查更新','当前已是最新正式版本 v'+APP_VERSION+'。')
                else:
                    self.request=Path(value);self.helper=launch_helper(self.request)
            except Exception as exc:
                logging.exception('Update operation failed')
                if operation=='prepare' or self.manual:QMessageBox.warning(self.pet,'小白龙 · 更新未完成',str(exc))
        if self.helper is not None and self.helper.poll() is not None:
            # Normally the independent helper gracefully closes this pet before
            # replacement. This branch surfaces failures before that handoff.
            result=self.request.parent/'result.json'
            try:value=json.loads(result.read_text(encoding='utf-8'))
            except (OSError,ValueError):value={'ok':False,'error':'更新辅助程序提前退出；程序尚未替换，请查看update-state.json。'}
            self.helper=None;self.request=None
            if not value.get('ok'):QMessageBox.warning(self.pet,'小白龙 · 更新未完成',value.get('error','未知错误'))

    def stop(self):
        self.stopped=True;self.timer.stop();self.auto_timer.stop();self.pool.shutdown(wait=False,cancel_futures=True)
