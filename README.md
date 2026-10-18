# ReleaseFlow

ReleaseFlow 是一个面向 Windows 的 GitHub Release 下载器。粘贴任意 GitHub
项目地址后，它会读取公开版本列表和附件，并通过可切换的国内加速线路下载。
它专注于稳定下载，不内置网页浏览器。

## 直接使用

双击 `ReleaseFlow.exe`，或在源码目录运行：

```powershell
python main.py
```

使用步骤：

1. 粘贴 `https://github.com/owner/repo`。
2. 按回车或点击搜索按钮。
3. 在左侧选择版本，在右侧勾选资产。
4. 点击“下载所选”。

不勾选资产时，“下载所选”会下载当前版本的全部附件。下载页支持暂停、
继续、取消、断点续传和线路回退。

## 特性

- 支持 GitHub 项目页、Release 页、`.git` 地址和末尾斜杠。
- 列出正式版本与可选预发布版本，显示资产大小和下载量。
- 默认使用 8 线程分片下载，可调整为 1 到 32。
- 自动检测镜像线路，并在失败时回退；也可指定镜像或 GitHub 直连。
- 分片失败自动重试，退出或暂停后可从已完成的分片继续。
- 支持 HTTP 代理、可选 GitHub Token 和可选的证书错误兼容模式。
- 不依赖 Node.js 或浏览器扩展。
- 纯下载器单文件发行，启动快，不包含浏览器运行时。

## 构建

需要 Python 3.11 或更高版本：

```powershell
python -m pip install -r requirements.txt pyinstaller
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

生成文件位于 `dist\ReleaseFlow.exe`。

## 系统要求

- Windows 10/11 64 位
- 源码运行需要 Python 3.11 或更高版本
- 发行版 `ReleaseFlow.exe` 不需要安装 Python

## 下载器信息

当前版本：`1.0.0`

默认下载目录为 `%USERPROFILE%\Downloads`，可以在设置中修改。
下载任务支持暂停、继续、取消和断点续传；程序退出时会保留已完成的分片。

## 说明

加速镜像由第三方提供，可用性可能变化。ReleaseFlow 不代理、不缓存也不托管
任何下载内容，只在请求时按所选线路重写 GitHub 下载地址。正式项目请优先
核对 GitHub 原始 Release 和发布者提供的校验值。
