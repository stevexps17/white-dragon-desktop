"""Native integration of this application's hotkey, motion preference and launcher."""
from __future__ import annotations
import ctypes,os,subprocess,sys,time
from ctypes import wintypes
from pathlib import Path

def codex_processes():
    """Return Codex desktop PIDs in this Windows session, or None if unreadable."""
    if sys.platform!='win32':return None
    class Entry(ctypes.Structure):
        _fields_=[('dwSize',wintypes.DWORD),('cntUsage',wintypes.DWORD),('th32ProcessID',wintypes.DWORD),('th32DefaultHeapID',ctypes.c_size_t),('th32ModuleID',wintypes.DWORD),('cntThreads',wintypes.DWORD),('th32ParentProcessID',wintypes.DWORD),('pcPriClassBase',wintypes.LONG),('dwFlags',wintypes.DWORD),('szExeFile',wintypes.WCHAR*260)]
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD];k.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
    k.Process32FirstW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)];k.Process32NextW.argtypes=k.Process32FirstW.argtypes
    k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];k.OpenProcess.restype=wintypes.HANDLE
    k.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    k.ProcessIdToSessionId.argtypes=[wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes=[wintypes.HANDLE]
    current_session=wintypes.DWORD()
    if not k.ProcessIdToSessionId(os.getpid(),ctypes.byref(current_session)):return None
    snap=k.CreateToolhelp32Snapshot(2,0)
    if snap in (None,ctypes.c_void_p(-1).value):return None
    e=Entry();e.dwSize=ctypes.sizeof(e);result=[]
    inaccessible=False
    try:
        ok=k.Process32FirstW(snap,ctypes.byref(e))
        if not ok:return None
        while ok:
            if e.szExeFile.lower()=='chatgpt.exe':
                session=wintypes.DWORD()
                if not k.ProcessIdToSessionId(e.th32ProcessID,ctypes.byref(session)) or session.value!=current_session.value:
                    ok=k.Process32NextW(snap,ctypes.byref(e))
                    continue
                handle=k.OpenProcess(0x1000,False,e.th32ProcessID)
                if handle:
                    try:
                        buf=ctypes.create_unicode_buffer(32768);n=wintypes.DWORD(len(buf))
                        if k.QueryFullProcessImageNameW(handle,0,buf,ctypes.byref(n)):
                            path=buf.value.lower()
                            if 'openai.codex_' in path or '\\openai\\codex\\' in path:
                                result.append(int(e.th32ProcessID))
                        elif ctypes.get_last_error()==5:inaccessible=True
                    finally:k.CloseHandle(handle)
                elif ctypes.get_last_error()==5:inaccessible=True
            ok=k.Process32NextW(snap,ctypes.byref(e))
        if not result and (inaccessible or ctypes.get_last_error()!=18):return None
    finally:k.CloseHandle(snap)
    return sorted(result)


def is_client_window(class_name, visible, owner, extended_style):
    # Electron also owns hidden message, tray and devtools helper windows.
    # IsWindowVisible remains true for a minimized main window.
    return class_name=='Chrome_WidgetWin_1' and bool(visible) and not owner and not (extended_style & 0x80)


def codex_windows(*, desktop=None):
    """Return main HWNDs on this desktop, [] when closed, None on API failure.

    A named desktop is only used by read-only diagnostics. Normal pet/watchers
    follow their own Windows desktop, just like their single-instance lock.
    """
    if sys.platform!='win32':return None
    processes=codex_processes()
    u=ctypes.WinDLL('user32',use_last_error=True)
    callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    u.GetClassNameW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    u.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    u.IsWindowVisible.argtypes=[wintypes.HWND]
    u.GetWindow.argtypes=[wintypes.HWND,wintypes.UINT];u.GetWindow.restype=wintypes.HWND
    u.GetWindowLongW.argtypes=[wintypes.HWND,ctypes.c_int]
    result=[];candidates=[]
    @callback_type
    def visit(hwnd,param):
        name=ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd,name,len(name))
        if is_client_window(name.value,u.IsWindowVisible(hwnd),u.GetWindow(hwnd,4),u.GetWindowLongW(hwnd,-20)):
            pid=wintypes.DWORD();u.GetWindowThreadProcessId(hwnd,ctypes.byref(pid));candidates.append(pid.value)
            if processes is not None and pid.value in processes:result.append(int(hwnd))
        return True
    handle=None
    try:
        ctypes.set_last_error(0)
        if desktop is None:
            u.EnumWindows.argtypes=[callback_type,wintypes.LPARAM]
            ok=u.EnumWindows(visit,0)
        else:
            u.OpenDesktopW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];u.OpenDesktopW.restype=wintypes.HANDLE
            u.EnumDesktopWindows.argtypes=[wintypes.HANDLE,callback_type,wintypes.LPARAM]
            u.CloseDesktop.argtypes=[wintypes.HANDLE]
            handle=u.OpenDesktopW(desktop,0,False,0x0041)
            if not handle:return None
            ctypes.set_last_error(0);ok=u.EnumDesktopWindows(handle,visit,0)
        if not ok and ctypes.get_last_error():return None
        if processes is None and candidates:return None
        return sorted(result)
    finally:
        if handle:u.CloseDesktop(handle)


class CodexSessionGate:
    """Launch once per window session; manual pet exit stays respected."""
    def __init__(self):self.running=False;self.missing=0
    def observe(self,windows):
        if windows is None:return False
        if windows:
            launch=not self.running;self.running=True;self.missing=0;return launch
        self.missing+=1
        if self.missing>=2:self.running=False
        return False


def follow_codex_exit(parent, close_pet, *, managed=False, interval_ms=1000):
    """Follow main windows; a manual pet can wait for the first client launch."""
    from PySide6.QtCore import QTimer

    seen_codex=managed
    missing=0
    timer=QTimer(parent)

    def check():
        nonlocal seen_codex,missing
        windows=codex_windows()
        if windows:
            seen_codex=True
        if windows or windows is None or not seen_codex:
            missing=0
            return
        missing+=1
        if missing>=2:
            timer.stop()
            close_pet()

    timer.timeout.connect(check)
    check()
    timer.start(interval_ms)
    return timer

def reduced_motion():
    if sys.platform!='win32':return False
    value=wintypes.BOOL(1)
    return bool(ctypes.windll.user32.SystemParametersInfoW(0x1042,0,ctypes.byref(value),0)) and not bool(value.value)

def startup_file():
    return Path(os.environ.get('APPDATA',Path.home()/'AppData/Roaming'))/'Microsoft/Windows/Start Menu/Programs/Startup/WhiteDragon-Codex.vbs'

def startup_content(exe):
    # A GUI watcher has no console and starts the visible pet only while Codex runs.
    command='"'+str(Path(exe).resolve())+'" --watch-codex'
    return 'CreateObject("WScript.Shell").Run "'+command.replace('"','""')+'", 0, False\r\n'

def install_startup(exe):
    target=startup_file();target.write_text(startup_content(exe),encoding='utf-16');return target

def uninstall_startup():startup_file().unlink(missing_ok=True)

def run_watcher(base,scope):
    from PySide6.QtCore import QCoreApplication,QLockFile
    app=QCoreApplication([]);lock=QLockFile(str(Path(base)/('.codex-watcher-'+scope+'.lock')))
    lock.setStaleLockTime(10000)
    if not lock.tryLock(0):return 0
    gate=CodexSessionGate()
    stop=Path(base)/('.codex-watcher-stop-'+scope)
    stop.unlink(missing_ok=True)
    try:
        while True:
            if stop.exists():stop.unlink(missing_ok=True);return 0
            if (Path(base)/'.update-in-progress').exists():
                time.sleep(.2);continue
            windows=codex_windows()
            if gate.observe(windows):
                if getattr(sys,'frozen',False):argv=[sys.executable,'--managed']
                else:argv=[sys.executable,str(Path(base)/'desktop.py'),'--managed']
                subprocess.Popen(argv,creationflags=0x08000000 if sys.platform=='win32' else 0)
            time.sleep(1)
    finally:lock.unlock()
