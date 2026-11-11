"""Transparent companion with a read-only local Codex activity adapter."""
from __future__ import annotations
import argparse, hashlib, json, logging, math, sys, time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
if not getattr(sys,'frozen',False):sys.path.insert(0,str(Path(__file__).resolve().parent))
# The copied helper must enter before importing Qt from any installation.
if __name__=='__main__' and '--apply-update' in sys.argv:
    from updates import apply_update_cli
    raise SystemExit(apply_update_cli(sys.argv[1:]))
if __name__=='__main__' and getattr(sys,'frozen',False) and '--update-health-check' not in sys.argv:
    from updates import update_in_progress,UpdateError
    try:
        if update_in_progress(Path(sys.executable).resolve().parent.parent):raise SystemExit(0)
    except UpdateError as exc:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,str(exc),'小白龙 · 更新恢复',0x10)
        raise SystemExit(1)
from PySide6.QtCore import Qt,QPoint,QPointF,QRectF,QTimer,QLockFile
from PySide6.QtGui import QImage,QPainter,QIcon,QAction,QCursor
from PySide6.QtWidgets import QApplication,QWidget,QMenu,QSystemTrayIcon,QMessageBox
from windows_link import reduced_motion,startup_file,install_startup,uninstall_startup
BASE=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
if getattr(sys,'frozen',False) and (BASE.parent/'assets').is_dir():BASE=BASE.parent
ASSETS=BASE/'assets'
STATUS_SPACE=34
FOOTER_HEIGHT=42
LABELS={'idle':'待机','waving':'轻摆回应','jumping':'轻跳','running-right':'向右倾斜','running-left':'向左倾斜','running':'思考','review':'检查','failed':'失落','waiting':'等待确认','success':'完成庆祝'}

def portrait_motion(state,elapsed):
    """Rigid transforms only: no face warping, pose interpolation or synthetic blink."""
    if state=='waving':
        u=min(1.,elapsed/1.4)
        return 0.,0.,2.0*math.sin(4*math.pi*u)*math.sin(math.pi*u)**2
    if state=='jumping':
        u=min(1.,elapsed/1.1)
        return 0.,-22.*math.sin(math.pi*u)**2,0.
    if state in ('running-left','running-right'):
        direction=-1 if state=='running-left' else 1
        return 0.,-2.,direction*2.5
    return 0.,0.,0.

def desktop_scope():
    """Single-instance ownership must not cross Windows desktop boundaries."""
    try:
        import ctypes
        u=ctypes.WinDLL('user32');k=ctypes.WinDLL('kernel32')
        u.GetProcessWindowStation.restype=ctypes.c_void_p
        u.GetThreadDesktop.restype=ctypes.c_void_p;u.GetThreadDesktop.argtypes=[ctypes.c_ulong]
        u.GetUserObjectInformationW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_void_p,ctypes.c_ulong,ctypes.POINTER(ctypes.c_ulong)]
        names=[]
        for handle in [u.GetProcessWindowStation(),u.GetThreadDesktop(k.GetCurrentThreadId())]:
            buf=ctypes.create_unicode_buffer(1024);needed=ctypes.c_ulong()
            if not u.GetUserObjectInformationW(handle,2,buf,2048,ctypes.byref(needed)):raise OSError('Cannot read current desktop name')
            names.append(buf.value)
        return hashlib.sha256('\\'.join(names).encode()).hexdigest()[:12]
    except (AttributeError,OSError):return 'default'

class Dragon(QWidget):
    def __init__(self,manifest,settings_path,integration=False):
        super().__init__()
        self.manifest=manifest;self.settings_path=settings_path
        try:self.settings=json.loads(settings_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):self.settings={}
        self.integration=integration;self.panel=None;self.updates=None;self.codex_status='idle';self.codex_activity_key=None
        self.canvas_top=STATUS_SPACE if integration else 0
        self.notifications=self.settings.get('notifications',True);self.os_reduced=reduced_motion();self.cleaned=False
        self.state='idle';self.started=time.perf_counter();self.paused=False;self.pause_elapsed=0
        self.media_started=self.started;self.media_pause_elapsed=0
        self.decoder=ThreadPoolExecutor(max_workers=1,thread_name_prefix='pet-decoder');self.prefetched=None
        self.portrait_mode=self.settings.get('portrait_mode',False);self.autoplay=self.settings.get('autoplay',True)
        self.fps=int(self.settings.get('fps',60)) if self.settings.get('animation_version')==2 else 60
        self.topmost=self.settings.get('topmost',True);self.clickthrough=False
        self.motion_offset=(0.,0.,0.);self.motion_from=self.motion_offset
        self.cache=OrderedDict();self.drag_origin=None;self.window_origin=None;self.dragged=False;self.suppress_click=False;self.frames_painted=0;self.errors=[]
        self.setWindowTitle('小白龙 · 独立桌宠')
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint|Qt.WindowType.Tool|Qt.WindowType.NoDropShadowWindowHint|(Qt.WindowType.WindowStaysOnTopHint if self.topmost else Qt.WindowType.Widget))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.portrait=QImage(str(ASSETS/'portrait.png'))
        if self.portrait.isNull():raise RuntimeError('portrait.png cannot be loaded')
        self.set_size(int(self.settings.get('width',260)),initial=True)
        screen=QApplication.primaryScreen().availableGeometry()
        saved_canvas_y=self.settings.get('y',screen.bottom()-self.height()-25+self.canvas_top)
        self.move(int(self.settings.get('x',screen.right()-self.width()-40)),int(saved_canvas_y)-self.canvas_top)
        self.keep_on_screen()
        self.timer=QTimer(self);self.timer.setTimerType(Qt.TimerType.PreciseTimer);self.timer.timeout.connect(self.tick);self.timer.start(round(1000/self.fps))
        self.click_timer=QTimer(self);self.click_timer.setSingleShot(True);self.click_timer.timeout.connect(lambda:self.interaction_play('waving'))
        self.auto_timer=QTimer(self);self.auto_timer.timeout.connect(self.auto_action);self.auto_timer.start(35000)
        self.tray=QSystemTrayIcon(QIcon(str(ASSETS/'portrait.png')),self)
        self.tray.setToolTip('小白龙：拖动、点击轻摆、双击轻跳、右键菜单')
        self.tray.activated.connect(self.tray_activated);self.refresh_tray_menu();self.tray.show()
        self.load_state('idle')
        if integration:
            from bridge_ui import ActivityPanel
            self.panel=ActivityPanel(self);self.position_panel()
            from update_ui import UpdateController
            self.updates=UpdateController(self,BASE,desktop_scope())
        self.motion_timer=QTimer(self);self.motion_timer.timeout.connect(self.refresh_motion);self.motion_timer.start(5000)

    def status_animation(self):
        state={'running':'running','needs_input':'waiting','ready':'success','blocked':'failed'}.get(self.codex_status,'idle')
        return state if state in self.manifest['states'] else 'idle'

    def refresh_motion(self):
        value=reduced_motion()
        if value!=self.os_reduced:self.os_reduced=value;self.update()

    def position_panel(self):
        if self.panel is not None:
            self.panel.setGeometry(0,self.canvas_top+self.canvas_height,self.width(),FOOTER_HEIGHT)
            status=self.panel.status
            if not status.isHidden():
                metrics=status.fontMetrics()
                text=self.panel.status_full_text
                width=min(metrics.horizontalAdvance(text)+24,self.width()-8)
                status.setText(metrics.elidedText(text,Qt.TextElideMode.ElideRight,width-24))
                # Use part of the sprite's clear headroom to keep the label close.
                top=2+round(self.canvas_height*.12)
                status.setGeometry((self.width()-width)//2,top,width,27)
            self.panel.presence.update_mask()

    def resizeEvent(self,event):
        super().resizeEvent(event);self.position_panel()

    def load_state(self,state,index=0):
        if state not in self.manifest['states']:state='idle'
        entry=self.manifest['states'][state]
        if entry['mode']=='portrait':return self.portrait
        chunk=index//entry['chunk_size'];key=(state,chunk)
        if key in self.cache:
            self.cache.move_to_end(key);self.prefetch_next(state,chunk);return self.cache[key]
        path=(ASSETS/entry['chunks'][chunk]).resolve()
        if not path.is_relative_to(ASSETS.resolve()):raise ValueError('Animation path outside assets')
        if self.prefetched is not None and self.prefetched[0]==key:
            image=self.prefetched[1].result();self.prefetched=None
        else:image=QImage(str(path))
        fw,fh=entry['width'],entry['height']
        count=min(entry['chunk_size'],entry['frames']-chunk*entry['chunk_size'])
        if image.isNull() or image.width()!=fw*entry['columns'] or image.height()!=fh*math.ceil(count/entry['columns']):
            message=f'Invalid continuous animation: {state}/{chunk}';logging.error(message);self.errors.append(message);raise RuntimeError(message)
        self.cache[key]=image
        while len(self.cache)>2:self.cache.popitem(last=False)
        self.prefetch_next(state,chunk)
        return image

    def prefetch_next(self,state,chunk):
        entry=self.manifest['states'][state]
        next_chunk=(chunk+1)%len(entry['chunks']);key=(state,next_chunk)
        if key in self.cache or (self.prefetched is not None and self.prefetched[0]==key):return
        if self.prefetched is not None:self.prefetched[1].cancel()
        path=(ASSETS/entry['chunks'][next_chunk]).resolve()
        if not path.is_relative_to(ASSETS.resolve()):raise ValueError('Animation path outside assets')
        self.prefetched=(key,self.decoder.submit(QImage,str(path)))

    def play(self,state):
        if state not in self.manifest['states']:return
        if self.paused:self.media_started=time.perf_counter()-self.media_pause_elapsed
        self.motion_from=self.motion_offset
        self.load_state(state);self.state=state;self.started=time.perf_counter();self.pause_elapsed=0;self.paused=False;self.portrait_mode=False;self.update()

    def interaction_play(self,state):
        # Moving/clicking a paused or static pet never changes that preference.
        if not self.paused and not self.portrait_mode:self.play(state)

    def elapsed(self):return self.pause_elapsed if self.paused else time.perf_counter()-self.started
    def media_elapsed(self):return self.media_pause_elapsed if self.paused else time.perf_counter()-self.media_started

    def render_source(self):
        entry=self.manifest['states'][self.state]
        if entry['mode']=='portrait' and self.manifest['states']['idle']['mode']=='continuous':return 'idle'
        return self.state

    def tick(self):
        if not self.isVisible() or self.paused or self.os_reduced:return
        entry=self.manifest['states'][self.state]
        if not self.portrait_mode and not entry['loop'] and self.elapsed()>=entry['duration']:
            target=self.status_animation();self.play(target if target!=self.state else 'idle')
        self.update()

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,True)
        p.translate(0,self.canvas_top)
        render_state=self.render_source();entry=self.manifest['states'][render_state]
        native_clip=self.manifest['states'][self.state]['mode']=='continuous'
        target=(0.,0.,0.) if self.portrait_mode or self.os_reduced or native_clip else portrait_motion(self.state,self.elapsed())
        u=min(1.,self.elapsed()/.24);blend=u*u*(3-2*u)
        self.motion_offset=tuple(a+(b-a)*blend for a,b in zip(self.motion_from,target))
        if self.os_reduced:self.motion_offset=(0.,0.,0.)
        dx,dy,angle=self.motion_offset
        height=self.canvas_height
        if self.portrait_mode or entry['mode']=='portrait':
            # Uniform scale and rigid transforms keep both eyes and every facial pixel intact.
            scale=min(self.width()*.91/self.portrait.width(),height*.91/self.portrait.height())
            w,h=self.portrait.width()*scale,self.portrait.height()*scale
            base=height*.97
            p.translate(self.width()/2+dx*self.width()/384,base+dy*height/448)
            p.rotate(angle)
            p.drawImage(QRectF(-w/2,-h,w,h),self.portrait)
        else:
            clock=self.media_elapsed() if render_state=='idle' else self.elapsed()
            index=0 if self.os_reduced else int(clock*entry['fps'])
            index=index%entry['frames'] if entry['loop'] else min(index,entry['frames']-1)
            image=self.load_state(render_state,index);local=index%entry['chunk_size']
            fw,fh=entry['width'],entry['height'];columns=entry['columns']
            base=height*.95
            p.translate(self.width()/2+dx*self.width()/384,base+dy*height/448);p.rotate(angle)
            p.drawImage(QRectF(-self.width()/2,-base,self.width(),height),image,QRectF(local%columns*fw,local//columns*fh,fw,fh))
        p.end();self.frames_painted+=1

    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:
            self.drag_origin=event.globalPosition().toPoint();self.window_origin=self.pos();self.dragged=False;self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self,event):
        if self.drag_origin is not None and event.buttons()&Qt.MouseButton.LeftButton:
            delta=event.globalPosition().toPoint()-self.drag_origin
            if delta.manhattanLength()>6:
                if not self.dragged:self.interaction_play('running-right' if delta.x()>=0 else 'running-left')
                self.dragged=True;self.move(self.window_origin+delta)

    def mouseReleaseEvent(self,event):
        if event.button()!=Qt.MouseButton.LeftButton:return
        had_drag=self.dragged;self.drag_origin=None;self.setCursor(Qt.CursorShape.OpenHandCursor)
        if self.suppress_click:self.suppress_click=False;return
        if had_drag:self.keep_on_screen();self.save();self.interaction_play(self.status_animation())
        else:self.click_timer.start(QApplication.doubleClickInterval())
        self.dragged=False

    def mouseDoubleClickEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:self.suppress_click=True;self.click_timer.stop();self.interaction_play('jumping')

    def wheelEvent(self,event):self.set_size(self.width()+(20 if event.angleDelta().y()>0 else -20))

    def set_size(self,width,initial=False):
        width=max(160,min(480,width));oldbottom=self.y()+self.height();self.canvas_height=round(width*448/384);self.resize(width,self.canvas_top+self.canvas_height+(FOOTER_HEIGHT if self.integration else 0));self.position_panel()
        if not initial:self.move(self.x(),oldbottom-self.height());self.keep_on_screen();self.save()

    def keep_on_screen(self):
        screens=QApplication.screens();point=self.pos()+QPoint(self.width()//2,self.height()//2)
        screen=next((s for s in screens if s.availableGeometry().contains(point)),QApplication.primaryScreen()).availableGeometry()
        self.move(max(screen.left(),min(self.x(),screen.right()-self.width()+1)),max(screen.top(),min(self.y(),screen.bottom()-self.height()+1)))

    def toggle_pause(self):
        if self.paused:
            self.started=time.perf_counter()-self.pause_elapsed;self.media_started=time.perf_counter()-self.media_pause_elapsed;self.paused=False
        else:self.pause_elapsed=self.elapsed();self.media_pause_elapsed=self.media_elapsed();self.paused=True

    def set_fps(self,fps):self.fps=fps;self.timer.setInterval(round(1000/fps));self.save()
    def toggle_portrait(self):self.portrait_mode=not self.portrait_mode;self.save();self.update()
    def toggle_auto(self):self.autoplay=not self.autoplay;self.save()
    def toggle_notifications(self):self.notifications=not self.notifications;self.save()
    def toggle_startup(self):
        try:
            if startup_file().exists():
                uninstall_startup();(BASE/('.codex-watcher-stop-'+desktop_scope())).touch()
            else:
                install_startup(BASE/'WhiteDragon/WhiteDragon.exe');self.start_watcher()
        except OSError as exc:QMessageBox.warning(self,'启动设置失败',str(exc))
    def start_watcher(self):
        import subprocess
        exe=BASE/'WhiteDragon/WhiteDragon.exe'
        subprocess.Popen([str(exe),'--watch-codex'],creationflags=0x08000000)
    def toggle_top(self):
        self.topmost=not self.topmost;self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint,self.topmost);self.show();self.save()
    def toggle_clickthrough(self):
        self.clickthrough=not self.clickthrough;self.setWindowFlag(Qt.WindowType.WindowTransparentForInput,self.clickthrough);self.show()
    def show_pet(self):
        if self.clickthrough:self.toggle_clickthrough()
        # A hidden Windows startup flag can leave Qt's visibility bookkeeping
        # out of sync with the native window. Force a real hide/show transition.
        self.hide();self.showNormal();self.raise_();self.keep_on_screen()
    def auto_action(self):
        if self.autoplay and not self.paused and not self.os_reduced and self.codex_status=='idle' and self.state=='idle' and not self.portrait_mode and self.isVisible():self.play('waving')
    def tray_activated(self,reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,QSystemTrayIcon.ActivationReason.DoubleClick):self.show_pet()

    def menu(self):
        menu=QMenu();menu.setStyleSheet('QMenu{font-family:Microsoft YaHei UI;font-size:13px;padding:5px;}QMenu::item{padding:6px 24px;}')
        title=menu.addAction('小白龙 · Codex 联动版');title.setEnabled(False);menu.addSeparator()
        if self.panel is not None:
            menu.addAction('Codex 活动列表',self.panel.show_tasks)
            menu.addAction('新聊天草稿 · Ctrl+Alt+Shift+P',self.panel.show_composer)
            menu.addAction('打开 Codex 聊天（语音使用原生麦克风）',self.panel.open_voice_chat)
            for label,value,fn in [('任务通知',self.notifications,self.toggle_notifications),('登录后随 Codex 启动',startup_file().exists(),self.toggle_startup)]:
                act=menu.addAction(label);act.setCheckable(True);act.setChecked(value);act.triggered.connect(fn)
            menu.addSeparator()
        menu.addAction('显示 / 恢复鼠标交互',self.show_pet)
        menu.addAction('移到当前屏幕右下角',self.reset_position)
        menu.addAction('隐藏到托盘',self.hide)
        actions=menu.addMenu('动作')
        for state,label in LABELS.items():
            if state in self.manifest['states']:actions.addAction(self.manifest['states'][state].get('label',label),lambda checked=False,s=state:self.play(s))
        menu.addAction('继续播放' if self.paused else '暂停动画',self.toggle_pause)
        for label,value,fn in [('静态显示',self.portrait_mode,self.toggle_portrait),('自动小动作',self.autoplay,self.toggle_auto),('始终置顶',self.topmost,self.toggle_top),('鼠标穿透（从托盘恢复）',self.clickthrough,self.toggle_clickthrough)]:
            act=menu.addAction(label);act.setCheckable(True);act.setChecked(value);act.triggered.connect(fn)
        sizes=menu.addMenu('大小（滚轮也可调整）')
        for size in [180,220,260,320,400]:sizes.addAction(f'{size}px',lambda checked=False,w=size:self.set_size(w))
        rate=menu.addMenu('播放帧率')
        for fps,label in [(60,'60 FPS'),(30,'30 FPS'),(15,'15 FPS · 省电')]:
            act=rate.addAction(label);act.setCheckable(True);act.setChecked(fps==self.fps);act.triggered.connect(lambda checked=False,f=fps:self.set_fps(f))
        if self.updates is not None:self.updates.add_menu(menu)
        menu.addSeparator();menu.addAction('退出桌宠',self.close)
        return menu

    def refresh_tray_menu(self):
        # Rebuild on open so checkmarks reflect changes from the window's context menu.
        self.tray_menu=self.menu();self.tray_menu.aboutToShow.connect(self.sync_tray_menu);self.tray.setContextMenu(self.tray_menu)
    def sync_tray_menu(self):
        fresh=self.menu();self.tray_menu.clear()
        for action in fresh.actions():self.tray_menu.addAction(action)
        self._fresh_menu=fresh
    def reset_position(self):
        screen=QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        rect=screen.availableGeometry();self.move(rect.right()-self.width()-30,rect.bottom()-self.height()-25)
        self.show_pet();self.save()
    def contextMenuEvent(self,event):self.menu().exec(event.globalPos())
    def save(self):
        data={'animation_version':2,'width':self.width(),'x':self.x(),'y':self.y()+self.canvas_top,'fps':self.fps,'topmost':self.topmost,'autoplay':self.autoplay,'portrait_mode':self.portrait_mode,'notifications':self.notifications}
        try:self.settings_path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        except OSError:logging.exception('Could not save settings')
    def cleanup(self):
        if self.cleaned:return
        self.cleaned=True
        if self.panel is not None:self.panel.stop()
        if self.updates is not None:self.updates.stop()
        self.decoder.shutdown(wait=False,cancel_futures=True)
        self.save();self.tray.hide()
    def closeEvent(self,event):
        self.cleanup();event.accept();QApplication.instance().quit()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--quit',action='store_true');parser.add_argument('--smoke-test',action='store_true');parser.add_argument('--managed',action='store_true');parser.add_argument('--watch-codex',action='store_true');parser.add_argument('--bridge-check',action='store_true');parser.add_argument('--snapshot');parser.add_argument('--report');parser.add_argument('--update-health-check',type=Path);args=parser.parse_args()
    logging.basicConfig(filename=str(BASE/'desktop.log'),level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    if args.update_health_check:
        from updates import installed_health
        app=QApplication([])
        return installed_health(BASE,args.update_health_check)
    if args.watch_codex:
        from windows_link import run_watcher
        return run_watcher(BASE,desktop_scope())
    if args.bridge_check:
        from codex_bridge import CodexReader
        result=CodexReader().scan().report()
        if args.report:Path(args.report).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return 0 if result['connected'] else 1
    app=QApplication(sys.argv);app.setQuitOnLastWindowClosed(False);app.setApplicationName('WhiteDragonDesktop');app.setApplicationDisplayName('小白龙独立桌宠')
    scope=desktop_scope()
    control_path=BASE/('.desktop-control-'+scope+'.json')
    instance_lock=None
    if not args.smoke_test:
        instance_lock=QLockFile(str(BASE/('.desktop-'+scope+'.lock')));instance_lock.setStaleLockTime(10000)
        if not instance_lock.tryLock(0):
            # Local files also work in Windows environments that restrict named pipes.
            temp=BASE/('.desktop-control-'+str(time.time_ns())+'.tmp')
            temp.write_text(json.dumps({'action':'quit' if args.quit else 'show','at':time.time()}),encoding='utf-8')
            temp.replace(control_path);return 0
        if args.quit:instance_lock.unlock();return 0
    manifest=json.loads((ASSETS/'manifest.json').read_text(encoding='utf-8'))
    settings=BASE/('test-settings.json' if args.smoke_test else 'settings.json')
    dragon=Dragon(manifest,settings,integration=not args.smoke_test);dragon.managed_launch=args.managed;dragon.show()
    if not args.smoke_test:
        from windows_link import follow_codex_exit
        QTimer.singleShot(120,dragon.show_pet)
        dragon.codex_exit_timer=follow_codex_exit(dragon,dragon.close,managed=args.managed)
        app.aboutToQuit.connect(dragon.codex_exit_timer.stop)
        def receive():
            if not control_path.exists():return
            try:
                command=json.loads(control_path.read_text(encoding='utf-8'));control_path.unlink(missing_ok=True)
                if abs(time.time()-command.get('at',0))>15:return
                action=command.get('action')
                if action=='quit' or (action=='quit-managed' and args.managed):dragon.close()
                elif action=='show':dragon.show_pet()
            except (OSError,ValueError):logging.exception('Could not read local control command')
        dragon.control_timer=QTimer(dragon);dragon.control_timer.timeout.connect(receive);dragon.control_timer.start(250)
        app.aboutToQuit.connect(instance_lock.unlock)
    app.aboutToQuit.connect(dragon.cleanup)
    if args.smoke_test:
        report={'assets':{},'flags':{'transparent':dragon.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground),'frameless':bool(dragon.windowFlags()&Qt.WindowType.FramelessWindowHint),'topmost':bool(dragon.windowFlags()&Qt.WindowType.WindowStaysOnTopHint)}}
        states=list(manifest['states']);start=time.perf_counter()
        def inspect_state(i=0):
            if i==len(states):
                report['paint_events']=dragon.frames_painted;report['elapsed_seconds']=time.perf_counter()-start;report['errors']=dragon.errors
                report['ok']=all(report['flags'].values()) and not dragon.errors and dragon.frames_painted>=15
                if args.snapshot:dragon.grab().save(args.snapshot)
                if args.report:Path(args.report).write_text(json.dumps(report,indent=2),encoding='utf-8')
                dragon.close();app.quit();return
            state=states[i];dragon.play(state);report['assets'][state]={'frames':manifest['states'][state]['frames'],'loaded':not dragon.load_state(state).isNull()}
            QTimer.singleShot(180,lambda:inspect_state(i+1))
        QTimer.singleShot(80,inspect_state)
    return app.exec()
if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:
        logging.exception('Desktop pet failed');raise
