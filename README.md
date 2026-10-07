# 奶蛙 · Naiwa

Windows 桌面上的孟菲斯像素奶蛙，显示 Cursor Agent 和 VS Code Codex 的运行状态。多个会话各有一支手和工作区标签，左侧 Cursor、右侧 Codex；提示栏分成两列，区分工作、工具、等你、完成和出错。无任务时侧躺，支持拖动、记住位置和持续显示详情。

## 直接安装

需要 **Windows、64 位 Python 3.11 或以上**和联网安装依赖。动画资源已经包含在 wheel 中，不需要源码、conda 或额外下载素材。

在 PowerShell 中运行：

```powershell
python -m pip install https://github.com/ybenzou/naiwa/releases/download/v0.8.9/naiwa-0.8.9-py3-none-any.whl
naiwa install
naiwa-desktop
```

也可从 [Releases](https://github.com/ybenzou/naiwa/releases) 下载 wheel，然后运行 `python -m pip install ./naiwa-0.8.9-py3-none-any.whl`。

`naiwa install` 合并并备份当前用户的 IDE 钩子配置。Codex 用户级钩子需要在 `/hooks` 中审查和信任；是否接入以扩展真实回合的新事件为准。仅使用一边时，运行 `naiwa install --source cursor` 或 `naiwa install --source codex`。

找不到 `naiwa` 命令时，用 `python -m naiwa install` 接入、`python -m naiwa` 启动。关闭奶蛙时，右键系统托盘图标选择“退出”。

当前通过 GitHub 分发本项目；尚未发布到 PyPI，不要用 `pip install naiwa` 安装同名的其他项目。

## 使用

- 拖动身体或手移动奶蛙；重启恢复位置。
- 单击身体或托盘“奶蛙说两句”，展开或收起两列提示栏。
- 单击手、工作区标签或卡片，查看该会话的最近动作。
- 多窗口、相同工作区的不同会话使用稳定编号区分。
- `naiwa demo` 可先预览动画，无需接入 IDE。

```powershell
naiwa --version
naiwa install --dry-run
naiwa probe --watch 60
naiwa doctor
```

“等你”依赖 IDE 发出的提问或审批钩子；没有事件时不会推断为等待输入。较长时间无事件会标为信号未更新，不代表任务成功或失败。查看到的工作区名称来自钩子元数据，不读取提示词、工具正文、IDE 内部日志或数据库。

## SSH 是可选功能

首次安装只观察自己的本机 IDE。安装包不包含任何服务器地址、账号、密码、SSH 私钥或个人连接配置，也没有默认服务器。已有远端采集器的用户可以显式指定自己 SSH 配置中的别名：

```powershell
naiwa remote --host example-server --interactive
```

监听只读取已经存在的远端采集记录，不自动上传或重新部署采集器。Windows 上监听在后台运行，不打开终端。需要认证时显示奶蛙登录卡片；密码通过本机内存管道交给 OpenSSH，不写入配置、日志或事件文件。关闭或取消卡片会取消这次登录；断线后在托盘菜单“SSH 连接”中点击“连接 / 重试”，不会循环弹出密码窗口。

升级时正在运行的旧监听会继续保留。在“SSH 连接”中点击“切换图形登录”，才会结束旧连接、用新卡片重新登录一次。

需要首次部署采集器时才运行：

```powershell
naiwa remote install --host example-server --workspace SAMPLE_PROJECT --interactive
```

这条显式安装命令会修改你的远端用户目录并合并用户级 Cursor 钩子。`--workspace` 是远端登录用户家目录下的项目目录名；多个同名目录时用 `--root` 明确指定。工作区名称不会自动绑定到示例中的名称。

## 升级和移除

先在托盘退出奶蛙，再安装新 wheel、重新运行 `naiwa install` 并启动。个人设置默认位于 `%USERPROFILE%/.agent-pet`，升级保留；不要把该目录上传到仓库或发送给别人。

```powershell
naiwa uninstall
python -m pip uninstall naiwa
```

移除钩子只移除奶蛙的处理器，保留其他处理器。

## 开发、打包与分享

```powershell
python -m pip install -e ".[dev,release]"
python -m pytest -q
python scripts/build_release.py
python scripts/audit_share.py artifacts/naiwa-0.8.9-py3-none-any.whl
```

`artifacts/` 生成 wheel、SHA-256 校验文件和使用说明。将这三个文件上传到 GitHub Release 即可分享。`scripts/export_share.py` 按允许的文件清单生成独立源码目录，不复制父仓库历史、运行记录、SSH 配置、预览截图或原画实验稿。

源码仓库和 wheel 都应通过发布检查。凭据、配置和事件只保存在使用者自己的电脑上；GitHub 仓库只包含通用程序、角色动画及使用虚构元数据的测试。
