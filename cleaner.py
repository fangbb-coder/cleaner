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
import base64
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ====================== 全局常量 ======================

APP_NAME = "绿色垃圾文件清理器"
APP_VERSION = "3.3.0"

# ====================== 日志系统 ======================
# 日志写入 %APPDATA%\GreenCleaner\green_cleaner.log
# WARNING/ERROR 关键路径,DEBUG 详细 IO。
_APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "GreenCleaner"
try:
    _APP_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    # 容错:无权限时退到 home
    _APP_DIR = Path.home() / "GreenCleaner"
    try:
        _APP_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        _APP_DIR = Path.cwd() / "GreenCleaner"
        try:
            _APP_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

_LOG_FILE = _APP_DIR / "green_cleaner.log"
_logger = logging.getLogger("green_cleaner")
_logger.setLevel(logging.DEBUG)
if not _logger.handlers:
    try:
        _fh = RotatingFileHandler(
            str(_LOG_FILE), maxBytes=2 * 1024 * 1024, backupCount=3,
            encoding="utf-8",
        )
        _fh.setLevel(logging.DEBUG)
        _formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        _fh.setFormatter(_formatter)
        _logger.addHandler(_fh)
    except Exception:
        # 容错:无法写文件时只输出到 stderr
        _sh = logging.StreamHandler()
        _sh.setLevel(logging.WARNING)
        _sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        _logger.addHandler(_sh)
_logger.propagate = False


def get_logger():
    """对外暴露 logger 实例,供模块内各处调用。"""
    return _logger


# ====================== CLEAN_TARGETS 数据模型 ======================
# 质量改进:把清理项从 dict 列表改为 dataclass + 枚举,
# 启动时 assert all_targets_valid(),任何缺字段/非法 kind/level 立即崩。

from dataclasses import dataclass, field
from enum import Enum


class Level(str, Enum):
    """清理档位。继承 str 让 Level.SAFE == "safe" 为 True,便于向后兼容 dict 风格的访问。"""
    SAFE = "safe"
    CAUTION = "caution"
    ADVANCED = "advanced"


class Kind(str, Enum):
    """清理项的执行类型。"""
    FILES = "files"
    WILD_TEMP = "wild_temp"
    COMMAND = "command"


@dataclass
class CleanTarget:
    """单个清理项的数据模型。

    字段:
      id              唯一标识
      name            UI 显示名
      level           safe/caution/advanced(枚举)
      desc            描述(UI tooltip / 日志)
      kind            files / wild_temp / command(枚举)
      paths           files 类型使用
      roots           wild_temp 类型使用
      patterns        wild_temp 类型使用(文件名通配)
      min_age_days    wild_temp 年龄阈值(天)
      min_size        wild_temp 大小阈值(字节)
      command_id      command 类型:AllowedCommand 表里的注册 id
    """
    id: str
    name: str
    level: Level
    desc: str
    kind: Kind = Kind.FILES
    paths: list = field(default_factory=list)
    roots: list = field(default_factory=list)
    patterns: list = field(default_factory=list)
    min_age_days: int = 7
    min_size: int = 50 * 1024 * 1024
    command_id: str = ""

    def __post_init__(self):
        # 容忍字符串入参(原 dict 是字符串)
        if isinstance(self.level, str):
            try:
                self.level = Level(self.level)
            except ValueError:
                raise ValueError(f"非法 level: {self.level!r} (id={self.id})")
        if isinstance(self.kind, str):
            try:
                self.kind = Kind(self.kind)
            except ValueError:
                raise ValueError(f"非法 kind: {self.kind!r} (id={self.id})")
        if not self.id or not isinstance(self.id, str):
            raise ValueError("CleanTarget 缺 id")
        if self.kind == Kind.FILES and not self.paths:
            raise ValueError(f"files 类型缺 paths: id={self.id}")
        if self.kind == Kind.WILD_TEMP:
            if not self.roots and not self.paths:
                raise ValueError(f"wild_temp 缺 roots/paths: id={self.id}")
            if not self.patterns:
                raise ValueError(f"wild_temp 缺 patterns: id={self.id}")
        if self.kind == Kind.COMMAND:
            if not self.command_id:
                raise ValueError(f"command 类型必须显式提供 command_id: id={self.id}")
            if build_allowed_command(self.command_id) is None:
                raise ValueError(f"command_id 未授权: {self.command_id!r} (id={self.id})")

    # 向后兼容:原有 dict 风格的访问 `t["id"]` / `t.get("level")` / `t.get("kind", "files")` 全部沿用
    def __getitem__(self, key):
        if key == "command":
            # 兼容旧字段访问;返回 AllowedCommand 解析结果
            return build_allowed_command(self.command_id) or []
        if not isinstance(key, str):
            # 阻止 Python 把本类当 iterable 回退迭代(否则 `x in t` 会调 t[0] 崩)
            raise TypeError(f"CleanTarget 索引必须为字符串,实际 {type(key).__name__}")
        return getattr(self, key)

    def get(self, key, default=None):
        try:
            return self[key]
        except AttributeError:
            return default

    def __contains__(self, key):
        # 让 `"id" in t` / `"paths" in t` 这类检查按字段名判断,不触发 iterable 回退
        if not isinstance(key, str):
            return False
        return hasattr(self, key)


def all_targets_valid(targets) -> bool:
    """启动时校验:每项 id 唯一、必填字段齐全、kind/level 合法、command 类型有授权命令。"""
    seen = set()
    for t in targets:
        # CleanTarget 实例
        if not isinstance(t, CleanTarget):
            raise TypeError(f"CLEAN_TARGETS 含非 CleanTarget 项: {type(t).__name__}")
        if t.id in seen:
            raise ValueError(f"CLEAN_TARGETS 重复 id: {t.id}")
        seen.add(t.id)
    return True


# ====================== 命令白名单枚举(P0 安全) ======================
# 必须在 CleanTarget.__post_init__ 之前定义,否则 CLEAN_TARGETS 校验时找不到。

class AllowedCommand:
    """命令白名单:任何 command target 的执行命令只能从这张表构造。

    设计目的:阻断「任意 list[str] 透传到 subprocess.run」这一类注入面。
    CLEAN_TARGETS 里 command 类型只能填 command_id(字符串),实际命令通过
    build_allowed_command(id) 解析得到。
    """
    POWERCFG_HIBERNATION_OFF = ("hibernation_disable", "powercfg", "/h", "off")
    VSSADMIN_DELETE_SHADOWS_OLDEST = (
        "vss_shadow_cleanup",
        "vssadmin", "delete", "shadows", "/for=C:", "/oldest", "/quiet",
    )

    _TABLE = {}

    @classmethod
    def _build_table(cls):
        if cls._TABLE:
            return
        for attr in dir(cls):
            if attr.startswith("_") or not attr.isupper():
                continue
            val = getattr(cls, attr, None)
            if not (isinstance(val, tuple) and len(val) >= 3 and isinstance(val[0], str)):
                continue
            cid = val[0]
            cls._TABLE[cid] = list(val[1:])

    @classmethod
    def all(cls):
        cls._build_table()
        return dict(cls._TABLE)


def build_allowed_command(command_id: str):
    """根据 command_id 拿到白名单命令(冻结 list 副本)。

    返回 None 表示该 id 未授权。
    """
    table = AllowedCommand.all()
    if command_id not in table:
        return None
    return list(table[command_id])

# ====================== 微信风格配色 ======================

class _LightTheme:
    """浅色(微信)主题"""
    BG = "#F7F7F7"
    CARD = "#FFFFFF"
    CARD_ALT = "#FAFAFA"
    GREEN = "#07C160"
    GREEN_DARK = "#06AE56"
    GREEN_LIGHT = "#E8F5E9"
    BLUE = "#10AEFF"
    RED = "#FA5151"
    RED_DARK = "#E54B4B"
    ORANGE = "#FA9D3B"
    TEXT = "#191919"
    TEXT2 = "#888888"
    TEXT3 = "#B2B2B2"
    BORDER = "#E5E5E5"
    BORDER_LIGHT = "#EFEFEF"
    TAB_BG = "#EDEDED"
    SEL_BG = "#E8F5E9"
    LOG_BG = "#FAFAFA"
    LOG_FG = "#191919"
    ENTRY_BG = "#FFFFFF"
    TREE_HEAD = "#EDEDED"
    TREE_ODD = "#FFFFFF"
    TREE_EVEN = "#FAFAFA"
    PROGRESS_TROUGH = "#EFEFEF"


class _DarkTheme:
    """暗色主题(深灰底 + 微信绿保留)"""
    BG = "#1E1E2E"            # 深灰主背景
    CARD = "#2A2A3E"          # 卡片深紫灰
    CARD_ALT = "#313145"      # 浅一档
    GREEN = "#07C160"         # 保留微信绿
    GREEN_DARK = "#06AE56"
    GREEN_LIGHT = "#2D4A35"   # 暗背景下的浅绿
    BLUE = "#10AEFF"
    RED = "#FA5151"
    RED_DARK = "#E54B4B"
    ORANGE = "#FA9D3B"
    TEXT = "#E4E4F4"          # 主文字浅色
    TEXT2 = "#A0A0B8"
    TEXT3 = "#6C7086"
    BORDER = "#3A3A52"
    BORDER_LIGHT = "#2A2A3E"
    TAB_BG = "#252538"
    SEL_BG = "#2D4A35"
    LOG_BG = "#181825"        # 日志更深一点
    LOG_FG = "#CDD6F4"
    ENTRY_BG = "#2A2A3E"
    TREE_HEAD = "#313145"
    TREE_ODD = "#2A2A3E"
    TREE_EVEN = "#313145"
    PROGRESS_TROUGH = "#3A3A52"


# 保持 WC 这个名字向后兼容(初始 = 浅色)
class WC:
    BG = _LightTheme.BG
    CARD = _LightTheme.CARD
    CARD_ALT = _LightTheme.CARD_ALT
    GREEN = _LightTheme.GREEN
    GREEN_DARK = _LightTheme.GREEN_DARK
    GREEN_LIGHT = _LightTheme.GREEN_LIGHT
    BLUE = _LightTheme.BLUE
    RED = _LightTheme.RED
    RED_DARK = _LightTheme.RED_DARK
    ORANGE = _LightTheme.ORANGE
    TEXT = _LightTheme.TEXT
    TEXT2 = _LightTheme.TEXT2
    TEXT3 = _LightTheme.TEXT3
    BORDER = _LightTheme.BORDER
    BORDER_LIGHT = _LightTheme.BORDER_LIGHT
    TAB_BG = _LightTheme.TAB_BG
    SEL_BG = _LightTheme.SEL_BG


# 当前主题(全局,可被 ThemeManager 切换)
CURRENT_THEME = _LightTheme


class ThemeManager:
    """主题切换:维护一组回调,切换时通知所有 widget 重画。"""
    _observers = []

    @classmethod
    def register(cls, fn):
        cls._observers.append(fn)

    @classmethod
    def switch(cls, theme_cls):
        global CURRENT_THEME
        CURRENT_THEME = theme_cls
        # 同步到 WC
        for attr in [a for a in dir(_LightTheme) if not a.startswith("_")]:
            setattr(WC, attr, getattr(theme_cls, attr))
        # 通知所有观察者
        for fn in cls._observers:
            try:
                fn()
            except Exception:
                pass


def apply_theme(theme_cls=None):
    """便捷:切换并广播。"""
    if theme_cls is None:
        theme_cls = _DarkTheme if CURRENT_THEME is _LightTheme else _LightTheme
    ThemeManager.switch(theme_cls)
    return theme_cls

# ====================== 清理项配置 ======================

# level 含义:
#   "safe"     = 默认勾选,删除不影响系统功能
#   "caution"  = 默认不勾,可能影响加速或占用大量空间
#   "advanced" = 默认不勾,需展开高级区才能看到,操作更激进或针对系统组件
_CLEAN_TARGET_SPECS = [
    # ====== 一、安全项(默认勾选) ======
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
        "id": "firefox_cache",
        "name": "Firefox 浏览器缓存",
        "paths": [
            r"%LOCALAPPDATA%\Mozilla\Firefox\Profiles\*\cache2",
            r"%LOCALAPPDATA%\Mozilla\Firefox\Profiles\*\thumbnails",
            r"%LOCALAPPDATA%\Mozilla\Firefox\Profiles\*\OfflineCache",
        ],
        "level": "safe",
        "desc": "Firefox 缓存(关闭 Firefox 后清理更彻底)",
    },
    {
        "id": "edge_deep_cache",
        "name": "Edge 浏览器深度缓存",
        "paths": [
            r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\IndexedDB",
            r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Service Worker",
            r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Storage",
            r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\File System",
        ],
        "level": "safe",
        "desc": "Edge IndexedDB / Service Worker / 本地存储(可释放数 GB)",
    },
    {
        "id": "chrome_deep_cache",
        "name": "Chrome 浏览器深度缓存",
        "paths": [
            r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\IndexedDB",
            r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Service Worker",
            r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Storage",
            r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\File System",
        ],
        "level": "safe",
        "desc": "Chrome IndexedDB / Service Worker / 本地存储(可释放数 GB)",
    },
    {
        "id": "vscode_cache",
        "name": "VSCode 缓存",
        "paths": [
            r"%APPDATA%\Code\Cache",
            r"%APPDATA%\Code\CachedData",
            r"%APPDATA%\Code\CachedExtensions",
            r"%APPDATA%\Code\GPUCache",
            r"%APPDATA%\Code\logs",
            r"%APPDATA%\Code\Service Worker",
            r"%APPDATA%\Code\Code Cache",
            r"%APPDATA%\Code\Crashpad",
        ],
        "level": "safe",
        "desc": "VSCode 运行时缓存,关闭 VSCode 后可清理",
    },
    {
        "id": "user_crashdumps",
        "name": "用户级崩溃转储",
        "paths": [
            r"%LOCALAPPDATA%\CrashDumps",
            r"%LOCALAPPDATA%\Microsoft\Windows\WER\ReportQueue",
            r"%LOCALAPPDATA%\Microsoft\Windows\WER\ReportArchive",
            r"%LOCALAPPDATA%\Microsoft\Windows\WER\Temp",
        ],
        "level": "safe",
        "desc": "应用崩溃时生成的 .dmp 文件,可能很大",
    },
    {
        "id": "icon_cache",
        "name": "图标缓存",
        "paths": [
            r"%LOCALAPPDATA%\IconCache.db",
            r"%LOCALAPPDATA%\Microsoft\Windows\Explorer\iconcache*",
        ],
        "level": "safe",
        "desc": "系统图标缓存,删除后自动重建",
    },
    {
        "id": "diagnostic",
        "name": "诊断数据",
        "paths": [
            r"%LOCALAPPDATA%\Microsoft\Windows\Diagnostic",
            r"%LOCALAPPDATA%\Microsoft\Windows\WER\Temp",
        ],
        "level": "safe",
        "desc": "Windows 诊断跟踪数据",
    },
    {
        "id": "notifications",
        "name": "通知缓存",
        "paths": [r"%LOCALAPPDATA%\Microsoft\Windows\Notifications"],
        "level": "safe",
        "desc": "Win11 通知中心缓存(可恢复丢失通知)",
    },
    {
        "id": "wmp_cache",
        "name": "Windows Media Player 缓存",
        "paths": [
            r"%LOCALAPPDATA%\Microsoft\Windows Media",
            r"%APPDATA%\Microsoft\Windows Media Player",
        ],
        "level": "safe",
        "desc": "WMP 临时文件",
    },
    {
        "id": "recent",
        "name": "最近访问快捷方式",
        "paths": [r"%APPDATA%\Microsoft\Windows\Recent"],
        "level": "safe",
        "desc": "资源管理器「最近」跳转列表(.lnk 快捷方式)",
    },
    {
        "id": "spotify_cache",
        "name": "Spotify 缓存",
        "paths": [
            r"%LOCALAPPDATA%\Spotify\Data",
            r"%LOCALAPPDATA%\Spotify\Storage",
        ],
        "level": "safe",
        "desc": "Spotify 离线歌曲 / 临时数据(关闭 Spotify 后清理)",
    },
    {
        "id": "slack_cache",
        "name": "Slack 缓存",
        "paths": [
            r"%APPDATA%\Slack\Cache",
            r"%APPDATA%\Slack\Code Cache",
            r"%APPDATA%\Slack\GPUCache",
        ],
        "level": "safe",
        "desc": "Slack 客户端缓存(关闭 Slack 后清理)",
    },
    {
        "id": "discord_cache",
        "name": "Discord 缓存",
        "paths": [
            r"%APPDATA%\discord\Cache",
            r"%APPDATA%\discord\Code Cache",
            r"%APPDATA%\discord\GPUCache",
        ],
        "level": "safe",
        "desc": "Discord 客户端缓存(关闭 Discord 后清理)",
    },
    {
        "id": "zoom_cache",
        "name": "Zoom 缓存",
        "paths": [r"%APPDATA%\Zoom\data"],
        "level": "safe",
        "desc": "Zoom 客户端缓存",
    },
    {
        "id": "steam_cache",
        "name": "Steam 缓存",
        "paths": [
            r"%LOCALAPPDATA%\Steam\htmlcache",
            r"%LOCALAPPDATA%\Steam\CachedData",
        ],
        "level": "safe",
        "desc": "Steam 客户端网页缓存",
    },
    # ====== 一-2、新增浏览器 ======
    {
        "id": "brave_cache",
        "name": "Brave 浏览器缓存",
        "paths": [
            r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data\Default\Cache",
            r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data\Default\Code Cache",
            r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data\Default\GPUCache",
            r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data\Default\IndexedDB",
            r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data\Default\Service Worker",
            r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data\Default\Storage",
        ],
        "level": "safe",
        "desc": "Brave 浏览器全套缓存(关闭 Brave 后清理更彻底)",
    },
    {
        "id": "opera_cache",
        "name": "Opera 浏览器缓存",
        "paths": [
            r"%LOCALAPPDATA%\Opera Software\Opera Stable\Cache",
            r"%LOCALAPPDATA%\Opera Software\Opera Stable\Code Cache",
            r"%LOCALAPPDATA%\Opera Software\Opera Stable\GPUCache",
            r"%APPDATA%\Opera Software\Opera Stable\IndexedDB",
        ],
        "level": "safe",
        "desc": "Opera 稳定版浏览器缓存",
    },
    {
        "id": "opera_gx_cache",
        "name": "Opera GX 浏览器缓存",
        "paths": [
            r"%LOCALAPPDATA%\Opera Software\Opera GX Stable\Cache",
            r"%LOCALAPPDATA%\Opera Software\Opera GX Stable\Code Cache",
            r"%LOCALAPPDATA%\Opera Software\Opera GX Stable\GPUCache",
        ],
        "level": "safe",
        "desc": "Opera GX 游戏浏览器缓存",
    },
    {
        "id": "vivaldi_cache",
        "name": "Vivaldi 浏览器缓存",
        "paths": [
            r"%LOCALAPPDATA%\Vivaldi\User Data\Default\Cache",
            r"%LOCALAPPDATA%\Vivaldi\User Data\Default\Code Cache",
            r"%LOCALAPPDATA%\Vivaldi\User Data\Default\GPUCache",
            r"%LOCALAPPDATA%\Vivaldi\User Data\Default\IndexedDB",
        ],
        "level": "safe",
        "desc": "Vivaldi 浏览器缓存",
    },
    {
        "id": "yandex_cache",
        "name": "Yandex 浏览器缓存",
        "paths": [
            r"%LOCALAPPDATA%\Yandex\YandexBrowser\User Data\Default\Cache",
            r"%LOCALAPPDATA%\Yandex\YandexBrowser\User Data\Default\Code Cache",
            r"%LOCALAPPDATA%\Yandex\YandexBrowser\User Data\Default\GPUCache",
        ],
        "level": "safe",
        "desc": "Yandex Browser 缓存",
    },
    # ====== 一-3、新增通信 ======
    {
        "id": "teams_cache",
        "name": "Microsoft Teams 缓存",
        "paths": [
            r"%APPDATA%\Microsoft\Teams\Cache",
            r"%APPDATA%\Microsoft\Teams\Code Cache",
            r"%APPDATA%\Microsoft\Teams\GPUCache",
            r"%APPDATA%\Microsoft\Teams\blob_storage",
            r"%APPDATA%\Microsoft\Teams\IndexedDB",
            r"%APPDATA%\Microsoft\Teams\Service Worker\CacheStorage",
            r"%APPDATA%\Microsoft\Teams\Service Worker\ScriptCache",
            r"%LOCALAPPDATA%\Microsoft\Teams\Cache",
            r"%LOCALAPPDATA%\Microsoft\Teams\Code Cache",
        ],
        "level": "safe",
        "desc": "Microsoft Teams 客户端全套缓存(可释放 1-5 GB,关闭 Teams 后清理)",
    },
    {
        "id": "skype_cache",
        "name": "Skype 缓存",
        "paths": [
            r"%APPDATA%\Microsoft\Skype for Desktop\Cache",
            r"%APPDATA%\Microsoft\Skype for Desktop\Code Cache",
            r"%APPDATA%\Microsoft\Skype for Desktop\GPUCache",
            r"%LOCALAPPDATA%\Microsoft\Skype for Desktop\Cache",
            r"%LOCALAPPDATA%\Packages\Microsoft.SkypeApp_*\LocalCache",
        ],
        "level": "safe",
        "desc": "Skype 桌面版缓存(关闭 Skype 后清理)",
    },
    # ====== 一-4、新增游戏启动器 ======
    {
        "id": "epic_launcher_cache",
        "name": "Epic Games Launcher 缓存",
        "paths": [
            r"%LOCALAPPDATA%\EpicGamesLauncher\Saved\webcache",
            r"%LOCALAPPDATA%\EpicGamesLauncher\Saved\Logs",
            r"%LOCALAPPDATA%\EpicGamesLauncher\Saved\HTTPCache",
            r"%LOCALAPPDATA%\EpicGamesLauncher\Intermediate",
        ],
        "level": "safe",
        "desc": "Epic Games Launcher 网页缓存与日志",
    },
    {
        "id": "ea_app_cache",
        "name": "EA App / Origin 缓存",
        "paths": [
            r"%LOCALAPPDATA%\Origin\Origin\Cache",
            r"%LOCALAPPDATA%\Origin\Origin\Logs",
            r"%LOCALAPPDATA%\Electronic Arts\EA Desktop\Cache",
            r"%LOCALAPPDATA%\Electronic Arts\EA Desktop\Logs",
        ],
        "level": "safe",
        "desc": "EA App (旧 Origin) 客户端缓存与日志",
    },
    {
        "id": "battlenet_cache",
        "name": "Battle.net 缓存",
        "paths": [
            r"%LOCALAPPDATA%\Battle.net\Cache",
            r"%LOCALAPPDATA%\Battle.net\Logs",
            r"%LOCALAPPDATA%\Battle.net\BrowserCache",
            r"%APPDATA%\Battle.net\Cache",
        ],
        "level": "safe",
        "desc": "暴雪 Battle.net 客户端缓存",
    },
    {
        "id": "ubisoft_cache",
        "name": "Ubisoft Connect 缓存",
        "paths": [
            r"%LOCALAPPDATA%\Ubisoft Game Launcher\cache",
            r"%LOCALAPPDATA%\Ubisoft Game Launcher\logs",
            r"%APPDATA%\Ubisoft Game Launcher\cache",
        ],
        "level": "safe",
        "desc": "Ubisoft Connect (uplay) 客户端缓存",
    },
    # ====== 一-5、新增 IDE ======
    {
        "id": "notepadpp_backup",
        "name": "Notepad++ 备份",
        "paths": [
            r"%APPDATA%\Notepad++\backup",
        ],
        "level": "safe",
        "desc": "Notepad++ 自动备份文件",
    },
    {
        "id": "sublime_cache",
        "name": "Sublime Text 缓存",
        "paths": [
            r"%APPDATA%\Sublime Text\Cache",
            r"%APPDATA%\Sublime Text\Local",
            r"%APPDATA%\Sublime Text\Index",
        ],
        "level": "safe",
        "desc": "Sublime Text 缓存与索引(关闭后清理)",
    },
    {
        "id": "eclipse_cache",
        "name": "Eclipse 缓存",
        "paths": [
            r"%USERPROFILE%\.eclipse",
        ],
        "level": "safe",
        "desc": "Eclipse IDE 工作目录与缓存(谨慎清理,可能影响未保存状态)",
    },
    # ====== 一-6、新增包管理 ======
    {
        "id": "nuget_cache",
        "name": "NuGet 缓存",
        "paths": [
            r"%LOCALAPPDATA%\NuGet\Cache",
            r"%USERPROFILE%\.nuget\packages",
        ],
        "level": "safe",
        "desc": "NuGet 包下载缓存(重装包时会重新下载)",
    },
    {
        "id": "chocolatey_cache",
        "name": "Chocolatey 缓存",
        "paths": [
            r"%LOCALAPPDATA%\Chocolatey\lib-bak",
            r"%TEMP%\chocolatey",
        ],
        "level": "safe",
        "desc": "Chocolatey 包管理 lib-bak 与安装临时",
    },
    {
        "id": "winget_cache",
        "name": "winget 缓存",
        "paths": [
            r"%LOCALAPPDATA%\Microsoft\WinGet\Packages",
            r"%LOCALAPPDATA%\Microsoft\WinGet\StateCache",
            r"%LOCALAPPDATA%\Packages\Microsoft.Winget.Source_*\LocalCache",
        ],
        "level": "safe",
        "desc": "winget 包下载缓存(可能影响离线安装,谨慎清理)",
    },

    # ====== 二、谨慎项(默认不勾) ======
    {
        "id": "update_cache",
        "name": "Windows 更新下载缓存",
        "paths": [r"C:\Windows\SoftwareDistribution\Download"],
        "level": "caution",
        "desc": "已下载的 Windows 更新补丁",
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
        "id": "prefetch",
        "name": "预读取文件 (Prefetch)",
        "paths": [r"C:\Windows\Prefetch"],
        "level": "caution",
        "desc": "程序预读取数据,清空不影响功能但丢加速",
    },
    {
        "id": "installer_temp",
        "name": "Installer 补丁缓存",
        "paths": [r"C:\Windows\Installer\$PatchCache$"],
        "level": "caution",
        "desc": "已安装程序的补丁缓存(占空间大,删了不能卸载旧更新)",
    },
    {
        "id": "live_kernel_reports",
        "name": "Windows 内核转储",
        "paths": [
            r"C:\Windows\LiveKernelReports",
            r"C:\Windows\MEMORY.DMP",
        ],
        "level": "caution",
        "desc": "完整内核转储(MEMORY.DMP 可达 16 GB),删除后无法回溯崩溃",
    },
    {
        "id": "defender_history",
        "name": "Defender 扫描历史",
        "paths": [
            r"C:\ProgramData\Microsoft\Windows Defender\Scans\History\Results",
            r"C:\ProgramData\Microsoft\Windows Defender\Scans\History\Resources",
        ],
        "level": "caution",
        "desc": "Defender 扫描结果历史(可释放数 GB)",
    },
    {
        "id": "defender_localcopy",
        "name": "Defender 离线扫描本地副本",
        "paths": [r"C:\ProgramData\Microsoft\Windows Defender\LocalCopy"],
        "level": "caution",
        "desc": "Defender 离线扫描时下载的特征库副本",
    },
    {
        "id": "downloaded_installations",
        "name": "下载的安装包缓存",
        "paths": [r"C:\Windows\Downloaded Installations"],
        "level": "caution",
        "desc": "MSI 安装时下载的临时包",
    },
    {
        "id": "system_apps_cache",
        "name": "Win11 系统应用缓存",
        "paths": [
            r"%LOCALAPPDATA%\Packages\Microsoft.WindowsSearch_*\LocalCache",
            r"%LOCALAPPDATA%\Packages\Microsoft.YourPhone_*\LocalCache",
            r"%LOCALAPPDATA%\Packages\microsoft.windowscommunicationsapps_*\LocalCache",
            r"%LOCALAPPDATA%\Packages\Microsoft.WindowsCamera_*\LocalCache",
            r"%LOCALAPPDATA%\Packages\Microsoft.XboxGameOverlay_*\LocalCache",
            r"%LOCALAPPDATA%\Packages\Microsoft.MicrosoftEdge_*\LocalCache",
        ],
        "level": "caution",
        "desc": "Win11 自带应用(搜索/手机连接/邮件/相机)的 LocalCache",
    },
    {
        "id": "office_filecache",
        "name": "Office 文件缓存",
        "paths": [
            r"%LOCALAPPDATA%\Microsoft\Office\16.0\OfficeFileCache",
            r"%LOCALAPPDATA%\Microsoft\Office\15.0\OfficeFileCache",
            r"%LOCALAPPDATA%\Microsoft\Office\16.0\INetCache",
        ],
        "level": "caution",
        "desc": "Office 最近打开文件缓存 / 联机文档缓存",
    },
    {
        "id": "outlook_roamcache",
        "name": "Outlook 漫游缓存",
        "paths": [r"%LOCALAPPDATA%\Microsoft\Outlook\RoamCache"],
        "level": "caution",
        "desc": "新 Outlook 漫游缓存(可释放 1-5 GB)",
    },
    {
        "id": "pip_cache",
        "name": "pip 缓存",
        "paths": [r"%LOCALAPPDATA%\pip\cache"],
        "level": "caution",
        "desc": "Python pip 下载缓存(重装包时还会重新下载)",
    },
    {
        "id": "uv_cache",
        "name": "uv 缓存",
        "paths": [r"%LOCALAPPDATA%\uv\cache"],
        "level": "caution",
        "desc": "Astral uv 包管理器缓存(可释放数 GB)",
    },
    {
        "id": "npm_cache",
        "name": "npm 缓存",
        "paths": [
            r"%LOCALAPPDATA%\npm-cache",
            r"%APPDATA%\npm-cache",
        ],
        "level": "caution",
        "desc": "Node.js npm 全局下载缓存",
    },
    {
        "id": "yarn_cache",
        "name": "Yarn 缓存",
        "paths": [r"%LOCALAPPDATA%\Yarn\Cache"],
        "level": "caution",
        "desc": "Yarn 包管理器缓存",
    },
    {
        "id": "pnpm_store",
        "name": "pnpm 存储",
        "paths": [r"%LOCALAPPDATA%\pnpm-store"],
        "level": "caution",
        "desc": "pnpm 全局存储(内容寻址,可释放数 GB)",
    },
    {
        "id": "cargo_cache",
        "name": "Cargo 缓存",
        "paths": [r"%USERPROFILE%\.cargo\registry\cache"],
        "level": "caution",
        "desc": "Rust cargo 编译缓存(可释放 1-10 GB)",
    },
    {
        "id": "gradle_cache",
        "name": "Gradle 缓存",
        "paths": [
            r"%USERPROFILE%\.gradle\caches\jars-*",
            r"%USERPROFILE%\.gradle\caches\transforms-*",
        ],
        "level": "caution",
        "desc": "Gradle 构建缓存(Android/Java 项目)",
    },
    {
        "id": "jetbrains_cache",
        "name": "JetBrains IDE 缓存",
        "paths": [
            r"%LOCALAPPDATA%\JetBrains\*\caches",
            r"%LOCALAPPDATA%\JetBrains\*\log",
            r"%LOCALAPPDATA%\JetBrains\*\tmp",
        ],
        "level": "caution",
        "desc": "IntelliJ / PyCharm / WebStorm 等 IDE 缓存和日志",
    },
    {
        "id": "adobe_cache",
        "name": "Adobe 缓存",
        "paths": [
            r"%LOCALAPPDATA%\Adobe\Acrobat\DC\Cache",
            r"%LOCALAPPDATA%\Adobe\Acrobat\DC\Cache_x64",
            r"%APPDATA%\Adobe\Common\Media Cache",
            r"%APPDATA%\Adobe\Common\Media Cache Files",
        ],
        "level": "caution",
        "desc": "Acrobat / Reader / Premiere 媒体缓存",
    },
    {
        "id": "onedrive_logs",
        "name": "OneDrive 日志",
        "paths": [r"%LOCALAPPDATA%\Microsoft\OneDrive\logs"],
        "level": "caution",
        "desc": "OneDrive 同步日志(不影响同步功能)",
    },
    # ====== 二-2、新增包管理(caution 级) ======
    {
        "id": "wild_temp_files",
        "name": "野生临时文件",
        "kind": "wild_temp",
        "roots": [
            "%TEMP%",
            r"%LOCALAPPDATA%\Temp",
            r"%APPDATA%\Microsoft\Windows\Recent",
            r"%LOCALAPPDATA%\Microsoft\Windows\INetCache",
            r"%LOCALAPPDATA%\CrashDumps",
            r"%APPDATA%\Microsoft\Teams\Cache",
        ],
        "patterns": ["*.tmp", "*.log", "*.bak", "*.old", "*.temp", "*~"],
        "min_age_days": 7,
        "min_size": 50 * 1024 * 1024,
        "level": "caution",
        "desc": "散落在用户临时目录的 *.tmp/*.log/*.bak/*.old,>7 天或>50MB 才清。可释放大量空间,但部分程序可能依赖旧日志",
    },
    {
        "id": "maven_cache",
        "name": "Maven 缓存",
        "paths": [
            r"%USERPROFILE%\.m2\repository",
        ],
        "level": "caution",
        "desc": "Maven 本地仓库(~/.m2/repository,Java 项目重编译时会重新下载)",
    },
    {
        "id": "composer_cache",
        "name": "PHP Composer 缓存",
        "paths": [
            r"%APPDATA%\Composer\cache",
            r"%LOCALAPPDATA%\Composer\cache",
        ],
        "level": "caution",
        "desc": "PHP Composer 下载缓存",
    },
    {
        "id": "bundler_cache",
        "name": "Ruby Bundler 缓存",
        "paths": [
            r"%USERPROFILE%\.bundle\cache",
        ],
        "level": "caution",
        "desc": "Ruby Bundler vendor cache",
    },
    {
        "id": "sbt_cache",
        "name": "Scala sbt / Ivy2 缓存",
        "paths": [
            r"%USERPROFILE%\.sbt",
            r"%USERPROFILE%\.ivy2\cache",
        ],
        "level": "caution",
        "desc": "Scala sbt 与 Ivy2 依赖缓存",
    },

    # ====== 三、高级项(默认不勾,折叠在「高级」区) ======
    {
        "id": "inf_logs",
        "name": "INF/Setup 日志",
        "paths": [
            r"C:\Windows\INF\setupapi.dev.log",
            r"C:\Windows\INF\setupapi.offline.log",
            r"C:\Windows\inf\setupapi.dev.log",
        ],
        "level": "advanced",
        "desc": "设备安装日志(诊断硬件问题时需要)",
    },
    {
        "id": "bitlog",
        "name": "BITS 传输日志",
        "paths": [r"C:\Windows\System32\bitslogfile.txt"],
        "level": "advanced",
        "desc": "后台智能传输服务日志(可能很大)",
    },
    {
        "id": "winsxs_manifest",
        "name": "WinSxS ManifestCache",
        "paths": [r"C:\Windows\WinSxS\ManifestCache"],
        "level": "advanced",
        "desc": "WinSxS 清单缓存(不要动 WinSxS 主体)",
    },
    {
        "id": "defender_quarantine",
        "name": "Defender 隔离区",
        "paths": [r"C:\ProgramData\Microsoft\Windows Defender\Quarantine\ResourceData"],
        "level": "advanced",
        "desc": "⚠ 病毒隔离样本,删了无法恢复误杀文件",
    },
    {
        "id": "windows_bt",
        "name": "旧升级残留 $WINDOWS.~BT",
        "paths": [r"C:\$WINDOWS.~BT"],
        "level": "advanced",
        "desc": "系统升级后保留的临时文件(可释放 1-5 GB)",
    },
    {
        "id": "sysreset",
        "name": "旧重置备份 $SysReset",
        "paths": [r"C:\$SysReset"],
        "level": "advanced",
        "desc": "系统重置时保留的旧文件",
    },
    {
        "id": "windows_old",
        "name": "旧系统 Windows.old",
        "paths": [r"C:\Windows.old"],
        "level": "advanced",
        "desc": "⚠ 升级前的旧 Windows 目录(删了不能回滚系统)",
    },
    # ====== 三-2、系统级高级操作 ======
    {
        "id": "hibernation_disable",
        "name": "关闭休眠(释放 hiberfil.sys)",
        "kind": "command",
        "command_id": "hibernation_disable",
        "level": "advanced",
        "desc": "⚠ 用 powercfg /h off 关闭休眠,释放 hiberfil.sys(可达内存大小,4-16 GB)。关闭后『快速启动』仍可用,但无法进入真正的休眠状态",
    },
    {
        "id": "vss_shadow_cleanup",
        "name": "清理旧卷影副本",
        "kind": "command",
        "command_id": "vss_shadow_cleanup",
        "level": "advanced",
        "desc": "⚠ 删除 C 盘最旧的卷影副本(系统还原点)。会大幅释放空间,但**会丢失可恢复的系统还原点**",
    },
]

# 把 spec dict 列表转为 CleanTarget dataclass 列表;模块加载时即校验。
# 任何缺字段、非法 kind/level、未授权 command_id 都会立即 raise。
CLEAN_TARGETS = [CleanTarget(**spec) for spec in _CLEAN_TARGET_SPECS]
assert all_targets_valid(CLEAN_TARGETS), "CLEAN_TARGETS 校验失败"

PROTECTED_NAMES = {
    "desktop.ini", "thumbs.db", "pagefile.sys", "hiberfil.sys",
    "swapfile.sys", "config.sys", "io.sys", "msdos.sys",
}

# ====================== 清理白名单 ======================
# 用户可配置:扩展名 + 路径前缀,清理时会跳过这些。
# 保存在 %APPDATA%\GreenCleaner\whitelist.json
# 兼容旧路径 %APPDATA%\C_Cleaner\whitelist.json(仅当新路径不存在时迁移一次)。

_WHITELIST_FILE = _APP_DIR / "whitelist.json"


def _migrate_legacy_appdata() -> None:
    """首次启动时:把旧 %APPDATA%\\C_Cleaner 下的 whitelist.json / config.json
    复制到新 %APPDATA%\\GreenCleaner 目录。新文件不存在时才迁移。"""
    legacy_dir = Path(os.environ.get("APPDATA", str(Path.home()))) / "C_Cleaner"
    if not legacy_dir.exists() or legacy_dir.resolve() == _APP_DIR.resolve():
        return
    for name in ("whitelist.json", "config.json"):
        old = legacy_dir / name
        new = _APP_DIR / name
        if old.exists() and not new.exists():
            try:
                _APP_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(old), str(new))
                _logger.info("迁移旧配置 %s -> %s", old, new)
            except Exception as e:
                _logger.warning("迁移 %s 失败: %s", name, e)

# 内置默认白名单(用户可编辑)
DEFAULT_WHITELIST = {
    "extensions": [".env", ".gitignore", ".htaccess", "id_rsa", "id_dsa"],
    "path_prefixes": [],
}


def load_whitelist() -> dict:
    """读白名单配置。无文件则用默认值。

    P0 安全:启动时自动迁移旧 C_Cleaner 路径下的 whitelist.json / config.json。
    """
    try:
        _migrate_legacy_appdata()
    except Exception as e:
        _logger.warning("迁移旧配置失败(忽略): %s", e)
    try:
        if _WHITELIST_FILE.exists():
            data = json.loads(_WHITELIST_FILE.read_text(encoding="utf-8"))
            # 合并默认值
            raw_prefixes = data.get("path_prefixes", []) or []
            cleaned_prefixes = sanitize_path_prefixes(raw_prefixes)
            return {
                "extensions": list(set(
                    data.get("extensions", []) + DEFAULT_WHITELIST["extensions"]
                )),
                "path_prefixes": cleaned_prefixes,
            }
    except Exception as e:
        _logger.warning("读白名单失败: %s", e)
    return {
        "extensions": list(DEFAULT_WHITELIST["extensions"]),
        "path_prefixes": list(DEFAULT_WHITELIST["path_prefixes"]),
    }


def sanitize_path_prefixes(prefixes) -> list:
    """P0 安全:拒绝不安全的 path_prefixes 条目。

    拒绝规则:
      1) 含 `..`(路径遍历)
      2) UNC 路径(`\\\\server\\share` 或 `//server/share`)
      3) 长度 < 3(过短,几乎一定是错误输入)
      4) normcase + resolve 失败
      5) 解析后仍是相对路径

    返回通过校验的列表(原顺序),被拒绝的会写日志。
    """
    out = []
    seen_norm = set()
    for raw in prefixes:
        if not isinstance(raw, str):
            _logger.warning("跳过非字符串 path_prefix: %r", raw)
            continue
        s = raw.strip()
        if len(s) < 3:
            _logger.warning("跳过过短 path_prefix(<3): %r", raw)
            continue
        # 路径遍历
        if ".." in Path(s).parts:
            _logger.warning("跳过含 .. 的 path_prefix: %r", raw)
            continue
        # UNC
        if s.startswith("\\\\") or s.startswith("//"):
            _logger.warning("跳过 UNC path_prefix: %r", raw)
            continue
        # normcase + resolve(只能解析已经存在的路径;不存在的允许通过但 normcase)
        try:
            norm = os.path.normcase(s)
            try:
                resolved = str(Path(s).resolve())
            except (OSError, RuntimeError):
                resolved = norm
        except Exception:
            _logger.warning("跳过无法解析的 path_prefix: %r", raw)
            continue
        key = (resolved or norm).lower()
        if key in seen_norm:
            continue
        seen_norm.add(key)
        out.append(resolved if resolved else norm)
    return out


def save_whitelist(wl: dict):
    """保存白名单到配置文件(去除默认项,只存用户自定义)。

    P0 安全:path_prefixes 先经 sanitize_path_prefixes 过滤,只存合规路径。
    """
    try:
        _WHITELIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 只存用户自定义的(去掉默认)
        custom = {
            "extensions": [e for e in wl.get("extensions", [])
                          if e.lower() not in {x.lower() for x in DEFAULT_WHITELIST["extensions"]}],
            "path_prefixes": sanitize_path_prefixes(wl.get("path_prefixes", []) or []),
        }
        _WHITELIST_FILE.write_text(
            json.dumps(custom, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        raise RuntimeError(f"保存白名单失败: {e}")


def is_whitelisted(path: str, wl: dict) -> bool:
    """判断文件是否在白名单中(扩展名匹配或路径前缀匹配)。"""
    if not wl:
        return False
    p_obj = Path(path)
    name = p_obj.name
    name_lower = name.lower()
    # 拿到所有可能的"扩展名形式":.env / .py / .tar.gz
    all_exts = []
    if name.startswith("."):
        # 点开头的文件名,如 .env / .gitignore → 整个当扩展名
        all_exts.append(name_lower)
    else:
        suffixes = p_obj.suffixes  # ['.tar', '.gz']
        for s in suffixes:
            all_exts.append(s.lower())
        # 也存完整文件名(用于 "id_rsa" 这种)
    for protected in wl.get("extensions", []):
        prot = protected.strip().lower()
        if not prot:
            continue
        # 形如 ".xxx" 或 ".env" → 匹配任一扩展名
        if prot.startswith("."):
            if prot in all_exts:
                return True
        else:
            # 形如 "id_rsa" / "id_dsa" → 完整文件名匹配
            if name_lower == prot:
                return True
    # 路径前缀匹配
    path_lower = path.lower().replace("\\", "/")
    for prefix in wl.get("path_prefixes", []):
        if path_lower.startswith(prefix.lower().replace("\\", "/")):
            return True
    return False

# ====================== 工具函数 ======================


def is_admin() -> bool:
    """检测是否以管理员权限运行。

    双 API 兜底:
    1) ctypes.windll.shell32.IsUserAnAdmin() — 简单可靠
    2) GetTokenInformation + TokenElevation — 更权威但 Win XP+ 才支持
    任一返回 True 即视为管理员;两者都失败/不可用时回退到 False。
    """
    try:
        if bool(ctypes.windll.shell32.IsUserAnAdmin()):
            return True
    except Exception:
        pass
    # 第二道:TokenElevation 校验
    try:
        TOKEN_QUERY = 0x0008
        TokenElevation = 20  # TOKEN_ELEVATION.Type
        TokenElevationTypeFull = 2  # TokenElevationTypeFull

        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        # 拿当前进程 token
        h_token = ctypes.c_void_p()
        if not kernel32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                         TOKEN_QUERY, ctypes.byref(h_token)):
            return False
        try:
            elev = ctypes.c_int()
            ret_len = ctypes.c_uint32()
            ok = advapi32.GetTokenInformation(
                h_token, TokenElevation, ctypes.byref(elev),
                ctypes.sizeof(elev), ctypes.byref(ret_len),
            )
            if ok and elev.value == TokenElevationTypeFull:
                return True
        finally:
            kernel32.CloseHandle(h_token)
    except Exception:
        pass
    return False


def set_dpi_awareness() -> bool:
    """Win10/11 高 DPI 适配:让 tkinter 不被自动缩放模糊。

    不调用 → 在 125%/150% 缩放下 tk.Tk() 在某些 Win11 环境会抛异常
    ("Tcl_InitError: Can't find a usable init.tcl") 或显示模糊。
    返回 True 表示设置成功。
    """
    try:
        # 优先用 Per-Monitor V2(Win10 1703+),失败回退到 system DPI aware
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE_V2
            return True
        except Exception:
            pass
        try:
            ctypes.windll.shell32.SetProcessDPIAware()  # SYSTEM_DPI_AWARE(Win Vista+)
            return True
        except Exception:
            pass
    except Exception:
        pass
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
    """删除单文件。NTFS 上 chmod 几乎无效,直接放弃 chmod 回退以省一次 syscall。"""
    if not path.exists():
        return False
    if path.name in PROTECTED_NAMES:
        return False
    try:
        path.unlink()
        return True
    except (PermissionError, OSError):
        return False


def _remove_files_concurrent(file_specs: list, workers: int = 8) -> tuple:
    """并发删除一组 (path, size) 文件。返回 (removed_count, freed_bytes)。

    用 ThreadPoolExecutor 并发,Windows 上对大量小文件有显著加速(2-6x)。
    """
    if not file_specs:
        return 0, 0
    removed = 0
    freed = 0
    # 用共享 list 收集结果(线程安全,因为只 append 数字)
    results = []
    # 退避:小批量(<= 50) 走单线程,避免线程开销反而变慢
    if len(file_specs) <= 50:
        for fp, size in file_specs:
            if safe_remove_file(fp):
                results.append(size)
    else:
        def _del(spec):
            fp, size = spec
            return size if safe_remove_file(fp) else 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for sz in ex.map(_del, file_specs, chunksize=max(1, len(file_specs) // (workers * 4))):
                if sz:
                    results.append(sz)
    removed = len(results)
    freed = sum(results)
    return removed, freed


def _delete_collected(files: list, dirs_with_depth: list, root: Path,
                      parallel: bool = True, workers: int = 8) -> tuple:
    """把单遍扫描结果(files + dirs_with_depth)实际删除。

    快路径:大目录(>5000 文件)直接 shutil.rmtree(ignore_errors=True),
    C 实现的递归+rmdir 比 Python 循环快 2-3x。
    """
    if not files and not dirs_with_depth:
        try:
            os.rmdir(root)
        except (PermissionError, OSError):
            pass
        return 0, 0

    # 大目录快路径(只在无白名单时调用,_safe_remove_dir_with_whitelist 走慢路径)
    if len(files) > 5000:
        try:
            shutil.rmtree(root, ignore_errors=True)
        except Exception:
            pass
        return len(files), sum(s for _, s in files)

    # 小目录:并发删文件 + 按深度 rmdir
    r, f = _remove_files_concurrent(files, workers=workers if parallel else 1)
    # 按深度降序排(os.rmdir 要求子目录先空)
    dirs_with_depth.sort(key=lambda x: x[1], reverse=True)
    if dirs_with_depth:
        if len(dirs_with_depth) <= 30 or not parallel:
            for d, _ in dirs_with_depth:
                try:
                    os.rmdir(d)
                except (PermissionError, OSError):
                    pass
        else:
            def _rm(spec):
                d, _ = spec
                try:
                    os.rmdir(d)
                except (PermissionError, OSError):
                    return False
                return True
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(_rm, dirs_with_depth,
                            chunksize=max(1, len(dirs_with_depth) // 32)))
    try:
        os.rmdir(root)
    except (PermissionError, OSError):
        pass
    return r, f


def safe_remove_dir(path: Path, parallel: bool = True, workers: int = 8,
                    onerror=None) -> tuple:
    """递归删除目录,返回 (removed, freed)。

    P0 安全:可选 onerror 回调收到 (func, path, exc_info) — 用于 UI 层
    收集「哪些文件没删掉」给用户看。空回调走原有静默逻辑。

    优化点:
    - 单遍扫描(_scan_for_deletion)同时收集文件和子目录(原版两次遍历)
    - >5000 文件走 shutil.rmtree(ignore_errors=True) 快路径
    - parallel=True 时并发删文件,大目录从分钟级降到 10-30 秒
    - 小目录(<50 文件)自动走单线程,避免线程开销
    """
    if not path.exists():
        return 0, 0

    def _collect_onerror(func, p, exc):
        try:
            _logger.warning("safe_remove_dir 失败 func=%s path=%s exc=%s",
                            getattr(func, "__name__", str(func)), p, exc[1])
        except Exception:
            pass
        if onerror is not None:
            try:
                onerror(func, p, exc)
            except Exception:
                pass

    # 第一步:单遍扫描同时收集文件和子目录
    try:
        files, dirs_with_depth = _scan_for_deletion(
            path, cancel=None,
            workers=workers if parallel else 1,
        )
    except Exception:
        # 退回到 os.walk
        files = []
        dirs_with_depth = []
        try:
            for root, dirs, fnames in os.walk(path):
                for f in fnames:
                    fp = Path(root) / f
                    try:
                        files.append((fp, fp.stat().st_size))
                    except Exception:
                        continue
                depth = root.replace(str(path), "").count(os.sep)
                for d in dirs:
                    dirs_with_depth.append((Path(root) / d, depth + 1))
        except Exception:
            pass
    # 第二步:删除(把 _collect_onerror 透传到 shutil.rmtree,适用快路径)
    if len(files) > 5000:
        try:
            shutil.rmtree(path, onerror=_collect_onerror)
        except Exception as e:
            _logger.warning("shutil.rmtree 快路径异常: %s", e)
        # shutil.rmtree 不返回 (removed, freed),自己估算
        return len(files), sum(s for _, s in files)
    return _delete_collected(files, dirs_with_depth, path,
                             parallel=parallel, workers=workers)


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


def create_restore_point(description: str = "绿色垃圾文件清理器-清理前备份") -> bool:
    """创建 Windows 系统还原点。

    安全说明(P0):
        description 通过 PowerShell 的 -EncodedCommand (Base64 UTF-16LE) 传入,
        而不是 -Command "<inline>" 拼接。避免 description 里的单引号、分号、
        $() 等被 PowerShell 当成命令执行造成的注入。
    """
    # 防御:严格白名单 — 拒绝包含 NUL、控制字符、单引号、双引号、反引号、
    # $、反斜杠、换行的描述,长度也限制在 64 字以内。
    if not isinstance(description, str):
        _logger.error("create_restore_point description 类型非法: %r", type(description))
        return False
    if len(description) > 64:
        description = description[:64]
    forbidden = set("'`\"$\\;<>|&()\n\r\t\0")
    if any(ch in forbidden for ch in description):
        # 净化:替换为 _
        description = "".join("_" if ch in forbidden else ch for ch in description)
        _logger.warning("create_restore_point description 含有可疑字符,已净化")
    # PowerShell 接受 Base64(UTF-16LE) 命令。用 -EncodedCommand 而非 -Command。
    ps_script = (
        f"Checkpoint-Computer -Description '{description}' "
        f"-RestorePointType MODIFY_SETTINGS"
    )
    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    cmd = [
        "powershell", "-NoProfile", "-NonInteractive",
        "-EncodedCommand", encoded,
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode != 0:
            _logger.warning("create_restore_point 失败 rc=%s: %s",
                            r.returncode, (r.stderr or "")[:200])
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        _logger.error("create_restore_point 超时")
        return False
    except Exception as e:
        _logger.error("create_restore_point 异常: %s", e)
        return False


def empty_recycle_bin() -> bool:
    try:
        ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x00000007)
        return True
    except Exception:
        return False


# ====================== 高性能 walk 工具 ======================

# 一律跳过的系统目录
_SKIP_DIRS = frozenset({
    "$Recycle.Bin", "System Volume Information", "$WinREAgent",
    "$RECYCLE.BIN", "Config.Msi", "MSOCache",
})


def _should_skip_dir(name: str) -> bool:
    return name in _SKIP_DIRS or name.startswith(".") and name in (".git", ".svn", ".hg")


def scandir_files(root: str, max_depth: int = 8,
                  extra_skip=None, cancel=None) -> list:
    """用 os.scandir 单线程遍历(比 os.walk 快 2-3 倍)。
    直接迭代 scandir() 迭代器,不 list() 化(省内存+省时)。
    返回 [(path, size, mtime), ...]
    cancel: 可选 threading.Event,用来随时停止。
    """
    results = []
    root_p = Path(root)
    if not root_p.exists():
        return results
    skip = frozenset(extra_skip) if extra_skip else frozenset()

    def _walk(p: Path, depth: int):
        if cancel is not None and cancel.is_set():
            return
        if depth > max_depth:
            return
        try:
            with os.scandir(p) as it:
                for entry in it:
                    if cancel is not None and cancel.is_set():
                        return
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            name = entry.name
                            if name in _SKIP_DIRS or name in skip:
                                continue
                            _walk(Path(entry.path), depth + 1)
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                st = entry.stat(follow_symlinks=False)
                                results.append((entry.path, st.st_size, st.st_mtime))
                            except (PermissionError, OSError):
                                continue
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError):
            return

    _walk(root_p, 0)
    return results


def scandir_files_parallel(root: str, max_depth: int = 8,
                            extra_skip=None, cancel=None,
                            workers: int = None) -> list:
    """工作窃取式并行扫描:queue.Queue + N worker 线程。

    与旧的「每个一级子目录一个线程」相比,在非均衡树
    (比如 90% 文件集中在 node_modules) 上快 2-5x:
    - 旧版:9 个线程 1 个干活 8 个空转
    - 新版:N 个 worker 共享任务队列,自动负载均衡
    """
    if workers is None:
        workers = min(8, max(2, (os.cpu_count() or 4)))
    root_p = Path(root)
    if not root_p.exists():
        return []
    skip = frozenset(extra_skip) if extra_skip else frozenset()

    results = []
    results_lock = threading.Lock()
    q = queue.Queue()
    q.put((root_p, 0))

    def _scan(p: Path, depth: int):
        if cancel is not None and cancel.is_set():
            q.task_done()
            return
        if depth > max_depth:
            q.task_done()
            return
        local = []
        try:
            with os.scandir(p) as it:
                for entry in it:
                    if cancel is not None and cancel.is_set():
                        break
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            name = entry.name
                            if name in _SKIP_DIRS or name in skip:
                                continue
                            q.put((Path(entry.path), depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                st = entry.stat(follow_symlinks=False)
                                local.append((entry.path, st.st_size, st.st_mtime))
                            except (PermissionError, OSError):
                                continue
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError):
            pass
        if local:
            with results_lock:
                results.extend(local)
        q.task_done()

    def _worker():
        while True:
            try:
                p, depth = q.get()
            except Exception:
                break
            if p is None:  # 哨兵:让 worker 退出
                q.task_done()
                break
            try:
                _scan(p, depth)
            except Exception:
                q.task_done()

    threads = []
    for _ in range(workers):
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        threads.append(t)

    q.join()  # 等所有任务(包括递归子任务)都完成

    for _ in range(workers):
        q.put((None, None))  # 哨兵,让 worker 跳出循环
    for t in threads:
        t.join()

    return results


def _scan_for_deletion(root_p: Path, max_depth: int = 20,
                       cancel=None, workers: int = None) -> tuple:
    """单遍扫描:同时收集 (path, size) 和 (path, depth) 两个列表。

    旧 safe_remove_dir 要先 scandir_files_parallel 收集文件,
    再 os.walk(topdown=False) 收集目录 - 两次全遍历。
    本函数把两次合并为一次,大目录场景下省一半时间。

    返回 (files, dirs_with_depth):
      files: [(Path, int_size), ...]
      dirs_with_depth: [(Path, int_depth), ...]
    """
    if workers is None:
        workers = min(8, max(2, (os.cpu_count() or 4)))
    if not root_p.exists():
        return [], []
    skip = _SKIP_DIRS

    files = []
    dirs_out = []
    out_lock = threading.Lock()
    q = queue.Queue()
    q.put((root_p, 0))

    def _scan(p: Path, depth: int):
        if cancel is not None and cancel.is_set():
            q.task_done()
            return
        if depth > max_depth:
            q.task_done()
            return
        local_files = []
        local_dirs = []
        try:
            with os.scandir(p) as it:
                for entry in it:
                    if cancel is not None and cancel.is_set():
                        break
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name in skip:
                                continue
                            sub = Path(entry.path)
                            local_dirs.append((sub, depth + 1))
                            q.put((sub, depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                st = entry.stat(follow_symlinks=False)
                                local_files.append((Path(entry.path), st.st_size))
                            except (PermissionError, OSError):
                                continue
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError):
            pass
        if local_files or local_dirs:
            with out_lock:
                files.extend(local_files)
                dirs_out.extend(local_dirs)
        q.task_done()

    def _worker():
        while True:
            try:
                p, depth = q.get()
            except Exception:
                break
            if p is None:
                q.task_done()
                break
            try:
                _scan(p, depth)
            except Exception:
                q.task_done()

    threads = []
    for _ in range(workers):
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        threads.append(t)

    q.join()
    for _ in range(workers):
        q.put((None, None))
    for t in threads:
        t.join()

    return files, dirs_out


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

    P0 安全:返回 bool 表示是否成功。同时记录失败原因到日志。
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
        if result != 0:
            _logger.warning("SHFileOperationW 失败 rc=%s path=%s", result, path)
            return False
        if op.fAnyOperationsAborted:
            _logger.warning("SHFileOperationW 用户中止 path=%s", path)
            return False
        return True
    except Exception as e:
        _logger.warning("SHFileOperationW 异常 path=%s: %s", path, e)
        return False


def move_to_recycle_bin_batch(paths: list) -> tuple:
    """批量移到回收站:一次 SHFileOperationW 处理 N 个路径。

    pFrom 用 \0 分隔多路径,末尾双 \0。1000 个文件从 1000 次 syscall 变 1 次,
    实测提速 10-50x。

    P0 安全:返回 (success_count, failed_paths) 而不是简单 (success, fail) 计数,
    让 UI 层能明确告诉用户哪些文件失败、便于人工介入。
    """
    if not paths:
        return 0, []
    # 过滤不存在的
    valid = [p for p in paths if p and os.path.exists(p)]
    invalid = [p for p in paths if p and not os.path.exists(p)]
    if not valid:
        return 0, list(invalid)
    try:
        # 多路径:每个路径以 \0 结尾,整体再以 \0 结尾
        # SHFileOperationW 要求 pFrom 是 \0 分隔的 unicode 字符串,末尾双 \0
        op = _SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = _FO_DELETE
        op.pFrom = "\0".join(valid) + "\0\0"
        op.pTo = None
        op.fFlags = _FOF_ALLOWUNDO | _FOF_SILENT | _FOF_NOCONFIRMATION | _FOF_NOERRORUI
        op.fAnyOperationsAborted = False
        op.hNameMappings = None
        op.lpszProgressTitle = None
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if result == 0 and not op.fAnyOperationsAborted:
            return len(valid), list(invalid)
        # 部分失败:Windows 用 fAnyOperationsAborted 表示,但具体哪个失败无法获取。
        # 保守策略:把全部 valid 视为失败,留给调用方重试或人工介入。
        _logger.warning("SHFileOperationW 批量部分失败 rc=%s aborted=%s count=%d",
                        result, op.fAnyOperationsAborted, len(valid))
        return 0, list(valid) + list(invalid)
    except Exception as e:
        _logger.warning("SHFileOperationW 批量异常: %s", e)
        return 0, list(valid) + list(invalid)


# ====================== 清理器(原逻辑保留) ======================


def _scan_wild_temp(target: dict, cancel=None) -> dict:
    """扫描野生临时文件:在 roots 下递归找匹配 patterns 的文件,
    满足「年龄超过 min_age_days」或「大小超过 min_size」的算可清理。

    target 需含:roots(list[str]), patterns(list[str]),
                min_age_days(int), min_size(int 字节)
    """
    import fnmatch
    roots_raw = target.get("roots") or target.get("paths") or []
    patterns = target.get("patterns") or ["*.tmp", "*.log", "*.bak", "*.old"]
    min_age_days = int(target.get("min_age_days", 7))
    min_size = int(target.get("min_size", 50 * 1024 * 1024))
    age_threshold = time.time() - min_age_days * 86400 if min_age_days > 0 else 0

    paths = []
    total_size = 0
    for root_raw in roots_raw:
        if "*" in root_raw or "?" in root_raw:
            parent = Path(root_raw).parent
            pat = Path(root_raw).name
            if not parent.exists():
                continue
            roots = [Path(m) for m in parent.glob(pat)]
        else:
            roots = [expand_path(root_raw)]
        for root in roots:
            if not root.exists():
                continue
            try:
                all_files = scandir_files_parallel(
                    str(root), max_depth=20, cancel=cancel,
                )
            except Exception:
                continue
            for path, size, mtime in all_files:
                if cancel is not None and cancel.is_set():
                    return {"size": total_size, "files": len(paths), "paths": paths}
                name = os.path.basename(path).lower()
                if not any(fnmatch.fnmatch(name, p.lower()) for p in patterns):
                    continue
                # 大文件 OR 旧文件 → 可清理
                if size >= min_size or (age_threshold and mtime < age_threshold):
                    paths.append(path)
                    total_size += size
    return {"size": total_size, "files": len(paths), "paths": paths}


def _estimate_command_target(target: dict) -> dict:
    """估算 command 类型 target 的释放空间(扫描阶段用)。
    - hibernation_disable: hiberfil.sys 大小
    - vss_shadow_cleanup: 不可预测,返回 0 但 exists=True
    """
    tid = target.get("id", "")
    if tid == "hibernation_disable":
        p = Path(r"C:\hiberfil.sys")
        if p.exists():
            try:
                size = p.stat().st_size
                return {"size": size, "files": 1, "paths": [str(p)],
                        "exists": True, "kind": "command"}
            except (PermissionError, OSError):
                pass
        return {"size": 0, "files": 0, "paths": [],
                "exists": False, "kind": "command"}
    if tid == "vss_shadow_cleanup":
        # 卷影副本空间无法预先估算,但动作可执行
        return {"size": 0, "files": 1, "paths": [],
                "exists": True, "kind": "command"}
    # 未知 command 类型
    return {"size": 0, "files": 0, "paths": [],
            "exists": True, "kind": "command"}


def _run_command_target(target: dict) -> dict:
    """执行 command 类型 target。

    P0 安全:不再接受 target["command"] 的任意 list[str] 透传。
    改为 AllowedCommand 枚举白名单 + 显式 ID 注册表:
      - hibernation_disable -> [powercfg, /h, off]
      - vss_shadow_cleanup   -> [vssadmin, delete, shadows, /for=C:, /oldest, /quiet]
    调用方必须通过 build_allowed_command(id) 拿到命令,任意拼接的 list 会被拒绝。
    """
    tid = target.get("id", "")
    cmd = build_allowed_command(tid)
    if cmd is None:
        _logger.error("command target 未授权 id=%s", tid)
        return {"removed": 0, "freed": 0, "errors": 1, "skipped": 0}
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode != 0:
            _logger.warning("command %s 失败 rc=%s stderr=%s",
                            tid, r.returncode, (r.stderr or "")[:200])
            return {"removed": 0, "freed": 0, "errors": 1, "skipped": 0}
        # 命令成功后,特殊处理 hiberfil.sys
        if tid == "hibernation_disable":
            hiber = Path(r"C:\hiberfil.sys")
            freed = 0
            removed = 0
            if hiber.exists():
                try:
                    freed = hiber.stat().st_size
                    # powercfg 已关休眠,hiberfil.sys 通常被自动删,但有时会残留
                    try:
                        hiber.unlink()
                        removed = 1
                    except (PermissionError, OSError) as e:
                        _logger.warning("hiberfil.sys 残留未删: %s", e)
                except (PermissionError, OSError) as e:
                    _logger.warning("hiberfil.sys 状态读取失败: %s", e)
            return {"removed": removed, "freed": freed, "errors": 0, "skipped": 0}
        # vss 等其他命令
        return {"removed": 1, "freed": 0, "errors": 0, "skipped": 0}
    except subprocess.TimeoutExpired:
        _logger.error("command %s 超时", tid)
        return {"removed": 0, "freed": 0, "errors": 1, "skipped": 0}
    except Exception as e:
        _logger.error("command %s 异常: %s", tid, e)
        return {"removed": 0, "freed": 0, "errors": 1, "skipped": 0}


class Scanner:
    def __init__(self, log_callback=None, progress_callback=None):
        self.log = log_callback or print
        self.progress = progress_callback or (lambda *a, **kw: None)
        self._cancel = threading.Event()
        self.results = {}

    def cancel(self):
        self._cancel.set()

    def scan_target(self, target: dict) -> dict:
        kind = target.get("kind", "files")
        if kind == "command":
            return _estimate_command_target(target)
        if kind == "wild_temp":
            try:
                r = _scan_wild_temp(target, cancel=self._cancel)
                r["exists"] = bool(r.get("files"))
                r["kind"] = "wild_temp"
                return r
            except Exception as e:
                self.log(f"  跳过 {target.get('name', '?')}: {e}")
                return {"size": 0, "files": 0, "paths": [],
                        "exists": False, "kind": "wild_temp"}
        # 默认 files 类型
        r = {"size": 0, "files": 0, "paths": [], "exists": False, "kind": "files"}
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
                if r.get("kind") == "command":
                    if r.get("exists"):
                        if r.get("size", 0) > 0:
                            self.log(f"  -> ⚙ {human_size(r['size'])} 可释放(执行系统命令)")
                        else:
                            self.log(f"  -> ⚙ 系统操作(可执行)")
                    else:
                        self.log(f"  -> 暂无内容")
                elif r["exists"] and r["size"] > 0:
                    self.log(f"  -> {human_size(r['size'])} ({r['files']} 个文件)")
                else:
                    self.log(f"  -> 暂无内容")
            except Exception as e:
                self.log(f"  -> 出错: {e}")
                self.results[target["id"]] = {"size": 0, "files": 0, "paths": [],
                                              "exists": False, "kind": "files"}

        self.progress(1.0, "扫描完成")
        self.log("=" * 60)
        total_size = sum(r.get("size", 0) for r in self.results.values())
        self.log(f"[扫描] 完成,总可清理: {human_size(total_size)}")
        self.log("=" * 60)
        return self.results


class Cleaner:
    def __init__(self, log_callback=None, progress_callback=None, whitelist=None):
        self.log = log_callback or print
        self.progress = progress_callback or (lambda *a, **kw: None)
        self._cancel = threading.Event()
        self._pause = threading.Event()
        self._pause.set()
        self.whitelist = whitelist  # dict 或 None

    def cancel(self):
        self._cancel.set()
        self._pause.set()

    def _safe_remove_dir_with_whitelist(self, path: Path) -> tuple:
        """类似 safe_remove_dir,但跳过白名单中的文件,并发删除。
        返回 (removed, freed, skipped)"""
        removed = 0
        freed = 0
        skipped = 0
        wl = self.whitelist or {}
        try:
            files = scandir_files_parallel(str(path), max_depth=20, cancel=self._cancel)
        except Exception:
            files = []
        # 第一遍:过滤白名单(单线程,is_whitelisted 不重)
        to_del = []
        for fp, size, _ in files:
            if is_whitelisted(fp, wl):
                skipped += 1
            else:
                to_del.append((Path(fp), size))
        # 第二遍:并发删除
        r, f = _remove_files_concurrent(to_del, workers=8)
        removed += r
        freed += f
        # 第三遍:删空目录
        all_dirs = []
        try:
            for root, dirs, _ in os.walk(path, topdown=False):
                for d in dirs:
                    all_dirs.append(Path(root) / d)
        except Exception:
            pass
        all_dirs.sort(key=lambda d: len(str(d)), reverse=True)
        if all_dirs:
            def _rm(d):
                try:
                    os.rmdir(d)
                except (PermissionError, OSError):
                    return False
                return True
            if len(all_dirs) <= 30:
                for d in all_dirs:
                    _rm(d)
            else:
                with ThreadPoolExecutor(max_workers=8) as ex:
                    list(ex.map(_rm, all_dirs,
                                chunksize=max(1, len(all_dirs) // 32)))
        try:
            os.rmdir(path)
        except (PermissionError, OSError):
            pass
        return removed, freed, skipped

    def clean_target(self, target: dict) -> dict:
        """调度入口:按 kind 分派到 clean_files / clean_wild_temp / clean_command。

        质量改进:把巨型 switch 拆成三个独立子方法,各自只关心自己的 kind,
        后续要加新 kind(如 uninstaller / registry)只需新增子方法,不污染主线。
        """
        kind = target.get("kind", "files")
        if kind == Kind.COMMAND:
            return self.clean_command(target)
        if kind == Kind.WILD_TEMP:
            return self.clean_wild_temp(target)
        return self.clean_files(target)

    def clean_files(self, target: dict) -> dict:
        """files 类型:按 paths 通配展开 → 单文件/目录走对应删除路径。"""
        result = {"removed": 0, "freed": 0, "errors": 0, "skipped": 0}
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
            # 单文件白名单检查
            if ep.is_file() and is_whitelisted(str(ep), self.whitelist or {}):
                result["skipped"] += 1
                continue
            try:
                if ep.is_file():
                    size = ep.stat().st_size
                    if safe_remove_file(ep):
                        result["removed"] += 1
                        result["freed"] += size
                    else:
                        result["errors"] += 1
                else:
                    # 目录:有白名单走细粒度删除,无白名单走快路径
                    if self.whitelist:
                        removed, freed, skipped = self._safe_remove_dir_with_whitelist(ep)
                    else:
                        removed, freed = safe_remove_dir(ep, onerror=self._collect_remove_error)
                        skipped = 0
                    result["removed"] += removed
                    result["freed"] += freed
                    result["skipped"] += skipped
                    # P0 安全:onerror 收集到的失败 = errors
                    if getattr(self, "_last_remove_errors", 0):
                        result["errors"] += self._last_remove_errors
                        self._last_remove_errors = 0
            except Exception as e:
                result["errors"] += 1
                self.log(f"  错误: {ep} -> {e}")
                _logger.warning("clean_files 单项异常 path=%s: %s", ep, e)
        return result

    def clean_wild_temp(self, target: dict) -> dict:
        """wild_temp 类型:重新扫描匹配的文件 → 走并发删除。"""
        result = {"removed": 0, "freed": 0, "errors": 0, "skipped": 0}
        try:
            scan = _scan_wild_temp(target, cancel=self._cancel)
        except Exception as e:
            self.log(f"  扫描失败: {e}")
            _logger.warning("clean_wild_temp scan 失败 id=%s: %s",
                            target.get("id"), e)
            result["errors"] = 1
            return result
        wl = self.whitelist or {}
        file_specs = []
        skipped = 0
        for p in scan.get("paths", []):
            if wl and is_whitelisted(p, wl):
                skipped += 1
                continue
            fp = Path(p)
            try:
                size = fp.stat().st_size if fp.exists() else 0
            except (PermissionError, OSError):
                continue
            file_specs.append((fp, size))
        r, f = _remove_files_concurrent(file_specs, workers=8)
        result["removed"] += r
        result["freed"] += f
        result["skipped"] += skipped
        result["errors"] += len(file_specs) - r
        return result

    def clean_command(self, target: dict) -> dict:
        """command 类型:委托给 _run_command_target(Already 走 AllowedCommand 白名单)。

        P0 安全:advanced 操作前必须 create_restore_point() 成功(已在 CleanerTab 层
        拦截,这里再做一层防御:即使外部忘了调,这里也只在白名单表内有 command_id 时
        才执行)。
        """
        return _run_command_target(target)

    def _collect_remove_error(self, func, path, exc):
        """P0 安全:safe_remove_dir 的 onerror 回调,收集失败数 + 写日志。"""
        try:
            self._last_remove_errors = getattr(self, "_last_remove_errors", 0) + 1
        except Exception:
            pass
        try:
            _logger.warning(
                "safe_remove_dir 失败 func=%s path=%s exc=%s",
                getattr(func, "__name__", str(func)), path,
                exc[1] if isinstance(exc, tuple) and len(exc) > 1 else exc,
            )
        except Exception:
            pass

    def clean_all(self, target_ids: list) -> dict:
        self._cancel.clear()
        self._pause.set()
        total_freed = 0
        total_removed = 0
        total_errors = 0
        total_skipped = 0
        targets = [t for t in CLEAN_TARGETS if t["id"] in target_ids]

        self.log("=" * 60)
        self.log(f"[清理] 开始,共 {len(targets)} 个清理项")
        if self.whitelist and (self.whitelist.get("extensions") or self.whitelist.get("path_prefixes")):
            self.log(
                f"[白名单] 已启用:{len(self.whitelist.get('extensions', []))} 个扩展名,"
                f"{len(self.whitelist.get('path_prefixes', []))} 个路径前缀"
            )
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
                total_skipped += r.get("skipped", 0)
                kind = target.get("kind", "files")
                if kind == "command":
                    if r["errors"]:
                        msg = f"  -> ✗ 命令执行失败(可能权限不足或无目标)"
                    elif r["freed"] > 0:
                        msg = f"  -> ⚙ 释放 {human_size(r['freed'])}"
                    else:
                        msg = f"  -> ⚙ 命令执行成功"
                else:
                    msg = f"  -> 删除 {r['removed']} 个,释放 {human_size(r['freed'])}"
                    if r.get("skipped"):
                        msg += f",跳过 {r['skipped']} 个(白名单)"
                    if r["errors"]:
                        msg += f",{r['errors']} 个失败"
                self.log(msg)
            except Exception as e:
                self.log(f"  -> 出错: {e}")
                total_errors += 1

        self.progress(1.0, "清理完成")
        self.log("=" * 60)
        skip_msg = f",跳过 {total_skipped} 个(白名单)" if total_skipped else ""
        err_msg = f",失败 {total_errors} 个" if total_errors else ""
        self.log(
            f"[清理] 完成,共删除 {total_removed} 个,释放 {human_size(total_freed)}"
            f"{skip_msg}{err_msg}"
        )
        self.log("=" * 60)
        return {
            "removed": total_removed, "freed": total_freed,
            "errors": total_errors, "skipped": total_skipped,
        }


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
                         top_n: int = 100, max_depth: int = 8,
                         min_mtime: float = 0,
                         ext_filter: set = None,
                         extra_skip: set = None) -> list:
        """扫描目录下所有大于 min_size 的文件,按大小降序返回 top_n。

        min_mtime:   只考虑 mtime >= 此值(epoch 秒,0 = 不过滤)
        ext_filter:  扩展名白名单 set(小写,带点,如 {'.mp4','.iso'}),None = 不过滤
        extra_skip:  额外要跳过的目录名 set
        """
        results = []
        root_p = Path(root)
        if not root_p.exists():
            self.log(f"[大文件] 路径不存在: {root}")
            return results
        ext_msg = f"扩展名 {sorted(ext_filter)} " if ext_filter else ""
        time_msg = f",{datetime.fromtimestamp(min_mtime):%Y-%m-%d} 后" if min_mtime else ""
        self.log(
            f"[大文件] 阶段 1/1:并行扫描 {root} (深度 {max_depth},{ext_msg}阈值 {human_size(min_size)}{time_msg})"
        )
        self.progress(0, "开始扫描...")
        try:
            files = scandir_files_parallel(
                str(root_p), max_depth=max_depth,
                extra_skip=extra_skip, cancel=self._cancel,
            )
        except Exception as e:
            self.log(f"[大文件] 扫描出错: {e}")
            files = []
        if self._cancel.is_set():
            self.log("[大文件] 用户取消")
            return results

        scanned = len(files)
        last_log = 0
        for idx, (path, size, mtime) in enumerate(files, 1):
            if self._cancel.is_set():
                self.log("[大文件] 用户取消")
                break
            if size < min_size:
                continue
            if min_mtime and mtime < min_mtime:
                continue
            if ext_filter:
                ext = os.path.splitext(path)[1].lower()
                if ext not in ext_filter:
                    continue
            results.append({"path": path, "size": size, "mtime": mtime})
            # 进度日志:每 5000 个文件
            if idx - last_log >= 5000:
                self.log(
                    f"  · 已扫 {idx}/{scanned} 个文件,发现 {len(results)} 个大文件"
                )
                last_log = idx
                self.progress(idx / max(scanned, 1), f"已扫 {idx}/{scanned},发现 {len(results)} 个")

        self.log(
            f"[大文件] 完成:共扫 {scanned} 个文件,发现 {len(results)} 个 ≥ {human_size(min_size)}"
        )
        results.sort(key=lambda x: x["size"], reverse=True)
        return results[:top_n]

    # ----- 文件夹大小 -----
    def find_large_dirs(self, root: str, top_n: int = 50, max_depth: int = 3,
                        extra_skip: set = None) -> list:
        """扫描目录下子文件夹大小,按大小降序返回 top_n。
        max_depth 是相对 root 的层级深度,避免无限递归。
        子目录大小计算用线程池并行。
        """
        results = []
        root_p = Path(root)
        if not root_p.exists():
            self.log(f"[文件夹] 路径不存在: {root}")
            return results
        self.log(f"[文件夹] 阶段 1/2:枚举 {root} 下的子目录(深度 {max_depth})")
        self.progress(0, "枚举子目录...")
        skip = frozenset(extra_skip) if extra_skip else frozenset()

        # 第一遍:列出所有子目录(用 scandandir 比 os.walk 快)
        subdirs = []
        try:
            def _enum_dirs(p: Path, depth: int):
                if self._cancel.is_set():
                    return
                if depth >= max_depth:
                    return
                try:
                    with os.scandir(p) as it:
                        for entry in it:
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    name = entry.name
                                    if name in _SKIP_DIRS or name in skip:
                                        continue
                                    sub_path = Path(entry.path)
                                    subdirs.append(sub_path)
                                    _enum_dirs(sub_path, depth + 1)
                            except (PermissionError, OSError):
                                continue
                except (PermissionError, OSError):
                    return

            _enum_dirs(root_p, 0)
        except Exception as e:
            self.log(f"[文件夹] 枚举出错: {e}")
        if self._cancel.is_set():
            self.log("[文件夹] 用户取消")
            return results

        self.log(f"[文件夹] 阶段 2/2:并行累计 {len(subdirs)} 个子目录的大小")
        total = len(subdirs)
        last_log = 0
        completed = [0]

        def _calc_size(d: Path) -> tuple:
            """算一个目录的大小,返回 (path, size, files) 或 None(失败/空)"""
            if self._cancel.is_set():
                return None
            try:
                s, c = calculate_dir_size(d, max_depth=6)
                completed[0] += 1
                if completed[0] % 20 == 0:
                    self.log(
                        f"  · 已分析 {completed[0]}/{total} 个目录 "
                        f"({len(results)} 个有内容)"
                    )
                    self.progress(
                        completed[0] / max(total, 1),
                        f"已分析 {completed[0]}/{total} 个目录"
                    )
                if s > 0:
                    return (str(d), s, c)
            except (PermissionError, OSError):
                pass
            return None

        # 并行算(8 线程)
        if subdirs:
            with ThreadPoolExecutor(max_workers=8) as ex:
                for r in ex.map(_calc_size, subdirs):
                    if self._cancel.is_set():
                        ex.shutdown(wait=False, cancel_futures=True)
                        break
                    if r:
                        results.append({"path": r[0], "size": r[1], "files": r[2]})

        self.log(f"[文件夹] 完成:共 {len(results)} 个有内容的目录")
        results.sort(key=lambda x: x["size"], reverse=True)
        return results[:top_n]

    # ----- 重复文件 -----
    # 三级哈希阈值(可在 __init__ 覆盖):
    HEAD_HASH_BYTES = 65536            # 头部预读字节数(64KB)
    LARGE_FILE_SKIP_HEAD = 100 * 1024 * 1024  # >100MB 跳过 head hash 直接走 MD5

    @staticmethod
    def _quick_hash(path, block_size: int = 65536) -> str:
        """快速 hash:头 64K + 尾 64K + size(用于大文件预筛,保留旧 API 兼容)。
        小于 256KB 的文件不建议用这个,直接走 _full_hash 即可。
        path 可以是 str 或 Path。
        """
        try:
            p_obj = Path(path) if not isinstance(path, Path) else path
            size = p_obj.stat().st_size
            h = hashlib.md5()
            h.update(f"{size}".encode())
            with open(p_obj, "rb") as f:
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
    def _full_hash(path) -> str:
        """完整 MD5,用于小文件(<= 256KB)或 quick hash 碰撞后再确认。"""
        try:
            p_obj = Path(path) if not isinstance(path, Path) else path
            h = hashlib.md5()
            with open(p_obj, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()
        except (PermissionError, OSError):
            return None

    @staticmethod
    def _head_hash(path, n_bytes: int = 65536) -> str:
        """L2 head hash:仅读文件头部 n_bytes 字节(默认 64KB)算 SHA-1。
        用作"同 size 但内容不同"的预筛,绝大多数不同文件 head 已经不一致,
        可以省掉后续完整 MD5 的开销(典型 5-20x 加速)。
        小于 n_bytes 的文件会把整个文件读进去。
        返回 hex 字符串,失败返回 None(权限/IO 错误)。
        path 可以是 str 或 Path。
        """
        try:
            p_obj = Path(path) if not isinstance(path, Path) else path
            h = hashlib.sha1()
            with open(p_obj, "rb") as f:
                head = f.read(n_bytes)
                h.update(head)
            return h.hexdigest()
        except (PermissionError, OSError):
            return None

    def find_duplicates(self, root: str, min_size: int = 1024 * 1024,
                        max_depth: int = 6, top_n_groups: int = 100) -> list:
        """找重复文件:三级哈希策略。

        L1 按文件大小 size 分桶(size 不同必不重复)。
        L2 同 size 组内,小文件算 head 64KB 的 SHA-1(快速预筛)、
           大文件(>LARGE_FILE_SKIP_HEAD,默认 100MB)直接走完整 MD5。
        L3 对 head hash 相同的候选,逐一做完整 MD5 确认。

        返回 groups: [[{path,size}, ...], ...],按"组总大小 × (副本数-1)"降序,
        即"可释放空间"从大到小;返回结构与旧版完全兼容。
        """
        root_p = Path(root)
        if not root_p.exists():
            self.log(f"[重复] 路径不存在: {root}")
            return []
        self.log(
            f"[重复] 阶段 1/3:并行扫描 {root} 并按文件大小 size 分桶 "
            f"(深度 {max_depth},阈值 {human_size(min_size)})"
        )
        self.progress(0, "阶段 1/3:扫描并按 size 分桶...")

        # 第一遍:用 scandir_parallel 收集所有文件
        try:
            files = scandir_files_parallel(
                str(root_p), max_depth=max_depth, cancel=self._cancel,
            )
        except Exception as e:
            self.log(f"[重复] 阶段 1 出错: {e}")
            files = []
        if self._cancel.is_set():
            self.log("[重复] 用户取消")
            return []

        # 按 size 分桶
        size_buckets = {}
        scanned = len(files)
        for path, size, _mtime in files:
            if size < min_size:
                continue
            size_buckets.setdefault(size, []).append(path)

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

        # 第二遍:L2 head hash。
        #   - 文件 >100MB:直接完整 MD5(读 head 64K 反而是浪费 IO)
        #   - 其他:仅读 head 64K 算 SHA-1(快速剔除大量"同 size 不同内容"假阳性)
        self.log(
            f"[重复] 阶段 2/3:L2 head hash {cand_count} 个候选 "
            f"(>{human_size(self.LARGE_FILE_SKIP_HEAD)} 走完整 MD5,其他走 head 64K SHA-1)"
        )
        self.progress(0, f"阶段 2/3:head hash {cand_count} 个文件...")
        hash_buckets = {}  # hash -> [paths]
        idx = 0
        last_log = 0
        for size, paths in dup_sizes.items():
            for p in paths:
                if self._cancel.is_set():
                    self.log("[重复] 用户取消")
                    return []
                idx += 1
                pname = os.path.basename(p)
                if size > self.LARGE_FILE_SKIP_HEAD:
                    # 大文件:跳过 head hash,直接走完整 MD5
                    h = self._full_hash(p)
                else:
                    # 普通文件:head 64K SHA-1 预筛
                    h = self._head_hash(p, self.HEAD_HASH_BYTES)
                if h is None:
                    continue
                hash_buckets.setdefault(h, []).append(p)
                if idx - last_log >= 20:
                    self.log(
                        f"  · 阶段 2:head hash {idx}/{cand_count} "
                        f"({pname})"
                    )
                    last_log = idx
                    self.progress(
                        idx / max(cand_count, 1),
                        f"阶段 2:head hash {idx}/{cand_count}"
                    )

        # 过滤出 hash 出现 >=2 的(剩下的是"head 看起来一致"的真可疑)
        dup_h = {h: ps for h, ps in hash_buckets.items() if len(ps) >= 2}
        cand2 = sum(len(v) for v in dup_h.values())
        self.log(
            f"[重复] 阶段 2 完成:缩到 {cand2} 个文件 "
            f"({len(dup_h)} 组),还要做完整 MD5 确认"
        )
        if cand2 == 0:
            self.log("[重复] 第二遍无重复,扫描结束")
            return []
        if self._cancel.is_set():
            return []

        # 第三遍:L3 完整 MD5 确认(逐字节读,这是"是否是同一文件"的最终依据)
        self.log(
            f"[重复] 阶段 3/3:对 {cand2} 个文件做完整 MD5 哈希 "
            f"(逐字节读取,这是确认'同一个文件'的最终依据)"
        )
        self.progress(0, f"阶段 3/3:MD5 {cand2} 个文件...")
        groups = []
        idx = 0
        last_log = 0
        for h, paths in dup_h.items():
            md5_buckets = {}
            for p in paths:
                if self._cancel.is_set():
                    self.log("[重复] 用户取消")
                    return []
                idx += 1
                pname = os.path.basename(p)
                fh = self._full_hash(p)
                if fh is None:
                    continue
                md5_buckets.setdefault(fh, []).append(p)
                if idx - last_log >= 5:
                    self.log(
                        f"  · 阶段 3:MD5 {idx}/{cand2} "
                        f"({pname})"
                    )
                    last_log = idx
                    self.progress(
                        idx / max(cand2, 1),
                        f"阶段 3:MD5 {idx}/{cand2}"
                    )
            for fh, ps in md5_buckets.items():
                if len(ps) >= 2:
                    groups.append([{"path": str(p), "size": Path(p).stat().st_size} for p in ps])

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
        indicatoron=False,  # 去掉右侧默认的下拉箭头
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


def ask_typed_confirmation(parent, title: str, prompt: str, expected: str) -> bool:
    """要求用户在 Toplevel 输入框中敲入 expected 字符串才返回 True。

    用于高危操作的「强确认」(比 messagebox.askyesno 更严格):
    - 默认焦点在 Entry
    - 回车即确认
    - 取消按钮 / 关闭窗口 → False

    测试友好:把对话框构造与 modal 等待拆开 — _build_confirm_dialog 单独可测。
    """
    if not TK_AVAILABLE:
        return False
    state = _build_confirm_dialog(parent, title, prompt, expected)
    if state is None:
        return False
    dlg = state["dlg"]
    parent.wait_window(dlg)
    return state["result"]["ok"]


def _build_confirm_dialog(parent, title: str, prompt: str, expected: str):
    """构造强确认对话框,返回包含 dlg/entry/on_ok/on_cancel/result 的 state dict。

    不调 wait_window,便于单元测试(测试只需要 entry.get() 验证 on_ok 逻辑)。
    返回 None 表示 TK 不可用。
    """
    if not TK_AVAILABLE:
        return None
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.geometry("520x260")
    dlg.configure(bg=WC.BG)
    dlg.transient(parent.winfo_toplevel())
    dlg.grab_set()
    result = {"ok": False}

    def _on_ok():
        if entry.get().strip() == expected:
            result["ok"] = True
            dlg.destroy()
        else:
            err_lbl.config(text=f"输入不正确,请重新输入(应为 {expected!r})", fg=WC.RED)

    def _on_cancel():
        result["ok"] = False
        dlg.destroy()

    tk.Label(
        dlg, text=title, font=("Microsoft YaHei UI", 13, "bold"),
        bg=WC.BG, fg=WC.TEXT,
    ).pack(anchor=tk.W, padx=16, pady=(14, 4))
    tk.Label(
        dlg, text=prompt, font=("Microsoft YaHei UI", 10),
        bg=WC.BG, fg=WC.TEXT2, justify=tk.LEFT, wraplength=480,
    ).pack(anchor=tk.W, padx=16, pady=(0, 8))

    entry_frame = tk.Frame(dlg, bg=WC.BG)
    entry_frame.pack(fill=tk.X, padx=16, pady=(0, 4))
    tk.Label(entry_frame, text="输入:", font=("Microsoft YaHei UI", 10),
             bg=WC.BG, fg=WC.TEXT).pack(side=tk.LEFT, padx=(0, 6))
    entry = tk.Entry(
        entry_frame, font=("Consolas", 11),
        bg=WC.CARD, fg=WC.TEXT, relief="solid", bd=1,
        highlightthickness=1, highlightbackground=WC.BORDER,
    )
    entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)

    err_lbl = tk.Label(dlg, text="", font=("Microsoft YaHei UI", 9),
                       bg=WC.BG, fg=WC.RED)
    err_lbl.pack(anchor=tk.W, padx=16, pady=(0, 4))

    btn_row = tk.Frame(dlg, bg=WC.BG)
    btn_row.pack(fill=tk.X, padx=16, pady=(6, 14))
    wechat_button(btn_row, "取消", _on_cancel, kind="default").pack(side=tk.RIGHT, padx=(8, 0))
    wechat_button(btn_row, "确认执行", _on_ok, kind="danger").pack(side=tk.RIGHT)

    entry.focus_set()
    dlg.bind("<Return>", lambda _e: _on_ok())
    dlg.bind("<Escape>", lambda _e: _on_cancel())
    dlg.protocol("WM_DELETE_WINDOW", _on_cancel)

    return {"dlg": dlg, "entry": entry, "on_ok": _on_ok,
            "on_cancel": _on_cancel, "result": result}


def get_recycle_bin_size() -> int:
    """估算回收站当前占用字节数。失败返回 0。"""
    try:
        drive = os.environ.get("SystemDrive", "C:") + "\\"
        # 用 SHGetFolderLocation 拿回收站路径太重;直接枚举 $Recycle.Bin
        rb = Path(drive) / "$Recycle.Bin"
        if not rb.exists():
            return 0
        total = 0
        for root, dirs, files in os.walk(rb):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except (PermissionError, OSError):
                    continue
        return total
    except Exception:
        return 0


# ====================== 主窗口:4 个 Tab ======================


class CleanerTab:
    """Tab 1: 垃圾清理(原功能)"""
    def __init__(self, parent, log_fn, status_fn):
        self.parent = tk.Frame(parent, bg=WC.BG)
        self.log = log_fn
        # UI 调整:任务进度条已统一在主窗口顶部,直接用主窗口的 set_status 即可
        # (不再在本 Tab 里包装同步顶部进度)
        self.set_status = status_fn
        self.busy = False
        self.scanner = None
        self.cleaner = None
        self.scan_results = {}
        self.check_vars = {}

        self._build()

    def _build(self):
        """顶层调度:把 UI 拆为三个子区域,各自由专门方法构建。

        task-fix-action-bar-hidden: pack 顺序至关重要。
        原 序:disk_card(TOP) -> list_card(TOP, expand=True) -> action_card(TOP)
        问题:list_card 先 pack 且 expand=True,会抢占全部剩余空间,
              导致后 pack 的 action_card 无空间渲染,扫描/清理按钮不可见。
        正确序:disk_card(TOP) -> action_card(BOTTOM) -> list_card(TOP, expand=True)
              顶部和底部固定区域先布局,list_card 再 expand 抢剩余中间空间,
              这是 tkinter pack 的经典模式。
        """
        self._build_disk_card()
        self._build_action_bar()
        self._build_targets_tree()
        # 初始
        self._refresh_disk()
        self._refresh_list()

    def _build_disk_card(self):
        """顶部:磁盘信息 + 可清理总量 + 本次进度。"""
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
        right = tk.Frame(top, bg=WC.CARD, width=220)
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
        # UI 调整(task-ui-adjust):原右侧的"本次进度"卡片已移除,统一到
        # MainWindow 顶部的全局任务进度条(所有 Tab 共享),避免 CleanerTab 内
        # 与底部 status_bar 的 progress 重复。

    def _build_targets_tree(self):
        """中部:三档清理项树(可滚动卡片,带全选/反选/仅安全)。"""
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
            b = wechat_button(btn_row, label, cmd, kind="ghost",
                              font=("Microsoft YaHei UI", 9), padx=10, pady=3)
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

    def _build_action_bar(self):
        """底部:操作区(扫描/刷新/白名单 + 选项 + 取消/清理)。

        task-fix-action-bar-hidden: 用 side=BOTTOM 从底部布局,
        确保不被 list_card(expand=True) 抢占空间。pack 顺序见 _build() 注释。
        """
        action_card = Card(self.parent, padding=14)
        action_card.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(0, 12))

        act_top = tk.Frame(action_card.pad_frame, bg=WC.CARD)
        act_top.pack(fill=tk.X)

        # 左:扫描 + 刷新 + 白名单
        left_act = tk.Frame(act_top, bg=WC.CARD)
        left_act.pack(side=tk.LEFT)
        self.scan_btn = wechat_button(left_act, "🔍 扫描", self._on_scan, kind="primary")
        self.scan_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.refresh_btn = wechat_button(left_act, "🔄 刷新磁盘", self._refresh_disk, kind="default")
        self.refresh_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.wl_btn = wechat_button(left_act, "🛡 白名单", self._on_whitelist, kind="default")
        self.wl_btn.pack(side=tk.LEFT)

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
        self.cancel_btn.config(state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.RIGHT, padx=(8, 0))
        self.clean_btn = wechat_button(right_act, "🧹 一键清理", self._on_clean, kind="danger")
        self.clean_btn.pack(side=tk.RIGHT)

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

    def _render_target_row(self, parent, target, name_color, tag_text):
        """渲染单个清理项行(用于三档分组复用)。"""
        r = self.scan_results.get(target["id"], {})
        size = r.get("size", 0)
        files = r.get("files", 0)
        exists = r.get("exists", False)
        scanned = target["id"] in self.scan_results

        row = tk.Frame(parent, bg=WC.CARD)
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
        display = target["name"] + (f"  {tag_text}" if tag_text else "")
        tk.Label(mid, text=display,
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

    def _render_section_header(self, parent, title, color, count, total_size):
        """渲染分组标题(安全/谨慎/高级)。"""
        hdr = tk.Frame(parent, bg=WC.CARD)
        hdr.pack(fill=tk.X, padx=4, pady=(10, 4))
        size_str = f" · 已发现 {human_size(total_size)}" if total_size else ""
        tk.Label(hdr,
                 text=f"● {title} ({count} 项){size_str}",
                 font=("Microsoft YaHei UI", 9, "bold"),
                 bg=WC.CARD, fg=color, anchor=tk.W
                 ).pack(side=tk.LEFT)

    def _refresh_list(self):
        """三档分组渲染:safe / caution / advanced,advanced 默认折叠。"""
        for w in self.list_inner.winfo_children():
            w.destroy()
        # 按 level 分桶
        buckets = {"safe": [], "caution": [], "advanced": []}
        for t in CLEAN_TARGETS:
            buckets.setdefault(t.get("level", "safe"), []).append(t)
        # 计算每组已扫描大小
        def group_total(level_items):
            total = 0
            for t in level_items:
                r = self.scan_results.get(t["id"], {})
                total += r.get("size", 0)
            return total
        # ====== 一、安全组 ======
        safe_items = buckets["safe"]
        safe_total = group_total(safe_items)
        self._render_section_header(
            self.list_inner, "安全(默认勾选,放心清理)", WC.GREEN,
            len(safe_items), safe_total,
        )
        for t in safe_items:
            self._render_target_row(self.list_inner, t, WC.TEXT, "")
        # ====== 二、谨慎组 ======
        caution_items = buckets["caution"]
        caution_total = group_total(caution_items)
        self._render_section_header(
            self.list_inner, "谨慎(默认不勾,可能影响加速或无法卸载旧更新)", WC.ORANGE,
            len(caution_items), caution_total,
        )
        for t in caution_items:
            self._render_target_row(self.list_inner, t, WC.ORANGE, "⚠ 谨慎")
        # ====== 三、高级组(折叠) ======
        advanced_items = buckets["advanced"]
        advanced_total = group_total(advanced_items)
        # 标题行带展开/收起按钮
        adv_hdr = tk.Frame(self.list_inner, bg=WC.CARD)
        adv_hdr.pack(fill=tk.X, padx=4, pady=(10, 4))
        adv_title_color = WC.RED
        self.adv_expanded = getattr(self, "adv_expanded", False)
        adv_arrow = "▼" if self.adv_expanded else "▶"
        adv_size_str = f" · 已发现 {human_size(advanced_total)}" if advanced_total else ""
        adv_title_lbl = tk.Label(
            adv_hdr,
            text=f"{adv_arrow} 高级({len(advanced_items)} 项 · 系统级操作,谨慎使用){adv_size_str}",
            font=("Microsoft YaHei UI", 9, "bold"),
            bg=WC.CARD, fg=adv_title_color, anchor=tk.W, cursor="hand2",
        )
        adv_title_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        # 容器,根据状态 pack/forget
        self.adv_container = tk.Frame(self.list_inner, bg=WC.CARD)
        for t in advanced_items:
            self._render_target_row(self.adv_container, t, WC.RED, "🔥 高级")

        def _toggle_advanced(_evt=None):
            self.adv_expanded = not getattr(self, "adv_expanded", False)
            if self.adv_expanded:
                self.adv_container.pack(fill=tk.X, pady=(0, 6))
                adv_title_lbl.config(text=adv_title_lbl.cget("text").replace("▶", "▼", 1))
            else:
                self.adv_container.pack_forget()
                adv_title_lbl.config(text=adv_title_lbl.cget("text").replace("▼", "▶", 1))
        adv_title_lbl.bind("<Button-1>", _toggle_advanced)
        if self.adv_expanded:
            self.adv_container.pack(fill=tk.X, pady=(0, 6))

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

    def _on_whitelist(self):
        """弹窗:配置白名单(扩展名 + 路径前缀)。"""
        wl = load_whitelist()
        dlg = tk.Toplevel(self.parent)
        dlg.title("清理白名单")
        dlg.geometry("640x520")
        dlg.configure(bg=WC.BG)
        dlg.transient(self.parent.winfo_toplevel())

        # 顶部说明
        tk.Label(
            dlg, text="🛡 清理白名单",
            font=("Microsoft YaHei UI", 14, "bold"),
            bg=WC.BG, fg=WC.TEXT
        ).pack(anchor=tk.W, padx=16, pady=(14, 4))
        tk.Label(
            dlg, text="匹配的文件/路径,清理时会自动跳过(已在清理项中的文件)。\n"
                     "扩展名示例:.env,.key  |  路径示例:C:\\Users\\me\\projects",
            font=("Microsoft YaHei UI", 9),
            bg=WC.BG, fg=WC.TEXT2, justify=tk.LEFT
        ).pack(anchor=tk.W, padx=16)

        # 扩展名区
        ext_card = Card(dlg, padding=12)
        ext_card.pack(fill=tk.BOTH, expand=True, padx=16, pady=(12, 6))
        tk.Label(ext_card.pad_frame, text="扩展名 / 文件名(每行一个)",
                 font=("Microsoft YaHei UI", 10, "bold"),
                 bg=WC.CARD, fg=WC.TEXT).pack(anchor=tk.W)
        ext_text = scrolledtext.ScrolledText(
            ext_card.pad_frame, height=8,
            font=("Consolas", 10), relief="flat", bd=1,
            bg=CURRENT_THEME.LOG_BG, fg=CURRENT_THEME.LOG_FG,
            highlightthickness=1, highlightbackground=WC.BORDER,
        )
        ext_text.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        ext_text.insert("1.0", "\n".join(wl.get("extensions", [])))

        # 路径前缀区
        path_card = Card(dlg, padding=12)
        path_card.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 6))
        tk.Label(path_card.pad_frame, text="路径前缀(每行一个,绝对路径)",
                 font=("Microsoft YaHei UI", 10, "bold"),
                 bg=WC.CARD, fg=WC.TEXT).pack(anchor=tk.W)
        path_text = scrolledtext.ScrolledText(
            path_card.pad_frame, height=5,
            font=("Consolas", 10), relief="flat", bd=1,
            bg=CURRENT_THEME.LOG_BG, fg=CURRENT_THEME.LOG_FG,
            highlightthickness=1, highlightbackground=WC.BORDER,
        )
        path_text.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        path_text.insert("1.0", "\n".join(wl.get("path_prefixes", [])))

        # 按钮行
        btn_row = tk.Frame(dlg, bg=WC.BG)
        btn_row.pack(fill=tk.X, padx=16, pady=14)
        def _save():
            exts = [e.strip() for e in ext_text.get("1.0", tk.END).splitlines() if e.strip()]
            paths = [p.strip() for p in path_text.get("1.0", tk.END).splitlines() if p.strip()]
            try:
                save_whitelist({"extensions": exts, "path_prefixes": paths})
                self.log(
                    f"[白名单] 已保存:{len(exts)} 个扩展名,{len(paths)} 个路径前缀", "ok"
                )
                dlg.destroy()
            except Exception as e:
                messagebox.showerror("错误", str(e), parent=dlg)
        wechat_button(btn_row, "取消", dlg.destroy, kind="default"
                      ).pack(side=tk.RIGHT, padx=(8, 0))
        wechat_button(btn_row, "💾 保存", _save, kind="primary"
                      ).pack(side=tk.RIGHT)

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

        # 按 level 分类
        selected_targets = [t for t in CLEAN_TARGETS if t["id"] in selected]
        caution = [t for t in selected_targets if t["level"] == Level.CAUTION]
        advanced = [t for t in selected_targets if t["level"] == Level.ADVANCED]

        # 1) 第一道确认:列出包含的谨慎/高级项 + 总可清理体积
        total_freed = sum(self.scan_results.get(t["id"], {}).get("size", 0)
                          for t in selected_targets)
        msg = f"将清理 {len(selected)} 个项目,预计可释放 {human_size(total_freed)}。\n\n"
        if caution:
            names = "、".join(t["name"] for t in caution[:3])
            more = f"等 {len(caution)} 个" if len(caution) > 3 else ""
            msg += f"⚠ 包含谨慎项: {names}{more}\n"
        if advanced:
            names = "、".join(t["name"] for t in advanced[:3])
            more = f"等 {len(advanced)} 个" if len(advanced) > 3 else ""
            msg += f"🔥 包含高级系统操作: {names}{more}\n"
        msg += "\n是否继续?"
        if not messagebox.askyesno("确认清理", msg):
            return

        # 2) 高级项二次确认:逐项弹专属对话框
        #    - hibernation_disable / vss_shadow_cleanup:详细告警
        #    - 其他高级项:要求输入 DELETE
        for t in advanced:
            if not self._confirm_advanced_target(t):
                self.log(f"[取消] 用户在高级项 {t['name']} 二次确认时取消", "warn")
                return

        # 3) 还原点硬约束:任何 advanced 操作前必须先 create_restore_point 成功
        if advanced and not self._ensure_restore_point_before_advanced(advanced):
            return

        # 4) 还原点(用户在勾选项外,只要选了 caution 也尝试建)
        if self.create_restore_var.get() or advanced:
            self.set_status("正在创建系统还原点...", 0)
            self.log("[备份] 创建系统还原点 ...", "info")
            if create_restore_point():
                self.log("  -> 还原点已创建", "ok")
            else:
                if advanced:
                    # 高级项 + 还原点失败 → 不允许继续
                    self.log("  -> ✗ 还原点创建失败,高级操作中止", "err")
                    messagebox.showerror(
                        "无法继续",
                        "高级项清理前必须先创建还原点,但还原点创建失败。\n"
                        "可能原因:未以管理员身份运行,或系统保护已关闭。",
                    )
                    return
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
            whitelist=load_whitelist(),
        )
        threading.Thread(target=self._clean_worker, args=(selected,), daemon=True).start()

    def _confirm_advanced_target(self, target) -> bool:
        """高级项二次确认。返回 True 表示用户确认继续。"""
        tid = target["id"]
        # 专项告警
        if tid == "hibernation_disable":
            return messagebox.askyesno(
                "确认:关闭休眠",
                "即将执行 `powercfg /h off` 关闭休眠功能。\n\n"
                "• 释放 hiberfil.sys(可达内存大小,4-16 GB)\n"
                "• 关闭后无法进入休眠状态(笔记本合盖可能直接关机)\n"
                "• 快速启动仍可使用\n"
                "• 需重启才能重新启用休眠\n\n"
                "是否继续?",
            )
        if tid == "vss_shadow_cleanup":
            return messagebox.askyesno(
                "确认:删除卷影副本",
                "即将执行 `vssadmin delete shadows /for=C: /oldest /quiet`。\n\n"
                "• 删除 C 盘最旧的卷影副本(系统还原点)\n"
                "• 会大幅释放空间,但**会丢失可恢复的系统还原点**\n"
                "• 已通过创建新还原点做了补救(若该步骤成功)\n\n"
                "是否继续?",
            )
        # 默认高级项:要求输入 DELETE 确认
        return ask_typed_confirmation(
            self.parent,
            title=f"确认:{target['name']}",
            prompt=(
                f"目标:{target['name']}\n"
                f"详情:{target['desc']}\n\n"
                f"如确认执行,请在下方输入 DELETE(大写)以继续:"
            ),
            expected="DELETE",
        )

    def _ensure_restore_point_before_advanced(self, advanced_targets) -> bool:
        """高级项前的还原点硬约束:已勾选自动创建则信任;否则强制确认。"""
        if self.create_restore_var.get():
            return True
        # 没勾选「清理前创建还原点」,但要执行高级项 → 二次确认
        names = "、".join(t["name"] for t in advanced_targets[:3])
        more = f"等 {len(advanced_targets)} 个" if len(advanced_targets) > 3 else ""
        return messagebox.askyesno(
            "强烈建议先建还原点",
            f"即将执行的高级操作( {names}{more} )风险较高。\n\n"
            f"你尚未勾选「清理前创建还原点」。\n"
            f"继续操作前是否同意自动创建一次还原点?\n\n"
            f"选「否」将中止本次清理。",
        )

    def _clean_worker(self, selected):
        self._clean_start_ts = time.time()
        try:
            result = self.cleaner.clean_all(selected)
            # UX 改进:明确的错误反馈 — "X 个文件无法访问,已跳过"
            errors = result.get("errors", 0)
            skipped = result.get("skipped", 0)
            if errors:
                self.log(
                    f"⚠ {errors} 个文件无法访问,已跳过(权限不足或被占用)", "warn",
                )
            if skipped:
                self.log(f"  其中 {skipped} 个命中白名单已跳过", "info")

            # UX 改进:回收站二次确认(显示体积)
            if self.empty_recycle_var.get():
                rb_size = get_recycle_bin_size()
                size_str = human_size(rb_size) if rb_size else "未知"
                self.log(
                    f"[回收站] 当前占用 {size_str},准备清空 ...", "info",
                )
                self.parent.after(
                    0,
                    lambda: self._confirm_and_empty_recycle(rb_size),
                )
                # 同步等待 _confirm_and_empty_recycle 在主线程做完
                # (after callback 已经在主线程弹窗)
                # 这里通过 Event 同步回收站操作结果
                self._rb_event = threading.Event()
                self._rb_event.wait()
                if getattr(self, "_rb_success", False):
                    self.log("  -> 回收站已清空", "ok")
                else:
                    self.log("  -> 回收站清空失败或用户取消", "warn")

            elapsed = time.time() - self._clean_start_ts
            self.log(
                f"🎉 清理完成!共释放 {human_size(result['freed'])},删除 {result['removed']} 个文件 "
                f"(用时 {elapsed:.1f}s)",
                "ok",
            )
        except Exception as e:
            self.log(f"清理出错: {e}", "err")
        finally:
            self.parent.after(0, self._clean_done)

    def _confirm_and_empty_recycle(self, rb_size: int):
        """UX:清空回收站前二次确认,显示体积。"""
        size_str = human_size(rb_size) if rb_size else "未知体积"
        try:
            ok = messagebox.askyesno(
                "确认:清空回收站",
                f"即将清空回收站,当前占用 {size_str}。\n\n"
                f"清空后将无法从回收站恢复。\n\n是否继续?",
            )
            if ok:
                self._rb_success = bool(empty_recycle_bin())
            else:
                self.log("[回收站] 用户取消", "info")
                self._rb_success = False
        finally:
            ev = getattr(self, "_rb_event", None)
            if ev is not None:
                ev.set()

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

        # 第一行:目录 + 最小 + 显示 + 扫描
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

        # 第二行:扩展名 + 时间 + 排除
        filt = tk.Frame(bar_inner, bg=WC.CARD)
        filt.pack(fill=tk.X, pady=(10, 0))

        tk.Label(filt, text="扩展名:", font=("Microsoft YaHei UI", 10),
                 bg=WC.CARD, fg=WC.TEXT2).pack(side=tk.LEFT, padx=(0, 4))
        self.ext_var = tk.StringVar(value="全部")
        ext_options = ["全部", "视频(.mp4,.mkv,.avi)", "图片(.jpg,.png,.psd)",
                       "压缩包(.zip,.rar,.7z)", "镜像(.iso)", "可执行(.exe,.msi)"]
        ext_combo = wechat_optionmenu(filt, self.ext_var, ext_options, width=22)
        ext_combo.pack(side=tk.LEFT, padx=(0, 12))

        tk.Label(filt, text="时间:", font=("Microsoft YaHei UI", 10),
                 bg=WC.CARD, fg=WC.TEXT2).pack(side=tk.LEFT, padx=(0, 4))
        self.time_var = tk.StringVar(value="不限")
        time_combo = wechat_optionmenu(
            filt, self.time_var,
            ["不限", "近 1 年", "近 2 年", "近 5 年"], width=10
        )
        time_combo.pack(side=tk.LEFT, padx=(0, 12))

        tk.Label(filt, text="排除目录:", font=("Microsoft YaHei UI", 10),
                 bg=WC.CARD, fg=WC.TEXT2).pack(side=tk.LEFT, padx=(0, 4))
        self.skip_var = tk.StringVar(value="node_modules,.git,.venv,__pycache__")
        skip_entry = tk.Entry(filt, textvariable=self.skip_var,
                              font=("Microsoft YaHei UI", 10),
                              bg=WC.CARD, fg=WC.TEXT, relief="solid", bd=1,
                              highlightthickness=0, highlightbackground=WC.BORDER)
        skip_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)

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

        # 解析扩展名过滤
        ext_filter = None
        ext_choice = self.ext_var.get()
        if ext_choice != "全部":
            # 解析括号里的扩展名
            try:
                inside = ext_choice.split("(", 1)[1].rstrip(")")
                ext_filter = {"." + e.lstrip(".").lower()
                              for e in inside.split(",") if e.strip()}
            except Exception:
                ext_filter = None

        # 解析时间过滤
        min_mtime = 0
        time_choice = self.time_var.get()
        if time_choice != "不限":
            years = {"近 1 年": 1, "近 2 年": 2, "近 5 年": 5}.get(time_choice, 0)
            if years:
                min_mtime = (datetime.now() - timedelta(days=365 * years)).timestamp()

        # 解析排除目录
        skip_raw = self.skip_var.get().strip()
        extra_skip = {s.strip() for s in skip_raw.split(",") if s.strip()} if skip_raw else None

        # 清空结果
        for item in self.tree.get_children():
            self.tree.delete(item)

        self.busy = True
        self.scan_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.set_status(f"扫描 {path} ...", 0)
        ext_msg = f",{ext_choice}" if ext_filter else ""
        time_msg = f",{time_choice}" if min_mtime else ""
        skip_msg = f",排除 {len(extra_skip)} 个目录" if extra_skip else ""
        self.log(
            f"[大文件] 开始扫描: {path} (≥ {min_size_mb} MB{ext_msg}{time_msg},"
            f" Top {top_n}{skip_msg})", "info"
        )
        self.analyzer = Analyzer(
            log_callback=lambda *a, **kw: self.log(a[0] if a else ""),
            progress_callback=lambda *a, **kw: self.set_status(
                a[1] if len(a) > 1 else "", a[0] if a else 0
            ),
        )
        threading.Thread(
            target=self._scan_worker,
            args=(path, min_size_mb * 1024 * 1024, top_n, ext_filter, min_mtime, extra_skip),
            daemon=True
        ).start()

    def _scan_worker(self, path, min_size, top_n, ext_filter, min_mtime, extra_skip):
        try:
            results = self.analyzer.find_large_files(
                path, min_size=min_size, top_n=top_n,
                min_mtime=min_mtime,
                ext_filter=ext_filter,
                extra_skip=extra_skip,
            )
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
        # 展开/收起按钮
        expand_btn = wechat_button(top, "▼ 全部展开", self._expand_all, kind="ghost",
                                    font=("Microsoft YaHei UI", 9), padx=10, pady=3)
        expand_btn.pack(side=tk.RIGHT, padx=(0, 4))
        self.delete_btn = wechat_button(top, "🗑 删除选中", self._on_delete, kind="danger")
        self.delete_btn.config(state=tk.DISABLED)
        self.delete_btn.pack(side=tk.RIGHT, padx=(0, 4))

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
            # 按扩展名分组(每组是一个类别,内含若干重复组)
            from collections import defaultdict
            ext_groups = defaultdict(list)  # ext -> [group]
            for group in results:
                if not group:
                    continue
                # 取该组所有文件的扩展名(应该都一致,取第一个)
                ext = os.path.splitext(group[0]["path"])[1].lower() or "(无扩展名)"
                ext_groups[ext].append(group)

            # 渲染:按扩展名分类,每类下面再列重复组
            for ext, groups in sorted(ext_groups.items(), key=lambda x: -sum(
                g[0]["size"] * (len(g) - 1) for g in x[1]
            )):
                ext_waste = sum(g[0]["size"] * (len(g) - 1) for g in groups)
                ext_files = sum(len(g) for g in groups)
                # 类别头
                self.parent.after(0, lambda ext=ext, w=ext_waste, n=ext_files: self.tree.insert(
                    "", "end",
                    values=("▼", f"📂 {ext}  ·  {len(groups)} 组  ·  {ext_files} 个文件  ·  浪费 {human_size(w)}", ""),
                    tags=("group",), open=True
                ))
                for i, group in enumerate(groups, 1):
                    wasted = group[0]["size"] * (len(group) - 1)
                    header = f"  · 第 {i} 组({len(group)} 个,浪费 {human_size(wasted)})"
                    self.parent.after(0, lambda h=header: self.tree.insert(
                        "", "end", values=("■", h, ""), tags=("group",), open=True
                    ))
                    for f in group:
                        p = f["path"]
                        var = tk.IntVar(value=0)  # 默认不勾,让用户自己挑保留哪个
                        self.check_vars[p] = var
                        self.parent.after(0, lambda p=p, s=human_size(f["size"]): self.tree.insert(
                            "", "end", values=("☐", p, s), tags=("file",)
                        ))
            total_waste = sum(
                group[0]["size"] * (len(group) - 1) for group in results if group
            )
            self.parent.after(0, lambda: self.summary_lbl.config(
                text=f"共 {len(ext_groups)} 个类别 / {len(results)} 组,可节省 {human_size(total_waste)}"
            ))
            self.parent.after(0, lambda: self.delete_btn.config(state=tk.NORMAL))
            self.log(f"[重复] 完成:共 {len(results)} 组,按 {len(ext_groups)} 个扩展名分类", "ok")
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

    def _expand_all(self):
        """展开所有节点。"""
        def _open_all(item=""):
            self.tree.item(item, open=True)
            for child in self.tree.get_children(item):
                _open_all(child)
        _open_all()
        self.log("[重复] 已展开所有节点", "info")

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
        # 批量移到回收站(一次 syscall 处理 N 个路径,比逐个删快 10x+)
        ok, failed_paths = move_to_recycle_bin_batch(to_delete)
        self.log(
            f"[删除] 完成: {ok} 成功,{len(failed_paths)} 失败",
            "ok" if not failed_paths else "warn",
        )
        if failed_paths:
            preview = "\n".join(failed_paths[:5])
            more = f"\n...等 {len(failed_paths)} 个" if len(failed_paths) > 5 else ""
            self.log(f"[删除] 失败文件:\n{preview}{more}", "warn")
            messagebox.showwarning(
                "部分失败",
                f"已移动 {ok} 个到回收站,{len(failed_paths)} 个失败。\n"
                f"前几个失败路径:\n{preview}{more}",
            )
        else:
            messagebox.showinfo("完成", f"已移动 {ok} 个到回收站")

    def _on_cancel(self):
        if self.analyzer:
            self.analyzer.cancel()
        self.log("已请求取消,请稍候...", "warn")


# ====================== 主窗口 ======================


class MainWindow:
    def __init__(self):
        # 诊断:记录 GUI 启动各阶段(task-fix-gui-startup)
        _logger.info(
            "GUI 启动: Python=%s Platform=%s Admin=%s DPI=%s",
            sys.version.split()[0], sys.platform, is_admin(),
            # DPI awareness 已在 main() 入口设置;这里只读取
            "see main()",
        )
        try:
            self.root = tk.Tk()
        except Exception as e:
            # tk.Tk() 失败时记录详细信息(常见原因:高 DPI 未设置 / display 缺失 / Tcl/Tk DLL 缺失)
            _logger.error("tk.Tk() 初始化失败: %s\ntraceback=%s",
                          e, __import__("traceback").format_exc())
            raise
        try:
            self.root.title(f"{APP_NAME} v{APP_VERSION}")
            self.root.geometry("1100x780")
            self.root.minsize(1000, 680)
            self.root.configure(bg=WC.BG)
        except Exception as e:
            _logger.error("tk root 基本属性设置失败: %s", e)
            raise

        # 响应式布局(task-responsive-layout):
        # root 用 grid 4 行 — 标题(0 固定) / 顶部进度条(1 固定) / Notebook(2 weight=3) / 日志(3 weight=1)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=0)  # 标题栏固定
        self.root.rowconfigure(1, weight=0)  # 顶部 task_progress 固定
        self.root.rowconfigure(2, weight=3)  # Notebook 主交互区
        self.root.rowconfigure(3, weight=1)  # 底部 log_panel
        self.root.rowconfigure(4, weight=0)  # 状态栏固定(task-fix-grid-pack-mix)

        make_styled_styles()
        ThemeManager.register(self._on_theme_change)

        # 顶部标题栏(row=0, sticky=EW 横向铺满)
        self._build_title()

        # 全局任务进度条(row=1, sticky=EW 横向铺满)
        self._build_top_progress()

        # 中间:Notebook(row=2, 主伸缩区, sticky=NSEW)
        self.notebook = ttk.Notebook(self.root, style="WC.TNotebook")
        self.notebook.grid(row=2, column=0, sticky="nsew", padx=12, pady=(4, 4))

        # 底部:日志 + 状态文字(row=3, sticky=NSEW)
        self._build_log_panel()

        # 底部:状态栏 — 创建 self.status_lbl(供 _set_status 使用)
        # task-fix-status-lbl 修复:之前 task-ui-adjust 合并 status_bar 进度条到顶部时,
        # 误删了 __init__ 中这一行调用,导致 CleanerTab._refresh_disk → _set_status 时
        # AttributeError: 'MainWindow' object has no attribute 'status_lbl'
        self._build_status_bar()

        # 4 个 tab
        self.tab_clean = CleanerTab(self.notebook, self._log, self._set_status)
        self.tab_big = BigFilesTab(self.notebook, self._log, self._set_status)
        self.tab_folder = FolderSizeTab(self.notebook, self._log, self._set_status)
        self.tab_dup = DuplicateTab(self.notebook, self._log, self._set_status)

        self.notebook.add(self.tab_clean.parent, text="🧹  垃圾清理")
        self.notebook.add(self.tab_big.parent, text="📦  大文件")
        self.notebook.add(self.tab_folder.parent, text="📂  文件夹大小")
        self.notebook.add(self.tab_dup.parent, text="🔁  重复文件")

        # 持久化主题选择
        self._load_theme_pref()

    def _build_log_panel(self):
        frm = tk.Frame(self.root, bg=WC.BG)
        # 响应式布局(task-responsive-layout):row=3, sticky=NSEW,跟随 root rowconfigure weight 伸缩
        frm.grid(row=3, column=0, sticky="nsew", padx=12, pady=(4, 12))
        self.log_frame = frm

        # 标题行:日志 + 级别过滤 + 清除
        title_row = tk.Frame(frm, bg=WC.BG)
        title_row.pack(fill=tk.X, pady=(0, 4))
        tk.Label(title_row, text="日志", font=("Microsoft YaHei UI", 10, "bold"),
                 bg=WC.BG, fg=WC.TEXT2).pack(side=tk.LEFT)
        # 级别过滤(中间)
        self.log_levels = {"info": tk.IntVar(value=1), "ok": tk.IntVar(value=1),
                           "warn": tk.IntVar(value=1), "err": tk.IntVar(value=1)}
        levels = tk.Frame(title_row, bg=WC.BG)
        levels.pack(side=tk.LEFT, padx=(20, 0))
        for label, lv, color in [
            ("信息", "info", WC.TEXT2),
            ("成功", "ok", WC.GREEN),
            ("警告", "warn", WC.ORANGE),
            ("错误", "err", WC.RED),
        ]:
            tk.Checkbutton(
                levels, text=label, variable=self.log_levels[lv],
                bg=WC.BG, fg=color,
                activebackground=WC.BG, activeforeground=color,
                selectcolor=WC.BG, font=("Microsoft YaHei UI", 9),
                relief="flat", bd=0, highlightthickness=0,
            ).pack(side=tk.LEFT, padx=2)
        clear_btn = wechat_button(title_row, "清除", self._clear_log, kind="ghost",
                                  font=("Microsoft YaHei UI", 9), padx=10, pady=2)
        clear_btn.pack(side=tk.RIGHT)

        # 日志框
        text_frame = tk.Frame(frm, bg=WC.BORDER_LIGHT)
        text_frame.pack(fill=tk.X)
        self.log_text = scrolledtext.ScrolledText(
            text_frame, bg=CURRENT_THEME.LOG_BG, fg=CURRENT_THEME.LOG_FG,
            font=("Consolas", 9), relief="flat", bd=0,
            insertbackground=CURRENT_THEME.LOG_FG, height=22, wrap=tk.WORD,
            highlightthickness=0
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        self.log_text.config(state=tk.DISABLED)

        self._log(f"欢迎使用 {APP_NAME} v{APP_VERSION}")
        if is_admin():
            self._log("✓ 已以管理员身份运行", "ok")
        else:
            self._log("⚠ 非管理员,部分系统目录无法访问", "warn")

    def _load_theme_pref(self):
        """启动时读上次保存的主题。"""
        try:
            import json
            cfg = Path(os.environ.get("APPDATA", str(Path.home()))) / "C_Cleaner" / "config.json"
            if cfg.exists():
                data = json.loads(cfg.read_text(encoding="utf-8"))
                if data.get("theme") == "dark":
                    apply_theme(_DarkTheme)
                    self.theme_btn.config(text="☀️")
        except Exception:
            pass

    def _save_theme_pref(self, name: str):
        try:
            import json
            cfg_dir = Path(os.environ.get("APPDATA", str(Path.home()))) / "C_Cleaner"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            cfg = cfg_dir / "config.json"
            cfg.write_text(json.dumps({"theme": name}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _on_theme_change(self):
        """主题切换广播:重建 ttk 样式 + 重画所有 widget。"""
        make_styled_styles()  # Treeview / Progressbar 颜色更新
        # 重画 root 和已知的 frame
        self.root.configure(bg=WC.BG)
        # 标题栏 / 状态栏 / 日志框 重新 config
        if hasattr(self, "title_frame"):
            for w in self.title_frame.winfo_children():
                self._recolor_widget(w)
            self.title_frame.configure(bg=WC.BG)
        if hasattr(self, "status_frame"):
            for w in self.status_frame.winfo_children():
                self._recolor_widget(w)
            self.status_frame.configure(bg=WC.BG)
        if hasattr(self, "log_frame"):
            for w in self.log_frame.winfo_children():
                self._recolor_widget(w)
            self.log_frame.configure(bg=WC.BG)
        # 日志框特殊处理
        if hasattr(self, "log_text"):
            self.log_text.configure(
                bg=CURRENT_THEME.LOG_BG, fg=CURRENT_THEME.LOG_FG,
                insertbackground=CURRENT_THEME.LOG_FG,
            )
        # 4 个 tab
        for tab in [self.tab_clean, self.tab_big, self.tab_folder, self.tab_dup]:
            tab._recolor()

    def _recolor_widget(self, w):
        """根据 widget 类型重设 bg/fg。简化处理:只对 Label/Frame/Button 生效。"""
        try:
            cls = w.winfo_class()
            if cls in ("Frame", "Labelframe"):
                # Frame 没记录"原始 bg",这里用 BG 兜底
                w.configure(bg=WC.BG)
            elif cls in ("Label", "Button", "Checkbutton", "Radiobutton"):
                # 读原始 fg(如可读),否则用 TEXT
                orig_fg = getattr(w, "_orig_fg", None)
                if orig_fg is None:
                    try:
                        orig_fg = w.cget("fg")
                        w._orig_fg = orig_fg
                    except Exception:
                        orig_fg = WC.TEXT
                # 简单规则:如果原 fg 偏灰,改成新的 TEXT2
                w.configure(bg=WC.BG, fg=orig_fg)
        except Exception:
            pass

    def _toggle_theme(self):
        if CURRENT_THEME is _LightTheme:
            apply_theme(_DarkTheme)
            self.theme_btn.config(text="☀️")
            self._save_theme_pref("dark")
        else:
            apply_theme(_LightTheme)
            self.theme_btn.config(text="🌙")
            self._save_theme_pref("light")

    def _build_title(self):
        frm = tk.Frame(self.root, bg=WC.BG)
        # 响应式布局:row=0, sticky=EW 横向铺满,高度由内容固定
        frm.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        self.title_frame = frm
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
        # 右:主题按钮 + 管理员标识
        self.theme_btn = wechat_button(
            frm, "🌙", self._toggle_theme, kind="ghost",
            font=("Microsoft YaHei UI", 12), padx=8, pady=2,
        )
        self.theme_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self.admin_lbl = tk.Label(
            frm,
            text="✓ 管理员" if is_admin() else "⚠ 非管理员",
            font=("Microsoft YaHei UI", 9),
            bg=WC.BG,
            fg=WC.GREEN if is_admin() else WC.ORANGE
        )
        self.admin_lbl.pack(side=tk.RIGHT)

    def _build_top_progress(self):
        """全局任务进度条(顶部,所有 Tab 共享)。

        UI 调整(task-ui-adjust):合并原 CleanerTab 顶部卡片右侧的"本次进度"
        和主窗口底部 status_bar 中的 progress / progress_pct(两者对 CleanerTab
        是重复信号),只保留这一处。所有 Tab 通过 set_status(percent) 都更新这里。
        """
        frm = tk.Frame(self.root, bg=WC.BG)
        # 响应式布局:row=1, sticky=EW 横向铺满,高度固定
        frm.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 4))
        self.top_progress_frame = frm
        tk.Label(frm, text="任务进度", font=("Microsoft YaHei UI", 9),
                 bg=WC.BG, fg=WC.TEXT2).pack(side=tk.LEFT)
        self.task_stage_lbl = tk.Label(
            frm, text="(空闲)",
            font=("Microsoft YaHei UI", 9),
            bg=WC.BG, fg=WC.TEXT3, anchor=tk.W,
        )
        self.task_stage_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 8))
        self.task_progress_pct = tk.Label(
            frm, text="  0%  ",
            font=("Consolas", 10, "bold"),
            bg=WC.BG, fg=WC.GREEN, width=7, anchor=tk.E,
        )
        self.task_progress_pct.pack(side=tk.RIGHT, padx=(0, 6))
        self.task_progress = ttk.Progressbar(
            frm, maximum=100, value=0, mode="determinate",
            style="WC.Horizontal.TProgressbar", length=240,
        )
        self.task_progress.pack(side=tk.RIGHT)

    def _build_status_bar(self):
        """状态栏(底部,只剩 status_lbl)。

        UI 调整:删除原 self.progress / self.progress_pct(与顶部 task_progress
        对 CleanerTab 重复)。其他 Tab 通过 set_status 进度信息已统一在顶部显示。

        task-fix-grid-pack-mix:状态栏 frame 作为 self.root 的直接子控件,
        必须用 grid(与 title/top_progress/notebook/log_panel 一致),
        否则与 root 内已有的 grid 子控件混用 pack 会抛
        TclError: cannot use geometry manager pack inside . which already has slaves managed by grid
        """
        frm = tk.Frame(self.root, bg=WC.BG)
        frm.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 8))
        self.status_frame = frm
        self.status_lbl = tk.Label(
            frm, text="就绪",
            font=("Microsoft YaHei UI", 9),
            bg=WC.BG, fg=WC.TEXT2, anchor=tk.W
        )
        self.status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _log(self, msg: str, level: str = "info"):
        if not hasattr(self, "log_text"):
            return
        # 级别过滤
        lv = self.log_levels.get(level) if hasattr(self, "log_levels") else None
        if lv is not None and not lv.get():
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
            pct = max(0, min(100, int(round(percent * 100))))
            # 顶部全局任务进度条(取代 CleanerTab 顶部 card 重复 + 底部 status_bar progress)
            self.task_progress["value"] = pct
            self.task_progress_pct.config(
                text=f"{pct:>3d}%",
                fg=WC.GREEN if pct < 100 else WC.BLUE,
            )
            if pct >= 100:
                stage = "✓ 完成"
            elif pct <= 0:
                stage = msg or "(准备)"
            else:
                stage = (msg or "进行中")[:30]
            self.task_stage_lbl.config(text=stage)
        elif hasattr(self, "task_stage_lbl"):
            self.task_stage_lbl.config(text=msg or "(空闲)")

    def run(self):
        self.root.mainloop()


# ====================== CLI / 入口 ======================


def _safe_input(prompt: str, default: str = "") -> str:
    """input() 的安全包装:无 stdin(GUI 子系统 / pipe 关闭)时返回 default,不抛 RuntimeError。

    PyInstaller --noconsole 打包后 sys.stdin=None,直接调 input() 会抛
    RuntimeError: input(): lost sys.stdin。这里捕获 EOFError / RuntimeError
    兜底,保证 CLI 在异常环境下也能稳定退出。
    """
    try:
        return input(prompt)
    except (RuntimeError, EOFError, KeyboardInterrupt):
        return default


def run_cli():
    """CLI 模式入口。所有 input() 调用通过 _safe_input 兜底,避免无 stdin 时崩溃。"""
    print(f"{APP_NAME} v{APP_VERSION} (CLI 模式)")
    print("=" * 60)
    info = get_disk_usage("C")
    print(f"C 盘: {human_size(info['used'])} / {human_size(info['total'])} ({info['percent']:.1f}%)")
    print("\n选项:1.清理  2.大文件  3.文件夹大小  4.重复文件  q.退出")
    choice = _safe_input("> ", "q").strip()
    if choice == "1":
        sc = Scanner(log_callback=lambda *a, **kw: print(*a))
        sc.scan_all()
        confirm = _safe_input("\n确认清理? (y/N) > ", "n").strip().lower()
        if confirm == "y":
            Cleaner(log_callback=lambda *a, **kw: print(*a)).clean_all(
                [t["id"] for t in CLEAN_TARGETS]
            )
    elif choice == "2":
        path = _safe_input("扫描目录 > ", ".").strip() or "."
        try:
            min_mb = int(_safe_input("最小 MB > ", "100").strip() or "100")
        except ValueError:
            min_mb = 100
        a = Analyzer(log_callback=lambda *a, **kw: print(*a))
        results = a.find_large_files(path, min_size=min_mb * 1024 * 1024, top_n=100)
        for r in results[:20]:
            print(f"  {human_size(r['size']):>10}  {r['path']}")
    elif choice == "3":
        path = _safe_input("扫描目录 > ", ".").strip() or "."
        a = Analyzer(log_callback=lambda *a, **kw: print(*a))
        results = a.find_large_dirs(path, top_n=50, max_depth=3)
        for r in results[:20]:
            print(f"  {human_size(r['size']):>10}  {r['path']}")
    elif choice == "4":
        path = _safe_input("扫描目录 > ", ".").strip() or "."
        try:
            min_mb = int(_safe_input("最小 MB > ", "1").strip() or "1")
        except ValueError:
            min_mb = 1
        a = Analyzer(log_callback=lambda *a, **kw: print(*a))
        results = a.find_duplicates(path, min_size=min_mb * 1024 * 1024)
        for i, group in enumerate(results, 1):
            print(f"\n组 {i} ({len(group)} 个,大小 {human_size(group[0]['size'])}):")
            for f in group:
                print(f"  {f['path']}")
    # 'q' 或无效输入:静默退出,避免在无 stdin 时循环卡住


def _has_stdin() -> bool:
    """检测 stdin 是否可用(非 None 且可读)。

    PyInstaller --noconsole 打包后 sys.stdin=None;某些受限环境下 sys.stdin
    存在但 isatty() 返回 False。两种情况都视为不可用。
    """
    try:
        if sys.stdin is None:
            return False
        # 某些环境 stdin 是 None-like 对象,isatty() 不存在
        if not hasattr(sys.stdin, "isatty"):
            return False
        return True
    except Exception:
        return False


# ---- PyInstaller Tcl/Tk 数据兜底(v3.3.2 修复 GUI 启动 Tcl_InitError)----
def _fix_tcl_tk_paths_for_pyinstaller():
    """PyInstaller --onefile 打包后 sys._MEIPASS 是临时解压目录;
    Tcl/Tk 需要 init.tcl / tk.tcl 路径,默认查找失败会 Tcl_InitError。
    在 main() 最开头调用,提前设好 TCL_LIBRARY / TK_LIBRARY 环境变量。
    """
    if not (getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')):
        return
    base = sys._MEIPASS
    for sub, envname in [('tcl', 'TCL_LIBRARY'), ('tk', 'TK_LIBRARY')]:
        p = os.path.join(base, sub)
        if os.path.isdir(p) and not os.environ.get(envname):
            os.environ[envname] = p


def main():
    _fix_tcl_tk_paths_for_pyinstaller()
    # 入口最优先:Win10/11 高 DPI 适配(tkinter 不显式设置会糊甚至崩溃)
    if not set_dpi_awareness():
        _logger.warning("DPI awareness 设置失败,继续")

    if not is_admin():
        print("=" * 60)
        print("  ⚠ 当前不是管理员权限,部分系统目录无法访问")
        print("  请右键 [运行.bat] -> [以管理员身份运行]")
        print("=" * 60)
        if "--cli" in sys.argv:
            # 双保险:即使显式 --cli,无 stdin 时也不调用 input()
            if _has_stdin():
                _safe_input("按回车退出...", "")
            return

    if TK_AVAILABLE and "--cli" not in sys.argv:
        try:
            MainWindow().run()
            return
        except Exception as e:
            # 完整 traceback 写 logger,便于排查 .exe 启动失败根因
            import traceback as _tb
            tb_text = _tb.format_exc()
            print(f"GUI 启动失败: {e},回退 CLI")
            _logger.error("GUI 启动失败:\n%s", tb_text)

    # 兜底:CLI 模式要求 stdin 可用;不可用时直接退出(避免 input() 抛 RuntimeError)
    if not _has_stdin():
        print("[CLI] 无可用 stdin(可能是 GUI 子系统打包或受限环境),改弹窗提示后退出。")
        _logger.error("CLI 模式无 stdin 可用,退出码 1")
        if TK_AVAILABLE:
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw()
                # 收集诊断信息
                diag = []
                diag.append(f"Python: {sys.version.split()[0]} ({sys.platform})")
                diag.append(f"Admin: {is_admin()}")
                diag.append(f"DPI set: 尝试过(见 logger)")
                _last_tb = _logger.handlers[0].baseFilename if _logger.handlers else "(no log)"
                diag.append(f"日志: {_last_tb}")
                diag_text = "\n".join(diag)
                messagebox.showerror(
                    f"{APP_NAME} v{APP_VERSION}",
                    "CLI 模式无可用 stdin,且 GUI 也无法启动。\n\n"
                    "排查建议:\n"
                    "1. 右键 [运行.bat] → [以管理员身份运行] 重试\n"
                    "2. 检查 %APPDATA%\\GreenCleaner\\green_cleaner.log 看 GUI 启动失败 traceback\n"
                    "3. 重新安装或检查 Windows Visual C++ 运行库\n\n"
                    f"环境信息:\n{diag_text}",
                )
                root.destroy()
            except Exception as ex:
                _logger.error("兜底弹窗失败: %s", ex)
        sys.exit(1)
    run_cli()


if __name__ == "__main__":
    main()
