"""测试夹具与构造器 (v1)

与 cleaner.py 实现解耦:
- fake_windows_fs 用 FakeFsConfig 控制每个目录节点是否存在
- hash_steps 按 "size 唯一 / size 同 head 异 / 全相同" 三组构造测试数据
- restore_point_inject_payloads 提供含 ; / ' / ` / $ 等危险字符的 description 样本

设计目标:实现尚未完成也能跑测试,断言留空即用。
"""

from __future__ import annotations

import hashlib
import os
import random
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ====================== 1. fake_windows_fs ======================


@dataclass
class FakeFsConfig:
    """控制 fake_windows_fs 构造哪些目录节点。

    每个字段名=C 盘下的"虚拟路径段",值为 True 表示创建(含占位文件),
    False 表示不创建(用于测"目录不存在→skip"分支)。

    字段名使用「脱敏」命名,不直接复用 C:\Windows\Temp 这种绝对路径
    是为了避免被误以为是真实系统目录——所有路径都在 tmp_path 下隔离。
    """

    # 系统级 (对应 C:\Windows\* 系列)
    win_temp: bool = True               # C:\Windows\Temp
    prefetch: bool = True               # C:\Windows\Prefetch
    software_distribution: bool = True  # C:\Windows\SoftwareDistribution\Download
    inf_logs: bool = True               # C:\Windows\INF
    cbs_logs: bool = True              # C:\Windows\Logs\CBS
    winsxs_manifest: bool = True        # C:\Windows\WinSxS\ManifestCache

    # 升级/重置残留 (advanced 档)
    windows_bt: bool = True             # C:\$WINDOWS.~BT
    sysreset: bool = True               # C:\$SysReset
    windows_old: bool = True            # C:\Windows.old

    # 系统级高级 (对应命令)
    hiberfil_sys: bool = False          # C:\hiberfil.sys (默认不存在,避免污染)
    vss_shadow_meta: bool = False       # C:\System Volume Information (默认不构造)

    # 防御/系统
    defender_quarantine: bool = True    # C:\ProgramData\Microsoft\Windows Defender\Quarantine

    # 用户临时
    user_temp: bool = True              # %TEMP%
    user_local_temp: bool = True        # %LOCALAPPDATA%\Temp

    # 每个目录构造几个占位文件 (字节内容随机但长度固定)
    file_count_per_dir: int = 3
    file_size_bytes: int = 512


# 路径段映射表:字段名 -> (segment_list, files_to_create)
# files_to_create 是 (name, size) 列表,None 表示自动生成
_FAKE_DIR_LAYOUT: dict[str, tuple[list[str], list[tuple[str, int] | None]]] = {
    "win_temp": (
        ["Windows", "Temp"],
        [("setup.log", 1024), ("crashdumps", 0), ("installer", 0)],
    ),
    "prefetch": (
        ["Windows", "Prefetch"],
        [("READYBOOST.PF", 8192), ("SYSTEM.PF", 16384)],
    ),
    "software_distribution": (
        ["Windows", "SoftwareDistribution", "Download"],
        [("update1.cab", 4096), ("update2.cab", 8192)],
    ),
    "inf_logs": (
        ["Windows", "INF"],
        [("setupapi.dev.log", 2048)],
    ),
    "cbs_logs": (
        ["Windows", "Logs", "CBS"],
        [("CbsPersist_20250101.log", 4096)],
    ),
    "winsxs_manifest": (
        ["Windows", "WinSxS", "ManifestCache"],
        [("manifest1.bin", 1024)],
    ),
    "windows_bt": (
        ["$WINDOWS.~BT"],
        [("Sources", 0), ("Windows", 0)],
    ),
    "sysreset": (
        ["$SysReset"],
        [("OldOS", 0)],
    ),
    "windows_old": (
        ["Windows.old"],
        [("Windows", 0), ("Program Files", 0)],
    ),
    "hiberfil_sys": (
        [],  # 文件,不是目录
        [("hiberfil.sys", 6 * 1024 * 1024)],  # 6 GB 占位
    ),
    "vss_shadow_meta": (
        ["System Volume Information"],
        [],  # 空
    ),
    "defender_quarantine": (
        ["ProgramData", "Microsoft", "Windows Defender", "Quarantine", "ResourceData"],
        [("payload.bin", 2048)],
    ),
    "user_temp": (
        ["Temp"],  # 走 tmp_path/Temp,跳过 c_root
        [("tmpA.tmp", 512), ("tmpB.log", 256)],
    ),
    "user_local_temp": (
        ["LocalAppData", "Temp"],  # 同上
        [("localA.tmp", 512)],
    ),
}


# 这些字段的 target 路径不走 c_root(用户级目录,不挂在 C:\ 下)
_SKIP_C_ROOT_FIELDS = frozenset({"user_temp", "user_local_temp"})


def fake_windows_fs(
    tmp_path: Path,
    config: FakeFsConfig | None = None,
    *,
    populate: bool = True,
) -> dict[str, Path | None]:
    """在 tmp_path 下构造一个 "假 C 盘" 目录树。

    返回路径映射字典,key 与 FakeFsConfig 字段名一致,value 是该目录/文件的
    真实路径,目录不存在则为 None。cleaner.py 中的测试可以直接拿到这些路径
    来构造扫描/清理 target——但因为绝对路径是 tmp_path 派生的,所以不会触达
    真实 C 盘。

    参数:
        tmp_path: 测试根目录(必须是已存在的目录)
        config:   控制每个节点是否创建,None 时用默认值
        populate: 是否写占位文件

    返回示例:
        {
            "win_temp": Path("/tmp/.../C/Windows/Temp"),
            "prefetch": Path("/tmp/.../C/Windows/Prefetch"),
            "hiberfil_sys": None,  # config 设了 False
            ...
        }
    """
    cfg = config or FakeFsConfig()
    paths: dict[str, Path | None] = {}

    # 假 C 盘根:tmp_path / "C"
    c_root = tmp_path / "C"
    c_root.mkdir(parents=True, exist_ok=True)

    for field_name, layout in _FAKE_DIR_LAYOUT.items():
        segments, files = layout
        # 计算目标路径
        if field_name in _SKIP_C_ROOT_FIELDS:
            # 用户级目录,不挂在 c_root 下
            target = tmp_path.joinpath(*segments)
        else:
            target = c_root.joinpath(*segments) if segments else c_root

        # 是否启用
        enabled = getattr(cfg, field_name)
        if not enabled:
            paths[field_name] = None
            continue

        if not segments:
            # 文件(hiberfil.sys 这种)
            target.parent.mkdir(parents=True, exist_ok=True)
            if populate and files:
                for fname, size in files:
                    fp = target.parent / fname
                    fp.write_bytes(b"\x00" * min(size, 1024 * 1024))  # 最大写 1MB 节省时间
            paths[field_name] = target if target.exists() else None
        else:
            target.mkdir(parents=True, exist_ok=True)
            if populate:
                for entry in files:
                    if entry is None:
                        continue
                    fname, size = entry
                    fp = target / fname
                    if size == 0:
                        # 当作子目录占位
                        fp.mkdir(parents=True, exist_ok=True)
                    else:
                        fp.parent.mkdir(parents=True, exist_ok=True)
                        # 占位文件只写 64 字节 (节约)
                        fp.write_bytes(b"\x00" * 64)
            paths[field_name] = target if target.exists() else None

    return paths


# ====================== 2. hash_steps ======================


@dataclass
class HashStepsResult:
    """hash_steps 返回值,所有路径都是字符串方便直接传 cleaner。"""

    fake_dup_paths: list[str]            # 全相同组(10 个,全 stage 都命中)
    size_unique_paths: list[str]         # size 唯一组(20 个,L1 即剔除)
    size_same_head_diff_paths: list[str] # size 同 head 异组(30 个,L2 剔除)
    all_paths: list[str]                 # 所有 60 个路径
    group_c_size: int                    # 真重复组的单文件大小
    fake_dup_count: int                  # 真重复组文件数

    def total(self) -> int:
        return len(self.all_paths)

    def expected_true_groups(self) -> list[dict[str, Any]]:
        """断言用:find_duplicates 应该返回至少 1 个组,size=group_c_size,副本数=fake_dup_count。"""
        return [{"size": self.group_c_size, "count": self.fake_dup_count}]


def hash_steps(
    tmp_path: Path,
    *,
    n_unique: int = 20,
    n_size_same_head_diff: int = 30,
    n_full_same: int = 10,
    base_size: int = 100 * 1024,   # 100 KB
    seed: int = 42,
) -> HashStepsResult:
    """构造 ≥60 个临时文件,按三级哈希阶段分组:

      group A:  n_unique 个 size 唯一文件 (L1 size 不同 → 直接剔除)
      group B:  n_size_same_head_diff 个 size 相同但 head 字节不同的文件 (L2 head 不同 → 剔除)
      group C:  n_full_same 个 size + head + md5 全相同文件 (L3 命中,1 个真重复组)

    设计要点:
      - group A 的 size 互不相同,确保 L1 后桶大小都是 1
      - group B 共享同一 size,但每个文件的前 base_size 字节用不同随机数据填充,
        head_hash(SHA-1)互不相同,L2 后全部剔除
      - group C 是 base_size 字节的零填充,完全相同,L3 后合并

    返回 HashStepsResult,测试可对 fake_dup_count (n_full_same) 断言"≥80% 剔除"
    与"≥1 个真重复组,组内有 n_full_same 个文件"。

    注意:实际剔除率取决于 Analyzer.find_duplicates 是否实现三级哈希——本工具只负责
    准备数据,断言逻辑写在 task-test 的扩展测试里。
    """
    rng = random.Random(seed)
    files_dir = tmp_path / "hash_steps_data"
    files_dir.mkdir(parents=True, exist_ok=True)

    fake_dup_paths: list[str] = []
    size_unique_paths: list[str] = []
    size_same_head_diff_paths: list[str] = []

    # ---- group C: 全相同 (先建,后建 A/B 时避开冲突) ----
    group_c_size = base_size
    group_c_content = b"\x00" * base_size
    for i in range(n_full_same):
        fp = files_dir / f"group_c_{i:03d}.bin"
        fp.write_bytes(group_c_content)
        fake_dup_paths.append(str(fp))

    # ---- group A: size 唯一(实际写不同 size,确保 L1 size 分桶时每桶 1 个) ----
    # size 必须互不相等,且与 group_c_size / group_b_size 不同
    for i in range(n_unique):
        sz = base_size + (i + 1) * 1024  # 101K, 102K, ..., 120K
        fp = files_dir / f"group_a_{i:03d}.bin"
        fp.write_bytes(bytes(rng.getrandbits(8) for _ in range(sz)))
        size_unique_paths.append(str(fp))

    # ---- group B: size 相同但 head 不同(与 group C 共享 size 但内容随机) ----
    # 共享 size=base_size (与 group C 一致),但每个文件内容随机 → head-hash 必然不同
    for i in range(n_size_same_head_diff):
        fp = files_dir / f"group_b_{i:03d}.bin"
        fp.write_bytes(bytes(rng.getrandbits(8) for _ in range(base_size)))
        size_same_head_diff_paths.append(str(fp))

    all_paths = fake_dup_paths + size_unique_paths + size_same_head_diff_paths

    return HashStepsResult(
        fake_dup_paths=fake_dup_paths,
        size_unique_paths=size_unique_paths,
        size_same_head_diff_paths=size_same_head_diff_paths,
        all_paths=all_paths,
        group_c_size=group_c_size,
        fake_dup_count=n_full_same,
    )


# ====================== 3. restore_point_inject_payloads ======================


# 8 个恶意 description 样本,覆盖 PowerShell 注入常见向量
_INJECT_PAYLOADS: list[str] = [
    # 1. 单引号闭合 + 注入新命令
    "clean'; Remove-Item C:\\Windows -Recurse -Force; '",
    # 2. 反引号执行 (PowerShell 转义)
    "clean` calc.exe",
    # 3. $() 子表达式
    "clean$(calc.exe)",
    # 4. & 后台链 + path 拼接
    "clean & del /F /Q C:\\Users\\*",
    # 5. ; 多命令串联
    "clean; rd /S /Q C:\\",
    # 6. 换行注入
    "clean\r\nRemove-Item C:\\Windows -Recurse -Force",
    # 7. UTF-8 BOM 攻击
    "\ufeffclean'",
    # 8. 双引号 + $variable 展开
    'clean" $env:USERNAME "',
]


def restore_point_inject_payloads() -> list[str]:
    """返回 8 个含 ; / ' / ` / $ 等危险字符的恶意 description 字符串。

    用法:测试 create_restore_point 是否对 description 做字符清洗或
    -EncodedCommand Base64 编码,避免任何 payload 在 PowerShell 命令行被解释。

    注意:返回的字符串仅用于「传入测试」,**不要**打印到控制台或执行
    PowerShell —— 这些是反面教材,直接执行会破坏系统。
    """
    return list(_INJECT_PAYLOADS)


# ====================== 4. 辅助工具 ======================


def make_fake_clean_target(
    *,
    target_id: str = "fake_target",
    name: str = "假目标",
    level: str = "safe",
    paths: list[str] | None = None,
    kind: str = "files",
    **extra: Any,
) -> dict[str, Any]:
    """构造一个 CLEAN_TARGETS 兼容的测试目标 dict。

    避免每个测试都重复 {"id": ..., "name": ..., ...} 的样板。
    """
    target: dict[str, Any] = {
        "id": target_id,
        "name": name,
        "level": level,
        "kind": kind,
        "desc": "test fixture",
    }
    if kind == "command":
        target["command"] = extra.pop("command", ["cmd", "/c", "exit", "0"])
    elif kind == "wild_temp":
        target["roots"] = extra.pop("roots", paths or [])
        target["patterns"] = extra.pop("patterns", ["*.tmp", "*.log"])
        target["min_age_days"] = extra.pop("min_age_days", 0)
        target["min_size"] = extra.pop("min_size", 1)
    else:
        target["paths"] = paths or []
    target.update(extra)
    return target


def quick_md5(path: str | Path) -> str:
    """对文件算 MD5 hexdigest,小文件用,大文件只算 head 64KB + size (用于断言)。"""
    p = Path(path)
    h = hashlib.md5()
    h.update(f"{p.stat().st_size}".encode())
    with open(p, "rb") as f:
        h.update(f.read(65536))
    return h.hexdigest()


def quick_sha1_head(path: str | Path, n_bytes: int = 65536) -> str:
    """对文件头部 n_bytes 算 SHA-1,用于断言 hash_steps 的 L2 行为。"""
    p = Path(path)
    h = hashlib.sha1()
    with open(p, "rb") as f:
        h.update(f.read(n_bytes))
    return h.hexdigest()


__all__ = [
    "FakeFsConfig",
    "fake_windows_fs",
    "HashStepsResult",
    "hash_steps",
    "restore_point_inject_payloads",
    "make_fake_clean_target",
    "quick_md5",
    "quick_sha1_head",
]
