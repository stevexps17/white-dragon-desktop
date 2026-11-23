# 小白龙桌宠：dsh-pet 对照与验证

参照 [dsh-pet 上游](https://github.com/PC2005-cloud/dsh-pet/tree/fb38ce620576f5ed3735a99536d2b1a4a9eb313f)，而非二次移植仓库。小白龙继续作为 Codex 原生对话入口，不另起一套聊天、语音、记忆或余额服务，不复制上游代码和角色素材。

| 对照项目 | 取舍 |
| --- | --- |
| 状态动作与连续动画 | 先让工作、待确认、完成、受阻可区分。复用白龙原始帧，仅做整体位移与旋转。完整独立素材、待机循环接缝精修留待同一角色设定制作。 |
| 独立运行 | 增加 `--standalone`，可独立演示，不读取 Codex、不随 Codex 退出、不检查更新。双击默认仍是联动模式。 |
| 多屏与掉屏恢复 | 拖到空隙时选择最近工作区，屏幕移除或工作区变化时重新约束位置。实际异构 DPI、Windows 原生遮罩需在有对应设备的 Windows 上验收。 |
| 播放性能 | 24 FPS 原始待机按素材频率绘制，交互保持所选帧率；暂停、隐藏和静态不跑绘制定时器。先不替换底层播放器。 |
| 提醒 | 短暂扫描失败不清空最后一次有效通知基线，重连同一任务不再重报。 |
| 下一轮 | 启动与多次切换应节制解码；按“新素材首帧就绪再换”重构缓存前先采样帧耗时。透明区域命中需验证 Windows 窗口点击模型，不能误裁剪动画。 |

上游参考：[动画切换](https://github.com/PC2005-cloud/dsh-pet/blob/fb38ce620576f5ed3735a99536d2b1a4a9eb313f/dsh-pet/src/client/pet/animation/animation.ts)、[多屏工作区](https://github.com/PC2005-cloud/dsh-pet/blob/fb38ce620576f5ed3735a99536d2b1a4a9eb313f/dsh-pet/src/shared/displays.ts)、[视觉尺寸与命中区](https://github.com/PC2005-cloud/dsh-pet/blob/fb38ce620576f5ed3735a99536d2b1a4a9eb313f/dsh-pet/src/shared/pet-size.ts)。

验收边界：云端 Linux/Qt 离屏可验证绘制、状态和窗口生命周期。Windows 验证工作流只构建测试，不发布；真实托盘、多显示器和本机 Codex 生命周期不能仅凭离屏测试判定通过。测试使用合成事件，不读取真实会话。
