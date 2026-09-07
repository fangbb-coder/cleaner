"""重复文件检测性能基准:对比改造前(全量 MD5) vs 改造后(三级 SHA-1 head + MD5)。

构造 N 个候选文件(同 size 不同内容 + 同 size 同内容混合),
分别用两种策略跑一遍,记录 wallclock。
"""
import os
import sys
import shutil
import tempfile
import time
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import cleaner
from cleaner import Analyzer


# ====== 测试数据构造 ======

def build_dataset(root: Path, n_files: int = 100, file_size: int = 2 * 1024 * 1024,
                  n_dup_groups: int = 5, dup_copies: int = 3):
    """构造混合数据集:
    - 一部分文件 size 相同但内容完全不同(假阳性候选,L2 head hash 应剔除)
    - 一部分文件 size 相同且内容相同(真重复,L3 MD5 确认)
    每组同 size 的文件数大约 5-10 个。
    """
    rng_byte = 0
    for i in range(n_files):
        path = root / f"f_{i:04d}.bin"
        if i % (n_files // max(n_dup_groups, 1)) < dup_copies and (i // (n_files // max(n_dup_groups, 1))) < n_dup_groups:
            # 真重复组:同 size 同内容
            path.write_bytes(bytes([i // dup_copies]) * file_size)
        else:
            # 假阳性:同 size 不同内容
            path.write_bytes(bytes([(i + 17) % 256]) * file_size)
        rng_byte += 1


def build_pure_dataset(root: Path, n_files: int = 100, file_size: int = 2 * 1024 * 1024):
    """纯假阳性数据集:100 个文件 size 都相同,但内容完全不同。
    这是最考验 L2 head hash 剔除能力的场景。"""
    for i in range(n_files):
        path = root / f"pf_{i:04d}.bin"
        path.write_bytes(bytes([i % 256]) * file_size)


# ====== 旧策略模拟:全量 MD5 ======

def legacy_full_md5_scan(root: Path):
    """模拟旧的 find_duplicates 行为:对所有候选文件直接算完整 MD5,不做 head hash 预筛。"""
    # 第一遍:列文件 + size 分桶(同 Analyzer)
    files = []
    for dp, _, fns in os.walk(root):
        for fn in fns:
            fp = Path(dp) / fn
            try:
                st = fp.stat()
                files.append((str(fp), st.st_size, st.st_mtime))
            except OSError:
                continue
    size_buckets = {}
    for path, size, _ in files:
        size_buckets.setdefault(size, []).append(path)
    dup_sizes = {s: ps for s, ps in size_buckets.items() if len(ps) >= 2}
    if not dup_sizes:
        return 0.0, 0
    cand_count = sum(len(v) for v in dup_sizes.values())
    # 第二/三遍合并:直接对所有候选算完整 MD5(不分级)
    md5_buckets = {}
    t0 = time.perf_counter()
    for paths in dup_sizes.values():
        for p in paths:
            try:
                h = hashlib.md5()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
                md5_buckets.setdefault(h.hexdigest(), []).append(p)
            except OSError:
                continue
    elapsed = time.perf_counter() - t0
    return elapsed, cand_count


# ====== 新策略:三级哈希 ======

def new_three_level_scan(root: Path):
    """用改造后的 Analyzer.find_duplicates 跑同一数据集。"""
    a = Analyzer(log_callback=lambda *a, **kw: None,
                 progress_callback=lambda *a, **kw: None)
    t0 = time.perf_counter()
    groups = a.find_duplicates(str(root), min_size=1024 * 1024, max_depth=3, top_n_groups=1000)
    elapsed = time.perf_counter() - t0
    return elapsed, len(groups), groups


# ====== 跑基准 ======

def main():
    print("=" * 72)
    print(f"Python {sys.version.split()[0]} | cleaner.py Analyzer 三级哈希性能基准")
    print("=" * 72)

    # 场景 1:100 个 2MB 同 size 不同内容(全是假阳性)
    print("\n[场景 1] 100 个 2MB 同 size,内容完全不同(纯假阳性)")
    with tempfile.TemporaryDirectory(prefix="bench1_") as tmp1:
        build_pure_dataset(Path(tmp1), n_files=100, file_size=2 * 1024 * 1024)
        t_old, n_old = legacy_full_md5_scan(Path(tmp1))
        t_new, n_groups, _ = new_three_level_scan(Path(tmp1))
        speedup = t_old / max(t_new, 1e-6)
        print(f"  候选文件数 : {n_old}")
        print(f"  旧(全量 MD5): {t_old:.3f}s")
        print(f"  新(三级哈希): {t_new:.3f}s ({n_groups} 组确认重复)")
        print(f"  加速比      : {speedup:.2f}x")
        scenario1 = (n_old, t_old, t_new, speedup)

    # 场景 2:100 个 2MB,混合真重复 + 假阳性
    print("\n[场景 2] 100 个 2MB,混合真重复 + 假阳性")
    with tempfile.TemporaryDirectory(prefix="bench2_") as tmp2:
        build_dataset(Path(tmp2), n_files=100, file_size=2 * 1024 * 1024,
                      n_dup_groups=5, dup_copies=3)
        t_old, n_old = legacy_full_md5_scan(Path(tmp2))
        t_new, n_groups, groups = new_three_level_scan(Path(tmp2))
        speedup = t_old / max(t_new, 1e-6)
        print(f"  候选文件数 : {n_old}")
        print(f"  旧(全量 MD5): {t_old:.3f}s")
        print(f"  新(三级哈希): {t_new:.3f}s ({n_groups} 组确认重复)")
        print(f"  加速比      : {speedup:.2f}x")
        scenario2 = (n_old, t_old, t_new, speedup, n_groups)

    # 场景 3:50 个 5MB 纯假阳性(更大文件,L2 优势更明显)
    print("\n[场景 3] 50 个 5MB 同 size,内容完全不同(更大文件)")
    with tempfile.TemporaryDirectory(prefix="bench3_") as tmp3:
        build_pure_dataset(Path(tmp3), n_files=50, file_size=5 * 1024 * 1024)
        t_old, n_old = legacy_full_md5_scan(Path(tmp3))
        t_new, n_groups, _ = new_three_level_scan(Path(tmp3))
        speedup = t_old / max(t_new, 1e-6)
        print(f"  候选文件数 : {n_old}")
        print(f"  旧(全量 MD5): {t_old:.3f}s")
        print(f"  新(三级哈希): {t_new:.3f}s ({n_groups} 组确认重复)")
        print(f"  加速比      : {speedup:.2f}x")
        scenario3 = (n_old, t_old, t_new, speedup)

    print("\n" + "=" * 72)
    print("汇总:")
    print(f"  场景 1(100x2MB 纯假阳):旧 {scenario1[1]:.3f}s -> 新 {scenario1[2]:.3f}s = {scenario1[3]:.2f}x")
    print(f"  场景 2(100x2MB 混合)  :旧 {scenario2[1]:.3f}s -> 新 {scenario2[2]:.3f}s = {scenario2[3]:.2f}x (确认 {scenario2[4]} 组)")
    print(f"  场景 3(50x5MB 纯假阳) :旧 {scenario3[1]:.3f}s -> 新 {scenario3[2]:.3f}s = {scenario3[3]:.2f}x")
    print("=" * 72)


if __name__ == "__main__":
    main()
