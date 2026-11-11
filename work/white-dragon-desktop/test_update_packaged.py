"""Real copied EXE helper: normal shutdown, DLL replacement, health and rollback.

All installs and baseline 1.0.9 are isolated local fixtures; nothing is published
and the user product installation/desktop is never stopped or updated.
"""
import json,os,shutil,subprocess,sys,tempfile,time,zipfile
from pathlib import Path
ROOT=Path('outputs/white-dragon-desktop').resolve();sys.path.insert(0,str(ROOT))
import updates as u
from desktop import desktop_scope
BUILD=Path('work/white-dragon-desktop/update-release/staging/WhiteDragon').resolve()
WORK=Path('work/white-dragon-desktop/update-release').resolve()
FLAGS=0x08000000;scope=desktop_scope()

def until(fn,timeout=15):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=fn()
        if value:return value
        time.sleep(.1)
    raise AssertionError('Timed out: '+repr(fn))

def make_payload(destination,version):
    destination.mkdir();shutil.copytree(BUILD,destination/'WhiteDragon')
    assets=json.loads((ROOT/'assets/manifest.json').read_text(encoding='utf-8'))
    names={'manifest.json','portrait.png'}|{name for entry in assets['states'].values() for name in entry.get('chunks',[])}
    for name in names:
        target=destination/'assets'/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/'assets'/name,target)
    for source in ROOT.glob('*.py'):shutil.copy2(source,destination/source.name)
    (destination/'CHANGELOG.md').write_text('Isolated updater fixture.',encoding='utf-8')
    return manifest(destination,version)

def manifest(root,version):
    files={p.relative_to(root).as_posix():u.sha256(p) for p in root.rglob('*') if p.is_file() and p.name!='release.json'}
    value={'schema':1,'product':u.PRODUCT,'repo':u.REPOSITORY,'version':version,'files':files}
    u.atomic_json(root/'release.json',value);return value

def stop_fixture(base):
    (base/('.codex-watcher-stop-'+scope)).touch()
    u.atomic_json(base/('.desktop-control-'+scope+'.json'),{'action':'quit','at':time.time()})
    until(lambda:not u.running_instances(base,scope),15)

report={}
with tempfile.TemporaryDirectory(prefix='updater-packaged-',dir=WORK) as temp:
    root=Path(temp)
    for mode in ['healthy','rollback']:
        base=root/('install-'+mode);old=make_payload(base,'1.0.9')
        (base/'settings.json').write_text('{"width":180,"autoplay":false}',encoding='utf-8')
        (base/'update-settings.json').write_text('{"automatic":false}',encoding='utf-8')
        (base/'desktop.log').write_text('preserve this local log\n',encoding='utf-8')
        (base/'personal-note.txt').write_text('preserve this note',encoding='utf-8')
        pet=subprocess.Popen([str(base/'WhiteDragon/WhiteDragon.exe')],creationflags=FLAGS)
        watcher=subprocess.Popen([str(base/'WhiteDragon/WhiteDragon.exe'),'--watch-codex'],creationflags=FLAGS)
        until(lambda:len(u.running_instances(base,scope))==2)
        job=Path(tempfile.mkdtemp(prefix='.white-dragon-update-',dir=root));stage=job/'payload'
        make_payload(stage,'1.1.0')
        if mode=='rollback':
            (stage/'assets/portrait.png').write_bytes(b'Controlled invalid PNG to fail actual new-EXE QImage health check')
            manifest(stage,'1.1.0')
        archive=job/u.release_asset_name('1.1.0')
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_STORED) as bundle:
            for path in stage.rglob('*'):
                if path.is_file():bundle.write(path,path.relative_to(stage).as_posix())
        shutil.copytree(base/'WhiteDragon',job/'helper')
        request={'schema':1,'base':str(base),'job':str(job),'stage':str(stage),'version':'1.1.0','archive':str(archive),'archive_sha256':u.sha256(archive),
                 'manifest_sha256':u.sha256(stage/'release.json'),'scope':scope,'managed':False,'instances':u.running_instances(base,scope)}
        u.atomic_json(job/'request.json',request)
        helper=u.launch_helper(job/'request.json')
        try:
            helper.wait(timeout=60)
            value=json.loads((job/'result.json').read_text(encoding='utf-8'))
            assert value['ok']==(mode=='healthy'),value
            assert pet.wait(timeout=10)==0 and watcher.wait(timeout=10)==0
            until(lambda:len(u.running_instances(base,scope))==2)
            expected='1.1.0' if mode=='healthy' else '1.0.9'
            u.validate_payload(base,expected,exact=False)
            assert not (base/'.update-in-progress').exists()
            assert json.loads((base/'settings.json').read_text())['width']==180
            assert (base/'desktop.log').read_text(encoding='utf-8').startswith('preserve this local log')
            assert (base/'personal-note.txt').read_text()=='preserve this note'
            report[mode]={'helper_exit':helper.returncode,'installed_version':expected,'old_pet_and_watcher_exited_normally':True,
                          'copied_helper_runtime_replaced_original_exe_and_dlls':True,'pet_and_watcher_restored':True,'settings_logs_unmanaged_files_preserved':True,
                          'actual_new_exe_health_check':json.loads((base/'update-health-report.json').read_text(encoding='utf-8'))}
        finally:
            stop_fixture(base)
report['real_product_deployed']=False;report['real_user_codex_stopped']=False;report['ok']=True
(ROOT/'update-packaged-test-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report,indent=2))
