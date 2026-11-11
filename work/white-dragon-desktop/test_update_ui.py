"""Consent and in-app notes in isolated Qt dialogs; no download or installation."""
import json,sys,tempfile,threading,time
from pathlib import Path
from unittest.mock import patch,Mock
ROOT=Path('outputs/white-dragon-desktop').resolve();sys.path.insert(0,str(ROOT))
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from desktop import Dragon
from updates import parse_release
from test_updates import release_data
from update_ui import BackgroundWorker

app=QApplication([]);manifest=json.loads((ROOT/'assets/manifest.json').read_text(encoding='utf-8'));report={}
with tempfile.TemporaryDirectory() as temp:
    base=Path(temp);(base/'CHANGELOG.md').write_text('# v1.1.0\n本地版本说明。',encoding='utf-8')
    pet=Dragon(manifest,base/'settings.json',integration=True);pet.show();pet.panel.controller.stop()
    controller=pet.updates;controller.base=base;controller.settings['automatic']=False
    with patch('update_ui.QDesktopServices.openUrl') as opened:
        controller.notes();assert '本地版本说明' in controller.notes_dialog.detailedText()
        next(b for b in controller.notes_dialog.buttons() if b.text()=='关闭').click()
        assert not opened.called
    report['version_notes_read_locally_without_browser']=True
    release=parse_release(release_data('1.2.0'))
    with patch('update_ui.prepare_update',return_value=base/'request.json') as prepare,patch('update_ui.launch_helper',return_value=Mock(poll=lambda:None)) as helper:
        controller.offer(release)
        assert not prepare.called
        next(b for b in controller.dialog.buttons() if b.text()=='稍后').click()
        QTest.qWait(30);assert not prepare.called and not helper.called
        report['decline_does_not_download_or_launch_helper']=True
        controller.offer(release)
        next(b for b in controller.dialog.buttons() if b.text()=='下载并安装').click()
        QTest.qWait(100);controller.collect()
        assert prepare.call_count==1 and helper.call_count==1
        report['explicit_acceptance_required_before_download']=True
    event=threading.Event();worker=BackgroundWorker();future=worker.submit(lambda:event.wait(2))
    assert any(t.name=='release-update' and t.daemon for t in threading.enumerate())
    worker.shutdown();event.set();future.result(timeout=3)
    report['background_network_worker_does_not_hold_process_open']=True
    pet.close()
report['ok']=True
(ROOT/'update-ui-test-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report,indent=2))
