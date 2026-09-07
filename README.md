# C 盘垃圾清理工具

Win11 上针对 C 盘的小工具,单文件 exe,双击即用,自带 UAC 提权。

## 快速使用(推荐)

**直接双击 →** `dist\C_Cleaner.exe`

- 自动弹出 UAC 窗口请求管理员权限(点"是")
- 不用装 Python,12 MB,任何 Win11 机器都能跑
- 可以把这个 exe 复制到任何位置运行,或发给同事

## 重新打包(改了代码后)

双击 `打包.bat`,会自动:
1. 检查/安装 PyInstaller
2. 把项目临时复制到 `%TEMP%` 下一份(纯英文路径,绕开 PyInstaller 不支持中文路径的 bug)
3. 在临时目录里打包
4. 把 `C_Cleaner.exe` 复制回当前 `dist\` 目录
5. 自动打开资源管理器定位 exe

## 不用 exe,用 Python 跑(开发调试用)

双击 `运行.bat`:
- 自动用 PowerShell 提权启动 `python cleaner.py`
- 需要先装 Python 3.8+(勾选 "Add to PATH")

## 功能特性(v3.2.0 - 性能/能力全面升级)

### 74 个清理项,分三档(v3.1 的 51 → v3.2 的 74,**+45%**)

- **安全**(默认勾选,39 项):
  - **用户/系统临时**:用户临时、系统临时、缩略图、DirectX 着色器、错误报告、诊断、通知、图标缓存、快捷方式、用户级崩溃转储
  - **浏览器缓存**:Edge、Chrome、Firefox、Brave、Opera、Opera GX、Vivaldi、Yandex(含 IndexedDB/ServiceWorker 深度缓存)
  - **通信**:VSCode、Spotify、Slack、Discord、Zoom、Steam、**Skype**、**Microsoft Teams**
  - **游戏启动器**:**Epic Games**、**EA App / Origin**、**Battle.net**、**Ubisoft Connect**
  - **IDE**:**Notepad++** 备份、**Sublime Text**、**Eclipse**
  - **包管理**:NuGet、Chocolatey、winget
  - **系统**:Windows Media Player、Microsoft Store 缓存
- **谨慎**(默认不勾,26 项):
  - **系统**:Windows 更新下载、Prefetch、Installer 补丁缓存、内核转储、Defender 扫描历史、旧升级下载、Win11 系统应用缓存、Office/Outlook 缓存、OneDrive 日志、Adobe 缓存
  - **包管理**:pip、uv、npm、yarn、pnpm、cargo、gradle、JetBrains、**Maven**、**Composer**、**Bundler**、**sbt**
  - **野生临时文件扫描**:在 %TEMP%/%LOCALAPPDATA%\Temp 下找散落的 `*.tmp`/`*.log`/`*.bak`/`*.old`,>7 天或 >50MB 才清(可释放大量散落空间)
- **高级**(默认不勾 + 折叠,9 项):INF/Setup 日志、BITS 日志、WinSxS ManifestCache、Defender 隔离区、`$WINDOWS.~BT`、`$SysReset`、`Windows.old`、**关闭休眠(释放 hiberfil.sys,可达 4-16 GB)**、**清理旧卷影副本(可释放数 GB)**

### 4 个 Tab

- 🧹 **垃圾清理**(74 项)
- 📦 **大文件**(Top N 扫描 + 扩展名/时间/排除目录过滤)
- 📂 **文件夹大小**(深度可调)
- 🔁 **重复文件**(MD5 哈希 + 扩展名分组 + 勾选删除)

### 性能改进(v3.2 - 全场景提速)

| 场景 | 改进 | 提速 |
|---|---|---|
| 并行扫描非均衡树(`node_modules` 等 90% 文件集中在一个子目录) | 旧:每子目录 1 个线程,8 线程 7 个空转<br>新:**工作窃取式** `Queue + N worker`,自动负载均衡 | **2-5x** |
| 删除大目录(>5000 文件) | 旧:Python 循环 unlink + rmdir<br>新:**`shutil.rmtree(ignore_errors=True)`** C 实现 | **2-3x** |
| `safe_remove_dir` 扫描 | 旧:`scandir_files_parallel` + `os.walk` 两次全遍历<br>新:**单遍**同时收集文件 + 子目录 | **~2x** |
| `safe_remove_file` 失败时 | 旧:`os.chmod(0o777)` 回退(NTFS 上无效)<br>新:直接放弃,省一次 syscall | 微小但每文件累计 |
| 重复 Tab 批量删除 N 个文件 | 旧:N 次 `SHFileOperationW` syscall<br>新:**批量 API 一次 syscall** | **10x+** |
| `scandir_files` 单线程 | 旧:`entries = list(it)` 物化迭代器<br>新:直接迭代 | 微小但大量目录累计 |

### 其他特性

- 实时显示 **C 盘使用率**
- 清理前可勾选 **"创建系统还原点"**(强烈建议)
- 可选 **"同时清空回收站"**
- 异步扫描 + 清理,UI 不卡
- 跳过被占用的文件,失败不崩
- 深色/浅色主题切换
- 微信风绿主题,支持鼠标滚轮
- 用户可配置 **白名单**(扩展名 + 路径前缀)

## 注意事项

- 必须以管理员权限运行(脚本/打包已自动处理)
- 浏览器缓存请先关闭对应浏览器再清理(否则部分文件被占用)
- 不要同时打开两个清理实例
- 清理后建议重启一次,某些缓存要重启才彻底释放
- 高级项(尤其 `Windows.old`、Defender 隔离区、卷影副本清理)删了无法恢复,看清说明再勾
- **关闭休眠**会丧失真正的休眠能力(但「快速启动」不受影响,Win11 默认走快速启动)
- **野生临时文件扫描**会清掉 *.log,某些程序依赖旧日志,如不确定可先不勾

## 常见问题

**Q: 双击 exe 什么都没弹出来?**
A: 看任务管理器是否有 `C_Cleaner.exe` 进程。如果有,说明 GUI 启动了,只是被其他窗口挡住了。如果连进程都没有,右键 exe → "以管理员身份运行"。

**Q: 打包失败?**
A: 打包.bat 会自动规避中文路径,如果还失败,看 cmd 窗口里 `build.log` 的最后 30 行,常见原因是:
- 没装 Python
- 网络问题导致 pip install pyinstaller 失败
- 杀毒软件干扰(临时关掉再试)

**Q: 怎么加新的清理项?**
A: 编辑 `cleaner.py` 顶部的 `CLEAN_TARGETS` 列表,加一项。普通文件清理用 `kind: "files"`(默认),按模式扫描用 `kind: "wild_temp"`,系统级操作用 `kind: "command"`。

**Q: 74 项一次清理大概能释放多少?**
A: 看使用情况。一般用户首次 8-25 GB,开发者/IDE 重度用户 30-80 GB,旧系统 50+ GB。建议先勾「安全」跑一次,再勾「谨慎」,高级项按需选。

**Q: 工作窃取式扫描有什么意义?**
A: 旧版给「每个一级子目录」分配一个线程。如果 90% 文件集中在 1 个子目录,8 个线程只有 1 个干活。新版 N 个 worker 共享任务队列,自动把活儿均分,大数据集快 2-5x。

**Q: 批量回收站 API 安全吗?**
A: 安全。Windows 的 `SHFileOperationW` 原生支持多路径操作,只是把多个删除请求合并成一次系统调用。所有路径都进回收站,不会绕过回收站直接删除。

## 项目结构

```
C盘清理工具/
├── cleaner.py           # 主程序(扫描 + 清理 + GUI,~4150 行)
├── 运行.bat              # 一键启动(用 Python 跑)
├── 打包.bat              # 一键打包成 exe
├── tests/
│   └── test_all.py      # 单元测试(25 个,覆盖 3 档 + 并发 + 智能扫描 + 批量 API)
├── README.md
└── dist/
    └── C_Cleaner.exe    # 打包好的单文件 exe (12 MB)
```

## 跑测试

```powershell
python tests\test_all.py
```