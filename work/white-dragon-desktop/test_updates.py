"""Offline updater contract, adversarial ZIPs, local changes and rollback tests."""
import copy,ctypes,hashlib,io,json,os,shutil,stat,subprocess,sys,tempfile,time,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
ROOT=Path('outputs/white-dragon-desktop').resolve();sys.path.insert(0,str(ROOT))
import updates as u

REQUIRED=['desktop.py','windows_link.py','codex_bridge.py','bridge_ui.py','panel_visibility.py','updates.py','update_ui.py']
def fixture(root,version='1.0.9',content=b'old'):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    files={'WhiteDragon/WhiteDragon.exe':content,'WhiteDragon/_internal/runtime.dll':content,'assets/portrait.png':content,
           'assets/manifest.json':json.dumps({'states':{'idle':{'mode':'portrait','frames':1}}}).encode(),'CHANGELOG.md':content}
    files.update({name:content for name in REQUIRED})
    for name,data in files.items():
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    value={'schema':1,'product':u.PRODUCT,'repo':u.REPOSITORY,'version':version,'files':{name:hashlib.sha256(data).hexdigest() for name,data in files.items()}}
    u.atomic_json(root/'release.json',value);return value

def zipped(root):
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w',zipfile.ZIP_DEFLATED) as bundle:
        for path in Path(root).rglob('*'):
            if path.is_file():bundle.write(path,path.relative_to(root).as_posix())
    return data.getvalue()

def release_data(version='1.1.0'):
    name=u.release_asset_name(version)
    return {'tag_name':'v'+version,'html_url':u.RELEASES_URL+'/tag/v'+version,'draft':False,'prerelease':False,'body':'notes','assets':[
        {'name':key,'state':'uploaded','browser_download_url':f'https://github.com/{u.REPOSITORY}/releases/download/v{version}/{key}'} for key in [name,name+'.sha256']]}

class Response(io.BytesIO):
    def __init__(self,data,url):super().__init__(data);self.url=url;self.headers={'Content-Length':str(len(data))}
    def geturl(self):return self.url

class UpdatesTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_release_and_semver(self):
        self.assertEqual(u.version_tuple('v1.10.2'),(1,10,2));self.assertGreater(u.version_tuple('1.10.0'),u.version_tuple('1.9.9'))
        for bad in ['1.1','v1.1.0-rc1','01.1.0','1.1.0+meta']:
            with self.assertRaises(u.UpdateError):u.version_tuple(bad)
        good=release_data();self.assertEqual(u.parse_release(good).version,'1.1.0')
        for mutate in [lambda d:d.update(prerelease=True),lambda d:d.update(html_url='https://evil.test'),lambda d:d['assets'][0].update(browser_download_url='https://evil.test/pkg'),lambda d:d.update(assets=[])]:
            data=copy.deepcopy(good);mutate(data)
            with self.assertRaises(u.UpdateError):u.parse_release(data)

    def test_api_rate_limit_uses_public_metadata_without_token(self):
        from urllib.error import HTTPError
        calls=[]
        def opener(request,timeout):
            calls.append(request.full_url)
            self.assertNotIn('Authorization',request.headers)
            if request.full_url==u.LATEST_API:raise HTTPError(request.full_url,403,'rate limit',{},None)
            return Response(json.dumps(release_data()).encode(),request.full_url)
        self.assertEqual(u.fetch_latest(opener).version,'1.1.0')
        self.assertEqual(calls,[u.LATEST_API,u.LATEST_METADATA])

    def test_unpack_limit_rejected_before_writing(self):
        archive=self.root/'oversize.zip'
        with zipfile.ZipFile(archive,'w') as bundle:bundle.writestr('assets/large.png',b'0123456789')
        with patch.object(u,'MAX_UNPACKED',5),self.assertRaises(u.UpdateError):u.extract_verified(archive,self.root/'unpacked','1.1.0')
        self.assertFalse((self.root/'unpacked').exists())

    def test_verified_download_and_exact_payload(self):
        source=self.root/'source';fixture(source,'1.1.0',b'new');data=zipped(source);release=u.parse_release(release_data())
        checksum=(hashlib.sha256(data).hexdigest()+'  '+release.archive_name+'\n').encode();calls=[]
        def opener(request,timeout):
            calls.append(request.full_url);return Response(checksum if request.full_url.endswith('.sha256') else data,request.full_url)
        archive,digest=u.download_release(release,self.root/'job',opener)
        self.assertEqual(len(calls),2);self.assertEqual(digest,u.sha256(archive))
        manifest=u.extract_verified(archive,self.root/'extracted','1.1.0');self.assertEqual(manifest['version'],'1.1.0')
        self.assertIn('CHANGELOG.md',manifest['files'])

    def test_bad_checksum_no_install(self):
        release=u.parse_release(release_data())
        def opener(request,timeout):return Response((b'0'*64+b'  '+release.archive_name.encode()) if request.full_url.endswith('.sha256') else b'bad zip',request.full_url)
        with self.assertRaisesRegex(u.UpdateError,'SHA256'):u.download_release(release,self.root/'job',opener)
        self.assertFalse((self.root/'job'/release.archive_name).exists());self.assertFalse(list((self.root/'job').glob('*.part')))
        for bad in ['0'*64,'0'*64+'  wrong.zip','0'*64+'  file.zip\n'+'1'*64+'  more.zip']:
            with self.assertRaises(u.UpdateError):u.checksum_value(bad,'file.zip')

    def test_zip_paths_links_duplicates_and_protected_files(self):
        cases=['../outside.py','WhiteDragon/../../outside.py','C:/bad.py','\\\\host\\x','assets\\x','/absolute.py','assets/NUL.png','assets/file.png:stream','assets/trailing.','settings.json','Settings.JSON','Update-State.JSON','update-state.json','backups/file.txt','.git/config']
        for i,name in enumerate(cases):
            archive=self.root/f'{i}.zip'
            with zipfile.ZipFile(archive,'w') as bundle:bundle.writestr(name,b'bad')
            with self.assertRaises(u.UpdateError,msg=name):u.extract_verified(archive,self.root/f'dest{i}','1.1.0')
        for i,kind in enumerate(['symlink','case','parent-file']):
            archive=self.root/f'attack-{i}.zip'
            with zipfile.ZipFile(archive,'w') as bundle:
                if kind=='symlink':
                    item=zipfile.ZipInfo('assets/link');item.create_system=3;item.external_attr=(stat.S_IFLNK|0o777)<<16;bundle.writestr(item,b'../../outside')
                elif kind=='case':bundle.writestr('assets/A.png',b'one');bundle.writestr('assets/a.png',b'two')
                else:bundle.writestr('assets/file.py',b'one');bundle.writestr('assets/file.py/child.py',b'two')
            with self.assertRaises(u.UpdateError):u.extract_verified(archive,self.root/f'attackdest{i}','1.1.0')
        self.assertFalse((self.root.parent/'outside.py').exists())

    def test_manifest_mismatch_extra_files_and_missing_assets(self):
        source=self.root/'source';manifest=fixture(source,'1.1.0',b'new')
        (source/'desktop.py').write_bytes(b'changed')
        with self.assertRaises(u.UpdateError):u.validate_payload(source)
        (source/'desktop.py').write_bytes(b'new');(source/'unexpected.py').write_bytes(b'extra')
        with self.assertRaises(u.UpdateError):u.validate_payload(source)
        (source/'unexpected.py').unlink()
        (source/'assets/manifest.json').write_text(json.dumps({'states':{'idle':{'chunks':['../outside.png']}}}))
        manifest['files']['assets/manifest.json']=u.sha256(source/'assets/manifest.json');u.atomic_json(source/'release.json',manifest)
        with self.assertRaises(u.UpdateError):u.validate_payload(source)

    def test_local_source_and_extra_asset_guard(self):
        base=self.root/'base';fixture(base);manifest=fixture(self.root/'new','1.1.0',b'new')
        (base/'settings.json').write_text('personal');(base/'desktop.log').write_text('local log');(base/'my-notes.md').write_text('unmanaged')
        u.guard_local_changes(base,manifest)
        (base/'desktop.py').write_bytes(b'edited')
        with self.assertRaises(u.LocalChanges):u.guard_local_changes(base,manifest)
        (base/'desktop.py').write_bytes(b'old');(base/'assets/user.png').write_bytes(b'user')
        with self.assertRaises(u.LocalChanges):u.guard_local_changes(base,manifest)

    @unittest.skipUnless(shutil.which('git'),'git not available')
    def test_git_dirty_guard(self):
        base=self.root/'repo';fixture(base);new=fixture(self.root/'new','1.1.0',b'new')
        def git(*args):subprocess.run(['git','-C',str(base),*args],check=True,capture_output=True)
        git('init');git('add','.');git('-c','user.name=Updater Test','-c','user.email=test@example.invalid','commit','-m','fixture')
        u.guard_local_changes(base,new)
        (base/'untracked.py').write_text('work in progress')
        with self.assertRaisesRegex(u.LocalChanges,'Git'):u.guard_local_changes(base,new)

    def test_transaction_updates_source_assets_runtime_preserves_settings(self):
        base=self.root/'base';old=fixture(base);stage=self.root/'payload';new=fixture(stage,'1.1.0',b'new')
        for name in ['settings.json','desktop.log','update-settings.json']:(base/name).write_text('personal')
        backup=u.apply_transaction(base,stage,new,health_check=lambda base,version:u.validate_payload(base,version,exact=False))
        self.assertEqual(u.read_manifest(base)['version'],'1.1.0');self.assertEqual((base/'desktop.py').read_bytes(),b'new')
        self.assertEqual((backup/'desktop.py').read_bytes(),b'old')
        self.assertTrue(all((base/name).read_text()=='personal' for name in ['settings.json','desktop.log','update-settings.json']))

    def test_mid_transaction_failure_and_health_failure_roll_back_every_unit(self):
        for mode in ['move','health']:
            base=self.root/('base-'+mode);old=fixture(base);stage=self.root/('stage-'+mode);new=fixture(stage,'1.1.0',b'new')
            def replace(source,target):
                if mode=='move' and Path(source)==stage/'desktop.py':raise OSError('controlled replacement failure')
                return os.replace(source,target)
            def health(*_):
                if mode=='health':raise u.UpdateError('controlled startup failure')
            with self.assertRaisesRegex(u.UpdateError,'已回退'):u.apply_transaction(base,stage,new,health_check=health,replace=replace)
            u.validate_payload(base,'1.0.9',exact=False)
            self.assertEqual((base/'desktop.py').read_bytes(),b'old');self.assertEqual((base/'assets/portrait.png').read_bytes(),b'old')

    @unittest.skipUnless(os.name=='nt','Windows lock semantics')
    def test_windows_locked_runtime_refuses_swap_then_succeeds_after_exit(self):
        from ctypes import wintypes as w
        base=self.root/'base';fixture(base);stage=self.root/'stage';new=fixture(stage,'1.1.0',b'new')
        k=ctypes.WinDLL('kernel32',use_last_error=True);k.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE];k.CreateFileW.restype=w.HANDLE;k.CloseHandle.argtypes=[w.HANDLE]
        handle=k.CreateFileW(str(base/'WhiteDragon'),0x80000000,1|2,None,3,0x02000000,None)
        self.assertNotIn(handle,[None,ctypes.c_void_p(-1).value])
        try:
            with self.assertRaisesRegex(u.UpdateError,'已回退'):u.apply_transaction(base,stage,new,health_check=lambda *_:None)
            u.validate_payload(base,'1.0.9',exact=False)
        finally:k.CloseHandle(handle)
        u.apply_transaction(base,stage,new,health_check=lambda *_:None)
        u.validate_payload(base,'1.1.0',exact=False)

    def test_stale_marker_recovers_only_complete_install(self):
        base=self.root/'base';fixture(base)
        u.atomic_json(base/'.update-in-progress',{'pid':123,'job':str(self.root/'job')})
        with patch.object(u,'process_path',return_value=None):self.assertFalse(u.update_in_progress(base))
        self.assertFalse((base/'.update-in-progress').exists());self.assertTrue(list(base.glob('.update-abandoned-*')))
        u.atomic_json(base/'.update-in-progress',{'pid':123,'job':str(self.root/'job')});(base/'desktop.py').write_bytes(b'partial')
        with patch.object(u,'process_path',return_value=None),self.assertRaises(u.UpdateError):u.update_in_progress(base)


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(UpdatesTests))
    (ROOT/'update-source-test-report.json').write_text(json.dumps({'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'ok':result.wasSuccessful(),'real_public_release_download_tested':False,'product_deployed':False},indent=2),encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
