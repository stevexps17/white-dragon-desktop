# 小白龙桌宠

Windows 透明桌面宠物，显示本机 Codex 聊天的中文任务进度，并跟随客户端主窗口启动和退出。

## 使用

从本仓库 Releases 下载 `WhiteDragon-v版本-windows-x64.zip`，解压到可写目录，打开 `WhiteDragon/WhiteDragon.exe`。保留同级 assets 和源码文件。右键菜单可以检查更新、查看版本说明、调整显示和开启登录后随 Codex 启动。

应用启动后和运行期间每六小时在后台检查本仓库的正式 Release，也可右键手动检查。确认更新后下载 ZIP、校验 SHA256 和逐文件清单，备份旧版、替换程序及随包源码，再恢复运行。更新错误时显示结果，失败可回退。个人设置独立保存。本地源码修改或脏 Git 工作副本需要先处理，再安装更新。推送源码后，维护者发布新的版本标签和 Release，客户端才会收到版本提示。

## 当前素材与功能

待机使用连续视频的 117 帧，原生 24 FPS，窗口默认 60 FPS 刷新。挥手、轻跳、工作、等待、完成与受阻尚待合格独立素材，现有交互为整体运动反馈。任务文字、活动列表、提醒和聊天跳转已接入。

任务读取使用本机 Codex 的 SQLite 和会话事件文件，属于版本相关适配。语音入口打开 Codex 聊天，使用原生麦克风。套餐额度显示尚待实现。

## 开发与发布

Python 3.12，依赖见 requirements.txt。源码在 outputs/white-dragon-desktop，构建和测试在 work/white-dragon-desktop。

```powershell
python -m pip install -r requirements.txt
python outputs/white-dragon-desktop/desktop.py
python -m PyInstaller --noconfirm --distpath dist --workpath build work/white-dragon-desktop/WhiteDragon.spec
python work/white-dragon-desktop/package_release.py --runtime dist/WhiteDragon --output release --version 1.1.0
```

修改 updates.py 的 APP_VERSION 和 CHANGELOG.md，提交后推送相同版本的 v 标签。GitHub Actions 会构建 Windows 程序、验证基本运行并发布 ZIP 与校验文件。标签必须与版本常量一致。发布前应验证退出、状态栏及动画衔接。

## 参考

动作制作参考 dsh-pet 的连续视频流程。本仓库角色素材来自本项目提供的参考和生成素材，使用或再分发时需自行确认素材权利。源码参考链接见下方。

https://github.com/PC2005-cloud/dsh-pet
