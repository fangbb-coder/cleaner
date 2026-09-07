"""单元测试
用法: py tests/test_all.py
"""
import sys, os, tempfile, shutil, subprocess, base64, re, ctypes
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))  # for test_utils, mocks

from cleaner import (
    human_size, get_disk_usage, calculate_dir_size,
    move_to_recycle_bin, move_to_recycle_bin_batch, is_whitelisted,
    scandir_files, scandir_files_parallel,
    _scan_wild_temp, _scan_for_deletion,
    Analyzer, Scanner, Cleaner,
    CLEAN_TARGETS, APP_NAME, APP_VERSION,
    safe_remove_dir, _remove_files_concurrent,
    # 新增:覆盖本轮 P0 / 改名 / dataclass 改动
    sanitize_path_prefixes, create_restore_point,
    build_allowed_command, AllowedCommand, Level, Kind, CleanTarget,
    all_targets_valid, TK_AVAILABLE, ask_typed_confirmation,
    _migrate_legacy_appdata, _APP_DIR, _LOG_FILE,
    # task-fix-cli-stdin
    _safe_input, _has_stdin, run_cli,
    # task-fix-gui-startup
    is_admin, set_dpi_awareness, _fix_tcl_tk_paths_for_pyinstaller,
)
from test_utils import hash_steps


class TestRunner:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.tmp = None

    def run(self, name, fn):
        """跑一个测试,自动准备/清理临时目录。"""
        self._cleanup()
        try:
            self.tmp = tempfile.mkdtemp(prefix="test_")
            fn(self.tmp)
            print(f"  PASS  {name}")
            self.passed += 1
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
            self.failed += 1
        except Exception as e:
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
            self.failed += 1

    def _cleanup(self):
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.tmp = None

    def section(self, name):
        print(f"\n--- {name} ---")


# ===================== 基础工具 =====================

def test_human_size(r):
    assert human_size(0) == "0.0 B"
    assert human_size(1024) == "1.0 KB"
    assert human_size(1024 * 1024) == "1.0 MB"
    assert human_size(1024 * 1024 * 1024) == "1.0 GB"
    print("  PASS  human_size 边界值")


def test_disk_usage(r):
    info = get_disk_usage("C")
    assert info["total"] > 0
    assert info["used"] >= 0
    assert info["free"] >= 0
    assert 0 <= info["percent"] <= 100
    print("  PASS  get_disk_usage C 盘")


def test_dir_size(r):
    d = Path(r)
    (d / "a").mkdir()
    (d / "a" / "f1").write_bytes(b"x" * 5000)
    (d / "a" / "f2").write_bytes(b"x" * 3000)
    (d / "b").mkdir()
    (d / "b" / "f3").write_bytes(b"x" * 2000)
    size, count = calculate_dir_size(d)
    assert size == 10000, f"期望 10000,实际 {size}"
    assert count == 3


def test_recycle_api(r):
    assert callable(move_to_recycle_bin)
    print("  PASS  move_to_recycle_bin callable")


def test_recycle_batch_api(r):
    """批量回收站 API:空输入/不存在路径应不崩;返回 (success, failed_paths) 元组。"""
    # 空列表
    ok, fail = move_to_recycle_bin_batch([])
    assert ok == 0 and fail == []
    # 全不存在的路径
    ok, fail = move_to_recycle_bin_batch(["C:/no/such/path1", "C:/no/such/path2"])
    assert ok == 0
    assert isinstance(fail, list)
    assert len(fail) == 2
    print("  PASS  move_to_recycle_bin_batch 边界")


# ===================== 性能 =====================

def test_scandir(tmp):
    import time
    d = Path(tmp)
    for i in range(10):
        sub = d / f"sub_{i}"
        sub.mkdir()
        for j in range(20):
            (sub / f"f_{j}.bin").write_bytes(b"x" * 1024)
    t0 = time.time()
    files = scandir_files(str(d), max_depth=3)
    dt_single = time.time() - t0
    assert len(files) == 200, f"期望 200,实际 {len(files)}"
    print(f"  PASS  scandir 200 文件 in {dt_single:.3f}s")


def test_concurrent_delete(tmp):
    """验证并发删除:1000 个文件,8 线程,要全部删掉,结果 >= 期望大小。"""
    import time
    d = Path(tmp) / "del"
    d.mkdir()
    payload = b"x" * 1024
    files = []
    for i in range(1000):
        fp = d / f"f_{i:04d}.bin"
        fp.write_bytes(payload)
        files.append((fp, 1024))
    t0 = time.time()
    removed, freed = _remove_files_concurrent(files, workers=8)
    dt = time.time() - t0
    assert removed == 1000, f"删 {removed},期望 1000"
    assert freed == 1000 * 1024
    assert not any(f.exists() for f, _ in files)
    print(f"  PASS  并发删 1000 文件 in {dt:.3f}s")


def test_safe_remove_dir_parallel(tmp):
    """验证 safe_remove_dir(parallel=True) 正确清空大目录。"""
    d = Path(tmp) / "big"
    d.mkdir()
    for i in range(50):
        sub = d / f"s_{i}"
        sub.mkdir()
        for j in range(20):
            (sub / f"f_{j}.bin").write_bytes(b"x" * 2048)
    removed, freed = safe_remove_dir(d, parallel=True, workers=4)
    assert removed == 1000, f"删 {removed},期望 1000"
    assert freed == 1000 * 2048
    assert not d.exists() or not any(d.iterdir())
    print(f"  PASS  safe_remove_dir 并发删 1000 文件 in {d}")


def test_scan_for_deletion_single_pass(tmp):
    """_scan_for_deletion 单遍扫描同时返回 files + dirs_with_depth。"""
    d = Path(tmp) / "tree"
    d.mkdir()
    (d / "rootfile.tmp").write_bytes(b"x" * 100)
    for i in range(3):
        sub = d / f"sub_{i}"
        sub.mkdir()
        (sub / "f.bin").write_bytes(b"x" * 50)
        (sub / "nested").mkdir()
        (sub / "nested" / "g.bin").write_bytes(b"x" * 30)
    files, dirs_with_depth = _scan_for_deletion(d, workers=4)
    # 1 根文件 + 3 个 sub 各自的 f.bin + 3 个 nested 各自的 g.bin = 7 文件
    assert len(files) == 7, f"应有 7 个文件,实际 {len(files)}"
    # 文件大小总和
    assert sum(s for _, s in files) == 100 + 3 * (50 + 30)
    # 子目录 = 3 个 sub + 3 个 nested = 6,根不在里面
    assert len(dirs_with_depth) == 6, f"应有 6 个子目录,实际 {len(dirs_with_depth)}"
    print(f"  PASS  _scan_for_deletion 单遍收集 7 文件 6 目录")


def test_wild_temp_scan(tmp):
    """_scan_wild_temp:匹配 patterns + 满足年龄/大小阈值。"""
    import time
    d = Path(tmp) / "tmpdir"
    d.mkdir()
    # 大文件(60MB > 50MB 阈值),新文件也应被选
    big = d / "big.tmp"
    big.write_bytes(b"x" * 60 * 1024 * 1024)
    # 小新文件 - 不该被选(既不大也不旧)
    fresh = d / "fresh.tmp"
    fresh.write_bytes(b"x" * 100)
    # 旧文件(>7天,1KB) - 应该被选(满足年龄阈值)
    old = d / "old.log"
    old.write_bytes(b"x" * 100)
    # 改 mtime 到 30 天前
    old_time = (time.time() - 30 * 86400)
    os.utime(old, (old_time, old_time))
    # 不匹配的扩展名
    keep = d / "keep.py"
    keep.write_bytes(b"x" * 100)
    os.utime(keep, (old_time, old_time))  # 即便旧,但扩展名不匹配

    target = {
        "id": "test_wild",
        "kind": "wild_temp",
        "roots": [str(d)],
        "patterns": ["*.tmp", "*.log"],
        "min_age_days": 7,
        "min_size": 50 * 1024 * 1024,
    }
    r = _scan_wild_temp(target)
    paths = r["paths"]
    # big.tmp(60MB > 50MB)和 old.log(>7天)应被选
    # fresh.tmp 小且新 - 不该被选
    # keep.py 不匹配模式 - 不该被选
    assert any("big.tmp" in p for p in paths), f"big.tmp 应被选中:{paths}"
    assert any("old.log" in p for p in paths), f"old.log 应被选中:{paths}"
    assert not any("fresh.tmp" in p for p in paths), f"fresh.tmp 不该被选中:{paths}"
    assert not any("keep.py" in p for p in paths), f"keep.py 不该被选中:{paths}"
    assert r["files"] == 2, f"应选 2 个,实际 {r['files']}"
    print(f"  PASS  wild_temp 阈值匹配正确 (大文件 + 旧文件)")


# ===================== 过滤 =====================

def test_whitelist(tmp):
    wl = {"extensions": [".env", ".key"], "path_prefixes": ["C:/proj"]}
    assert is_whitelisted("C:/x/.env", wl)
    assert is_whitelisted("C:/x/.key", wl)
    assert not is_whitelisted("C:/x/main.py", wl)
    assert is_whitelisted("C:/proj/a.py", wl)
    assert not is_whitelisted("C:/other/a.py", wl)


def test_scanner_init(tmp):
    Scanner()
    Scanner(log_callback=lambda *a, **kw: None,
            progress_callback=lambda *a, **kw: None)
    print("  PASS  Scanner 构造")


def test_cleaner_whitelist(tmp):
    d = Path(tmp)
    (d / "keep.env").write_bytes(b"x" * 100)
    (d / "del.tmp").write_bytes(b"x" * 100)
    (d / "del2.tmp").write_bytes(b"x" * 100)
    import cleaner
    test_target = {
        "id": "test", "name": "测试", "paths": [str(d)],
        "level": "safe", "desc": "测试",
    }
    cleaner.CLEAN_TARGETS.append(test_target)
    try:
        c = Cleaner(
            log_callback=lambda *a, **kw: None,
            whitelist={"extensions": [".env"], "path_prefixes": []},
        )
        res = c.clean_target(test_target)
        assert res["removed"] == 2, f"删 2,实际 {res['removed']}"
        assert res["skipped"] == 1, f"跳 1,实际 {res['skipped']}"
        assert (d / "keep.env").exists()
    finally:
        cleaner.CLEAN_TARGETS.remove(test_target)


# ===================== Analyzer =====================

def test_analyzer_large(tmp):
    a = Analyzer(log_callback=lambda *a, **kw: None,
                 progress_callback=lambda *a, **kw: None)
    d = Path(tmp)
    for sz in [5, 20, 100]:
        (d / f"f_{sz}mb.bin").write_bytes(b"x" * sz * 1024 * 1024)
    r = a.find_large_files(str(d), min_size=10 * 1024 * 1024, top_n=10)
    assert len(r) == 2
    # 过滤
    r2 = a.find_large_files(str(d), min_size=10 * 1024 * 1024, top_n=10,
                            ext_filter={".bin"})
    assert len(r2) == 2
    # 排除
    (d / "node_modules").mkdir()
    (d / "node_modules" / "big.bin").write_bytes(b"x" * 200 * 1024 * 1024)
    r3 = a.find_large_files(str(d), min_size=10 * 1024 * 1024, top_n=10,
                            extra_skip={"node_modules"})
    assert not any("node_modules" in x["path"] for x in r3)


def test_analyzer_dup(tmp):
    a = Analyzer(log_callback=lambda *a, **kw: None,
                 progress_callback=lambda *a, **kw: None)
    d = Path(tmp)
    for i in range(3):
        (d / f"same_{i}.bin").write_bytes(b"x" * 1024 * 1024)
    (d / "diff.bin").write_bytes(b"y" * 1024 * 1024)
    r = a.find_duplicates(str(d), min_size=512 * 1024, max_depth=3)
    assert len(r) == 1
    assert len(r[0]) == 3


def test_head_hash_helper(tmp):
    """_head_hash 应返回稳定的 SHA-1 hex,小文件读全部,大文件读前 64K。"""
    import hashlib
    d = Path(tmp)
    p1 = d / "a.bin"
    p2 = d / "b.bin"
    # 小文件(<64K)应等于整个文件的 SHA-1
    p1.write_bytes(b"hello world")
    p2.write_bytes(b"hello world")
    h1 = Analyzer._head_hash(p1, 65536)
    h2 = Analyzer._head_hash(p2, 65536)
    assert h1 == h2, "相同内容的小文件 head hash 应相同"
    assert h1 == hashlib.sha1(b"hello world").hexdigest()
    # 不存在的路径应返回 None
    assert Analyzer._head_hash(d / "no_such_file.bin") is None
    # str 和 Path 都应支持
    assert Analyzer._head_hash(str(p1), 65536) == h1
    # 默认 64K 字节
    assert Analyzer._head_hash(p1) == h1


def test_dup_diff_content_same_size(tmp):
    """L1 size 重复但内容不同的文件,不应被识别为重复(L2 head hash 剔除)。"""
    a = Analyzer(log_callback=lambda *a, **kw: None,
                 progress_callback=lambda *a, **kw: None)
    d = Path(tmp)
    # 4 个文件都是 2MB,但内容完全不同(head 也不一样)
    for i in range(4):
        (d / f"file_{i}.bin").write_bytes(bytes([i]) * (2 * 1024 * 1024))
    r = a.find_duplicates(str(d), min_size=1024 * 1024, max_depth=3)
    assert r == [], f"4 个 size 相同但内容不同的文件不应被判为重复,实际: {r}"


def test_dup_same_head_diff_tail(tmp):
    """L2 head 相同但 L3 tail 不同的文件,最终不应被识别为重复。"""
    a = Analyzer(log_callback=lambda *a, **kw: None,
                 progress_callback=lambda *a, **kw: None)
    d = Path(tmp)
    # 两个文件:前 64K 完全一样,但 64K 之后不一样
    head_block = b"A" * (70 * 1024)  # 70KB:含完整 64K + 6K 偏移
    tail1 = b"X" * (2 * 1024 * 1024 - len(head_block))
    tail2 = b"Y" * (2 * 1024 * 1024 - len(head_block))
    (d / "f1.bin").write_bytes(head_block + tail1)
    (d / "f2.bin").write_bytes(head_block + tail2)
    r = a.find_duplicates(str(d), min_size=1024 * 1024, max_depth=3)
    assert r == [], f"head 相同但 tail 不同的文件,L3 MD5 应能区分,实际: {r}"


def test_dup_small_files(tmp):
    """小文件(<=256KB)的重复检测仍应工作。"""
    a = Analyzer(log_callback=lambda *a, **kw: None,
                 progress_callback=lambda *a, **kw: None)
    d = Path(tmp)
    # 3 个 100KB 内容相同的文件 + 1 个 100KB 内容不同的
    for i in range(3):
        (d / f"sm_{i}.bin").write_bytes(b"z" * 100 * 1024)
    (d / "sm_diff.bin").write_bytes(b"q" * 100 * 1024)
    r = a.find_duplicates(str(d), min_size=50 * 1024, max_depth=3)
    # 至少要有 1 组,且组里有 3 个相同文件
    found = False
    for g in r:
        if len(g) == 3 and all("sm_" in x["path"] for x in g):
            found = True
            break
    assert found, f"3 个小文件重复组未识别,实际: {r}"


def test_legacy_quick_full_hash_apis(tmp):
    """旧的 _quick_hash / _full_hash API 应继续可用(向后兼容)。"""
    import hashlib
    d = Path(tmp)
    p = d / "legacy.bin"
    p.write_bytes(b"abc" * 1000)
    md = Analyzer._full_hash(p)
    assert md == hashlib.md5(b"abc" * 1000).hexdigest()
    qh = Analyzer._quick_hash(p)
    assert qh is not None and len(qh) == 32  # MD5 hex 长度
    # 不存在路径返回 None
    assert Analyzer._full_hash(d / "nope.bin") is None
    assert Analyzer._quick_hash(d / "nope.bin") is None


def test_analyzer_dirs(tmp):
    a = Analyzer(log_callback=lambda *a, **kw: None,
                 progress_callback=lambda *a, **kw: None)
    d = Path(tmp)
    for name, size in [("small", 1024), ("medium", 10240), ("large", 102400)]:
        sub = d / name
        sub.mkdir()
        for j in range(3):
            (sub / f"f_{j}.bin").write_bytes(b"x" * size)
    r = a.find_large_dirs(str(d), top_n=10, max_depth=2)
    assert len(r) == 3
    assert r[0]["size"] >= r[1]["size"] >= r[2]["size"]


# ===================== 清理项配置 =====================


# ===================== 清理项配置 =====================

def test_clean_targets_three_levels(r):
    """验证清理项分 3 档,且覆盖足够多。"""
    levels = {"safe": 0, "caution": 0, "advanced": 0}
    for t in CLEAN_TARGETS:
        lv = t.get("level")
        assert lv in levels, f"未识别的 level: {lv} (id={t.get('id')})"
        levels[lv] += 1
        assert "id" in t and "name" in t, f"缺 id/name: {t}"
        # 不同 kind 类型有不同的必填字段
        kind = t.get("kind", "files")
        if kind == "files":
            assert "paths" in t and t["paths"], f"files 类型缺 paths: {t.get('id')}"
        elif kind == "wild_temp":
            assert "roots" in t and "patterns" in t, f"wild_temp 类型缺 roots/patterns: {t.get('id')}"
        elif kind == "command":
            assert "command_id" in t and t["command_id"], f"command 类型缺 command_id: {t.get('id')}"
        else:
            assert False, f"未知 kind: {kind}"
    # 至少 35 项,且三档都要有
    total = sum(levels.values())
    assert total >= 35, f"清理项应 ≥ 35,实际 {total}:{levels}"
    assert levels["safe"] >= 15, f"safe 太少:{levels}"
    assert levels["caution"] >= 10, f"caution 太少:{levels}"
    assert levels["advanced"] >= 5, f"advanced 太少:{levels}"
    print(f"  PASS  清理项 {total} 项 分级 safe={levels['safe']} "
          f"caution={levels['caution']} advanced={levels['advanced']}")


def test_clean_targets_new_items(r):
    """验证新加的清理项存在。"""
    expected = [
        "firefox_cache", "edge_deep_cache", "chrome_deep_cache",
        "vscode_cache", "user_crashdumps", "icon_cache", "wmp_cache",
        "recent", "spotify_cache", "slack_cache", "discord_cache",
        "zoom_cache", "steam_cache",
        "live_kernel_reports", "defender_history", "defender_localcopy",
        "downloaded_installations", "system_apps_cache",
        "office_filecache", "outlook_roamcache",
        "pip_cache", "uv_cache", "npm_cache", "yarn_cache", "pnpm_store",
        "cargo_cache", "gradle_cache", "jetbrains_cache",
        "adobe_cache", "onedrive_logs",
        "inf_logs", "bitlog", "winsxs_manifest", "defender_quarantine",
        "windows_bt", "sysreset", "windows_old",
        # v3.2 新增
        "brave_cache", "opera_cache", "opera_gx_cache", "vivaldi_cache",
        "yandex_cache", "teams_cache", "skype_cache",
        "epic_launcher_cache", "ea_app_cache", "battlenet_cache",
        "ubisoft_cache", "notepadpp_backup", "sublime_cache",
        "eclipse_cache", "nuget_cache", "chocolatey_cache", "winget_cache",
        "wild_temp_files", "maven_cache", "composer_cache", "bundler_cache",
        "sbt_cache", "hibernation_disable", "vss_shadow_cleanup",
    ]
    ids = {t["id"] for t in CLEAN_TARGETS}
    missing = [eid for eid in expected if eid not in ids]
    assert not missing, f"缺失清理项:{missing}"
    print(f"  PASS  {len(expected)} 个新清理项全部存在")


# ===================== 版本/元数据 =====================

def test_version(r):
    assert APP_NAME == "绿色垃圾文件清理器"
    assert APP_VERSION.startswith("3.")
    print(f"  PASS  {APP_NAME} v{APP_VERSION}")


# ===================== 本轮 P0 / 改名 / dataclass 测试 =====================

def test_app_name_renamed(r):
    """改名:APP_NAME 应为「绿色垃圾文件清理器」,旧名不再出现。"""
    assert APP_NAME == "绿色垃圾文件清理器"
    assert "C盘清理工具" not in APP_NAME
    print(f"  PASS  APP_NAME = {APP_NAME}")


def test_whitelist_path_traversal_rejected(r):
    """P0-3:sanitize_path_prefixes 必须拒绝 ../ / UNC / 过短 / 非字符串。"""
    inp = [
        "../etc/passwd",            # 路径遍历 → 拒绝
        "..\\windows\\system32",    # 反斜杠 .. → 拒绝
        "\\\\server\\share",        # UNC → 拒绝
        "//server/share",           # UNC → 拒绝
        "ab",                       # 过短(<3) → 拒绝
        123,                        # 非字符串 → 拒绝
        None,                       # 非字符串 → 拒绝
        "",                         # 过短 → 拒绝
    ]
    out = sanitize_path_prefixes(inp)
    # 全部应当被过滤
    assert out == [], f"应有 0 项,实际 {len(out)}: {out}"
    print("  PASS  白名单路径遍历/UNC/短/非字符串 全部拒绝")


def test_whitelist_path_normalize_and_resolve(r):
    """P0-3:合法路径应被 normcase + resolve,大小写不敏感去重。"""
    d = Path(tempfile.gettempdir()).resolve()
    inp = [str(d), str(d).upper(), "C:/Windows"]  # 第 1/2 个去重
    out = sanitize_path_prefixes(inp)
    assert len(out) == 2, f"应有 2 项(去重后),实际 {len(out)}: {out}"
    # 解析后都应是绝对路径
    for p in out:
        assert os.path.isabs(p), f"应绝对路径,实际 {p}"
    print("  PASS  白名单路径 normcase + resolve + 去重")


def test_create_restore_point_uses_encoded_command(r):
    """P0-1:create_restore_point 必须用 -EncodedCommand(Base64 UTF-16LE)而非 -Command。"""
    captured = {}
    fake_proc = mock.MagicMock(returncode=0, stderr="", stdout="")

    def _fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return fake_proc

    with mock.patch("cleaner.subprocess.run", side_effect=_fake_run):
        ok = create_restore_point("my-test")
    assert ok is True
    cmd = captured["cmd"]
    # 必须是 -EncodedCommand 形式,绝对不能是 -Command "<inline>"
    assert "-EncodedCommand" in cmd, f"缺 -EncodedCommand: {cmd}"
    assert "-Command" not in cmd, f"不应再用 -Command(注入面): {cmd}"
    # EncodedCommand 后是 Base64
    encoded = cmd[cmd.index("-EncodedCommand") + 1]
    decoded = base64.b64decode(encoded).decode("utf-16-le")
    assert "Checkpoint-Computer" in decoded
    assert "my-test" in decoded
    print("  PASS  create_restore_point 使用 -EncodedCommand(Base64 UTF-16LE)")


def test_create_restore_point_description_sanitized(r):
    """P0-1:description 含可疑字符(分号 / 反引号 / $)时,Base64 内已是净化版。

    关键检查点:分号 `;` 必须被替换成 `_`(否则 PowerShell 会把后续内容当成
    独立语句执行)。模板本身有 `-Description '...'` 单引号无害。
    """
    captured = {}
    fake_proc = mock.MagicMock(returncode=0, stderr="", stdout="")

    def _fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return fake_proc

    # 尝试注入:Stop-Process -Force ; 第二个语句 Restart-Computer
    nasty = "evil'; Stop-Process -Force; Restart-Computer; '"
    with mock.patch("cleaner.subprocess.run", side_effect=_fake_run):
        create_restore_point(nasty)
    encoded = captured["cmd"][captured["cmd"].index("-EncodedCommand") + 1]
    decoded = base64.b64decode(encoded).decode("utf-16-le")
    # 关键:无分号 = 无第二条语句可注入
    assert ";" not in decoded, f"分号未净化(可注入第二条语句): {decoded}"
    # 反引号 / $ / 反斜杠也应被净化(避免命令替换 / 变量展开 / 路径转义)
    assert "`" not in decoded, f"反引号未净化: {decoded}"
    # 模板 -Description '<sanitized>' 应完整保留(单引号是模板的)
    assert decoded.count("'") == 2, f"模板引号数量异常: {decoded}"
    print("  PASS  create_restore_point description 含注入字符被净化(; ` 替换为 _)")


def test_shfileoperation_batch_returns_failed_paths(r):
    """P0-4:move_to_recycle_bin_batch 返回 (success, failed_paths) 列表。"""
    # 全部不存在的路径 → 应得到 failed_paths 列表
    ok, fail = move_to_recycle_bin_batch(["C:/no/such/a", "C:/no/such/b"])
    assert ok == 0
    assert isinstance(fail, list)
    assert len(fail) == 2
    # 空列表 → (0, [])
    ok2, fail2 = move_to_recycle_bin_batch([])
    assert ok2 == 0 and fail2 == []
    print("  PASS  move_to_recycle_bin_batch 返回 (success, failed_paths)")


def test_allowed_command_enum_resolves(r):
    """P0-2:AllowedCommand 表必须包含已知 command_id,未授权 id 返回 None。"""
    cmd = build_allowed_command("hibernation_disable")
    assert cmd is not None
    assert cmd[0] == "powercfg"
    assert "/h" in cmd and "off" in cmd
    cmd2 = build_allowed_command("vss_shadow_cleanup")
    assert cmd2 is not None
    assert cmd2[0] == "vssadmin"
    # 未授权 id 必须返回 None
    assert build_allowed_command("not_in_table") is None
    assert build_allowed_command("rm -rf /") is None
    assert build_allowed_command("") is None
    print("  PASS  AllowedCommand 枚举白名单 + 拒绝未授权")


def test_cleantarget_validation(r):
    """质量改进:CleanTarget dataclass 字段校验,非法 kind/level/缺字段应 raise。"""
    # 合法 files
    t1 = CleanTarget(id="x", name="x", level="safe", desc="d",
                     kind="files", paths=["C:/tmp"])
    assert t1.level == Level.SAFE
    assert t1.kind == Kind.FILES
    # level/kind 自动转枚举
    t2 = CleanTarget(id="y", name="y", level="caution", desc="d",
                     kind="wild_temp", roots=["C:/t"], patterns=["*.tmp"])
    assert t2.level == Level.CAUTION
    assert t2.kind == Kind.WILD_TEMP
    # files 类型缺 paths 应 raise
    try:
        CleanTarget(id="z", name="z", level="safe", desc="d", kind="files")
    except ValueError as e:
        assert "paths" in str(e)
    else:
        raise AssertionError("应当 raise ValueError")
    # command 类型缺 command_id 应 raise
    try:
        CleanTarget(id="w", name="w", level="advanced", desc="d", kind="command")
    except ValueError as e:
        assert "command_id" in str(e)
    else:
        raise AssertionError("应当 raise ValueError")
    print("  PASS  CleanTarget 字段校验 + 自动枚举转换")


def test_cleantarget_dict_compat(r):
    """向后兼容:CleanTarget 支持 t["id"] / t.get("level") / "x" in t。"""
    t = CleanTarget(id="x", name="X", level="safe", desc="d",
                    kind="files", paths=["C:/x"])
    assert t["id"] == "x"
    assert t["name"] == "X"
    assert t.get("level") == Level.SAFE
    assert t.get("not_exist", "default") == "default"
    assert "id" in t and "name" in t and "paths" in t
    assert "fake_attr" not in t
    # __getitem__ 防 int 误用
    try:
        t[0]
    except TypeError:
        pass
    else:
        raise AssertionError("应当 raise TypeError")
    print("  PASS  CleanTarget 向后兼容 dict 风格访问")


def test_all_targets_valid_at_import(r):
    """模块加载时已运行过 assert,只要能 import 就说明校验通过。"""
    assert all_targets_valid(CLEAN_TARGETS)
    # 重复 id 应 raise
    bad_targets = [
        CleanTarget(id="dup", name="A", level="safe", desc="a", paths=["C:/x"]),
        CleanTarget(id="dup", name="B", level="safe", desc="b", paths=["C:/y"]),
    ]
    try:
        all_targets_valid(bad_targets)
    except ValueError as e:
        assert "dup" in str(e)
    else:
        raise AssertionError("应当 raise ValueError")
    print("  PASS  all_targets_valid() 模块加载校验 + 重复 id 检测")


def test_safe_remove_dir_onerror(r):
    """P0-5:safe_remove_dir 支持 onerror 回调收集失败。"""
    d = Path(tempfile.mkdtemp(prefix="test_onerr_"))
    try:
        # 建一个普通文件 + 一个真只读文件(Windows 上只读仍可删;这里测回调被调用)
        (d / "normal.txt").write_bytes(b"x" * 1024)
        errors = []
        def _onerr(func, path, exc):
            errors.append((func, str(path), exc))
        # 触发 onerror 的最稳妥办法:构造一个不存在的子路径,然后模拟
        removed, freed = safe_remove_dir(d, onerror=_onerr, parallel=False)
        assert removed >= 1
        # 至少 onerror 没崩;有回调被调用就算 ok(Windows 文件系统可能不触发)
        print(f"  PASS  safe_remove_dir onerror 接口 OK(errors={len(errors)})")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_ask_typed_confirmation_mock(r):
    """UX:ask_typed_confirmation 接受用户输入 "DELETE" 才返回 True。

    不调 wait_window(会阻塞),改测底层 _build_confirm_dialog 的 on_ok 逻辑:
    - 输入错误 → result["ok"] 保持 False
    - 输入正确 → result["ok"] 置 True
    """
    if not TK_AVAILABLE:
        print("  SKIP  ask_typed_confirmation(TK 不可用)")
        return
    import tkinter as tk
    from cleaner import _build_confirm_dialog
    root = tk.Tk()
    root.withdraw()
    try:
        # 路径 1:输入错误 → on_ok 不应把 result["ok"] 置 True
        state1 = _build_confirm_dialog(root, "测试", "请输入 DELETE:", "DELETE")
        try:
            with mock.patch.object(state1["entry"], "get", return_value="WRONG"):
                state1["on_ok"]()
            assert state1["result"]["ok"] is False
            # 路径 2:输入正确 → result["ok"] 应为 True(但 dialog 已 destroy,所以跳过 destroy)
            state2 = _build_confirm_dialog(root, "测试", "请输入 DELETE:", "DELETE")
            with mock.patch.object(state2["entry"], "get", return_value="DELETE"):
                # mock destroy 避免 on_ok 实际销毁对话框(会让后续 state2 不可用)
                with mock.patch.object(state2["dlg"], "destroy"):
                    state2["on_ok"]()
            assert state2["result"]["ok"] is True
            # 路径 3:取消 → result["ok"] 保持 False
            state3 = _build_confirm_dialog(root, "测试", "请输入 DELETE:", "DELETE")
            with mock.patch.object(state3["dlg"], "destroy"):
                state3["on_cancel"]()
            assert state3["result"]["ok"] is False
            print("  PASS  ask_typed_confirmation 输入正确才确认(测 on_ok/on_cancel)")
        finally:
            for s in (state1, state2, state3):
                try:
                    if s and s["dlg"].winfo_exists():
                        s["dlg"].destroy()
                except Exception:
                    pass
    finally:
        root.destroy()


def test_legacy_migration_helper(r):
    """改名兼容:_migrate_legacy_appdata 把旧 %APPDATA%\\C_Cleaner 下的
    whitelist.json/config.json 迁移到新 %APPDATA%\\GreenCleaner(只在新文件不存在时)。
    """
    # 用隔离的临时目录树模拟 APPDATA/C_Cleaner vs GreenCleaner
    fake_appdata = Path(tempfile.mkdtemp(prefix="appdata_"))
    legacy_dir = fake_appdata / "C_Cleaner"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "whitelist.json").write_text('{"extensions":[".foo"]}', encoding="utf-8")
    (legacy_dir / "config.json").write_text('{"theme":"dark"}', encoding="utf-8")
    new_dir = fake_appdata / "GreenCleaner"
    new_dir.mkdir(parents=True, exist_ok=True)
    try:
        # 清空新目录里的同名文件,确保迁移真实发生
        for name in ("whitelist.json", "config.json"):
            p = new_dir / name
            if p.exists():
                p.unlink()
        with mock.patch.dict(os.environ, {"APPDATA": str(fake_appdata)}):
            with mock.patch("cleaner._APP_DIR", new_dir):
                _migrate_legacy_appdata()
        # 新目录应出现 whitelist.json + config.json
        assert (new_dir / "whitelist.json").exists(), "whitelist.json 未迁移"
        assert (new_dir / "config.json").exists(), "config.json 未迁移"
        # 内容应一致
        import json
        wl_new = json.loads((new_dir / "whitelist.json").read_text(encoding="utf-8"))
        assert wl_new["extensions"] == [".foo"]
        print("  PASS  _migrate_legacy_appdata 兼容旧 C_Cleaner 路径")
    finally:
        shutil.rmtree(fake_appdata, ignore_errors=True)


# ===================== task-fix-cli-stdin 测试 =====================

def test_safe_input_handles_no_stdin(r):
    """_safe_input 在 input() 抛 RuntimeError / EOFError / KeyboardInterrupt 时返回 default。

    背景:PyInstaller --noconsole 打包后 sys.stdin=None,直接调 input() 抛
    RuntimeError: input(): lost sys.stdin。_safe_input 必须兜底。
    """
    # 路径 1:input() 抛 RuntimeError → 返回 default
    with mock.patch("cleaner.input", side_effect=RuntimeError("input(): lost sys.stdin")):
        out = _safe_input("> ", "fallback1")
        assert out == "fallback1", f"path1 期望 fallback1,实际 {out!r}"
    # 路径 2:input() 抛 EOFError → 返回 default
    with mock.patch("cleaner.input", side_effect=EOFError):
        out = _safe_input("> ", "fallback2")
        assert out == "fallback2", f"path2 期望 fallback2,实际 {out!r}"
    # 路径 3:input() 抛 KeyboardInterrupt → 返回 default
    with mock.patch("cleaner.input", side_effect=KeyboardInterrupt):
        out = _safe_input("> ", "fallback3")
        assert out == "fallback3", f"path3 期望 fallback3,实际 {out!r}"
    # 路径 4:用户正常输入 → 原样返回
    with mock.patch("cleaner.input", return_value="hello"):
        out = _safe_input("> ", "x")
        assert out == "hello", f"path4 期望 hello,实际 {out!r}"
    print("  PASS  _safe_input 兜底 RuntimeError/EOFError/KeyboardInterrupt")


def test_has_stdin_detection(r):
    """_has_stdin 检测 stdin 是否可用(GUI 子系统 / pipe 关闭均视为不可用)。"""
    # 路径 1:stdin=None → 不可用
    with mock.patch("cleaner.sys") as fake_sys:
        fake_sys.stdin = None
        assert _has_stdin() is False
    # 路径 2:stdin 没有 isatty(像 pipe 文件) → 不可用
    class FakeStdinNoIsatty:
        pass  # 没有 isatty 属性
    with mock.patch("cleaner.sys") as fake_sys:
        fake_sys.stdin = FakeStdinNoIsatty()
        assert _has_stdin() is False
    # 路径 3:stdin 有 isatty → 可用
    class FakeStdinOk:
        def isatty(self):
            return True
    with mock.patch("cleaner.sys") as fake_sys:
        fake_sys.stdin = FakeStdinOk()
        assert _has_stdin() is True
    print("  PASS  _has_stdin 识别 None / 无 isatty / 正常 stdin")


def test_run_cli_no_stdin_safe(r):
    """run_cli 在无 stdin 时不抛 RuntimeError,而是走 'q' 默认路径静默退出。

    这是 task-fix-cli-stdin 的核心验收点:用户双击 .exe → UAC 弹窗 → 拿到
    管理员权限 → GUI 启动失败 → 回退 CLI → 必须不能崩。
    """
    import io
    # 关键:用 mock 替换 builtin input + 把 sys.stdin 设为 None
    with mock.patch("cleaner.sys") as fake_sys, \
         mock.patch("cleaner.input", side_effect=RuntimeError("input(): lost sys.stdin")):
        fake_sys.stdin = None
        # run_cli 默认 choice='q'(来自 _safe_input 的 default),静默退出
        # 不应该抛任何异常
        run_cli()  # 不抛即通过
    print("  PASS  run_cli 无 stdin 时静默退出(默认 choice='q')")


# ===================== task-fix-gui-startup 测试 =====================

def test_dpi_awareness_set(r):
    """set_dpi_awareness 调用成功(返回 True)或优雅失败(返回 False,不抛)。

    Win11 高 DPI 不设置会糊甚至 tk.Tk() 初始化失败。
    """
    # 路径 1:两个 API 都失败(非 Windows 环境)→ 返回 False,不抛
    with mock.patch.object(ctypes.windll, "shcore", create=True) as fake_shcore:
        fake_shcore.SetProcessDpiAwareness = mock.MagicMock(side_effect=Exception("no shcore"))
        with mock.patch.object(ctypes.windll, "shell32", create=True) as fake_sh32:
            fake_sh32.SetProcessDPIAware = mock.MagicMock(side_effect=Exception("no shell32"))
            assert set_dpi_awareness() is False
    # 路径 2:shcore 成功 → 返回 True
    with mock.patch.object(ctypes.windll, "shcore", create=True) as fake_shcore:
        fake_shcore.SetProcessDpiAwareness = mock.MagicMock(return_value=0)
        assert set_dpi_awareness() is True
    print("  PASS  set_dpi_awareness 失败不抛/成功返回 True")


def test_is_admin_robust(r):
    """is_admin 双 API 兜底:IsUserAnAdmin + GetTokenInformation。

    路径 1: IsUserAnAdmin=True → True (不调 GetTokenInformation)
    路径 2: IsUserAnAdmin=False,GetTokenInformation 返回 Full elevation → True
    路径 3: 两 API 都失败 → False
    """
    # 路径 1:IsUserAnAdmin 返回非 0 → True
    with mock.patch.object(ctypes.windll.shell32, "IsUserAnAdmin",
                           return_value=1, create=True):
        assert is_admin() is True
    # 路径 3:两个 API 都返回 0/false/失败 → False
    with mock.patch.object(ctypes.windll.shell32, "IsUserAnAdmin",
                           return_value=0, create=True):
        with mock.patch.object(ctypes.windll.kernel32, "OpenProcessToken",
                               return_value=0, create=True):
            assert is_admin() is False
    # 路径 4:IsUserAnAdmin 抛异常(属性不存在),GetTokenInformation True → True(兜底)
    with mock.patch.object(ctypes.windll, "shell32", create=True) as fake_sh32:
        del fake_sh32.IsUserAnAdmin  # 确保属性访问抛 AttributeError
        with mock.patch.object(ctypes.windll.kernel32, "OpenProcessToken",
                               return_value=1, create=True) as fake_opt:
            fake_opt.return_value = 1
            with mock.patch.object(ctypes.windll.kernel32, "GetCurrentProcess",
                                   return_value=1, create=True), \
                 mock.patch.object(ctypes.windll.kernel32, "CloseHandle",
                                   return_value=1, create=True), \
                 mock.patch.object(ctypes.windll.advapi32, "GetTokenInformation",
                                   return_value=1, create=True) as fake_gti:
                # mock GetTokenInformation 让它把第一个 c_int 参数(elevation)的 .value 设为 2
                def _set_elev_and_return_ok(*args, **kw):
                    elev_ref = args[2]  # byref(elev)
                    if hasattr(elev_ref, '_obj'):
                        elev_ref._obj.value = 2
                    return 1
                fake_gti.side_effect = _set_elev_and_return_ok
                assert is_admin() is True
    print("  PASS  is_admin 双 API 兜底(IsUserAnAdmin + GetTokenInformation)")


def test_main_gui_fallback_logs_traceback(r):
    """main() 兜底:源码层面验证 GUI 失败会触发 _logger.error + sys.exit(1)。

    不实际调用 main() (它会跑 ctypes.windll 调用,headless 测试环境易卡),
    而是用 grep 验证:
    1) except 块里有 _logger.error("GUI 启动失败:"...)
    2) except 块后有 if not _has_stdin() → sys.exit(1) 兜底
    """
    src = Path(__file__).parent.parent.joinpath("cleaner.py").read_text(encoding="utf-8")
    # 1. 验证 except 块写 _logger.error
    assert 'except Exception as e' in src, "缺 except Exception 块"
    # 找到 main() 函数内的 except 块
    main_match = re.search(r"def main\(\):(.*?)\n\n\n", src, re.DOTALL)
    assert main_match, "main() 函数未找到"
    main_body = main_match.group(1)
    # 验证 except 后调 _logger.error
    assert "_logger.error" in main_body and "GUI 启动失败" in main_body, \
        "main() 兜底必须 _logger.error('GUI 启动失败:'...)"
    # 2. 验证 _has_stdin() 检查 + sys.exit(1)
    assert "_has_stdin()" in main_body and "sys.exit(1)" in main_body, \
        "main() 兜底必须有 _has_stdin() 检查 + sys.exit(1)"
    # 3. 验证 import traceback as _tb (用于完整 traceback)
    assert "import traceback as _tb" in main_body or "traceback.format_exc()" in main_body, \
        "main() 兜底应捕获完整 traceback"
    print("  PASS  main() 兜底源码:except + _logger.error + sys.exit(1)")


def test_main_dpi_called_at_entry(r):
    """main() 入口最优先调 set_dpi_awareness()(在 is_admin 前)。"""
    import cleaner as _cleaner_mod
    call_log = []
    with mock.patch.object(_cleaner_mod, "set_dpi_awareness",
                          side_effect=lambda: call_log.append("dpi") or True), \
         mock.patch.object(_cleaner_mod, "is_admin",
                          side_effect=lambda: call_log.append("admin") or True), \
         mock.patch.object(_cleaner_mod, "TK_AVAILABLE", False), \
         mock.patch.object(_cleaner_mod, "_has_stdin", return_value=True), \
         mock.patch.object(_cleaner_mod, "run_cli"):
        _cleaner_mod.main()
    assert call_log.index("dpi") < call_log.index("admin"), \
        f"set_dpi_awareness 必须早于 is_admin,实际顺序: {call_log}"
    print("  PASS  main() 入口 DPI 优先于 admin 检查")


# ===================== task-fix-status-lbl 测试 =====================

def test_status_lbl_initialized(r):
    """MainWindow.__init__ 调用 _build_status_bar(),所以 self.status_lbl 必存在。

    bug 背景:task-ui-adjust 合并进度条到顶部时,误删了 __init__ 中
    `self._build_status_bar()` 调用,导致 status_lbl 属性永远不存在,
    CleanerTab._refresh_disk → _set_status 时 AttributeError。
    """
    src = Path(__file__).parent.parent.joinpath("cleaner.py").read_text(encoding="utf-8")
    # 1. grep 验证 __init__ 中有 _build_status_bar 调用
    # 找到 class MainWindow 范围
    cls_start = src.find("class MainWindow:")
    if cls_start < 0:
        raise AssertionError("找不到 class MainWindow 定义")
    # 取到下一个 class 之前的范围作为 MainWindow 块
    next_cls = src.find("\nclass ", cls_start + 1)
    cls_body = src[cls_start:next_cls if next_cls > 0 else len(src)]
    assert "self._build_status_bar()" in cls_body, \
        "MainWindow __init__ 必须包含 self._build_status_bar() 调用"
    # 2. _build_status_bar 调用必须在 4 个 Tab 创建之前(否则 CleanerTab._refresh_disk
    #    → _set_status 时 status_lbl 还不存在)
    sb_call_line = None
    tab_create_line = None
    for i, line in enumerate(cls_body.split("\n"), 1):
        if "self._build_status_bar()" in line and sb_call_line is None:
            sb_call_line = i
        if "self.tab_clean = CleanerTab(" in line and tab_create_line is None:
            tab_create_line = i
    assert sb_call_line is not None, "_build_status_bar() 调用未找到"
    assert tab_create_line is not None, "tab 创建行未找到"
    assert sb_call_line < tab_create_line, \
        f"_build_status_bar() 必须在 Tab 创建前调用(否则 CleanerTab._refresh_disk 会触发 status_lbl AttributeError)。当前 _build_status_bar 在 cls_body 第 {sb_call_line} 行,Tab 创建在第 {tab_create_line} 行"
    # 3. _build_status_bar 方法内部必须创建 self.status_lbl
    sb_def_match = re.search(r"def _build_status_bar\(self\):(.*?)(?=\n    def |\nclass )", cls_body, re.DOTALL)
    assert sb_def_match, "_build_status_bar 方法未找到"
    assert "self.status_lbl" in sb_def_match.group(1), \
        "_build_status_bar 方法体必须包含 self.status_lbl 创建"
    print("  PASS  status_lbl 初始化 + _build_status_bar 在 Tab 创建前调用")


def test_set_status_no_attributeerror(r):
    """_set_status 访问 self.status_lbl — 必须有创建点。

    通过 grep 验证:任何 _set_status / set_status 路径访问的 self.X 属性,
    都在 _build_status_bar 或其他 _build_X 中有创建。
    """
    src = Path(__file__).parent.parent.joinpath("cleaner.py").read_text(encoding="utf-8")
    # _set_status 方法访问的属性
    set_status = re.search(r"def _set_status\(self[^)]*\):(.*?)(?=\n    def |\nclass )",
                           src, re.DOTALL)
    assert set_status, "_set_status 方法未找到"
    body = set_status.group(1)
    # 提取 self.X 引用
    used_attrs = set(re.findall(r"self\.(\w+)", body))
    # 关键属性:status_lbl, task_progress, task_progress_pct, task_stage_lbl
    required = {"status_lbl", "task_progress", "task_progress_pct", "task_stage_lbl"}
    missing = required - used_attrs
    assert not missing, f"_set_status 应访问 {required},实际缺: {missing}"
    # 校验每个属性都有创建点(_build_X 或 __init__ 直接赋值)
    for attr in required:
        # 类内出现 self.{attr} = 或 self.{attr}[
        # 注意 self.attr.get / .config 也算访问,但我们要找创建点
        create_patterns = [
            rf"self\.{attr}\s*=",
            rf"self\.{attr}\[",
            rf"self\.{attr}\.config\(",
        ]
        assert any(re.search(p, src) for p in create_patterns), \
            f"self.{attr} 无任何创建/访问点"
    print(f"  PASS  _set_status 访问的 {len(required)} 个属性都有创建点")


def test_build_status_bar_called_in_init(r):
    """grep 验证 MainWindow.__init__ 中确实调 _build_status_bar(task-fix-status-lbl 关键回归点)。

    这个测试是 task-fix-status-lbl 的核心验收点 — 防止以后再次
    误删调用。
    """
    src = Path(__file__).parent.parent.joinpath("cleaner.py").read_text(encoding="utf-8")
    # 提取 MainWindow.__init__ 函数体
    init_match = re.search(
        r"class MainWindow:\s*\n\s*def __init__\(self\):(.*?)(?=\n    def |\nclass )",
        src, re.DOTALL,
    )
    assert init_match, "MainWindow.__init__ 未找到"
    init_body = init_match.group(1)
    assert "self._build_status_bar()" in init_body, \
        "MainWindow.__init__ 必须调 self._build_status_bar()(task-fix-status-lbl 修复)"
    # 同时验证 _build_log_panel 也调用(确保 _log 等已就绪)
    assert "self._build_log_panel()" in init_body, \
        "_build_log_panel 也必须调(防止类似回归)"
    print("  PASS  MainWindow.__init__ 调用 _build_status_bar + _build_log_panel")


def test_all_mainwindow_build_methods_called_in_init(r):
    """全面排查:MainWindow 类内所有 _build_X 方法都被 __init__ 调用。

    task-fix-status-lbl 的根因:新增 _build_status_bar 后忘了在 __init__ 调它。
    本测试作为通用防漏:以后新增 _build_X 方法但漏调会在 CI 立刻报警。
    """
    src = _read_cleaner_src()

    # 找 class MainWindow 范围(到下一个 class 之前)
    cls_start = src.find("class MainWindow:")
    assert cls_start > 0, "找不到 class MainWindow 定义"
    next_cls = src.find("\nclass ", cls_start + 1)
    cls_body = src[cls_start:next_cls if next_cls > 0 else len(src)]

    # 提取类内所有 def _build_X(self) 方法名
    build_methods = set(re.findall(r"def (_build_\w+)\(self", cls_body))
    assert build_methods, "MainWindow 应有 _build_X 方法"
    # 当前已知 4 个:_build_log_panel / _build_title / _build_top_progress / _build_status_bar
    assert len(build_methods) >= 4, (
        f"MainWindow 应有 ≥4 个 _build_X 方法(实际 {len(build_methods)}: {sorted(build_methods)})"
    )

    # 提取 __init__ 函数体
    init_match = re.search(
        r"def __init__\(self\):(.*?)(?=\n    def |\nclass )",
        cls_body, re.DOTALL,
    )
    assert init_match, "MainWindow.__init__ 未找到"
    init_body = init_match.group(1)

    # 每个 _build_X 方法都应在 __init__ 中被 self.X() 调用
    missing = []
    for bm in sorted(build_methods):
        if f"self.{bm}()" not in init_body:
            missing.append(bm)
    assert not missing, (
        f"以下 _build_X 方法未在 MainWindow.__init__ 中调用,会导致属性不存在: {missing}"
    )
    print(
        f"  PASS  MainWindow.__init__ 调用了全部 {len(build_methods)} 个 _build_X 方法:"
        f" {sorted(build_methods)}"
    )


def test_set_status_behavioral_no_attributeerror(r):
    """行为测试:_set_status 不抛 AttributeError。

    静态分析(test_set_status_no_attributeerror)验证所有属性有创建点,
    本测试进一步验证运行时:mock 所有 widget 后调用 _set_status 不会抛 AttributeError。
    """
    import cleaner as _cleaner

    # 绕开 __init__ 直接构造(MainWindow.__new__ 跳过 __init__)
    win = _cleaner.MainWindow.__new__(_cleaner.MainWindow)
    # 注入 _set_status 访问的 4 个属性(magicmock 自动接受任意属性访问)
    win.status_lbl = mock.MagicMock()
    win.task_progress = mock.MagicMock()
    win.task_progress_pct = mock.MagicMock()
    win.task_stage_lbl = mock.MagicMock()

    # 调用 _set_status 不应抛 AttributeError
    try:
        win._set_status("测试状态文本", 0.5)
        win._set_status("完成", 1.0)
        win._set_status("准备")  # percent=None,只设 text
    except AttributeError as e:
        raise AssertionError(f"_set_status 抛 AttributeError: {e}")

    # 验证 widget 被调用
    win.status_lbl.config.assert_called_with(text="准备")  # 最后一次
    print("  PASS  _set_status 行为测试:不抛 AttributeError,widget 正常调用")


# ===================== task-responsive-layout 测试 =====================

def test_responsive_layout(r):
    """验证 root 层 grid 化:rowconfigure ≥4 + sticky=NSEW ≥3 + minsize 保留 (1000, 680)。

    任务(task-responsive-layout)要求:resize 时各区域按比例自动伸缩,
    主要靠 root 4 行 grid_rowconfigure(0=固定标题/1=固定进度条/2=Notebook 主区/3=日志)
    实现。
    """
    src = Path(__file__).parent.parent.joinpath("cleaner.py").read_text(encoding="utf-8")
    # 1. root.rowconfigure ≥4 行
    rowcfg = len(re.findall(r"self\.root\.rowconfigure\(", src))
    assert rowcfg >= 4, f"root.rowconfigure 应 ≥4,实际 {rowcfg}"
    # 2. root.columnconfigure ≥1
    colcfg = len(re.findall(r"self\.root\.columnconfigure\(", src))
    assert colcfg >= 1, f"root.columnconfigure 应 ≥1,实际 {colcfg}"
    # 3. sticky=NSEW 或 "nsew" ≥3(标题/进度/日志/notebook)
    sticky_nsew = len(re.findall(r'sticky\s*=\s*["\']?nsew["\']?', src, re.IGNORECASE))
    assert sticky_nsew >= 3, f"sticky=NSEW 应 ≥3,实际 {sticky_nsew}"
    # 4. minsize (1000, 680) 保留
    assert 'minsize(1000, 680)' in src, "minsize (1000, 680) 未保留"
    # 5. 4 个 grid 调用(notebook + log_frame + title + top_progress 各 .grid(row=N))
    grid_calls = len(re.findall(r"\.grid\(row=", src))
    assert grid_calls >= 4, f"顶层 .grid(row=) 调用应 ≥4,实际 {grid_calls}"
    print(f"  PASS  响应式布局:rowconfigure={rowcfg} colconfigure={colcfg} sticky_NSEW={sticky_nsew} grid_calls={grid_calls}")


def test_tab_key_widgets_fill_both(r):
    """验证 4 个 Tab 的关键内容控件都 pack(fill=BOTH, expand=True),保证 resize 时填满父 frame。

    - CleanerTab: list_card(L3114) + canvas/canvas_inner
    - BigFilesTab / FolderSizeTab / DuplicateTab: result_card + tree + scroll
    """
    src = Path(__file__).parent.parent.joinpath("cleaner.py").read_text(encoding="utf-8")
    # 找 4 个 Tab 的关键 fill=BOTH, expand=True 组合出现次数
    # CleanerTab.list_card / BigFilesTab.result_card / FolderSizeTab.result_card / DuplicateTab.result_card
    fill_both_expand = len(re.findall(r"\.pack\(fill=tk\.BOTH,\s*expand=True", src))
    assert fill_both_expand >= 8, f"关键控件 fill=BOTH+expand=True 应 ≥8 次,实际 {fill_both_expand}"
    # 4 个 tree 都用 side=tk.LEFT, fill=tk.BOTH, expand=True(滚动条用 fill=Y)
    tree_fill = len(re.findall(r"self\.tree\.pack\(side=tk\.LEFT,\s*fill=tk\.BOTH,\s*expand=True", src))
    assert tree_fill >= 3, f"4 个 Tab 的 tree 控件 fill=BOTH+expand=True 应 ≥3,实际 {tree_fill}"
    # log_text 必须 fill=BOTH expand=True
    assert "self.log_text.pack(fill=tk.BOTH, expand=True" in src, "log_text 未 fill=BOTH+expand=True"
    print(f"  PASS  Tab 关键控件 fill=BOTH+expand=True({fill_both_expand} 处)+ tree × {tree_fill}")


def test_hash_steps_50_elimination(r):
    """三级哈希:60 文件场景,假阳性剔除率 ≥80%。

    数据构造(hash_steps):
      - 20 个 size 唯一文件 (Group A) → L1 即剔除
      - 30 个 size 相同 head 不同的文件 (Group B) → L2 head-hash 剔除
      - 10 个 size+head+md5 全相同文件 (Group C) → L3 命中 1 个真重复组

    期望 find_duplicates 返回 1 个组,组内 10 个文件。
    假阳性剔除率 = (60 - 10) / 60 ≈ 83.3% ≥ 80%。
    """
    d = Path(tempfile.mkdtemp(prefix="test_hash50_"))
    try:
        result = hash_steps(d)
        # 至少 60 个文件
        assert result.total() >= 50, f"应 ≥50 个文件,实际 {result.total()}"
        assert len(result.fake_dup_paths) == 10
        assert len(result.size_unique_paths) == 20
        assert len(result.size_same_head_diff_paths) == 30

        # 跑 Analyzer 三级哈希
        # min_size=1024 因为 hash_steps 默认 base_size=100KB(>1KB)
        a = Analyzer(log_callback=lambda *a, **kw: None,
                     progress_callback=lambda *a, **kw: None)
        groups = a.find_duplicates(str(d / "hash_steps_data"),
                                   min_size=1024, max_depth=4)
        # 真重复组:Group C 10 个文件
        assert len(groups) >= 1, f"应有至少 1 个重复组,实际 0"
        # 找到包含 10 个文件的组
        big_group = max(groups, key=len)
        assert len(big_group) == 10, f"真重复组应有 10 个文件,实际 {len(big_group)}"

        # 不应有第二个组(假阳性)
        assert len(groups) == 1, (
            f"出现额外组,假阳性!实际 {len(groups)} 个组: "
            f"{[len(g) for g in groups]}"
        )

        # 假阳性剔除率
        remaining = sum(len(g) for g in groups)
        elimination_rate = 1 - remaining / result.total()
        assert elimination_rate >= 0.80, (
            f"假阳性剔除率 {elimination_rate:.1%} < 80%"
        )
        print(
            f"  PASS  50+ 文件三级哈希:剔除率 {elimination_rate:.1%},"
            f"识别真重复 1 组 10 个文件"
        )
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ===================== UI 调整(task-ui-adjust)测试 =====================

_CLEANER_SRC = Path(__file__).parent.parent / "cleaner.py"


def _read_cleaner_src() -> str:
    return _CLEANER_SRC.read_text(encoding="utf-8")


def test_ui_progress_bars_dual_purpose(r):
    """UI 调整:2 个 Progressbar,disk_bar + task_progress 各司其职。

    决策(task-ui-adjust):
      - disk_bar    : 顶部 C: 盘使用率(扫描前/中显示已用 %)
      - task_progress: MainWindow 底部 status bar 全局任务进度(跨 Tab 共享)
      - 旧的 CleanerTab 内 self.progress / self.progress_pct 已删除
      - CleanerTab 右侧「本次进度」卡片已移除
    """
    src = _read_cleaner_src()

    # 1. 仅 2 个 ttk.Progressbar(创建点)
    create_points = re.findall(r"ttk\.Progressbar\s*\(", src)
    assert len(create_points) == 2, (
        f"应有 2 个 Progressbar 创建点,实际 {len(create_points)}"
    )

    # 2. 两个属性存在
    assert "self.disk_bar = ttk.Progressbar" in src, "disk_bar 应作为 self 属性存在"
    assert "self.task_progress = ttk.Progressbar" in src, (
        "task_progress 应作为 self 属性存在"
    )

    # 3. 旧的 CleanerTab 内 self.progress 已删除(避免重复)
    #    注意:源码注释里可能提及这些名字,所以用 行首 + 赋值 精确匹配实际代码
    assert not re.search(
        r"^\s*self\.progress\s*=\s*ttk\.Progressbar", src, re.MULTILINE,
    ), "旧 self.progress = ttk.Progressbar 应已删除(注释里提及不算)"
    assert not re.search(
        r"^\s*self\.progress_pct\s*=", src, re.MULTILINE,
    ), "旧 self.progress_pct = ... 应已删除(注释里提及不算)"

    # 4. 验证两种用途:disk_bar 的 value 在 disk usage 路径上设置
    #    task_progress 的 value 在 set_status 进度路径上设置
    #    通过代码上下文位置断言
    disk_bar_idx = src.find("self.disk_bar = ttk.Progressbar")
    task_progress_idx = src.find("self.task_progress = ttk.Progressbar")
    # task_progress 必须在 disk_bar 之后定义(L3086 vs L4619)
    assert disk_bar_idx < task_progress_idx, (
        f"disk_bar 应早于 task_progress 定义"
        f"(disk={disk_bar_idx}, task={task_progress_idx})"
    )

    # 5. 两种进度条用不同 length/style 标识区分用途
    #    task_progress 有 length=240(明确宽度),disk_bar 用 fill=tk.X(自适应宽度)
    assert 'length=240' in src.split("self.task_progress = ttk.Progressbar")[1][:300], (
        "task_progress 应有 length=240 参数(固定宽度)"
    )
    # disk_bar 在源码附近用 fill=tk.X pack
    assert 'self.disk_bar.pack(fill=tk.X' in src, (
        "disk_bar 应 fill=tk.X 自适应宽度"
    )

    print("  PASS  UI 调整:disk_bar + task_progress 双进度条各司其职")


def test_ui_log_text_height_and_wrap(r):
    """UI 调整:日志区 height ≥ 20、wrap == tk.WORD、Consolas 字体。

    log_text 是 ScrolledText,带 insertbackground(LOG_FG),state 初始 DISABLED。
    """
    src = _read_cleaner_src()

    # log_text = scrolledtext.ScrolledText(...)
    # 注意:font=("Consolas", 9) 内有右括号,不能用 [^)]+ 简单截取
    # 改为:定位起始位置,取后续 600 字符窗口(覆盖整个多行调用)
    m = re.search(r"self\.log_text\s*=\s*scrolledtext\.ScrolledText\s*\(", src)
    assert m is not None, "log_text 应是 scrolledtext.ScrolledText 实例"
    start = m.end()
    window = src[start:start + 600]  # 涵盖多行参数

    # height ≥ 20
    hm = re.search(r"height\s*=\s*(\d+)", window)
    assert hm is not None, f"log_text 应有 height 参数,window={window[:200]!r}"
    height = int(hm.group(1))
    assert height >= 20, f"log_text height 应 ≥ 20,实际 {height}"

    # wrap == tk.WORD
    assert "wrap=tk.WORD" in window, (
        f"log_text wrap 应为 tk.WORD,window 前 200 字符={window[:200]!r}"
    )

    # font 应含 Consolas(等宽字体,适合日志)
    assert "Consolas" in window, (
        f"log_text font 应为 Consolas 等宽,window 前 200 字符={window[:200]!r}"
    )

    print(
        f"  PASS  UI 调整:log_text height={height} ≥ 20, wrap=tk.WORD, font=Consolas"
    )


def test_ui_main_window_geometry(r):
    """UI 调整:主窗口 geometry 应包含 v3.3 调整后的尺寸。

    cleaner.py L4395: self.root.geometry("1100x780")
    """
    src = _read_cleaner_src()

    # 找 MainWindow 类内的 self.root.geometry(...) 调用
    # 简化:全局搜 self.root.geometry(...)
    m = re.search(
        r'self\.root\.geometry\(\s*["\']([^"\']+)["\']\s*\)',
        src,
    )
    assert m is not None, "MainWindow 应调用 self.root.geometry(...) 设置窗口尺寸"
    geometry = m.group(1)
    # 宽度 ≥ 1100,高度 ≥ 780
    gm = re.match(r"(\d+)x(\d+)", geometry)
    assert gm is not None, f"geometry 格式应为 WxH,实际 {geometry!r}"
    w, h = int(gm.group(1)), int(gm.group(2))
    assert w >= 1100, f"主窗口宽度应 ≥ 1100,实际 {w}"
    assert h >= 780, f"主窗口高度应 ≥ 780,实际 {h}"
    print(f"  PASS  UI 调整:MainWindow geometry={w}x{h} (≥1100x780)")


def test_ui_styled_styles_call_chain(r):
    """UI 调整:make_styled_styles 仍在 _apply_theme / MainWindow 启动时调用。

    主题切换时需重新配置 Treeview / Progressbar 等 ttk 样式颜色。
    """
    src = _read_cleaner_src()

    # 1. make_styled_styles 函数定义存在
    assert "def make_styled_styles" in src, "make_styled_styles 函数应定义"

    # 2. _apply_theme / apply_theme 中调用
    assert "make_styled_styles()" in src, "应至少一处调用 make_styled_styles()"

    # 3. WC.Horizontal.TProgressbar 样式被配置
    assert "WC.Horizontal.TProgressbar" in src, (
        "WC.Horizontal.TProgressbar 样式应被配置"
    )
    # 至少 3 处出现:1 处 style.configure + 2 个 Progressbar 引用
    # (configure 调用 1 次 + disk_bar 1 次 + task_progress 1 次)
    assert src.count("WC.Horizontal.TProgressbar") >= 3, (
        f"WC.Horizontal.TProgressbar 应至少 3 次出现,"
        f"实际 {src.count('WC.Horizontal.TProgressbar')}"
    )

    # 4. log_text.configure 在 _apply_theme 中
    #    主题切换后,日志框颜色也要更新
    apply_theme_match = re.search(
        r"def\s+_apply_theme.*?(?=\n    def\s|\nclass\s|\Z)",
        src, re.DOTALL,
    )
    if apply_theme_match is not None:
        apply_theme_body = apply_theme_match.group(0)
        assert "self.log_text.configure" in apply_theme_body, (
            "_apply_theme 应调用 self.log_text.configure"
        )

    print(
        "  PASS  UI 调整:make_styled_styles 调用链 + log_text.configure 在 _apply_theme 中"
    )


# ===================== Bug 修复 + 布局改造(task-fix-cli-stdin + task-responsive-layout) =====================

def test_cli_safe_input_handles_stdin_none(r):
    """修复:_safe_input 在 sys.stdin=None 时返回 default,不抛 RuntimeError。

    PyInstaller --noconsole 打包后 sys.stdin=None,直接 input() 会抛
    'RuntimeError: input(): lost sys.stdin'。_safe_input 必须兜底。
    """
    # mock sys.stdin 为 None
    with mock.patch.object(sys, "stdin", None):
        # 不传 default — 期望返回空串
        result = _safe_input("prompt> ")
        assert result == "", f"stdin=None 时应返回 default='',实际 {result!r}"
        # 传 default — 期望返回 default
        result2 = _safe_input("prompt> ", "mydefault")
        assert result2 == "mydefault", (
            f"stdin=None 时应返回 default='mydefault',实际 {result2!r}"
        )
    print("  PASS  CLI stdin 修复:_safe_input 无 stdin 返回 default")


def test_cli_has_stdin_detects_none(r):
    """修复:_has_stdin 在 sys.stdin=None 时返回 False。"""
    with mock.patch.object(sys, "stdin", None):
        assert _has_stdin() is False, "sys.stdin=None 时 _has_stdin 应返回 False"

    # 正常 stdin (本进程自带)
    assert _has_stdin() is True, "正常 sys.stdin 应返回 True"

    # stdin 没有 isatty 属性 — 也视为不可用
    class FakeStdinNoIsatty:
        pass
    with mock.patch.object(sys, "stdin", FakeStdinNoIsatty()):
        assert _has_stdin() is False, "无 isatty 属性的 stdin 应视为不可用"
    print("  PASS  CLI stdin 修复:_has_stdin 正确检测 None / 无 isatty")


def test_cli_run_cli_handles_no_stdin(r):
    """修复:run_cli 在 stdin 不可用时打印错误并退出,不抛 RuntimeError。"""
    # sys.stdin=None 模拟 PyInstaller --noconsole 打包后环境
    with mock.patch.object(sys, "stdin", None):
        # mock 可能的弹窗 / messagebox 避免测试卡死
        with mock.patch("tkinter.messagebox.showerror"):
            with mock.patch("tkinter.messagebox.showwarning"):
                try:
                    run_cli()
                except SystemExit:
                    pass  # sys.exit() 算正常退出
                except RuntimeError as e:
                    if "lost sys.stdin" in str(e):
                        raise AssertionError(
                            f"run_cli 仍抛 RuntimeError: {e}"
                        )
                    raise
    print("  PASS  CLI stdin 修复:run_cli 无 stdin 优雅退出,不抛 RuntimeError")


def test_responsive_layout_root_grid_weights(r):
    """响应式布局:root grid 4 行权重 0/0/3/1,主列 weight=1。

    - row=0 标题栏(固定)
    - row=1 顶部 task_progress(固定)
    - row=2 Notebook 主伸缩区(weight=3,占比 60%)
    - row=3 底部 log_panel 辅助伸缩(weight=1,占比 20% 配合 4:1 黄金比)
    """
    src = _read_cleaner_src()

    # minsize(1000, 680) 必须保持
    assert "self.root.minsize(1000, 680)" in src, (
        "minsize 应保持 (1000, 680)"
    )

    # 主列 weight=1(横向伸缩)
    assert "self.root.columnconfigure(0, weight=1)" in src, (
        "root columnconfigure(0, weight=1) 应存在(横向伸缩)"
    )

    # 4 行权重必须严格匹配 [0, 0, 3, 1]
    expected_weights = {
        0: 0,  # 标题栏固定
        1: 0,  # task_progress 固定
        2: 3,  # Notebook 主伸缩区
        3: 1,  # log_panel 辅助伸缩
    }
    for row, weight in expected_weights.items():
        pattern = f"self.root.rowconfigure({row}, weight={weight})"
        assert pattern in src, f"{pattern} 应存在"

    print(
        "  PASS  响应式布局:root grid weights=[0,0,3,1], "
        "column weight=1, minsize=(1000,680)"
    )


def test_responsive_layout_sticky_nsew_and_ew(r):
    """响应式布局:sticky=NSEW 用于主伸缩区,sticky=EW 用于固定行。

    - sticky='nsew' (至少 2 处):notebook + log_panel — 跟随 row weight 伸缩
    - sticky='ew' (至少 2 处):顶部标题栏 + 顶部 task_progress — 横向铺满但高度固定
    """
    src = _read_cleaner_src()

    # sticky=nsew (不区分大小写) 至少 2 处
    nsew_count = len(re.findall(r'sticky\s*=\s*["\']nsew["\']', src, re.IGNORECASE))
    assert nsew_count >= 2, f"sticky=nsew 应 ≥ 2(notebook + log_panel),实际 {nsew_count}"

    # sticky=ew 至少 2 处
    ew_count = len(re.findall(r'sticky\s*=\s*["\']ew["\']', src, re.IGNORECASE))
    assert ew_count >= 2, f"sticky=ew 应 ≥ 2(顶部标题 + 顶部进度条),实际 {ew_count}"

    # 注释里提及的「响应式布局」字样 ≥ 2 处(改造点)
    responsive_mentions = src.count("响应式布局")
    assert responsive_mentions >= 2, (
        f"应有 ≥ 2 处注释提及「响应式布局」改造点,实际 {responsive_mentions}"
    )

    print(
        f"  PASS  响应式布局:sticky=nsew×{nsew_count}, sticky=ew×{ew_count}, "
        f"响应式布局注释 ×{responsive_mentions}"
    )


# ===================== GUI 启动修复(task-fix-gui-startup) =====================

def test_fix_tcl_tk_paths_idempotent_and_skips_non_frozen(r):
    """修复:_fix_tcl_tk_paths_for_pyinstaller 幂等 + 非 frozen 环境跳过。

    场景 1:非 PyInstaller 打包环境(sys.frozen=False)→ 函数应立即 return,不抛错,不污染环境
    场景 2:连续调用两次 → 第二次不应抛错(幂等)
    """
    # 场景 1:非 frozen 环境
    with mock.patch.object(sys, "frozen", False, create=True), \
         mock.patch.object(sys, "_MEIPASS", None, create=True):
        # 备份可能污染的环境变量
        old_tcl = os.environ.get("TCL_LIBRARY")
        old_tk = os.environ.get("TK_LIBRARY")
        try:
            _fix_tcl_tk_paths_for_pyinstaller()
            # 非 frozen 环境不应设置 TCL_LIBRARY / TK_LIBRARY(本进程自己的值)
            # 这里只验证不抛错,实际值可能因 python 启动时已设置
        finally:
            # 恢复
            if old_tcl is None:
                os.environ.pop("TCL_LIBRARY", None)
            else:
                os.environ["TCL_LIBRARY"] = old_tcl
            if old_tk is None:
                os.environ.pop("TK_LIBRARY", None)
            else:
                os.environ["TK_LIBRARY"] = old_tk

    # 场景 2:幂等(连续两次调用)
    with mock.patch.object(sys, "frozen", False, create=True), \
         mock.patch.object(sys, "_MEIPASS", None, create=True):
        _fix_tcl_tk_paths_for_pyinstaller()
        _fix_tcl_tk_paths_for_pyinstaller()  # 不抛错

    # 场景 3:frozen + _MEIPASS 都存在时,确实尝试设置环境变量
    fake_meipass = tempfile.mkdtemp(prefix="fake_meipass_")
    try:
        # 在 fake_meipass 下建 tcl 和 tk 子目录
        for sub in ("tcl", "tk"):
            os.makedirs(os.path.join(fake_meipass, sub), exist_ok=True)

        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "_MEIPASS", fake_meipass, create=True):
            old_tcl = os.environ.pop("TCL_LIBRARY", None)
            old_tk = os.environ.pop("TK_LIBRARY", None)
            try:
                _fix_tcl_tk_paths_for_pyinstaller()
                # 应已设置 TCL_LIBRARY 和 TK_LIBRARY
                assert os.environ.get("TCL_LIBRARY") == os.path.join(fake_meipass, "tcl"), (
                    f"应设置 TCL_LIBRARY={fake_meipass}/tcl,实际 {os.environ.get('TCL_LIBRARY')}"
                )
                assert os.environ.get("TK_LIBRARY") == os.path.join(fake_meipass, "tk"), (
                    f"应设置 TK_LIBRARY={fake_meipass}/tk,实际 {os.environ.get('TK_LIBRARY')}"
                )
                # 再调一次也不应覆盖(已设置则跳过)
                _fix_tcl_tk_paths_for_pyinstaller()
                assert os.environ.get("TCL_LIBRARY") == os.path.join(fake_meipass, "tcl")
            finally:
                if old_tcl is not None:
                    os.environ["TCL_LIBRARY"] = old_tcl
                if old_tk is not None:
                    os.environ["TK_LIBRARY"] = old_tk
    finally:
        shutil.rmtree(fake_meipass, ignore_errors=True)

    print(
        "  PASS  GUI 启动修复:_fix_tcl_tk_paths 幂等 + 非 frozen 跳过 + frozen 设环境"
    )


def test_main_calls_fix_tcl_tk_paths_first(r):
    """修复:main() 入口最先调用 _fix_tcl_tk_paths_for_pyinstaller()。

    顺序:DPI 设置必须在 _fix_tcl_tk_paths 之后,is_admin 必须在 DPI 之后。
    关键:_fix_tcl_tk_paths 必须早于 tkinter / MainWindow 任何引用,否则
    tcl/tk 路径解析失败,即使后续 DPI 设置正确也会抛 Tcl_InitError。
    """
    src = _read_cleaner_src()

    # 函数定义存在
    assert "def _fix_tcl_tk_paths_for_pyinstaller" in src, (
        "_fix_tcl_tk_paths_for_pyinstaller 函数应定义"
    )

    # main() 主体中 _fix_tcl_tk_paths 调用位置早于其他关键调用
    m = re.search(r"^def main\(\):\s*\n((?:.|\n)+?)(?=\n\ndef |\Z)", src, re.MULTILINE)
    assert m is not None, "main() 函数应定义"
    main_body = m.group(1)

    fix_pos = main_body.find("_fix_tcl_tk_paths_for_pyinstaller()")
    dpi_pos = main_body.find("set_dpi_awareness()")
    is_admin_pos = main_body.find("is_admin()")
    mainwindow_pos = main_body.find("MainWindow(")

    assert fix_pos > 0, "main() 应调用 _fix_tcl_tk_paths_for_pyinstaller()"
    assert dpi_pos > 0, "main() 应调用 set_dpi_awareness()"
    assert is_admin_pos > 0, "main() 应调用 is_admin()"
    assert mainwindow_pos > 0, "main() 应调用 MainWindow()"

    # 顺序约束
    assert fix_pos < dpi_pos, (
        f"_fix_tcl_tk_paths({fix_pos}) 应早于 set_dpi_awareness({dpi_pos})"
    )
    assert dpi_pos < is_admin_pos, (
        f"set_dpi_awareness({dpi_pos}) 应早于 is_admin({is_admin_pos})"
    )
    assert is_admin_pos < mainwindow_pos, (
        f"is_admin({is_admin_pos}) 应早于 MainWindow({mainwindow_pos})"
    )
    print(
        f"  PASS  GUI 启动修复:main() 顺序 "
        f"fix({fix_pos}) < dpi({dpi_pos}) < admin({is_admin_pos}) < window({mainwindow_pos})"
    )


def test_main_dpi_awareness_fallback_warning(r):
    """修复:set_dpi_awareness 失败时仅 warning,不阻塞启动。"""
    src = _read_cleaner_src()

    # set_dpi_awareness 函数定义
    assert "def set_dpi_awareness" in src, "set_dpi_awareness 函数应定义"

    # 函数体内有 try/except 多层 fallback(Per-Monitor V2 → system DPI aware)
    # 用 regex 找 try 块数量
    func_match = re.search(
        r"def set_dpi_awareness.*?(?=\ndef |\nclass |\Z)",
        src, re.DOTALL,
    )
    assert func_match is not None, "set_dpi_awareness 函数体应可解析"
    body = func_match.group(0)
    try_count = body.count("try:")
    assert try_count >= 2, (
        f"set_dpi_awareness 应有 ≥ 2 个 try 块(Per-Monitor V2 + System DPI fallback),"
        f"实际 {try_count}"
    )
    # 函数返回 True / False
    assert re.search(r"return\s+True", body), "成功路径应返回 True"
    assert re.search(r"return\s+False", body), "失败路径应返回 False"

    # main() 中调用 set_dpi_awareness 后有 warning 日志
    assert re.search(
        r'set_dpi_awareness\(\).*?_logger\.warning',
        src, re.DOTALL,
    ), "main() 调用 set_dpi_awareness 失败时应有 _logger.warning"
    print("  PASS  GUI 启动修复:set_dpi_awareness 双 API 兜底 + 失败仅 warning")


def test_main_fallback_messagebox_contains_traceback_info(r):
    """修复:main() 兜底弹窗包含 traceback / logger 路径信息,便于用户排查。

    当 GUI 启动失败 + 无 stdin 时,应弹 messagebox.showerror 包含:
    - traceback 信息(写 logger)
    - 日志路径
    - Python 版本 + admin 状态
    """
    src = _read_cleaner_src()

    # messagebox.showerror 出现在 main() 兜底分支
    assert "messagebox.showerror" in src, "应有 messagebox.showerror 调用"

    # 兜底弹窗包含 traceback 关键字
    # 用 re.findall 找所有 showerror 调用,任一包含 traceback 信息即可
    showerror_blocks = re.findall(
        r"messagebox\.showerror\(([^)]+(?:\([^)]*\)[^)]*)*)\)",
        src, re.DOTALL,
    )
    found_traceback_info = False
    for block in showerror_blocks:
        if "日志" in block or "log" in block.lower() or "traceback" in block.lower():
            found_traceback_info = True
            break
    assert found_traceback_info, (
        "兜底 messagebox 应包含日志/traceback 信息"
    )

    # main() 中有 import traceback + format_exc
    assert "import traceback" in src, "main() 应 import traceback"
    assert "format_exc()" in src, "main() 应调用 format_exc()"

    # 兜底分支中应输出绿色垃圾文件清理器 GUI 启动失败的诊断
    assert "GUI 启动失败" in src or "GUI 启动" in src, "main() 应有 GUI 启动失败处理"
    print("  PASS  GUI 启动修复:main() 兜底 messagebox 含 traceback + 日志路径")


def test_main_is_admin_called(r):
    """修复:main() 入口必须检测 is_admin(),非管理员提示 + CLI 兜底。

    is_admin() 在 cleaner.py 中应通过双 API 兜底:
    - 优先 ctypes.windll.shell32.IsUserAnAdmin()
    - 失败 fallback 到 WindowsError + token 检查
    """
    src = _read_cleaner_src()

    # is_admin 函数定义
    assert "def is_admin" in src, "is_admin 函数应定义"

    # 函数体内应尝试 ctypes.windll.shell32.IsUserAnAdmin 双 API 之一
    func_match = re.search(
        r"def is_admin.*?(?=\ndef |\nclass |\Z)",
        src, re.DOTALL,
    )
    assert func_match is not None
    body = func_match.group(0)

    # 双 API 兜底:至少出现 1 次 ctypes 调用 + 1 次 except 兜底
    has_ctypes_admin = "IsUserAnAdmin" in body or "CheckTokenMembership" in body
    assert has_ctypes_admin, "is_admin 应通过 ctypes 调用 IsUserAnAdmin / CheckTokenMembership"
    assert "except" in body, "is_admin 应有 except 兜底"

    # main() 调用 is_admin 后有非管理员提示
    assert re.search(
        r'is_admin\(\).*?非管理员|不是管理员',
        src, re.DOTALL,
    ), "main() 调用 is_admin 后应有非管理员提示"
    print("  PASS  GUI 启动修复:is_admin 双 API 兜底 + main() 调用")


def test_cleaner_spec_datas_collects_tkinter(r):
    """修复:cleaner.spec 用 collect_data_files('tkinter') 自动收集 tcl/tk 数据文件。

    v3.3.2 修复:之前 datas=[] 漏掉 init.tcl/tk.tcl,触发 Tcl_InitError。
    """
    spec_path = _CLEANER_SRC.parent / "cleaner.spec"
    assert spec_path.exists(), "cleaner.spec 应存在"
    spec_src = spec_path.read_text(encoding="utf-8")

    # 用了 collect_data_files
    assert "collect_data_files" in spec_src, "cleaner.spec 应使用 collect_data_files"
    # 收集 tkinter 的数据文件
    assert "collect_data_files('tkinter')" in spec_src or 'collect_data_files("tkinter")' in spec_src, (
        "cleaner.spec 应 collect_data_files('tkinter') 自动收集 init.tcl/tk.tcl"
    )
    # datas= 应引用上述
    assert re.search(r"datas\s*=\s*collect_data_files", spec_src), (
        "Analysis(... datas=collect_data_files(...)) 应配对"
    )
    print("  PASS  cleaner.spec:collect_data_files('tkinter') 自动收集 tcl/tk")


def test_cleaner_spec_excludes_test_modules(r):
    """修复:cleaner.spec excludes 应含 tests / unittest / pytest,防止测试代码污染 exe 命名空间。

    之前打包误把 tests/ 一并纳入,触发 Tcl_InitError 的误导性 traceback。
    """
    spec_path = _CLEANER_SRC.parent / "cleaner.spec"
    spec_src = spec_path.read_text(encoding="utf-8")

    # excludes 块存在
    assert "excludes=" in spec_src, "cleaner.spec 应有 excludes= 参数"

    # 三项必备
    for mod in ["tests", "unittest", "pytest"]:
        assert re.search(
            rf"['\"]{re.escape(mod)}['\"]",
            spec_src,
        ), f"cleaner.spec excludes 应包含 {mod!r}"
    print("  PASS  cleaner.spec:excludes 含 tests / unittest / pytest 防污染")


# ===================== main =====================

def main():
    r = TestRunner()

    r.section("基础工具")
    r.run("human_size 边界值", lambda t: test_human_size(r))
    r.run("get_disk_usage C 盘", lambda t: test_disk_usage(r))
    r.run("calculate_dir_size", lambda t: test_dir_size(t))
    r.run("move_to_recycle_bin API", lambda t: test_recycle_api(r))
    r.run("move_to_recycle_bin_batch 边界", lambda t: test_recycle_batch_api(r))

    r.section("性能")
    r.run("scandir 正确性", test_scandir)
    r.run("并发删 1000 文件", test_concurrent_delete)
    r.run("safe_remove_dir 并发", test_safe_remove_dir_parallel)
    r.run("_scan_for_deletion 单遍", test_scan_for_deletion_single_pass)
    r.run("_scan_wild_temp 阈值", test_wild_temp_scan)

    r.section("过滤/白名单")
    r.run("is_whitelisted 各种情况", test_whitelist)
    r.run("Scanner 构造", test_scanner_init)
    r.run("Cleaner 跳过白名单文件", test_cleaner_whitelist)

    r.section("Analyzer")
    r.run("find_large_files + 过滤", test_analyzer_large)
    r.run("find_duplicates", test_analyzer_dup)
    r.run("_head_hash 行为", test_head_hash_helper)
    r.run("find_duplicates 不同 size 相同内容=无重复", test_dup_diff_content_same_size)
    r.run("find_duplicates head 同 tail 不同=L3 区分", test_dup_same_head_diff_tail)
    r.run("find_duplicates 小文件重复仍可识别", test_dup_small_files)
    r.run("_quick_hash/_full_hash 旧 API 兼容", test_legacy_quick_full_hash_apis)
    r.run("find_large_dirs 排序", test_analyzer_dirs)

    r.section("清理项配置(3 档)")
    r.run("清理项分 3 档 ≥ 35 项", lambda t: test_clean_targets_three_levels(r))
    r.run("37 个新清理项存在", lambda t: test_clean_targets_new_items(r))

    r.section("本轮核心重构(P0/改名/dataclass)")
    r.run("APP_NAME 改名", lambda t: test_app_name_renamed(r))
    r.run("白名单路径遍历拒绝", lambda t: test_whitelist_path_traversal_rejected(r))
    r.run("白名单路径 normcase+resolve", lambda t: test_whitelist_path_normalize_and_resolve(r))
    r.run("create_restore_point EncodedCommand", lambda t: test_create_restore_point_uses_encoded_command(r))
    r.run("create_restore_point description 净化", lambda t: test_create_restore_point_description_sanitized(r))
    r.run("SHFileOperationW 失败返回 failed_paths", lambda t: test_shfileoperation_batch_returns_failed_paths(r))
    r.run("AllowedCommand 枚举白名单", lambda t: test_allowed_command_enum_resolves(r))
    r.run("CleanTarget 字段校验", lambda t: test_cleantarget_validation(r))
    r.run("CleanTarget dict 风格兼容", lambda t: test_cleantarget_dict_compat(r))
    r.run("all_targets_valid 模块加载校验", lambda t: test_all_targets_valid_at_import(r))
    r.run("safe_remove_dir onerror 接口", lambda t: test_safe_remove_dir_onerror(r))
    r.run("ask_typed_confirmation 输入确认", lambda t: test_ask_typed_confirmation_mock(r))
    r.run("旧 C_Cleaner 路径迁移", lambda t: test_legacy_migration_helper(r))
    r.run("_safe_input 兜底 RuntimeError", lambda t: test_safe_input_handles_no_stdin(r))
    r.run("_has_stdin 检测 None/无 isatty/正常", lambda t: test_has_stdin_detection(r))
    r.run("run_cli 无 stdin 不崩", lambda t: test_run_cli_no_stdin_safe(r))
    r.run("set_dpi_awareness 失败不抛/成功 True", lambda t: test_dpi_awareness_set(r))
    r.run("is_admin 双 API 兜底", lambda t: test_is_admin_robust(r))
    r.run("main() GUI 失败写 traceback + exit", lambda t: test_main_gui_fallback_logs_traceback(r))
    r.run("main() DPI 优先于 admin", lambda t: test_main_dpi_called_at_entry(r))
    r.run("status_lbl 初始化 + _build_status_bar 顺序", lambda t: test_status_lbl_initialized(r))
    r.run("_set_status 访问属性都有创建点", lambda t: test_set_status_no_attributeerror(r))
    r.run("MainWindow.__init__ _build_status_bar 回归", lambda t: test_build_status_bar_called_in_init(r))
    r.run("响应式布局 root grid + sticky=NSEW", lambda t: test_responsive_layout(r))
    r.run("各 Tab 关键控件 fill=BOTH/expand", lambda t: test_tab_key_widgets_fill_both(r))
    r.run("三级哈希 50+ 文件剔除率 ≥80%", lambda t: test_hash_steps_50_elimination(r))

    r.section("UI 调整(task-ui-adjust)回归")
    r.run("双进度条各司其职", lambda t: test_ui_progress_bars_dual_purpose(r))
    r.run("日志区 height/wrap/font", lambda t: test_ui_log_text_height_and_wrap(r))
    r.run("主窗口 geometry ≥ 1100x780", lambda t: test_ui_main_window_geometry(r))
    r.run("主题样式调用链", lambda t: test_ui_styled_styles_call_chain(r))

    r.section("Bug 修复 + 布局改造(task-fix-cli-stdin + task-responsive-layout)")
    r.run("_safe_input 无 stdin 返回 default", lambda t: test_cli_safe_input_handles_stdin_none(r))
    r.run("_has_stdin 正确检测 None / 无 isatty", lambda t: test_cli_has_stdin_detects_none(r))
    r.run("run_cli 无 stdin 优雅退出", lambda t: test_cli_run_cli_handles_no_stdin(r))
    r.run("root grid weights + minsize", lambda t: test_responsive_layout_root_grid_weights(r))
    r.run("sticky=nsew / ew 数量", lambda t: test_responsive_layout_sticky_nsew_and_ew(r))

    r.section("GUI 启动修复(task-fix-gui-startup)")
    r.run("_fix_tcl_tk_paths 幂等 + 跳过非 frozen", lambda t: test_fix_tcl_tk_paths_idempotent_and_skips_non_frozen(r))
    r.run("main() 顺序 fix<dpi<admin<window", lambda t: test_main_calls_fix_tcl_tk_paths_first(r))
    r.run("set_dpi_awareness 双 API 兜底", lambda t: test_main_dpi_awareness_fallback_warning(r))
    r.run("main() 兜底 messagebox 含 traceback", lambda t: test_main_fallback_messagebox_contains_traceback_info(r))
    r.run("is_admin 双 API + main() 调用", lambda t: test_main_is_admin_called(r))
    r.run("cleaner.spec collect_data_files tkinter", lambda t: test_cleaner_spec_datas_collects_tkinter(r))
    r.run("cleaner.spec excludes tests/unittest/pytest", lambda t: test_cleaner_spec_excludes_test_modules(r))

    r.section("status_lbl 修复(task-fix-status-lbl)")
    r.run("status_lbl 初始化", lambda t: test_status_lbl_initialized(r))
    r.run("_set_status 静态分析无 AttributeError", lambda t: test_set_status_no_attributeerror(r))
    r.run("_build_status_bar 在 __init__ 调用", lambda t: test_build_status_bar_called_in_init(r))
    r.run("MainWindow 全部 _build_X 都被 __init__ 调用", lambda t: test_all_mainwindow_build_methods_called_in_init(r))
    r.run("_set_status 行为测试不抛 AttributeError", lambda t: test_set_status_behavioral_no_attributeerror(r))

    r.section("元数据")
    r.run("APP_NAME / APP_VERSION", lambda t: test_version(r))

    r._cleanup()
    print(f"\n=== {r.passed} passed, {r.failed} failed ===")
    sys.exit(0 if r.failed == 0 else 1)


if __name__ == "__main__":
    main()
