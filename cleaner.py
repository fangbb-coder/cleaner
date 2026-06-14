"""
Win11 C盘垃圾清理 + 磁盘分析工具
=====================================
功能:
  1. 垃圾清理(临时文件、Windows 更新缓存、缩略图、浏览器缓存、回收站等)
  2. 磁盘大文件扫描(Top N)
  3. 文件夹大小排序(Top N)
  4. 重复文件查找(MD5 哈希)

UI: 微信风格(白底 + 微信绿 + 圆角卡片),4 个 Tab 切换。
运行: 需要管理员权限(双击会自动请求 UAC)
依赖: 仅 Python 3.8+ 标准库
作者: Mavis
"""

import os
import sys
import shutil
import ctypes
import threading
import subprocess
import json
import time
import re
import gc
import hashlib
import queue
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ====================== 全局常量 ======================

APP_NAME = "C盘清理工具"
APP_VERSION = "2.0.0"

# ====================== 微信风格配色 ======================

class WC:
    """微信风格配色常量"""
    BG = "#F7F7F7"          # 主背景(微信聊天底色)
    CARD = "#FFFFFF"        # 卡片白
    CARD_ALT = "#FAFAFA"    # 浅卡片
    GREEN = "#07C160"       # 微信绿(主操作按钮)
    GREEN_DARK = "#06AE56"  # 按下时
    GREEN_LIGHT = "#E8F5E9" # 极浅绿(选中/高亮)
    BLUE = "#10AEFF"        # 链接蓝
    RED = "#FA5151"         # 警告红
    RED_DARK = "#E54B4B"    # 按下时
    ORANGE = "#FA9D3B"      # 谨慎项
    TEXT = "#191919"        # 主文字
    TEXT2 = "#888888"       # 次要文字
    TEXT3 = "#B2B2B2"       # 极淡
    BORDER = "#E5E5E5"      # 边框
    BORDER_LIGHT = "#EFEFEF"
    TAB_BG = "#EDEDED"      # tab 底色
    SEL_BG = "#E8F5E9"      # 选中行

# ====================== 清理项配置 ======================

CLEAN_TARGETS = [
    {
        "id": "user_temp",
        "name": "用户临时文件夹",
        "paths": ["%TEMP%", "%TMP%", r"%LOCALAPPDATA%\Temp"],
        "level": "safe",
        "desc": "软件运行产生的临时文件,通常可安全删除",
    },
    {
        "id": "windows_temp",
        "name": "Windows 临时文件夹",
        "paths": [r"C:\Windows\Temp"],
        "level": "safe",
        "desc": "系统级临时目录",
    },
    {
        "id": "update_cache",
        "name": "Windows 更新下载缓存",
        "paths": [r"C:\Windows\SoftwareDistribution\Download"],
        "level": "safe",
        "desc": "已下载的 Windows 更新补丁",
    },
    {
        "id": "thumb_cache",
        "name": "缩略图缓存",
        "paths": [r"%LOCALAPPDATA%\Microsoft\Windows\Explorer"],
        "level": "safe",
        "desc": "资源管理器缩略图,删除后自动重建",
    },
    {
        "id": "dx_shader",
        "name": "DirectX 着色器缓存",
        "paths": [
            r"%LOCALAPPDATA%\D3DSCache",
            r"C:\Windows\System32\DriverStore\Temp",
        ],
        "level": "safe",
        "desc": "GPU 着色器编译缓存",
    },
    {
        "id": "wer_reports",
        "name": "Windows 错误报告",
        "paths": [
            r"C:\ProgramData\Microsoft\Windows\WER",
            r"%LOCALAPPDATA%\Microsoft\Windows\WER",
        ],
        "level": "safe",
        "desc": "崩溃/错误日志",
    },
    {
        "id": "old_logs",
        "name": "旧日志文件",
        "paths": [
            r"C:\Windows\Logs",
            r"C:\Windows\debug",
            r"C:\Windows\Panther",
            r"C:\Windows\Minidump",
        ],
        "level": "caution",
        "desc": "系统和应用的旧日志、崩溃转储",
    },
    {
        "id": "edge_cache",
        "name": "Edge 浏览器缓存",
        "paths": [
            r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Cache",
            r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Code Cache",
            r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\GPUCache",
        ],
        "level": "safe",
        "desc": "Edge 浏览器缓存(关闭浏览器后清理更彻底)",
    },
    {
        "id": "chrome_cache",
        "name": "Chrome 浏览器缓存",
        "paths": [
            r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Cache",
            r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Code Cache",
            r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\GPUCache",
        ],
        "level": "safe",
        "desc": "Chrome 浏览器缓存(需关闭 Chrome)",
    },
    {
        "id": "ms_store_cache",
        "name": "Microsoft Store 缓存",
        "paths": [r"%LOCALAPPDATA%\Packages\Microsoft.WindowsStore_*\LocalCache"],
        "level": "safe",
        "desc": "应用商店缓存",
    },
    {
        "id": "prefetch",
        "name": "预读取文件 (Prefetch)",
        "paths": [r"C:\Windows\Prefetch"],
        "level": "caution",
        "desc": "程序预读取数据,清空不影响功能但丢加速",
    },
    {
        "id": "installer_temp",
        "name": "Installer 临时文件",
        "paths": [r"C:\Windows\Installer\$PatchCache$"],
        "level": "caution",
        "desc": "已安装程序的补丁缓存",
    },
]

PROTECTED_NAMES = {
    "desktop.ini", "thumbs.db", "pagefile.sys", "hiberfil.sys",
    "swapfile.sys", "config.sys", "io.sys", "msdos.sys",
}

# ====================== 工具函数 ======================


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def expand_path(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


def human_size(num) -> str:
    if num is None:
        return "-"
    try:
        num = float(num)
    except (TypeError, ValueError):
        return str(num)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def get_disk_usage(drive: str = "C") -> dict:
    try:
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            f"{drive}:\\", None, ctypes.pointer(total), ctypes.pointer(free)
        )
        t, f = total.value, free.value
        return {
            "total": t, "used": t - f, "free": f,
            "percent": (t - f) / t * 100 if t else 0,
        }
    except Exception as e:
        return {"total": 0, "used": 0, "free": 0, "percent": 0, "error": str(e)}


def safe_remove_file(path: Path) -> bool:
    if not path.exists():
        return False
    if path.name in PROTECTED_NAMES:
        return False
    try:
        os.chmod(path, 0o777)
        path.unlink()
        return True
    except (PermissionError, OSError):
        return False


def safe_remove_dir(path: Path) -> tuple:
    removed = 0
    freed = 0
    if not path.exists():
        return 0, 0
    all_files = []
    try:
        for root, dirs, files in os.walk(path, topdown=False):
            for f in files:
                fp = Path(root) / f
                try:
                    all_files.append((fp, fp.stat().st_size))
                except Exception:
                    continue
    except Exception:
        pass
    for fp, size in all_files:
        if safe_remove_file(fp):
            removed += 1
            freed += size
    all_dirs = []
    try:
        for root, dirs, files in os.walk(path, topdown=False):
            for d in dirs:
                all_dirs.append(Path(root) / d)
    except Exception:
        pass
    for d in all_dirs:
        try:
            os.rmdir(d)
        except (PermissionError, OSError):
            pass
    try:
        os.rmdir(path)
    except (PermissionError, OSError):
        pass
    return removed, freed


def calculate_dir_size(path: Path, max_depth: int = 6) -> tuple:
    total = 0
    count = 0
    if not path.exists():
        return 0, 0

    def _walk(p: Path, depth: int):
        nonlocal total, count
        if depth > max_depth:
            return
        try:
            for entry in p.iterdir():
                try:
                    if entry.is_file() and not entry.is_symlink():
                        try:
                            total += entry.stat().st_size
                            count += 1
                        except OSError:
                            continue
                    elif entry.is_dir() and not entry.is_symlink():
                        _walk(entry, depth + 1)
                except (PermissionError, OSError):
                    continue
        except (PermissionError, OSError):
            return

    _walk(path, 0)
    return total, count


def create_restore_point(description: str = "C盘清理工具-清理前备份") -> bool:
    try:
        cmd = [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            f"Checkpoint-Computer -Description '{description}' -RestorePointType MODIFY_SETTINGS"
        ]
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        return r.returncode == 0
    except Exception:
        return False


def empty_recycle_bin() -> bool:
    try:
        ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x00000007)
        return True
    except Exception:
        return False


# SHFileOperationW 结构体(用 ctypes 调 Windows 原生 API 把文件移到回收站,
# 完全静默,不会弹 PowerShell 窗口,不会让前台窗口丢焦点)
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", ctypes.c_bool),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


# FO_DELETE = 删除(配合 FOF_ALLOWUNDO = 移到回收站)
_FO_DELETE = 0x0003
_FOF_ALLOWUNDO = 0x0040   # 关键:可撤销(→ 回收站)
_FOF_SILENT = 0x0004      # 不显示进度
_FOF_NOCONFIRMATION = 0x0010  # 不弹确认
_FOF_NOERRORUI = 0x0400   # 不弹错误框
_FOF_WANTNUKEWARNING = 0x0000  # 不需要


def move_to_recycle_bin(path: str) -> bool:
    """用 Windows 原生 SHFileOperationW 把文件/目录移到回收站。
    完全静默,无 PowerShell 窗口,不影响 GUI 焦点。
    """
    if not path or not os.path.exists(path):
        return False
    try:
        # pFrom 必须是双 \0 结尾的 unicode 字符串
        op = _SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = _FO_DELETE
        op.pFrom = str(path) + "\0\0"
        op.pTo = None
        op.fFlags = _FOF_ALLOWUNDO | _FOF_SILENT | _FOF_NOCONFIRMATION | _FOF_NOERRORUI
        op.fAnyOperationsAborted = False
        op.hNameMappings = None
        op.lpszProgressTitle = None
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        # 0 = 成功, 非 0 = 失败
        return result == 0 and not op.fAnyOperationsAborted
    except Exception:
        return False


# ====================== 清理器(原逻辑保留) ======================


class Scanner:
    def __init__(self, log_callback=None, progress_callback=None):
        self.log = log_callback or print
        self.progress = progress_callback or (lambda *a, **kw: None)
        self._cancel = threading.Event()
        self.results = {}

    def cancel(self):
        self._cancel.set()

    def scan_target(self, target: dict) -> dict:
        r = {"size": 0, "files": 0, "paths": [], "exists": False}
        expanded = []
        for p in target["paths"]:
            if "*" in p or "?" in p:
                parent = Path(p).parent
                pattern = Path(p).name
                if parent.exists():
                    expanded.extend([str(m) for m in parent.glob(pattern)])
            else:
                expanded.append(p)
        for p in expanded:
            ep = expand_path(p)
            if not ep.exists():
                continue
            r["exists"] = True
            r["paths"].append(str(ep))
            try:
                size, count = calculate_dir_size(ep)
                r["size"] += size
                r["files"] += count
            except Exception as e:
                self.log(f"  跳过 {ep}: {e}")
        return r

    def scan_all(self, target_ids=None):
        self._cancel.clear()
        self.results = {}
        targets = CLEAN_TARGETS
        if target_ids is not None:
            targets = [t for t in CLEAN_TARGETS if t["id"] in target_ids]

        self.log("=" * 60)
        self.log(f"[扫描] 开始,共 {len(targets)} 个清理项")
        self.log("=" * 60)

        for idx, target in enumerate(targets, 1):
            if self._cancel.is_set():
                self.log("[扫描] 用户取消")
                break
            self.progress(
                (idx - 1) / max(len(targets), 1),
                f"扫描中({idx}/{len(targets)}): {target['name']}"
            )
            self.log(f"[{idx}/{len(targets)}] {target['name']} ...")
            try:
                r = self.scan_target(target)
                self.results[target["id"]] = r
                if r["exists"] and r["size"] > 0:
                    self.log(f"  -> {human_size(r['size'])} ({r['files']} 个文件)")
                else:
                    self.log(f"  -> 暂无内容")
            except Exception as e:
                self.log(f"  -> 出错: {e}")
                self.results[target["id"]] = {"size": 0, "files": 0, "paths": [], "exists": False}

        self.progress(1.0, "扫描完成")
        self.log("=" * 60)
        total_size = sum(r.get("size", 0) for r in self.results.values())
        self.log(f"[扫描] 完成,总可清理: {human_size(total_size)}")
        self.log("=" * 60)
        return self.results


class Cleaner:
    def __init__(self, log_callback=None, progress_callback=None):
        self.log = log_callback or print
        self.progress = progress_callback or (lambda *a, **kw: None)
        self._cancel = threading.Event()
        self._pause = threading.Event()
        self._pause.set()

    def cancel(self):
        self._cancel.set()
        self._pause.set()

    def clean_target(self, target: dict) -> dict:
        result = {"removed": 0, "freed": 0, "errors": 0}
        expanded = []
        for p in target["paths"]:
            if "*" in p or "?" in p:
                parent = Path(p).parent
                pattern = Path(p).name
                if parent.exists():
                    expanded.extend([str(m) for m in parent.glob(pattern)])
            else:
                expanded.append(p)
        for p in expanded:
            ep = expand_path(p)
            if not ep.exists():
                continue
            try:
                if ep.is_file():
                    size = ep.stat().st_size
                    if safe_remove_file(ep):
                        result["removed"] += 1
                        result["freed"] += size
                else:
                    removed, freed = safe_remove_dir(ep)
                    result["removed"] += removed
                    result["freed"] += freed
            except Exception as e:
                result["errors"] += 1
                self.log(f"  错误: {ep} -> {e}")
        return result

    def clean_all(self, target_ids: list) -> dict:
        self._cancel.clear()
        self._pause.set()
        total_freed = 0
        total_removed = 0
        total_errors = 0
        targets = [t for t in CLEAN_TARGETS if t["id"] in target_ids]

        self.log("=" * 60)
        self.log(f"[清理] 开始,共 {len(targets)} 个清理项")
        self.log("=" * 60)

        for idx, target in enumerate(targets, 1):
            if self._cancel.is_set():
                self.log("[清理] 用户取消")
                break
            self._pause.wait()
            self.progress(
                (idx - 1) / max(len(targets), 1),
                f"清理中({idx}/{len(targets)}): {target['name']}"
            )
            self.log(f"[{idx}/{len(targets)}] {target['name']} ...")
            try:
                r = self.clean_target(target)
                total_removed += r["removed"]
                total_freed += r["freed"]
                total_errors += r["errors"]
                msg = f"  -> 删除 {r['removed']} 个,释放 {human_size(r['freed'])}"
                if r["errors"]:
                    msg += f",{r['errors']} 个失败"
                self.log(msg)
            except Exception as e:
                self.log(f"  -> 出错: {e}")
                total_errors += 1

        self.progress(1.0, "清理完成")
        self.log("=" * 60)
        self.log(
            f"[清理] 完成,共删除 {total_removed} 个,释放 {human_size(total_freed)}"
            + (f",失败 {total_errors} 个" if total_errors else "")
        )
        self.log("=" * 60)
        return {"removed": total_removed, "freed": total_freed, "errors": total_errors}


# ====================== 磁盘分析器(新) ======================


class Analyzer:
    """磁盘分析:大文件 / 文件夹大小 / 重复文件"""

    def __init__(self, log_callback=None, progress_callback=None):
        self.log = log_callback or print
        self.progress = progress_callback or (lambda *a, **kw: None)
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    # ----- 大文件 -----
    def find_large_files(self, root: str, min_size: int = 100 * 1024 * 1024,
                         top_n: int = 100, max_depth: int = 8) -> list:
        """扫描目录下所有大于 min_size 的文件,按大小降序返回 top_n。"""
        results = []
        root_p = Path(root)
        if not root_p.exists():
            self.log(f"[大文件] 路径不存在: {root}")
            return results
        self.log(f"[大文件] 阶段 1/1:遍历 {root} (深度 {max_depth},阈值 {human_size(min_size)})")
        self.progress(0, "开始扫描...")
        scanned = 0
        dirs_visited = 0
        last_log = 0
        try:
            for dirpath, dirnames, filenames in os.walk(root_p, topdown=True):
                # 深度限制
                depth = str(dirpath).count(os.sep) - str(root_p).count(os.sep)
                if depth > max_depth:
                    dirnames.clear()
                    continue
                # 跳过明显不相关的目录
                dirnames[:] = [
                    d for d in dirnames
                    if d not in ("$Recycle.Bin", "System Volume Information", "$WinREAgent")
                ]
                if self._cancel.is_set():
                    self.log("[大文件] 用户取消")
                    break
                # 日志:每进入一个新一级目录打一条(显示当前在哪儿)
                if depth <= 2:
                    self.log(f"  → 进入目录: {dirpath}")
                for f in filenames:
                    if self._cancel.is_set():
                        break
                    fp = Path(dirpath) / f
                    try:
                        st = fp.stat()
                        if st.st_size >= min_size:
                            results.append({
                                "path": str(fp),
                                "size": st.st_size,
                                "mtime": st.st_mtime,
                            })
                    except (PermissionError, OSError):
                        continue
                scanned += len(filenames)
                dirs_visited += 1
                # 进度日志:每 1000 个文件打一次
                if scanned - last_log >= 1000:
                    self.log(
                        f"  · 已扫 {scanned} 个文件,走过 {dirs_visited} 个目录,"
                        f"发现 {len(results)} 个大文件"
                    )
                    last_log = scanned
                    self.progress(
                        0,
                        f"已扫 {scanned} 个文件,发现 {len(results)} 个大文件"
                    )
        except Exception as e:
            self.log(f"[大文件] 遍历出错: {e}")

        self.log(
            f"[大文件] 完成:共扫 {scanned} 个文件 / {dirs_visited} 个目录,"
            f"发现 {len(results)} 个 ≥ {human_size(min_size)}"
        )
        results.sort(key=lambda x: x["size"], reverse=True)
        return results[:top_n]

    # ----- 文件夹大小 -----
    def find_large_dirs(self, root: str, top_n: int = 50, max_depth: int = 3) -> list:
        """扫描目录下子文件夹大小,按大小降序返回 top_n。
        max_depth 是相对 root 的层级深度,避免无限递归。
        """
        results = []
        root_p = Path(root)
        if not root_p.exists():
            self.log(f"[文件夹] 路径不存在: {root}")
            return results
        self.log(f"[文件夹] 阶段 1/2:枚举 {root} 下的子目录(深度 {max_depth})")
        self.progress(0, "枚举子目录...")
        try:
            # 第一遍:列出所有子目录(深度限制)
            subdirs = []
            for dirpath, dirnames, filenames in os.walk(root_p, topdown=True):
                depth = str(dirpath).count(os.sep) - str(root_p).count(os.sep)
                if depth >= max_depth:
                    dirnames.clear()
                    continue
                if self._cancel.is_set():
                    self.log("[文件夹] 用户取消")
                    return results
                for d in dirnames:
                    subdirs.append(Path(dirpath) / d)
                # 日志:每 200 个子目录打一次
                if len(subdirs) % 200 == 0 and subdirs:
                    self.log(f"  · 已枚举 {len(subdirs)} 个子目录 ...")

            self.log(f"[文件夹] 阶段 2/2:累计 {len(subdirs)} 个子目录的大小")
            total = len(subdirs)
            last_log = 0
            for i, d in enumerate(subdirs, 1):
                if self._cancel.is_set():
                    break
                try:
                    size, count = calculate_dir_size(d, max_depth=6)
                    if size > 0:
                        results.append({
                            "path": str(d),
                            "size": size,
                            "files": count,
                        })
                except (PermissionError, OSError):
                    continue
                if i - last_log >= 50:
                    self.log(
                        f"  · 已分析 {i}/{total} 个目录 "
                        f"({len(results)} 个有内容)"
                    )
                    last_log = i
                    self.progress(i / total, f"已分析 {i}/{total} 个目录")
        except Exception as e:
            self.log(f"[文件夹] 遍历出错: {e}")

        self.log(f"[文件夹] 完成:共 {len(results)} 个有内容的目录")
        results.sort(key=lambda x: x["size"], reverse=True)
        return results[:top_n]

    # ----- 重复文件 -----
    @staticmethod
    def _quick_hash(path: Path, block_size: int = 65536) -> str:
        """快速 hash:头 64K + 尾 64K + size。
        对大文件来说比完整 MD5 快几十倍,误判率极低(同样的 size + 头/尾相同基本就是重复)。
        """
        try:
            size = path.stat().st_size
            h = hashlib.md5()
            h.update(f"{size}".encode())
            with open(path, "rb") as f:
                # 头部
                head = f.read(block_size)
                h.update(head)
                # 中间
                if size > block_size * 3:
                    f.seek(size // 2)
                    mid = f.read(block_size)
                    h.update(mid)
                # 尾部
                if size > block_size:
                    f.seek(max(0, size - block_size))
                    tail = f.read(block_size)
                    h.update(tail)
            return h.hexdigest()
        except (PermissionError, OSError):
            return None

    @staticmethod
    def _full_hash(path: Path) -> str:
        """完整 MD5,用于小文件或 quick hash 碰撞后再确认。"""
        try:
            h = hashlib.md5()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()
        except (PermissionError, OSError):
            return None

    def find_duplicates(self, root: str, min_size: int = 1024 * 1024,
                        max_depth: int = 6, top_n_groups: int = 100) -> list:
        """找重复文件:按 size 分桶 → quick hash 分桶 → 完整 MD5 确认。
        返回 groups: [[{path,size}, ...], ...] 按组总大小降序。
        """
        root_p = Path(root)
        if not root_p.exists():
            self.log(f"[重复] 路径不存在: {root}")
            return []
        self.log(
            f"[重复] 阶段 1/3:遍历 {root} 并按文件大小 size 分桶 "
            f"(深度 {max_depth},阈值 {human_size(min_size)})"
        )
        self.progress(0, "阶段 1/3:扫描并按 size 分桶...")

        # 第一遍:收集所有文件,按 size 分组
        size_buckets = {}  # size -> [paths]
        scanned = 0
        last_log = 0
        try:
            for dirpath, dirnames, filenames in os.walk(root_p, topdown=True):
                depth = str(dirpath).count(os.sep) - str(root_p).count(os.sep)
                if depth > max_depth:
                    dirnames.clear()
                    continue
                dirnames[:] = [
                    d for d in dirnames
                    if d not in ("$Recycle.Bin", "System Volume Information", "$WinREAgent")
                ]
                if self._cancel.is_set():
                    self.log("[重复] 用户取消")
                    return []
                for f in filenames:
                    fp = Path(dirpath) / f
                    try:
                        st = fp.stat()
                        if st.st_size >= min_size:
                            size_buckets.setdefault(st.st_size, []).append(fp)
                    except (PermissionError, OSError):
                        continue
                scanned += len(filenames)
                if scanned - last_log >= 1000:
                    self.log(
                        f"  · 阶段 1:已扫 {scanned} 个文件,"
                        f"size 分桶 {len(size_buckets)} 个"
                    )
                    last_log = scanned
                    self.progress(0, f"阶段 1:已扫 {scanned} 个文件")
        except Exception as e:
            self.log(f"[重复] 阶段 1 出错: {e}")

        # 过滤出 size 出现 >=2 的
        dup_sizes = {s: ps for s, ps in size_buckets.items() if len(ps) >= 2}
        cand_count = sum(len(v) for v in dup_sizes.values())
        self.log(
            f"[重复] 阶段 1 完成:扫描 {scanned} 个文件,发现 {len(size_buckets)} 个不同 size,"
            f"其中 {len(dup_sizes)} 个 size 有重复 → {cand_count} 个文件需进一步哈希"
        )
        if cand_count == 0:
            self.log("[重复] 没有 size 重复的文件,扫描结束")
            return []
        if self._cancel.is_set():
            return []

        # 第二遍:quick hash(头/中/尾 64K + size 拼出的 MD5)
        self.log(
            f"[重复] 阶段 2/3:对 {cand_count} 个候选做 quick hash "
            f"(读头/中/尾 64K,比完整 MD5 快几十倍)"
        )
        self.progress(0, f"阶段 2/3:quick hash {cand_count} 个文件...")
        quick_buckets = {}  # quick_hash -> [paths]
        idx = 0
        last_log = 0
        for size, paths in dup_sizes.items():
            for p in paths:
                if self._cancel.is_set():
                    self.log("[重复] 用户取消")
                    return []
                idx += 1
                qh = self._quick_hash(p)
                if qh is None:
                    continue
                quick_buckets.setdefault(qh, []).append(p)
                if idx - last_log >= 20:
                    self.log(
                        f"  · 阶段 2:quick hash {idx}/{cand_count} "
                        f"({p.name})"
                    )
                    last_log = idx
                    self.progress(
                        idx / max(cand_count, 1),
                        f"阶段 2:quick hash {idx}/{cand_count}"
                    )

        # 过滤出 quick_hash 出现 >=2 的
        dup_qh = {qh: ps for qh, ps in quick_buckets.items() if len(ps) >= 2}
        cand2 = sum(len(v) for v in dup_qh.values())
        self.log(
            f"[重复] 阶段 2 完成:quick hash 缩到 {cand2} 个文件 "
            f"({len(dup_qh)} 组),还要做完整 MD5 确认"
        )
        if cand2 == 0:
            self.log("[重复] quick hash 阶段无重复,扫描结束")
            return []
        if self._cancel.is_set():
            return []

        # 第三遍:完整 MD5(逐字节)
        self.log(
            f"[重复] 阶段 3/3:对 {cand2} 个文件做完整 MD5 哈希 "
            f"(逐字节读取,这是确认'同一个文件'的最终依据)"
        )
        self.progress(0, f"阶段 3/3:MD5 {cand2} 个文件...")
        groups = []
        idx = 0
        last_log = 0
        for qh, paths in dup_qh.items():
            md5_buckets = {}
            for p in paths:
                if self._cancel.is_set():
                    self.log("[重复] 用户取消")
                    return []
                idx += 1
                fh = self._full_hash(p)
                if fh is None:
                    continue
                md5_buckets.setdefault(fh, []).append(p)
                if idx - last_log >= 5:
                    self.log(
                        f"  · 阶段 3:MD5 {idx}/{cand2} "
                        f"({p.name})"
                    )
                    last_log = idx
                    self.progress(
                        idx / max(cand2, 1),
                        f"阶段 3:MD5 {idx}/{cand2}"
                    )
            for fh, ps in md5_buckets.items():
                if len(ps) >= 2:
                    groups.append([{"path": str(p), "size": p.stat().st_size} for p in ps])

        # 按组总大小降序
        groups.sort(key=lambda g: sum(x["size"] for x in g) * (len(g) - 1), reverse=True)
        total_waste = sum(
            group[0]["size"] * (len(group) - 1) for group in groups
        )
        self.log(
            f"[重复] 阶段 3 完成:共 {len(groups)} 个确认的重复组,"
            f"可释放 {human_size(total_waste)}"
        )
        return groups[:top_n_groups]


# ====================== 微信风格 UI 组件 ======================

TK_AVAILABLE = True
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, scrolledtext, filedialog
except ImportError:
    TK_AVAILABLE = False


def make_styled_styles():
    """配置 ttk 样式为微信风格。"""
    if not TK_AVAILABLE:
        return
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass

    # Treeview(列表)
    style.configure(
        "WC.Treeview",
        background=WC.CARD,
        fieldbackground=WC.CARD,
        foreground=WC.TEXT,
        rowheight=28,
        font=("Microsoft YaHei UI", 10),
        borderwidth=0,
    )
    style.configure(
        "WC.Treeview.Heading",
        background=WC.TAB_BG,
        foreground=WC.TEXT,
        font=("Microsoft YaHei UI", 10, "bold"),
        relief="flat",
        borderwidth=0,
        padding=(8, 6),
    )
    style.map(
        "WC.Treeview",
        background=[("selected", WC.SEL_BG)],
        foreground=[("selected", WC.TEXT)],
    )
    style.map(
        "WC.Treeview.Heading",
        background=[("active", WC.BORDER)],
    )

    # Notebook(tabs) - 用普通 ttk.Notebook 但锁死 padding,不让选中变小
    style.configure(
        "WC.TNotebook",
        background=WC.BG,
        borderwidth=0,
        tabposition="n",
    )
    style.configure(
        "WC.TNotebook.Tab",
        background=WC.BG,
        foreground=WC.TEXT2,
        font=("Microsoft YaHei UI", 11),
        padding=(24, 12),
        borderwidth=0,
        focuscolor=WC.BG,
    )
    # 强制 selected/active 时所有属性都跟未选中一致(关键!)
    style.map(
        "WC.TNotebook.Tab",
        background=[("selected", WC.BG), ("active", WC.BG), ("!selected", WC.BG)],
        foreground=[("selected", WC.GREEN), ("active", WC.TEXT2), ("!selected", WC.TEXT2)],
        padding=[("selected", (24, 12)), ("active", (24, 12)), ("!selected", (24, 12))],
        font=[("selected", ("Microsoft YaHei UI", 11)),
              ("active", ("Microsoft YaHei UI", 11)),
              ("!selected", ("Microsoft YaHei UI", 11))],
    )

    # Progressbar
    style.configure(
        "WC.Horizontal.TProgressbar",
        troughcolor=WC.BORDER_LIGHT,
        background=WC.GREEN,
        borderwidth=0,
        thickness=6,
    )

    # Combobox
    style.configure(
        "WC.TCombobox",
        fieldbackground=WC.CARD,
        background=WC.CARD,
        foreground=WC.TEXT,
        arrowcolor=WC.TEXT2,
        borderwidth=1,
        relief="solid",
    )
    style.map(
        "WC.TCombobox",
        fieldbackground=[("readonly", WC.CARD)],
        selectbackground=[("readonly", WC.CARD)],
    )




def wechat_optionmenu(parent, variable, values, width=8, **kw):
    """微信风格下拉选择(用 tk.OptionMenu,比 ttk.Combobox 稳)。"""
    if variable.get() not in values:
        if values:
            variable.set(values[0])
    om = tk.OptionMenu(
        parent, variable, *values,
    )
    om.config(
        bg=WC.CARD, fg=WC.TEXT,
        activebackground=WC.GREEN_LIGHT, activeforeground=WC.TEXT,
        highlightthickness=0, relief="solid", bd=1,
        font=("Microsoft YaHei UI", 10),
        indicatoron=True,
        width=width,
        cursor="hand2",
    )
    om["menu"].config(
        bg=WC.CARD, fg=WC.TEXT,
        activebackground=WC.GREEN_LIGHT, activeforeground=WC.TEXT,
        font=("Microsoft YaHei UI", 10),
        relief="flat", bd=1,
    )
    return om

class Card(tk.Frame):
    """微信风格卡片:白底 + 浅边框 + 内边距。
    tkinter 不支持真圆角,用浅色 Frame 包一层模拟。
    """
    def __init__(self, parent, padding=14, **kw):
        super().__init__(
            parent,
            bg=WC.BORDER_LIGHT,
            **kw
        )
        self.inner = tk.Frame(self, bg=WC.CARD)
        self.inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        # 内边距用一个空 Frame 实现
        self.pad_frame = tk.Frame(self.inner, bg=WC.CARD)
        self.pad_frame.pack(fill=tk.BOTH, expand=True, padx=padding, pady=padding)

    def add(self, widget_class, **kw):
        """添加一个子 widget 到 padding frame。"""
        return widget_class(self.pad_frame, **kw)


def wechat_button(parent, text, command, kind="primary", **kw):
    """微信风按钮:主操作绿色 / 危险红色 / 次要灰白。"""
    color_map = {
        "primary": (WC.GREEN, "#FFFFFF", WC.GREEN_DARK),
        "danger": (WC.RED, "#FFFFFF", WC.RED_DARK),
        "default": (WC.CARD, WC.TEXT, WC.BORDER),
        "ghost": (WC.BG, WC.TEXT, WC.BORDER_LIGHT),
    }
    bg, fg, active_bg = color_map.get(kind, color_map["primary"])
    font = kw.pop("font", ("Microsoft YaHei UI", 10, "bold" if kind in ("primary", "danger") else "normal"))
    padx = kw.pop("padx", 16)
    pady = kw.pop("pady", 7)
    return tk.Button(
        parent, text=text, command=command,
        bg=bg, fg=fg, activebackground=active_bg, activeforeground=fg,
        font=font, relief="flat", bd=0,
        padx=padx, pady=pady,
        cursor="hand2",
        **kw
    )


# ====================== 主窗口:4 个 Tab ======================


class CleanerTab:
    """Tab 1: 垃圾清理(原功能)"""
    def __init__(self, parent, log_fn, status_fn):
        self.parent = tk.Frame(parent, bg=WC.BG)
        self.log = log_fn
        self.set_status = status_fn
        self.busy = False
        self.scanner = None
        self.cleaner = None
        self.scan_results = {}
        self.check_vars = {}

        self._build()

    def _build(self):
        # 顶部:磁盘信息 + 可清理总量
        disk_card = Card(self.parent, padding=14)
        disk_card.pack(fill=tk.X, padx=12, pady=(12, 8))

        top = tk.Frame(disk_card.pad_frame, bg=WC.CARD)
        top.pack(fill=tk.X)

        # 左:磁盘信息
        left = tk.Frame(top, bg=WC.CARD)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.disk_total_lbl = tk.Label(left, text="C 盘总容量: -",
                                       font=("Microsoft YaHei UI", 10),
                                       bg=WC.CARD, fg=WC.TEXT2, anchor=tk.W)
        self.disk_total_lbl.pack(anchor=tk.W)
        self.disk_used_lbl = tk.Label(left, text="已用 / 可用: -",
                                      font=("Microsoft YaHei UI", 10),
                                      bg=WC.CARD, fg=WC.TEXT2, anchor=tk.W)
        self.disk_used_lbl.pack(anchor=tk.W, pady=(2, 6))
        self.disk_bar = ttk.Progressbar(left, maximum=100, value=0, mode="determinate",
                                        style="WC.Horizontal.TProgressbar")
        self.disk_bar.pack(fill=tk.X, pady=(0, 4))
        self.disk_percent_lbl = tk.Label(left, text="使用率: -",
                                         font=("Microsoft YaHei UI", 9),
                                         bg=WC.CARD, fg=WC.TEXT3, anchor=tk.W)
        self.disk_percent_lbl.pack(anchor=tk.W)

        # 右:可清理
        right = tk.Frame(top, bg=WC.CARD, width=200)
        right.pack(side=tk.RIGHT, fill=tk.Y)
        tk.Label(right, text="可清理", font=("Microsoft YaHei UI", 10),
                 bg=WC.CARD, fg=WC.TEXT2).pack(anchor=tk.E)
        self.cleanable_lbl = tk.Label(right, text="0 B",
                                      font=("Microsoft YaHei UI", 22, "bold"),
                                      bg=WC.CARD, fg=WC.GREEN)
        self.cleanable_lbl.pack(anchor=tk.E)
        self.cleanable_files_lbl = tk.Label(right, text="0 个文件",
                                            font=("Microsoft YaHei UI", 9),
                                            bg=WC.CARD, fg=WC.TEXT3)
        self.cleanable_files_lbl.pack(anchor=tk.E)

        # 中部:清理项列表(可滚动卡片)
        list_card = Card(self.parent, padding=8)
        list_card.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        # 标题行
        title_row = tk.Frame(list_card.pad_frame, bg=WC.CARD)
        title_row.pack(fill=tk.X, pady=(2, 6))
        tk.Label(title_row, text="清理项", font=("Microsoft YaHei UI", 11, "bold"),
                 bg=WC.CARD, fg=WC.TEXT).pack(side=tk.LEFT)
        btn_row = tk.Frame(title_row, bg=WC.CARD)
        btn_row.pack(side=tk.RIGHT)
        for label, cmd in [
            ("全选", self._select_all),
            ("反选", self._select_invert),
            ("仅安全", self._select_safe_only),
        ]:
            b = wechat_button(btn_row, label, cmd, kind="ghost", font=("Microsoft YaHei UI", 9), padx=10, pady=3)
            b.pack(side=tk.LEFT, padx=2)

        # 列表容器
        list_frame = tk.Frame(list_card.pad_frame, bg=WC.CARD)
        list_frame.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(list_frame, bg=WC.CARD, highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.list_inner = tk.Frame(canvas, bg=WC.CARD)
        self._canvas_window = canvas.create_window((0, 0), window=self.list_inner, anchor="nw")
        self.list_inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfigure(self._canvas_window, width=e.width)
        )
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # 底部:操作区
        action_card = Card(self.parent, padding=14)
        action_card.pack(fill=tk.X, padx=12, pady=(0, 12))

        act_top = tk.Frame(action_card.pad_frame, bg=WC.CARD)
        act_top.pack(fill=tk.X)

        # 左:扫描 + 刷新
        left_act = tk.Frame(act_top, bg=WC.CARD)
        left_act.pack(side=tk.LEFT)
        self.scan_btn = wechat_button(left_act, "🔍 扫描", self._on_scan, kind="primary")
        self.scan_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.refresh_btn = wechat_button(left_act, "🔄 刷新磁盘", self._refresh_disk, kind="default")
        self.refresh_btn.pack(side=tk.LEFT)

        # 中:选项
        mid_act = tk.Frame(act_top, bg=WC.CARD)
        mid_act.pack(side=tk.LEFT, padx=24)
        self.create_restore_var = tk.IntVar(value=1)
        tk.Checkbutton(mid_act, text="清理前创建还原点",
                       variable=self.create_restore_var,
                       bg=WC.CARD, fg=WC.TEXT,
                       activebackground=WC.CARD, activeforeground=WC.TEXT,
                       selectcolor=WC.CARD,
                       font=("Microsoft YaHei UI", 9), relief="flat", bd=0
                       ).pack(side=tk.LEFT)
        self.empty_recycle_var = tk.IntVar(value=1)
        tk.Checkbutton(mid_act, text="同时清空回收站",
                       variable=self.empty_recycle_var,
                       bg=WC.CARD, fg=WC.TEXT,
                       activebackground=WC.CARD, activeforeground=WC.TEXT,
                       selectcolor=WC.CARD,
                       font=("Microsoft YaHei UI", 9), relief="flat", bd=0
                       ).pack(side=tk.LEFT, padx=(10, 0))

        # 右:取消 + 清理
        right_act = tk.Frame(act_top, bg=WC.CARD)
        right_act.pack(side=tk.RIGHT)
        self.cancel_btn = wechat_button(right_act, "取消", self._on_cancel, kind="default", state=tk.DISABLED)
        # 注意 state 参数需要 widget 创建后设置
        self.cancel_btn.config(state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.RIGHT, padx=(8, 0))
        self.clean_btn = wechat_button(right_act, "🧹 一键清理", self._on_clean, kind="danger")
        self.clean_btn.pack(side=tk.RIGHT)

        # 初始
        self._refresh_disk()
        self._refresh_list()

    def _refresh_disk(self):
        info = get_disk_usage("C")
        if info.get("error"):
            self.disk_total_lbl.config(text=f"获取失败: {info['error']}")
            return
        self.disk_total_lbl.config(text=f"C 盘总容量: {human_size(info['total'])}")
        self.disk_used_lbl.config(text=f"已用 {human_size(info['used'])} / 可用 {human_size(info['free'])}")
        pct = info["percent"]
        self.disk_bar["value"] = pct
        self.disk_percent_lbl.config(text=f"使用率: {pct:.1f}%", fg=WC.RED if pct > 85 else (WC.ORANGE if pct > 70 else WC.TEXT3))
        self.set_status(f"磁盘就绪 C 盘 {pct:.1f}%")

    def _refresh_list(self):
        for w in self.list_inner.winfo_children():
            w.destroy()
        for target in CLEAN_TARGETS:
            r = self.scan_results.get(target["id"], {})
            size = r.get("size", 0)
            files = r.get("files", 0)
            exists = r.get("exists", False)
            scanned = target["id"] in self.scan_results

            # 整行卡片
            row = tk.Frame(self.list_inner, bg=WC.CARD)
            row.pack(fill=tk.X, padx=2, pady=3)

            var = self.check_vars.get(target["id"])
            if var is None:
                var = tk.IntVar(value=1 if target["level"] == "safe" else 0)
                self.check_vars[target["id"]] = var
            chk = tk.Checkbutton(
                row, variable=var,
                bg=WC.CARD, fg=WC.TEXT,
                activebackground=WC.CARD, activeforeground=WC.TEXT,
                selectcolor=WC.CARD, relief="flat", bd=0, highlightthickness=0
            )
            chk.pack(side=tk.LEFT, padx=(6, 4), pady=8)

            mid = tk.Frame(row, bg=WC.CARD)
            mid.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=8)
            name_color = WC.ORANGE if target["level"] == "caution" else WC.TEXT
            tag = "  ⚠ 谨慎" if target["level"] == "caution" else ""
            tk.Label(mid, text=target["name"] + tag,
                     font=("Microsoft YaHei UI", 10, "bold"),
                     bg=WC.CARD, fg=name_color, anchor=tk.W
                     ).pack(anchor=tk.W)
            tk.Label(mid, text=target["desc"],
                     font=("Microsoft YaHei UI", 8),
                     bg=WC.CARD, fg=WC.TEXT3, anchor=tk.W, wraplength=480
                     ).pack(anchor=tk.W)

            right = tk.Frame(row, bg=WC.CARD)
            right.pack(side=tk.RIGHT, padx=12, pady=8)
            if scanned and exists and size > 0:
                tk.Label(right, text=human_size(size),
                         font=("Microsoft YaHei UI", 12, "bold"),
                         bg=WC.CARD, fg=WC.GREEN
                         ).pack(anchor=tk.E)
                tk.Label(right, text=f"{files} 个文件",
                         font=("Microsoft YaHei UI", 8),
                         bg=WC.CARD, fg=WC.TEXT3
                         ).pack(anchor=tk.E)
            elif scanned:
                tk.Label(right, text="—", font=("Microsoft YaHei UI", 12),
                         bg=WC.CARD, fg=WC.TEXT3).pack(anchor=tk.E)
            else:
                tk.Label(right, text="未扫描", font=("Microsoft YaHei UI", 9),
                         bg=WC.CARD, fg=WC.TEXT3).pack(anchor=tk.E)

    def _select_all(self):
        for v in self.check_vars.values():
            v.set(1)

    def _select_invert(self):
        for v in self.check_vars.values():
            v.set(1 - v.get())

    def _select_safe_only(self):
        for t in CLEAN_TARGETS:
            self.check_vars[t["id"]].set(1 if t["level"] == "safe" else 0)

    def _update_cleanable_summary(self):
        total = 0
        files = 0
        for tid, var in self.check_vars.items():
            if var.get():
                r = self.scan_results.get(tid, {})
                total += r.get("size", 0)
                files += r.get("files", 0)
        self.cleanable_lbl.config(text=human_size(total))
        self.cleanable_files_lbl.config(text=f"{files:,} 个文件")

    def _on_scan(self):
        if self.busy:
            self.log("上一次操作还没结束,稍等", "warn")
            return
        try:
            self.log("[按钮] 收到扫描请求", "info")
            self.busy = True
            self.scan_btn.config(state=tk.DISABLED)
            self.clean_btn.config(state=tk.DISABLED)
            self.cancel_btn.config(state=tk.NORMAL)
            self._refresh_disk()
            self.set_status("扫描中...", 0)
            self.scanner = Scanner(
                log_callback=lambda *a, **kw: self.log(a[0] if a else ""),
                progress_callback=lambda *a, **kw: self.set_status(
                    a[1] if len(a) > 1 else "", a[0] if a else 0
                ),
            )
            threading.Thread(target=self._scan_worker, daemon=True).start()
        except Exception as e:
            self.busy = False
            self.scan_btn.config(state=tk.NORMAL)
            self.clean_btn.config(state=tk.NORMAL)
            self.cancel_btn.config(state=tk.DISABLED)
            self.log(f"启动扫描失败: {e}", "err")
            import traceback
            self.log(traceback.format_exc(), "err")

    def _scan_worker(self):
        try:
            self.scanner.scan_all()
        except Exception as e:
            self.log(f"扫描出错: {e}", "err")
            import traceback
            self.log(traceback.format_exc(), "err")
        finally:
            self.parent.after(0, self._scan_done)

    def _scan_done(self):
        self.scan_results = self.scanner.results if self.scanner else {}
        self.busy = False
        self.scan_btn.config(state=tk.NORMAL)
        self.clean_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED)
        self.set_status("扫描完成", 1.0)
        self._refresh_list()
        self._update_cleanable_summary()
        self._refresh_disk()

    def _on_clean(self):
        if self.busy:
            return
        selected = [tid for tid, v in self.check_vars.items() if v.get()]
        if not selected:
            messagebox.showwarning("提示", "请先勾选要清理的项")
            return
        if not self.scan_results:
            if not messagebox.askyesno("未扫描", "还没扫描过,是否直接清理?"):
                return
        caution = [t for t in CLEAN_TARGETS if t["id"] in selected and t["level"] == "caution"]
        msg = f"将清理 {len(selected)} 个项目。\n\n"
        if caution:
            names = "、".join(t["name"] for t in caution[:3])
            more = f"等 {len(caution)} 个" if len(caution) > 3 else ""
            msg += f"⚠ 包含谨慎项: {names}{more}\n\n"
        msg += "是否继续?"
        if not messagebox.askyesno("确认清理", msg):
            return

        if self.create_restore_var.get():
            self.set_status("正在创建系统还原点...", 0)
            self.log("[备份] 创建系统还原点 ...", "info")
            if create_restore_point():
                self.log("  -> 还原点已创建", "ok")
            else:
                self.log("  -> 还原点创建失败(继续清理)", "warn")

        self.busy = True
        self.scan_btn.config(state=tk.DISABLED)
        self.clean_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.cleaner = Cleaner(
            log_callback=lambda *a, **kw: self.log(a[0] if a else ""),
            progress_callback=lambda *a, **kw: self.set_status(
                a[1] if len(a) > 1 else "", a[0] if a else 0
            ),
        )
        threading.Thread(target=self._clean_worker, args=(selected,), daemon=True).start()

    def _clean_worker(self, selected):
        try:
            result = self.cleaner.clean_all(selected)
            if self.empty_recycle_var.get():
                self.log("[回收站] 正在清空 ...", "info")
                if empty_recycle_bin():
                    self.log("  -> 回收站已清空", "ok")
                else:
                    self.log("  -> 回收站清空失败", "warn")
            self.log(
                f"🎉 清理完成!共释放 {human_size(result['freed'])},删除 {result['removed']} 个文件",
                "ok"
            )
        except Exception as e:
            self.log(f"清理出错: {e}", "err")
        finally:
            self.parent.after(0, self._clean_done)

    def _clean_done(self):
        self.busy = False
        self.scan_btn.config(state=tk.NORMAL)
        self.clean_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED)
        self.set_status("清理完成", 1.0)
        self.scan_results = {}
        self._refresh_list()
        self._refresh_disk()
        self._update_cleanable_summary()
        messagebox.showinfo("完成", "清理完成!建议重启浏览器使缓存完全生效。")

    def _on_cancel(self):
        if self.scanner:
            self.scanner.cancel()
        if self.cleaner:
            self.cleaner.cancel()
        self.log("已请求取消,请稍候...", "warn")


class BigFilesTab:
    """Tab 2: 大文件扫描"""
    def __init__(self, parent, log_fn, status_fn):
        self.parent = tk.Frame(parent, bg=WC.BG)
        self.log = log_fn
        self.set_status = status_fn
        self.busy = False
        self.analyzer = None
        self._build()

    def _build(self):
        # 工具栏
        bar = Card(self.parent, padding=12)
        bar.pack(fill=tk.X, padx=12, pady=(12, 8))
        bar_inner = bar.pad_frame

        top = tk.Frame(bar_inner, bg=WC.CARD)
        top.pack(fill=tk.X)

        tk.Label(top, text="扫描目录:", font=("Microsoft YaHei UI", 10),
                 bg=WC.CARD, fg=WC.TEXT2).pack(side=tk.LEFT, padx=(0, 6))
        self.path_var = tk.StringVar(value=str(Path.home()))
        path_entry = tk.Entry(top, textvariable=self.path_var,
                              font=("Microsoft YaHei UI", 10),
                              bg=WC.CARD, fg=WC.TEXT, relief="solid", bd=1,
                              highlightthickness=0, highlightbackground=WC.BORDER)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6), ipady=4)

        browse_btn = wechat_button(top, "📁 浏览", self._browse, kind="default")
        browse_btn.pack(side=tk.LEFT, padx=(0, 12))

        tk.Label(top, text="最小:", font=("Microsoft YaHei UI", 10),
                 bg=WC.CARD, fg=WC.TEXT2).pack(side=tk.LEFT, padx=(0, 4))
        self.min_size_var = tk.StringVar(value="100")
        size_combo = wechat_optionmenu(top, self.min_size_var, ["10", "50", "100", "500", "1024"], width=8)
        size_combo.pack(side=tk.LEFT, padx=(0, 4))
        tk.Label(top, text="MB", font=("Microsoft YaHei UI", 10),
                 bg=WC.CARD, fg=WC.TEXT2).pack(side=tk.LEFT, padx=(0, 12))

        tk.Label(top, text="显示:", font=("Microsoft YaHei UI", 10),
                 bg=WC.CARD, fg=WC.TEXT2).pack(side=tk.LEFT, padx=(0, 4))
        self.top_n_var = tk.StringVar(value="100")
        n_combo = wechat_optionmenu(top, self.top_n_var, ["50", "100", "200", "500"], width=6)
        n_combo.pack(side=tk.LEFT, padx=(0, 12))

        self.scan_btn = wechat_button(top, "🔍 开始扫描", self._on_scan, kind="primary")
        self.scan_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.cancel_btn = wechat_button(top, "取消", self._on_cancel, kind="default")
        self.cancel_btn.config(state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT)

        # 结果区
        result_card = Card(self.parent, padding=8)
        result_card.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        cols = ("size", "path", "mtime")
        self.tree = ttk.Treeview(result_card.pad_frame, columns=cols, show="headings",
                                 style="WC.Treeview")
        self.tree.heading("size", text="大小")
        self.tree.heading("path", text="路径")
        self.tree.heading("mtime", text="修改时间")
        self.tree.column("size", width=120, anchor=tk.E)
        self.tree.column("path", width=600, anchor=tk.W)
        self.tree.column("mtime", width=180, anchor=tk.W)
        self.tree.tag_configure("odd", background=WC.CARD)
        self.tree.tag_configure("even", background=WC.CARD_ALT)
        scroll = ttk.Scrollbar(result_card.pad_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 右键菜单
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.context_menu = tk.Menu(self.tree, tearoff=0)
        self.context_menu.add_command(label="在资源管理器中打开", command=self._open_in_explorer)
        self.context_menu.add_command(label="复制路径", command=self._copy_path)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="删除文件(到回收站)", command=self._delete_to_recycle)

    def _browse(self):
        d = filedialog.askdirectory(title="选择扫描目录", initialdir=self.path_var.get())
        if d:
            self.path_var.set(d)

    def _show_context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.tk_popup(event.x_root, event.y_root)

    def _get_selected_path(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.item(sel[0])["values"][1]

    def _open_in_explorer(self):
        p = self._get_selected_path()
        if p and os.path.exists(p):
            subprocess.run(["explorer", "/select,", p])

    def _copy_path(self):
        p = self._get_selected_path()
        if p:
            self.parent.clipboard_clear()
            self.parent.clipboard_append(p)
            self.log(f"已复制: {p}", "ok")

    def _delete_to_recycle(self):
        p = self._get_selected_path()
        if not p:
            return
        if not messagebox.askyesno("确认", f"删除此文件?(移到回收站)\n\n{p}"):
            return
        # 用 Windows 原生 SHFileOperationW(完全静默,无 PowerShell 窗口)
        if move_to_recycle_bin(p):
            self.log(f"已移到回收站: {p}", "ok")
            self.tree.delete(self.tree.selection()[0])
        else:
            self.log(f"移到回收站失败: {p}", "err")

    def _on_scan(self):
        if self.busy:
            return
        try:
            min_size_mb = int(self.min_size_var.get())
            top_n = int(self.top_n_var.get())
            path = self.path_var.get().strip()
        except ValueError:
            messagebox.showerror("错误", "最小大小和数量必须是数字")
            return
        if not path or not Path(path).exists():
            messagebox.showerror("错误", f"目录不存在: {path}")
            return

        # 清空结果
        for item in self.tree.get_children():
            self.tree.delete(item)

        self.busy = True
        self.scan_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.set_status(f"扫描 {path} ...", 0)
        self.log(f"[大文件] 开始扫描: {path} (≥ {min_size_mb} MB, Top {top_n})", "info")
        self.analyzer = Analyzer(
            log_callback=lambda *a, **kw: self.log(a[0] if a else ""),
            progress_callback=lambda *a, **kw: self.set_status(
                a[1] if len(a) > 1 else "", a[0] if a else 0
            ),
        )
        threading.Thread(
            target=self._scan_worker, args=(path, min_size_mb * 1024 * 1024, top_n),
            daemon=True
        ).start()

    def _scan_worker(self, path, min_size, top_n):
        try:
            results = self.analyzer.find_large_files(path, min_size=min_size, top_n=top_n)
            for i, r in enumerate(results):
                tag = "even" if i % 2 else "odd"
                mtime = datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M")
                self.parent.after(0, lambda r=r, tag=tag, mtime=mtime: self.tree.insert(
                    "", "end", values=(human_size(r["size"]), r["path"], mtime), tags=(tag,)
                ))
            self.log(f"[大文件] 完成,显示 {len(results)} 个", "ok")
        except Exception as e:
            self.log(f"[大文件] 扫描出错: {e}", "err")
        finally:
            self.parent.after(0, self._scan_done)

    def _scan_done(self):
        self.busy = False
        self.scan_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED)
        self.set_status("扫描完成", 1.0)

    def _on_cancel(self):
        if self.analyzer:
            self.analyzer.cancel()
        self.log("已请求取消,请稍候...", "warn")


class FolderSizeTab:
    """Tab 3: 文件夹大小排序"""
    def __init__(self, parent, log_fn, status_fn):
        self.parent = tk.Frame(parent, bg=WC.BG)
        self.log = log_fn
        self.set_status = status_fn
        self.busy = False
        self.analyzer = None
        self._build()

    def _build(self):
        bar = Card(self.parent, padding=12)
        bar.pack(fill=tk.X, padx=12, pady=(12, 8))

        top = tk.Frame(bar.pad_frame, bg=WC.CARD)
        top.pack(fill=tk.X)

        tk.Label(top, text="扫描目录:", font=("Microsoft YaHei UI", 10),
                 bg=WC.CARD, fg=WC.TEXT2).pack(side=tk.LEFT, padx=(0, 6))
        self.path_var = tk.StringVar(value=str(Path.home()))
        path_entry = tk.Entry(top, textvariable=self.path_var,
                              font=("Microsoft YaHei UI", 10),
                              bg=WC.CARD, fg=WC.TEXT, relief="solid", bd=1,
                              highlightthickness=0, highlightbackground=WC.BORDER)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6), ipady=4)

        browse_btn = wechat_button(top, "📁 浏览", self._browse, kind="default")
        browse_btn.pack(side=tk.LEFT, padx=(0, 12))

        tk.Label(top, text="深度:", font=("Microsoft YaHei UI", 10),
                 bg=WC.CARD, fg=WC.TEXT2).pack(side=tk.LEFT, padx=(0, 4))
        self.depth_var = tk.StringVar(value="3")
        depth_combo = wechat_optionmenu(top, self.depth_var, ["1", "2", "3", "4", "5"], width=4)
        depth_combo.pack(side=tk.LEFT, padx=(0, 4))
        tk.Label(top, text="层", font=("Microsoft YaHei UI", 10),
                 bg=WC.CARD, fg=WC.TEXT2).pack(side=tk.LEFT, padx=(0, 12))

        tk.Label(top, text="显示:", font=("Microsoft YaHei UI", 10),
                 bg=WC.CARD, fg=WC.TEXT2).pack(side=tk.LEFT, padx=(0, 4))
        self.top_n_var = tk.StringVar(value="50")
        n_combo = wechat_optionmenu(top, self.top_n_var, ["20", "50", "100", "200"], width=6)
        n_combo.pack(side=tk.LEFT, padx=(0, 12))

        self.scan_btn = wechat_button(top, "🔍 开始扫描", self._on_scan, kind="primary")
        self.scan_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.cancel_btn = wechat_button(top, "取消", self._on_cancel, kind="default")
        self.cancel_btn.config(state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT)

        # 结果区
        result_card = Card(self.parent, padding=8)
        result_card.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        cols = ("size", "files", "path")
        self.tree = ttk.Treeview(result_card.pad_frame, columns=cols, show="headings",
                                 style="WC.Treeview")
        self.tree.heading("size", text="大小")
        self.tree.heading("files", text="文件数")
        self.tree.heading("path", text="路径")
        self.tree.column("size", width=120, anchor=tk.E)
        self.tree.column("files", width=100, anchor=tk.E)
        self.tree.column("path", width=700, anchor=tk.W)
        self.tree.tag_configure("odd", background=WC.CARD)
        self.tree.tag_configure("even", background=WC.CARD_ALT)
        scroll = ttk.Scrollbar(result_card.pad_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 右键菜单
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.context_menu = tk.Menu(self.tree, tearoff=0)
        self.context_menu.add_command(label="在资源管理器中打开", command=self._open_in_explorer)
        self.context_menu.add_command(label="复制路径", command=self._copy_path)

    def _browse(self):
        d = filedialog.askdirectory(title="选择扫描目录", initialdir=self.path_var.get())
        if d:
            self.path_var.set(d)

    def _show_context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.tk_popup(event.x_root, event.y_root)

    def _get_selected_path(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.item(sel[0])["values"][2]

    def _open_in_explorer(self):
        p = self._get_selected_path()
        if p and os.path.exists(p):
            subprocess.run(["explorer", p])

    def _copy_path(self):
        p = self._get_selected_path()
        if p:
            self.parent.clipboard_clear()
            self.parent.clipboard_append(p)
            self.log(f"已复制: {p}", "ok")

    def _on_scan(self):
        if self.busy:
            return
        try:
            depth = int(self.depth_var.get())
            top_n = int(self.top_n_var.get())
            path = self.path_var.get().strip()
        except ValueError:
            messagebox.showerror("错误", "深度和数量必须是数字")
            return
        if not path or not Path(path).exists():
            messagebox.showerror("错误", f"目录不存在: {path}")
            return

        for item in self.tree.get_children():
            self.tree.delete(item)

        self.busy = True
        self.scan_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.set_status(f"扫描 {path} ...", 0)
        self.log(f"[文件夹] 开始扫描: {path} (深度 {depth}, Top {top_n})", "info")
        self.analyzer = Analyzer(
            log_callback=lambda *a, **kw: self.log(a[0] if a else ""),
            progress_callback=lambda *a, **kw: self.set_status(
                a[1] if len(a) > 1 else "", a[0] if a else 0
            ),
        )
        threading.Thread(
            target=self._scan_worker, args=(path, depth, top_n),
            daemon=True
        ).start()

    def _scan_worker(self, path, depth, top_n):
        try:
            results = self.analyzer.find_large_dirs(path, top_n=top_n, max_depth=depth)
            for i, r in enumerate(results):
                tag = "even" if i % 2 else "odd"
                self.parent.after(0, lambda r=r, tag=tag: self.tree.insert(
                    "", "end",
                    values=(human_size(r["size"]), f"{r['files']:,}", r["path"]),
                    tags=(tag,)
                ))
            self.log(f"[文件夹] 完成,显示 {len(results)} 个", "ok")
        except Exception as e:
            self.log(f"[文件夹] 扫描出错: {e}", "err")
        finally:
            self.parent.after(0, self._scan_done)

    def _scan_done(self):
        self.busy = False
        self.scan_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED)
        self.set_status("扫描完成", 1.0)

    def _on_cancel(self):
        if self.analyzer:
            self.analyzer.cancel()
        self.log("已请求取消,请稍候...", "warn")


class DuplicateTab:
    """Tab 4: 重复文件查找"""
    def __init__(self, parent, log_fn, status_fn):
        self.parent = tk.Frame(parent, bg=WC.BG)
        self.log = log_fn
        self.set_status = status_fn
        self.busy = False
        self.analyzer = None
        self.duplicates = []  # 全部组
        self.check_vars = {}  # path -> IntVar
        self._build()

    def _build(self):
        # 工具栏
        bar = Card(self.parent, padding=12)
        bar.pack(fill=tk.X, padx=12, pady=(12, 8))

        top = tk.Frame(bar.pad_frame, bg=WC.CARD)
        top.pack(fill=tk.X)

        tk.Label(top, text="扫描目录:", font=("Microsoft YaHei UI", 10),
                 bg=WC.CARD, fg=WC.TEXT2).pack(side=tk.LEFT, padx=(0, 6))
        self.path_var = tk.StringVar(value=str(Path.home() / "Documents"))
        path_entry = tk.Entry(top, textvariable=self.path_var,
                              font=("Microsoft YaHei UI", 10),
                              bg=WC.CARD, fg=WC.TEXT, relief="solid", bd=1,
                              highlightthickness=0, highlightbackground=WC.BORDER)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6), ipady=4)

        browse_btn = wechat_button(top, "📁 浏览", self._browse, kind="default")
        browse_btn.pack(side=tk.LEFT, padx=(0, 12))

        tk.Label(top, text="最小:", font=("Microsoft YaHei UI", 10),
                 bg=WC.CARD, fg=WC.TEXT2).pack(side=tk.LEFT, padx=(0, 4))
        self.min_size_var = tk.StringVar(value="1")
        size_combo = wechat_optionmenu(top, self.min_size_var, ["1", "10", "50", "100", "500"], width=6)
        size_combo.pack(side=tk.LEFT, padx=(0, 4))
        tk.Label(top, text="MB", font=("Microsoft YaHei UI", 10),
                 bg=WC.CARD, fg=WC.TEXT2).pack(side=tk.LEFT, padx=(0, 12))

        self.scan_btn = wechat_button(top, "🔍 查找重复", self._on_scan, kind="primary")
        self.scan_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.cancel_btn = wechat_button(top, "取消", self._on_cancel, kind="default")
        self.cancel_btn.config(state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT, padx=(0, 6))

        # 统计 + 删除按钮
        self.summary_lbl = tk.Label(top, text="", font=("Microsoft YaHei UI", 10),
                                    bg=WC.CARD, fg=WC.GREEN)
        self.summary_lbl.pack(side=tk.LEFT, padx=(12, 6))
        self.delete_btn = wechat_button(top, "🗑 删除选中", self._on_delete, kind="danger")
        self.delete_btn.config(state=tk.DISABLED)
        self.delete_btn.pack(side=tk.RIGHT)

        # 结果区(分组展示)
        result_card = Card(self.parent, padding=8)
        result_card.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        # 用 Treeview,带组头(用 ¶ 标识)
        cols = ("select", "path", "size")
        self.tree = ttk.Treeview(result_card.pad_frame, columns=cols, show="headings",
                                 style="WC.Treeview")
        self.tree.heading("select", text="选择")
        self.tree.heading("path", text="路径")
        self.tree.heading("size", text="大小")
        self.tree.column("select", width=60, anchor=tk.CENTER)
        self.tree.column("path", width=750, anchor=tk.W)
        self.tree.column("size", width=120, anchor=tk.E)
        self.tree.tag_configure("group", background=WC.GREEN_LIGHT,
                                foreground=WC.TEXT, font=("Microsoft YaHei UI", 10, "bold"))
        self.tree.tag_configure("file", background=WC.CARD)
        scroll = ttk.Scrollbar(result_card.pad_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.context_menu = tk.Menu(self.tree, tearoff=0)
        self.context_menu.add_command(label="在资源管理器中打开", command=self._open_in_explorer)
        self.context_menu.add_command(label="复制路径", command=self._copy_path)

    def _browse(self):
        d = filedialog.askdirectory(title="选择扫描目录", initialdir=self.path_var.get())
        if d:
            self.path_var.set(d)

    def _on_click(self, event):
        item = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not item or col != "#1":
            return
        vals = self.tree.item(item, "values")
        if not vals or vals[0] == "■":  # 组头
            return
        path = vals[1]
        var = self.check_vars.get(path)
        if var is None:
            return
        var.set(1 - var.get())
        new_mark = "☑" if var.get() else "☐"
        self.tree.item(item, values=(new_mark, path, vals[2]))

    def _show_context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.tk_popup(event.x_root, event.y_root)

    def _get_selected_path(self):
        sel = self.tree.selection()
        if not sel:
            return None
        vals = self.tree.item(sel[0])["values"]
        return vals[1] if len(vals) > 1 else None

    def _open_in_explorer(self):
        p = self._get_selected_path()
        if p and os.path.exists(p):
            subprocess.run(["explorer", "/select,", p])

    def _copy_path(self):
        p = self._get_selected_path()
        if p:
            self.parent.clipboard_clear()
            self.parent.clipboard_append(p)
            self.log(f"已复制: {p}", "ok")

    def _on_scan(self):
        if self.busy:
            return
        try:
            min_size_mb = int(self.min_size_var.get())
            path = self.path_var.get().strip()
        except ValueError:
            messagebox.showerror("错误", "最小大小必须是数字")
            return
        if not path or not Path(path).exists():
            messagebox.showerror("错误", f"目录不存在: {path}")
            return

        for item in self.tree.get_children():
            self.tree.delete(item)
        self.check_vars.clear()
        self.duplicates = []

        self.busy = True
        self.scan_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.delete_btn.config(state=tk.DISABLED)
        self.set_status(f"查找重复 {path} ...", 0)
        self.log(f"[重复] 开始: {path} (≥ {min_size_mb} MB)", "info")
        self.analyzer = Analyzer(
            log_callback=lambda *a, **kw: self.log(a[0] if a else ""),
            progress_callback=lambda *a, **kw: self.set_status(
                a[1] if len(a) > 1 else "", a[0] if a else 0
            ),
        )
        threading.Thread(
            target=self._scan_worker, args=(path, min_size_mb * 1024 * 1024),
            daemon=True
        ).start()

    def _scan_worker(self, path, min_size):
        try:
            results = self.analyzer.find_duplicates(path, min_size=min_size)
            self.duplicates = results
            # 渲染
            for i, group in enumerate(results, 1):
                total_size = group[0]["size"] if group else 0
                wasted = total_size * (len(group) - 1)
                header = f"📁 第 {i} 组 · {len(group)} 个文件 · 浪费 {human_size(wasted)}"
                self.parent.after(0, lambda h=header: self.tree.insert(
                    "", "end", values=("■", h, ""), tags=("group",), open=True
                ))
                for f in group:
                    p = f["path"]
                    var = tk.IntVar(value=1 if False else 0)  # 默认不勾,让用户自己挑保留哪个
                    self.check_vars[p] = var
                    self.parent.after(0, lambda p=p, s=human_size(f["size"]): self.tree.insert(
                        "", "end", values=("☐", p, s), tags=("file",)
                    ))
            total_waste = sum(
                group[0]["size"] * (len(group) - 1) for group in results if group
            )
            self.parent.after(0, lambda: self.summary_lbl.config(
                text=f"共 {len(results)} 组,可节省 {human_size(total_waste)}"
            ))
            self.parent.after(0, lambda: self.delete_btn.config(state=tk.NORMAL))
            self.log(f"[重复] 完成,共 {len(results)} 组", "ok")
        except Exception as e:
            self.log(f"[重复] 出错: {e}", "err")
            import traceback
            self.log(traceback.format_exc(), "err")
        finally:
            self.parent.after(0, self._scan_done)

    def _scan_done(self):
        self.busy = False
        self.scan_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED)
        self.set_status("查找完成", 1.0)

    def _on_delete(self):
        if not self.duplicates:
            return
        # 收集选中的
        to_delete = []
        for path, var in self.check_vars.items():
            if var.get():
                to_delete.append(path)
        if not to_delete:
            messagebox.showwarning("提示", "请先勾选要删除的文件")
            return
        if not messagebox.askyesno(
            "确认删除",
            f"将删除 {len(to_delete)} 个文件到回收站。\n\n"
            f"建议:每组至少保留 1 个文件!\n\n是否继续?"
        ):
            return
        # 逐个移动到回收站(用 Windows 原生 SHFileOperationW,完全静默)
        ok, fail = 0, 0
        for p in to_delete:
            if move_to_recycle_bin(p):
                ok += 1
            else:
                fail += 1
        self.log(f"[删除] 完成: {ok} 成功,{fail} 失败", "ok" if fail == 0 else "warn")
        messagebox.showinfo("完成", f"已移动 {ok} 个到回收站,{fail} 个失败")

    def _on_cancel(self):
        if self.analyzer:
            self.analyzer.cancel()
        self.log("已请求取消,请稍候...", "warn")


# ====================== 主窗口 ======================


class MainWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("1000x720")
        self.root.minsize(900, 620)
        self.root.configure(bg=WC.BG)

        make_styled_styles()

        # 顶部标题栏
        self._build_title()

        # 状态 + 进度
        self._build_status_bar()

        # 中间:Notebook
        self.notebook = ttk.Notebook(self.root, style="WC.TNotebook")
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 4))

        # 底部:日志
        self._build_log_panel()

        # 4 个 tab
        self.tab_clean = CleanerTab(self.notebook, self._log, self._set_status)
        self.tab_big = BigFilesTab(self.notebook, self._log, self._set_status)
        self.tab_folder = FolderSizeTab(self.notebook, self._log, self._set_status)
        self.tab_dup = DuplicateTab(self.notebook, self._log, self._set_status)

        self.notebook.add(self.tab_clean.parent, text="🧹  垃圾清理")
        self.notebook.add(self.tab_big.parent, text="📦  大文件")
        self.notebook.add(self.tab_folder.parent, text="📂  文件夹大小")
        self.notebook.add(self.tab_dup.parent, text="🔁  重复文件")

    def _build_title(self):
        frm = tk.Frame(self.root, bg=WC.BG)
        frm.pack(fill=tk.X, padx=16, pady=(14, 4))
        tk.Label(
            frm, text="🧹 " + APP_NAME,
            font=("Microsoft YaHei UI", 16, "bold"),
            bg=WC.BG, fg=WC.TEXT
        ).pack(side=tk.LEFT)
        tk.Label(
            frm, text=f"  v{APP_VERSION}",
            font=("Microsoft YaHei UI", 10),
            bg=WC.BG, fg=WC.TEXT3
        ).pack(side=tk.LEFT)
        tk.Label(
            frm, text="  · Win11 工具集",
            font=("Microsoft YaHei UI", 9),
            bg=WC.BG, fg=WC.TEXT3
        ).pack(side=tk.LEFT)
        # 右:管理员标识
        self.admin_lbl = tk.Label(
            frm,
            text="✓ 管理员" if is_admin() else "⚠ 非管理员",
            font=("Microsoft YaHei UI", 9),
            bg=WC.BG,
            fg=WC.GREEN if is_admin() else WC.ORANGE
        )
        self.admin_lbl.pack(side=tk.RIGHT)

    def _build_status_bar(self):
        frm = tk.Frame(self.root, bg=WC.BG)
        frm.pack(fill=tk.X, padx=16, pady=(0, 4))
        self.status_lbl = tk.Label(
            frm, text="就绪",
            font=("Microsoft YaHei UI", 9),
            bg=WC.BG, fg=WC.TEXT2, anchor=tk.W
        )
        self.status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.progress = ttk.Progressbar(
            frm, maximum=100, value=0, mode="determinate",
            style="WC.Horizontal.TProgressbar", length=200
        )
        self.progress.pack(side=tk.RIGHT)

    def _build_log_panel(self):
        frm = tk.Frame(self.root, bg=WC.BG)
        frm.pack(fill=tk.X, padx=12, pady=(4, 12))

        # 标题 + 清除
        title_row = tk.Frame(frm, bg=WC.BG)
        title_row.pack(fill=tk.X, pady=(0, 4))
        tk.Label(title_row, text="日志", font=("Microsoft YaHei UI", 10, "bold"),
                 bg=WC.BG, fg=WC.TEXT2).pack(side=tk.LEFT)
        clear_btn = wechat_button(title_row, "清除", self._clear_log, kind="ghost",
                                  font=("Microsoft YaHei UI", 9), padx=10, pady=2)
        clear_btn.pack(side=tk.RIGHT)

        # 日志框
        text_frame = tk.Frame(frm, bg=WC.BORDER_LIGHT)
        text_frame.pack(fill=tk.X)
        self.log_text = scrolledtext.ScrolledText(
            text_frame, bg="#FAFAFA", fg=WC.TEXT,
            font=("Consolas", 9), relief="flat", bd=0,
            insertbackground=WC.TEXT, height=7, wrap=tk.WORD,
            highlightthickness=0
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        self.log_text.config(state=tk.DISABLED)

        self._log(f"欢迎使用 {APP_NAME} v{APP_VERSION}")
        if is_admin():
            self._log("✓ 已以管理员身份运行", "ok")
        else:
            self._log("⚠ 非管理员,部分系统目录无法访问", "warn")

    def _log(self, msg: str, level: str = "info"):
        if not hasattr(self, "log_text"):
            return
        self.log_text.config(state=tk.NORMAL)
        ts = datetime.now().strftime("%H:%M:%S")
        prefix = {"info": "  ", "ok": "✓ ", "warn": "! ", "err": "✗ "}.get(level, "  ")
        self.log_text.insert(tk.END, f"[{ts}] {prefix}{msg}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _set_status(self, msg: str, percent: float = None):
        self.status_lbl.config(text=msg)
        if percent is not None:
            self.progress["value"] = percent * 100

    def run(self):
        self.root.mainloop()


# ====================== CLI / 入口 ======================


def run_cli():
    print(f"{APP_NAME} v{APP_VERSION} (CLI 模式)")
    print("=" * 60)
    info = get_disk_usage("C")
    print(f"C 盘: {human_size(info['used'])} / {human_size(info['total'])} ({info['percent']:.1f}%)")
    print("\n选项:1.清理  2.大文件  3.文件夹大小  4.重复文件")
    choice = input("> ").strip()
    if choice == "1":
        sc = Scanner(log_callback=lambda *a, **kw: print(*a))
        sc.scan_all()
        if input("\n确认清理? (y/N) > ").strip().lower() == "y":
            Cleaner(log_callback=lambda *a, **kw: print(*a)).clean_all(
                [t["id"] for t in CLEAN_TARGETS]
            )
    elif choice == "2":
        path = input("扫描目录 > ").strip() or "."
        try:
            min_mb = int(input("最小 MB > ").strip() or "100")
        except ValueError:
            min_mb = 100
        a = Analyzer(log_callback=lambda *a, **kw: print(*a))
        results = a.find_large_files(path, min_size=min_mb * 1024 * 1024, top_n=100)
        for r in results[:20]:
            print(f"  {human_size(r['size']):>10}  {r['path']}")
    elif choice == "3":
        path = input("扫描目录 > ").strip() or "."
        a = Analyzer(log_callback=lambda *a, **kw: print(*a))
        results = a.find_large_dirs(path, top_n=50, max_depth=3)
        for r in results[:20]:
            print(f"  {human_size(r['size']):>10}  {r['path']}")
    elif choice == "4":
        path = input("扫描目录 > ").strip() or "."
        try:
            min_mb = int(input("最小 MB > ").strip() or "1")
        except ValueError:
            min_mb = 1
        a = Analyzer(log_callback=lambda *a, **kw: print(*a))
        results = a.find_duplicates(path, min_size=min_mb * 1024 * 1024)
        for i, group in enumerate(results, 1):
            print(f"\n组 {i} ({len(group)} 个,大小 {human_size(group[0]['size'])}):")
            for f in group:
                print(f"  {f['path']}")


def main():
    if not is_admin():
        print("=" * 60)
        print("  ⚠ 当前不是管理员权限,部分系统目录无法访问")
        print("  请右键 [运行.bat] -> [以管理员身份运行]")
        print("=" * 60)
        if "--cli" in sys.argv:
            input("按回车退出...")
            return

    if TK_AVAILABLE and "--cli" not in sys.argv:
        try:
            MainWindow().run()
            return
        except Exception as e:
            print(f"GUI 启动失败: {e},回退 CLI")
    run_cli()


if __name__ == "__main__":
    main()
