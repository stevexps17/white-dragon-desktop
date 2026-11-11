"""Public-release updater, independent of Qt so its copied helper can replace DLLs.

Trust boundary: fixed public repository + HTTPS + release ZIP checksum + complete
per-file manifest. SHA256 detects corruption; it is not a publisher signature.
No Git fetch/reset, shell execution, authentication, forced process termination,
or writes to Codex data. A dirty development checkout is deliberately not updated.
"""
from __future__ import annotations
import argparse,ctypes,hashlib,json,logging,os,re,shutil,stat,subprocess,sys,tempfile,time,uuid,zipfile
from dataclasses import dataclass
from pathlib import Path,PurePosixPath
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request,urlopen

APP_VERSION='1.1.0'
REPOSITORY='stevexps17/white-dragon-desktop'
RELEASES_URL=f'https://github.com/{REPOSITORY}/releases'
LATEST_API=f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
LATEST_METADATA=f'{RELEASES_URL}/latest/download/update.json'
PRODUCT='white-dragon-desktop'
MAX_DOWNLOAD=600*1024*1024
MAX_UNPACKED=1800*1024*1024
MAX_MEMBERS=12000
MUTABLE_NAMES={'settings.json','test-settings.json','update-settings.json','update-state.json','update-health-report.json','desktop.log','update.log'}
RESERVED={'con','prn','aux','nul',*(f'com{i}' for i in range(1,10)),*(f'lpt{i}' for i in range(1,10))}

class UpdateError(Exception):pass
class LocalChanges(UpdateError):pass

def version_tuple(value):
    match=re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)',str(value))
    if not match:raise UpdateError('只支持正式语义版本，例如 v1.1.0')
    return tuple(map(int,match.groups()))

def sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()

def atomic_json(path,value):
    path=Path(path);temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temp,path)

def release_asset_name(version):return f'WhiteDragon-v{".".join(map(str,version_tuple(version)))}-windows-x64.zip'

@dataclass(frozen=True)
class Release:
    version:str
    notes:str
    page_url:str
    archive_url:str
    checksum_url:str
    archive_name:str

def _github_download(url,version,filename):
    parsed=urlparse(url)
    expected=f'/{REPOSITORY}/releases/download/v{version}/{filename}'
    if parsed.scheme!='https' or parsed.netloc!='github.com' or parsed.path!=expected or parsed.query or parsed.fragment:
        raise UpdateError('发行附件地址与固定仓库或版本不符')
    return url

def parse_release(data):
    if not isinstance(data,dict) or data.get('draft') or data.get('prerelease'):raise UpdateError('该发行版不是正式版本')
    version='.'.join(map(str,version_tuple(data.get('tag_name'))))
    name=release_asset_name(version);assets={a.get('name'):a for a in data.get('assets',[]) if isinstance(a,dict)}
    if name not in assets or name+'.sha256' not in assets:raise UpdateError('最新发行版缺少更新包或SHA256文件')
    page=f'{RELEASES_URL}/tag/v{version}'
    if data.get('html_url')!=page:raise UpdateError('发行说明地址与固定仓库不符')
    for key in [name,name+'.sha256']:
        if assets[key].get('state') not in (None,'uploaded'):raise UpdateError('发行附件尚未上传完成')
    return Release(version,str(data.get('body') or '')[:30000],page,
        _github_download(assets[name].get('browser_download_url',''),version,name),
        _github_download(assets[name+'.sha256'].get('browser_download_url',''),version,name+'.sha256'),name)

def _request(url):
    return Request(url,headers={'Accept':'application/vnd.github+json','User-Agent':f'WhiteDragonDesktop/{APP_VERSION}','X-GitHub-Api-Version':'2022-11-28'})

def _check_redirect(response):
    parsed=urlparse(response.geturl())
    if parsed.scheme!='https' or parsed.hostname not in {'api.github.com','github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'}:
        raise UpdateError('下载重定向到了非GitHub地址')

def fetch_latest(opener=urlopen):
    try:
        with opener(_request(LATEST_API),timeout=15) as response:
            _check_redirect(response);body=response.read(2*1024*1024+1)
            if len(body)>2*1024*1024:raise UpdateError('发行信息过大')
        return parse_release(json.loads(body))
    except HTTPError as exc:
        if exc.code==404:raise UpdateError('此仓库尚无可用的正式发行版') from exc
        if exc.code in {403,429}:
            # Public, publisher-generated metadata attachment; no credential or
            # token is requested or read. Identical fixed-repo validation.
            try:
                with opener(_request(LATEST_METADATA),timeout=15) as response:
                    _check_redirect(response);body=response.read(2*1024*1024+1)
                    if len(body)>2*1024*1024:raise UpdateError('发行信息过大')
                return parse_release(json.loads(body))
            except Exception as fallback:raise UpdateError('GitHub检查暂时不可用，请稍后再试') from fallback
        raise UpdateError(f'GitHub检查失败（{exc.code}）') from exc

def checksum_value(text,filename):
    # Standard sha256sum format, exactly the requested attachment (not a generic
    # multi-asset checksum list whose first entry could belong to another file).
    match=re.fullmatch(r'\s*([0-9a-fA-F]{64})[ \t]+\*?([^\r\n]+)\s*',text)
    if not match or match.group(2).strip()!=filename:raise UpdateError('SHA256文件格式或对应文件名不符')
    return match.group(1).lower()

def download_release(release,job,opener=urlopen):
    job=Path(job);job.mkdir(parents=True,exist_ok=True)
    with opener(_request(release.checksum_url),timeout=20) as response:
        _check_redirect(response);checksum=response.read(4097)
        if len(checksum)>4096:raise UpdateError('SHA256文件过大')
    expected=checksum_value(checksum.decode('utf-8'),release.archive_name)
    archive=job/release.archive_name;temp=archive.with_suffix('.part');total=0
    try:
        with opener(_request(release.archive_url),timeout=30) as response,temp.open('xb') as stream:
            _check_redirect(response)
            length=response.headers.get('Content-Length')
            if length and int(length)>MAX_DOWNLOAD:raise UpdateError('更新包超过大小限制')
            while True:
                chunk=response.read(1024*1024)
                if not chunk:break
                total+=len(chunk)
                if total>MAX_DOWNLOAD:raise UpdateError('更新包超过大小限制')
                stream.write(chunk)
        if sha256(temp)!=expected:raise UpdateError('更新包SHA256校验失败，未安装')
        os.replace(temp,archive)
    finally:temp.unlink(missing_ok=True)
    return archive,expected

def safe_relative(name):
    if not isinstance(name,str) or not name or '\\' in name or ':' in name or '\x00' in name:
        raise UpdateError('更新包包含不安全路径')
    path=PurePosixPath(name)
    if path.is_absolute() or name.startswith('/') or any(part in {'','.','..'} for part in name.split('/')):
        raise UpdateError('更新包包含路径穿越')
    for part in path.parts:
        if part!=part.rstrip(' .') or part.split('.')[0].casefold() in RESERVED:raise UpdateError('更新包包含Windows保留路径')
    return path

def allowed_managed(name):
    path=safe_relative(name);first=path.parts[0]
    if first in {'WhiteDragon','assets'} and len(path.parts)>1:return True
    return len(path.parts)==1 and name.casefold()!='release.json' and name.casefold() not in MUTABLE_NAMES and not name.startswith('.') and path.suffix.lower() in {'.py','.md','.txt','.json'}

def read_manifest(root,expected_version=None):
    path=Path(root)/'release.json'
    if not path.is_file() or path.stat().st_size>4*1024*1024:raise UpdateError('缺少有效的release.json')
    value=json.loads(path.read_text(encoding='utf-8'))
    if value.get('schema')!=1 or value.get('product')!=PRODUCT or value.get('repo')!=REPOSITORY:raise UpdateError('更新包产品或仓库不符')
    version='.'.join(map(str,version_tuple(value.get('version'))))
    if expected_version and version_tuple(version)!=version_tuple(expected_version):raise UpdateError('更新包版本与发行版本不符')
    files=value.get('files')
    if not isinstance(files,dict) or not files or len(files)>MAX_MEMBERS:raise UpdateError('更新文件清单无效')
    folded=set()
    for name,digest in files.items():
        if not allowed_managed(name) or not re.fullmatch('[0-9a-f]{64}',str(digest)):raise UpdateError('更新文件清单路径或哈希无效')
        if name.casefold() in folded:raise UpdateError('更新文件清单有大小写冲突')
        folded.add(name.casefold())
    required={'WhiteDragon/WhiteDragon.exe','assets/manifest.json','assets/portrait.png','desktop.py','windows_link.py','codex_bridge.py','bridge_ui.py','panel_visibility.py','updates.py','update_ui.py'}
    if not required.issubset(files):raise UpdateError('更新包缺少程序、素材或同版源码')
    return value

def validate_payload(root,expected_version=None,*,exact=True):
    root=Path(root).resolve();manifest=read_manifest(root,expected_version)
    expected=set(manifest['files'])|{'release.json'}
    for name,digest in manifest['files'].items():
        path=root.joinpath(*PurePosixPath(name).parts)
        for parent in [path,*path.parents]:
            if parent==root:break
            if parent.is_symlink() or (hasattr(parent,'is_junction') and parent.is_junction()):raise UpdateError('发行目录不能包含链接或目录联接')
        if not path.is_file() or path.is_symlink() or sha256(path)!=digest:raise UpdateError('文件哈希或类型不符：'+name)
    if exact:
        actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
        if actual!=expected:raise UpdateError('更新包的文件与完整清单不一致')
    assets=json.loads((root/'assets/manifest.json').read_text(encoding='utf-8'))
    if not isinstance(assets.get('states'),dict) or 'idle' not in assets['states']:raise UpdateError('动画manifest无有效待机')
    for entry in assets['states'].values():
        for chunk in entry.get('chunks',[]):
            safe_relative(chunk)
            if 'assets/'+chunk not in manifest['files']:raise UpdateError('动画manifest引用了清单外素材')
    return manifest

def extract_verified(archive,destination,version):
    destination=Path(destination)
    if destination.exists():raise UpdateError('解压目标必须为新的空目录')
    entries=[];seen=set();size=0
    with zipfile.ZipFile(archive) as bundle:
        if len(bundle.infolist())>MAX_MEMBERS:raise UpdateError('更新包文件数量超限')
        for item in bundle.infolist():
            name=item.filename[:-1] if item.is_dir() else item.filename;path=safe_relative(name)
            key=name.casefold()
            if key in seen:raise UpdateError('更新包含重复或大小写冲突路径')
            seen.add(key)
            mode=(item.external_attr>>16)&0xffff
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0,stat.S_IFREG,stat.S_IFDIR}) or item.flag_bits&1:raise UpdateError('更新包不能含链接、特殊或加密文件')
            if not item.is_dir() and name!='release.json' and not allowed_managed(name):raise UpdateError('更新包尝试覆盖本地设置或非发行目录')
            if item.is_dir() and path.parts[0] not in {'WhiteDragon','assets'}:raise UpdateError('更新包含非发行目录')
            size+=item.file_size
            if size>MAX_UNPACKED:raise UpdateError('更新包解压大小超限')
            entries.append((item,path))
        # A file cannot also be an ancestor of another file (Windows ambiguity).
        files={str(path).casefold() for item,path in entries if not item.is_dir()}
        for _,path in entries:
            if any(str(parent).casefold() in files for parent in path.parents if str(parent)!='.'):raise UpdateError('更新包路径类型冲突')
        destination.mkdir(parents=True)
        try:
            for item,path in entries:
                target=destination.joinpath(*path.parts)
                if item.is_dir():target.mkdir(parents=True,exist_ok=True);continue
                target.parent.mkdir(parents=True,exist_ok=True)
                with bundle.open(item) as source,target.open('xb') as output:shutil.copyfileobj(source,output,1024*1024)
            return validate_payload(destination,version)
        except BaseException:
            # Only remove the newly created extraction directory, never an
            # installation or a caller-supplied pre-existing directory.
            shutil.rmtree(destination);raise

def _git_guard(base):
    base=Path(base).resolve()
    if not any((parent/'.git').exists() for parent in [base,*base.parents]):return
    git=shutil.which('git')
    if not git:raise LocalChanges('此目录位于Git工作副本中，但无法检查改动；请手动更新')
    result=subprocess.run([git,'--no-optional-locks','-C',str(base),'status','--porcelain','--untracked-files=normal'],capture_output=True,text=True,timeout=15,creationflags=0x08000000 if os.name=='nt' else 0)
    if result.returncode or result.stdout.strip():raise LocalChanges('Git工作副本含本地改动或无法验证；更新已停止，请先处理改动')

def guard_local_changes(base,new_manifest):
    base=Path(base).resolve();_git_guard(base)
    try:old=read_manifest(base)
    except (OSError,ValueError,UpdateError) as exc:raise LocalChanges('当前安装缺少发行基线，无法保护本地源码改动；请手动安装完整发行包') from exc
    for name,digest in old['files'].items():
        path=base.joinpath(*PurePosixPath(name).parts)
        if not path.is_file() or path.is_symlink() or sha256(path)!=digest:raise LocalChanges('检测到本地发行文件已修改：'+name)
    for directory in ['WhiteDragon','assets']:
        actual={p.relative_to(base).as_posix() for p in (base/directory).rglob('*') if p.is_file()}
        if actual!={name for name in old['files'] if name.startswith(directory+'/')}:raise LocalChanges('运行目录或素材含本地新增文件：'+directory)
    for name in new_manifest['files']:
        if name not in old['files'] and (base/name).exists():raise LocalChanges('新发行文件与本地文件冲突：'+name)
    return old

def process_path(pid):
    if os.name!='nt':return None
    from ctypes import wintypes as w
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
    k.QueryFullProcessImageNameW.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,ctypes.POINTER(w.DWORD)]
    k.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];k.CloseHandle.argtypes=[w.HANDLE]
    handle=k.OpenProcess(0x101000,False,int(pid))
    if not handle:
        if ctypes.get_last_error()==5:raise UpdateError('无法确认桌宠进程身份，已停止更新')
        return None
    try:
        if k.WaitForSingleObject(handle,0)!=258:return None
        buf=ctypes.create_unicode_buffer(32768);length=w.DWORD(len(buf))
        if not k.QueryFullProcessImageNameW(handle,0,buf,ctypes.byref(length)):raise UpdateError('无法读取桌宠进程路径')
        return Path(buf.value).resolve()
    finally:k.CloseHandle(handle)

def update_in_progress(base):
    """Recover a stale marker only if the installed release is still complete."""
    base=Path(base).resolve();marker=base/'.update-in-progress'
    if not marker.exists():return False
    try:
        value=json.loads(marker.read_text(encoding='utf-8'))
        expected=Path(value['job']).resolve()/'helper/WhiteDragon.exe'
        if process_path(value['pid'])==expected:return True
        validate_payload(base,exact=False)
        os.replace(marker,base/('.update-abandoned-'+str(time.time_ns())+'.json'))
        atomic_json(base/'update-state.json',{'ok':False,'error':'上次更新辅助程序提前退出；已确认当前发行文件完整并保留原版，未继续安装。'})
        return False
    except Exception as exc:raise UpdateError('上次更新未完成且当前发行不完整；请保留backups目录并手动恢复。') from exc

def running_instances(base,scope):
    base=Path(base).resolve();instances=[];exe=base/'WhiteDragon/WhiteDragon.exe'
    for pattern,kind in [('.desktop-*.lock','pet'),('.codex-watcher-*.lock','watcher')]:
        prefix='.desktop-' if kind=='pet' else '.codex-watcher-'
        for lock in base.glob(pattern):
            try:pid=int(lock.read_text().splitlines()[0])
            except (OSError,ValueError,IndexError):continue
            actual=process_path(pid)
            if actual is None:continue
            if actual!=exe:raise UpdateError('桌宠锁的进程路径不符，已停止更新')
            found_scope=lock.name[len(prefix):-5]
            if found_scope!=scope:raise UpdateError('另一个Windows桌面仍在使用此安装，请先退出那里的小白龙')
            instances.append({'pid':pid,'kind':kind,'scope':scope})
    return instances

def prepare_update(base,release,scope,managed=False,opener=urlopen):
    base=Path(base).resolve()
    if not getattr(sys,'frozen',False):raise UpdateError('源码运行模式请通过Git或手动发行包更新；自动安装仅适用于打包程序')
    if Path(sys.executable).resolve()!=base/'WhiteDragon/WhiteDragon.exe':raise UpdateError('程序安装路径不符合更新要求')
    guard_local_changes(base,{'files':{}})
    job=Path(tempfile.mkdtemp(prefix='.white-dragon-update-',dir=base.parent))
    try:
        archive,digest=download_release(release,job,opener)
        stage=job/'payload';manifest=extract_verified(archive,stage,release.version)
        guard_local_changes(base,manifest)
        helper=job/'helper';shutil.copytree(base/'WhiteDragon',helper)
        # Validate the copied helper runtime too; no executable/DLL remains
        # loaded from the installation that is about to be replaced.
        old=read_manifest(base)
        for name,expected in old['files'].items():
            if name.startswith('WhiteDragon/') and sha256(helper/name[len('WhiteDragon/'):])!=expected:raise UpdateError('更新辅助程序复制校验失败')
        request={'schema':1,'base':str(base),'job':str(job),'stage':str(stage),'version':release.version,'archive':str(archive),'archive_sha256':digest,
                 'manifest_sha256':sha256(stage/'release.json'),'scope':scope,'managed':bool(managed),'instances':running_instances(base,scope)}
        atomic_json(job/'request.json',request)
        return job/'request.json'
    except BaseException:
        shutil.rmtree(job);raise

def launch_helper(request):
    request=Path(request).resolve();helper=request.parent/'helper/WhiteDragon.exe'
    return subprocess.Popen([str(helper),'--apply-update',str(request)],cwd=str(request.parent),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,creationflags=0x08000000 if os.name=='nt' else 0)

def apply_transaction(base,stage,manifest,*,health_check,replace=os.replace):
    """Atomically rename each managed unit, rolling back the whole set on error.

    Unknown root files, settings and logs stay in place. Complete runtime/assets
    directories are replaced together with their matching root source files.
    """
    base=Path(base).resolve();stage=Path(stage).resolve();old=guard_local_changes(base,manifest)
    backup=base/'backups'/f'update-{old["version"]}-to-{manifest["version"]}-{time.time_ns()}'
    backup.mkdir(parents=True)
    root_names={PurePosixPath(name).parts[0] for name in old['files']}|{PurePosixPath(name).parts[0] for name in manifest['files']}|{'release.json'}
    units=['WhiteDragon','assets']+sorted(root_names-{'WhiteDragon','assets','release.json'})+['release.json']
    moved_old=[];moved_new=[]
    try:
        for name in units:
            if (base/name).exists():replace(base/name,backup/name);moved_old.append(name)
            if (stage/name).exists():replace(stage/name,base/name);moved_new.append(name)
        validate_payload(base,manifest['version'],exact=False)
        health_check(base,manifest['version'])
    except BaseException as exc:
        failures=[]
        for name in reversed(moved_new):
            try:
                if (base/name).exists():os.replace(base/name,stage/name)
            except OSError as rollback:failures.append(str(rollback))
        for name in reversed(moved_old):
            try:
                if (backup/name).exists():os.replace(backup/name,base/name)
            except OSError as rollback:failures.append(str(rollback))
        if failures:raise UpdateError('更新失败，回退未完整完成；请保留备份：'+str(backup)+'；'+'; '.join(failures)) from exc
        raise UpdateError('更新失败，已回退旧版：'+str(exc)) from exc
    return backup

def _health_subprocess(base,version):
    report=base/'update-health-report.json';env=dict(os.environ);env['QT_QPA_PLATFORM']='offscreen'
    result=subprocess.run([str(base/'WhiteDragon/WhiteDragon.exe'),'--update-health-check',str(report)],cwd=str(base),env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30,creationflags=0x08000000 if os.name=='nt' else 0)
    if result.returncode or not report.is_file():raise UpdateError('新版本启动检查未通过')
    value=json.loads(report.read_text(encoding='utf-8'))
    if not value.get('ok') or version_tuple(value.get('version'))!=version_tuple(version):raise UpdateError('新版本版本号或素材加载检查未通过')

def _restart(base,instances,managed):
    exe=Path(base)/'WhiteDragon/WhiteDragon.exe';watcher=any(i['kind']=='watcher' for i in instances);pet=any(i['kind']=='pet' for i in instances)
    commands=[]
    if watcher:commands.append([str(exe),'--watch-codex'])
    if pet and (not watcher or not managed):commands.append([str(exe)]+(['--managed'] if managed else []))
    for command in commands:subprocess.Popen(command,cwd=str(base),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,creationflags=0x08000000 if os.name=='nt' else 0)

def apply_update_cli(arguments):
    parser=argparse.ArgumentParser();parser.add_argument('--apply-update',required=True,type=Path);args=parser.parse_args(arguments)
    request_path=args.apply_update.resolve();job=request_path.parent
    result_path=job/'result.json';base=None;instances=[];stopped=False;marker=None
    try:
        request=json.loads(request_path.read_text(encoding='utf-8'))
        base=Path(request['base']).resolve();stage=Path(request['stage']).resolve();archive=Path(request['archive']).resolve()
        if request.get('schema')!=1 or Path(request['job']).resolve()!=job or not job.name.startswith('.white-dragon-update-') or job.parent!=base.parent or stage!=job/'payload' or archive.parent!=job:
            raise UpdateError('更新交接路径无效')
        if Path(sys.executable).resolve()!=job/'helper/WhiteDragon.exe':raise UpdateError('必须从独立复制的辅助运行目录更新')
        if sha256(archive)!=request['archive_sha256'] or sha256(stage/'release.json')!=request['manifest_sha256']:raise UpdateError('更新交接文件校验失败')
        manifest=validate_payload(stage,request['version']);old=guard_local_changes(base,manifest)
        if version_tuple(manifest['version'])<=version_tuple(old['version']):raise UpdateError('拒绝同版本覆盖或降级')
        instances=running_instances(base,request['scope'])
        if {(i['pid'],i['kind']) for i in instances}!={(i['pid'],i['kind']) for i in request['instances']}:raise UpdateError('桌宠运行实例在更新期间发生变化，请重试')
        candidate=base/'.update-in-progress'
        try:
            with candidate.open('x',encoding='utf-8') as stream:json.dump({'pid':os.getpid(),'job':str(job),'time':time.time()},stream)
        except FileExistsError as exc:raise UpdateError('已有更新操作或未完成的更新记录，请先查看update-state.json') from exc
        marker=candidate
        atomic_json(job/'ready.json',{'ok':True})
        for instance in instances:
            if instance['kind']=='watcher':(base/('.codex-watcher-stop-'+instance['scope'])).touch()
        atomic_json(base/('.desktop-control-'+request['scope']+'.json'),{'action':'quit','at':time.time()})
        stopped=True;deadline=time.monotonic()+30
        while any(process_path(i['pid'])==base/'WhiteDragon/WhiteDragon.exe' for i in instances):
            if time.monotonic()>deadline:raise UpdateError('桌宠或监视器未能正常退出，未替换任何程序')
            time.sleep(.15)
        # Recheck after all old writers have stopped and before any replacement.
        guard_local_changes(base,manifest)
        backup=apply_transaction(base,stage,manifest,health_check=_health_subprocess)
        result={'ok':True,'version':manifest['version'],'backup':str(backup),'job':str(job)}
        atomic_json(base/'update-state.json',result);atomic_json(result_path,result)
        marker.unlink();marker=None
        _restart(base,instances,request['managed'])
        return 0
    except BaseException as exc:
        result={'ok':False,'error':str(exc),'job':str(job)}
        try:
            atomic_json(result_path,result)
            if base is not None:atomic_json(base/'update-state.json',result)
        except OSError:pass
        if marker is not None:marker.unlink(missing_ok=True)
        if stopped and base is not None:
            try:_restart(base,instances,request.get('managed',False))
            except OSError:pass
        return 1

def installed_health(base,report_path):
    """Called after Qt loads from the *new* install, before creating a pet."""
    try:
        validate_payload(base,APP_VERSION,exact=False)
        from PySide6.QtGui import QImage
        assets=Path(base)/'assets';value=json.loads((assets/'manifest.json').read_text(encoding='utf-8'))
        paths=[assets/'portrait.png']+[assets/chunk for entry in value['states'].values() for chunk in entry.get('chunks',[])]
        if any(QImage(str(path)).isNull() for path in paths):raise UpdateError('新版本素材无法解码')
        result={'ok':True,'version':APP_VERSION}
    except Exception as exc:result={'ok':False,'version':APP_VERSION,'error':str(exc)}
    atomic_json(report_path,result);return 0 if result['ok'] else 1
