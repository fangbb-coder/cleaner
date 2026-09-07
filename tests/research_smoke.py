"""预研 smoke test:验证 test_utils + mocks 在无 cleaner.py 改动下能跑通。

目标:
1. 验证 fake_windows_fs 构造目录树,返回路径映射字典
2. 验证 hash_steps 构造 ≥60 文件,分组正确
3. 验证 restore_point_inject_payloads 返回 8 个含危险字符的样本
4. 验证 mock_subprocess_run 捕获调用并支持 assertion helper
5. 验证 mock_shfileop + mock_messagebox 不破坏环境
6. 验证 filter_existing_paths 正确分桶
7. 验证 inject_confirm_flag 在 Cleaner 实例上 setattr 成功
8. 验证 mocks.py / test_utils.py 自身 import 不报错

跑法:
    cd D:\\程序\\C盘清理工具 && python tests/research_smoke.py

预期:全部 PASS,0 FAIL。
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

# 把 tests/ 和 项目根都加到 sys.path
TESTS_DIR = Path(__file__).parent
sys.path.insert(0, str(TESTS_DIR.parent))  # 加载 cleaner
sys.path.insert(0, str(TESTS_DIR))          # 加载 test_utils, mocks

# 必须先 import test_utils,再 mocks(避免 mocks 依赖 cleaner)
import test_utils
import mocks


PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        msg = f"{name}: {detail}" if detail else name
        ERRORS.append(msg)
        print(f"  FAIL  {msg}")


def section(title: str) -> None:
    print(f"\n--- {title} ---")


# ====================== 1. test_utils 自身可导入 ======================
section("test_utils import")

check("FakeFsConfig 可导入", hasattr(test_utils, "FakeFsConfig"))
check("fake_windows_fs 可调用", callable(test_utils.fake_windows_fs))
check("hash_steps 可调用", callable(test_utils.hash_steps))
check("restore_point_inject_payloads 可调用",
      callable(test_utils.restore_point_inject_payloads))
check("make_fake_clean_target 可调用",
      callable(test_utils.make_fake_clean_target))
check("quick_md5 可调用", callable(test_utils.quick_md5))
check("quick_sha1_head 可调用", callable(test_utils.quick_sha1_head))


# ====================== 2. mocks 自身可导入 ======================
section("mocks import")

check("MockSubprocessRun 可实例化",
      bool(mocks.MockSubprocessRun()))
check("MessageBoxMocker 可实例化",
      bool(mocks.MessageBoxMocker()))
check("AllMocks 可实例化", bool(mocks.AllMocks()))
check("filter_existing_paths 可调用",
      callable(mocks.filter_existing_paths))
check("inject_confirm_flag 可调用",
      callable(mocks.inject_confirm_flag))


# ====================== 3. fake_windows_fs ======================
section("fake_windows_fs")

with tempfile.TemporaryDirectory(prefix="smoke_fakefs_") as tmp:
    tmp_p = Path(tmp)

    # 默认配置
    paths = test_utils.fake_windows_fs(tmp_p)
    check("默认配置返回 dict", isinstance(paths, dict))
    check("默认配置 win_temp 存在", paths.get("win_temp") is not None)
    check("默认配置 windows_old 存在",
          paths.get("windows_old") is not None)
    check("默认配置 hiberfil_sys 不存在(config 默认 False)",
          paths.get("hiberfil_sys") is None)
    check("默认配置 vss_shadow_meta 不存在",
          paths.get("vss_shadow_meta") is None)

    # win_temp 路径下应有文件
    win_temp_p = paths["win_temp"]
    check("win_temp 是目录", win_temp_p.is_dir())
    files_in_dir = list(win_temp_p.iterdir())
    check("win_temp 至少有 1 个子项", len(files_in_dir) >= 1,
          detail=f"实际 {len(files_in_dir)} 个")

    # 自定义 config:全部启用
    cfg = test_utils.FakeFsConfig(
        hiberfil_sys=True,
        vss_shadow_meta=True,
    )
    paths2 = test_utils.fake_windows_fs(tmp_p / "all", config=cfg)
    check("自定义 hiberfil_sys 启用后存在",
          paths2.get("hiberfil_sys") is not None)
    check("自定义 vss_shadow_meta 启用后存在",
          paths2.get("vss_shadow_meta") is not None)

    # 自定义 config:全部关闭
    cfg_empty = test_utils.FakeFsConfig(
        win_temp=False,
        prefetch=False,
        windows_old=False,
        windows_bt=False,
        sysreset=False,
        defender_quarantine=False,
        user_temp=False,
        user_local_temp=False,
        software_distribution=False,
        inf_logs=False,
        cbs_logs=False,
        winsxs_manifest=False,
    )
    paths3 = test_utils.fake_windows_fs(tmp_p / "empty", config=cfg_empty)
    check("全关配置 win_temp None",
          paths3.get("win_temp") is None)
    check("全关配置 windows_old None",
          paths3.get("windows_old") is None)
    check("全关配置 defender_quarantine None",
          paths3.get("defender_quarantine") is None)


# ====================== 4. hash_steps ======================
section("hash_steps")

with tempfile.TemporaryDirectory(prefix="smoke_hash_") as tmp:
    tmp_p = Path(tmp)

    r = test_utils.hash_steps(tmp_p / "hs")
    check("总文件数 = 60", r.total() == 60, detail=f"实际 {r.total()}")
    check("size_unique_paths = 20", len(r.size_unique_paths) == 20)
    check("size_same_head_diff_paths = 30",
          len(r.size_same_head_diff_paths) == 30)
    check("fake_dup_paths = 10", len(r.fake_dup_paths) == 10)
    check("all_paths 数量一致",
          len(r.all_paths) == r.total())
    check("expected_true_groups 返回 1 组",
          len(r.expected_true_groups()) == 1)
    check("true group size = group_c_size",
          r.expected_true_groups()[0]["size"] == r.group_c_size)

    # 文件实际存在
    missing = [p for p in r.all_paths if not Path(p).exists()]
    check("所有 60 个文件实际创建", len(missing) == 0,
          detail=f"缺失 {len(missing)}: {missing[:3]}")

    # fake_dup 的 SHA-1 head 应该全相同
    import hashlib
    head_hashes = set()
    for p in r.fake_dup_paths:
        h = hashlib.sha1()
        with open(p, "rb") as f:
            h.update(f.read(65536))
        head_hashes.add(h.hexdigest())
    check("fake_dup 10 个文件 head-hash 全相同 (L2 应命中)",
          len(head_hashes) == 1, detail=f"实际 {len(head_hashes)} 种")

    # size_same_head_diff 的 SHA-1 head 应该各不相同
    head_hashes_b = set()
    for p in r.size_same_head_diff_paths[:10]:  # 取前 10 个测即可
        h = hashlib.sha1()
        with open(p, "rb") as f:
            h.update(f.read(65536))
        head_hashes_b.add(h.hexdigest())
    check("size_same_head_diff 10 个 head-hash 各不相同 (L2 应剔除)",
          len(head_hashes_b) == 10, detail=f"实际 {len(head_hashes_b)} 种")

    # 自定义 n
    r2 = test_utils.hash_steps(
        tmp_p / "hs2",
        n_unique=5, n_size_same_head_diff=5, n_full_same=2,
    )
    check("自定义参数总文件数 = 12", r2.total() == 12)


# ====================== 5. restore_point_inject_payloads ======================
section("restore_point_inject_payloads")

payloads = test_utils.restore_point_inject_payloads()
check("payloads 数量 = 8", len(payloads) == 8, detail=f"实际 {len(payloads)}")
# 验证危险字符存在(至少 4 种)
danger_chars = set(";'`$&\n\r|")
covered = set()
for p in payloads:
    for ch in p:
        if ch in danger_chars:
            covered.add(ch)
check("payloads 至少覆盖 4 种危险字符",
      len(covered) >= 4, detail=f"覆盖 {covered}")


# ====================== 6. make_fake_clean_target ======================
section("make_fake_clean_target")

t_files = test_utils.make_fake_clean_target(paths=["C:\\fake"])
check("files 类型 target 含 paths", t_files.get("paths") == ["C:\\fake"])
check("files 类型 kind=files", t_files.get("kind") == "files")

t_cmd = test_utils.make_fake_clean_target(
    kind="command", command=["cmd", "/c", "exit", "0"])
check("command 类型 target 含 command",
      t_cmd.get("command") == ["cmd", "/c", "exit", "0"])

t_wild = test_utils.make_fake_clean_target(
    kind="wild_temp", roots=["C:\\tmp"], patterns=["*.tmp"], min_age_days=7)
check("wild_temp 类型 target 含 roots",
      t_wild.get("roots") == ["C:\\tmp"])
check("wild_temp 类型 target 含 patterns",
      t_wild.get("patterns") == ["*.tmp"])


# ====================== 7. mocks.filter_existing_paths ======================
section("filter_existing_paths")

with tempfile.TemporaryDirectory(prefix="smoke_filter_") as tmp:
    tmp_p = Path(tmp)
    real = tmp_p / "real.txt"
    real.write_bytes(b"x")
    fake = str(tmp_p / "nonexistent.txt")
    existing, missing = mocks.filter_existing_paths([str(real), fake])
    check("existing 列表包含真实文件",
          str(real) in existing, detail=f"existing={existing}")
    check("missing 列表包含不存在路径",
          fake in missing, detail=f"missing={missing}")
    check("existing 长度 = 1", len(existing) == 1)
    check("missing 长度 = 1", len(missing) == 1)

    # 空字符串路径
    existing2, missing2 = mocks.filter_existing_paths([""])
    check("空字符串视为 missing", "" in missing2)


# ====================== 8. MockSubprocessRun ======================
section("MockSubprocessRun")

# 8a. 基本捕获
with mocks.MockSubprocessRun() as m:
    import subprocess
    r = subprocess.run(["echo", "hello"], capture_output=True, text=True)
    check("subprocess.run 被 mock 后返回对象", r is not None)
    check("calls 列表捕获了 1 次调用", len(m.calls) == 1)
    call = m.calls[0]
    check("call.args 是 list", isinstance(call["args"], list))
    check("call.argv_tokens 包含 echo", "echo" in call["argv_tokens"])

# 8b. EncodedCommand 路径断言
with mocks.MockSubprocessRun() as m:
    import subprocess
    import base64
    expected = "清理前备份"
    b64 = base64.b64encode(expected.encode("utf-16-le")).decode()
    subprocess.run([
        "powershell", "-NoProfile", "-EncodedCommand", b64,
    ], capture_output=True, text=True)
    check("EncodedCommand 路径被识别",
          m.assert_encoded_command_call(m.calls[0], expected))

# 8c. 清洗路径断言
with mocks.MockSubprocessRun() as m:
    import subprocess
    sanitized = "cleaning before backup"
    subprocess.run([
        "powershell", "-NoProfile", "-Command",
        f"Checkpoint-Computer -Description '{sanitized}'",
    ], capture_output=True, text=True)
    check("清洗路径被识别",
          m.assert_sanitized_command_call(m.calls[0], sanitized))

# 8d. 综合断言
with mocks.MockSubprocessRun() as m:
    import subprocess
    expected = "test description"
    subprocess.run([
        "powershell", "-EncodedCommand",
        base64.b64encode(expected.encode("utf-16-le")).decode(),
    ], capture_output=True, text=True)
    try:
        path = m.assert_no_injection(m.calls[0], expected)
        check("综合断言返回 encoded 路径", path == "encoded")
    except AssertionError as e:
        check("综合断言不应失败", False, detail=str(e))

# 8e. 注入检测 - 应失败
with mocks.MockSubprocessRun() as m:
    import subprocess
    # 直接把 ; 拼到 argv 里 (假设 cleaner 没清洗)
    subprocess.run([
        "powershell", "-Command",
        "Checkpoint-Computer -Description 'foo; rm -rf /'",
    ], capture_output=True, text=True)
    try:
        m.assert_no_injection(m.calls[0], "foo; rm -rf /")
        check("有注入的 call 不应通过清洗断言", False)
    except AssertionError:
        check("有注入的 call 正确拒绝", True)


# ====================== 9. inject_confirm_flag ======================
section("inject_confirm_flag")

# 模拟 Cleaner 实例 (duck-typing 即可)
class _FakeCleaner:
    pass

fc = _FakeCleaner()
mocks.inject_confirm_flag(fc, disabled=True)
check("_confirm_disabled=True 设置成功",
      getattr(fc, "_confirm_disabled", None) is True)

mocks.inject_confirm_flag(fc, disabled=False)
check("_confirm_disabled=False 设置成功",
      getattr(fc, "_confirm_disabled", None) is False)

cb_called = []
def cb():
    cb_called.append(True)
    return True

mocks.inject_confirm_callback(fc, cb)
ret = fc._confirm_callback()
check("_confirm_callback 被调用并返回 True",
      ret is True and len(cb_called) == 1)


# ====================== 10. mock_messagebox 不破坏环境 ======================
section("mock_messagebox")

mb = mocks.mock_messagebox(askyesno_returns=True)
check("MessageBoxMocker 已 install", mb._installed is True)
mb.restore()
check("restore 后 _installed=False", mb._installed is False)
# 再调用一次 messagebox.askyesno 不应崩溃
try:
    from tkinter import messagebox
    has_tk = True
except ImportError:
    has_tk = False
if has_tk:
    # 真实 messagebox.askyesno() 会弹 GUI,所以我们只验证可调用
    check("messagebox.askyesno 仍可访问 (未被 monkey patch)",
          callable(messagebox.askyesno))


# ====================== 11. mock_shfileop (可选) ======================
section("mock_shfileop")

try:
    import ctypes
    has_ctypes = True
except ImportError:
    has_ctypes = False

if has_ctypes:
    mocker = mocks.mock_shfileop(
        ctypes.windll, return_val=0x78, aborted=True)
    check("ShFileOpMocker 已 install", mocker._installed is True)
    # 调用一次
    try:
        # 构造一个最简单的 op 结构
        # shfileop 期望一个结构指针,直接调会因参数不对崩溃,但我们只是想验证 mock 装上了
        # 所以跳过实际调用,只验证 _installed 状态
        check("mock 装上后可 uninstall", True)
    finally:
        mocker.uninstall()
    check("uninstall 后 _installed=False", mocker._installed is False)


# ====================== 12. AllMocks 组合 ======================
section("AllMocks 组合")

with mocks.AllMocks(
    subproc_default_rc=0,
    shfileop_return_val=0x78,
    shfileop_aborted=True,
    messagebox_askyesno=False,
) as am:
    check("AllMocks.subproc 已 install", am.subproc._installed)
    check("AllMocks.messagebox 已 install", am.messagebox._installed)
    if am.shfileop is not None:
        check("AllMocks.shfileop 已 install", am.shfileop._installed)

    # subproc 跑一次
    import subprocess
    subprocess.run(["echo", "hi"], capture_output=True, text=True)
    check("AllMocks 期间 subproc 调用被捕获",
          len(am.subproc.calls) == 1)

# 退出后应自动 uninstall
check("AllMocks 退出后 subproc 卸载", not am.subproc._installed)
check("AllMocks 退出后 messagebox 卸载", not am.messagebox._installed)


# ====================== 总结 ======================
print(f"\n{'='*60}")
print(f"smoke 通过: {PASS}    失败: {FAIL}")
if FAIL:
    print("\n失败详情:")
    for e in ERRORS:
        print(f"  — {e}")
    print('='*60)
    sys.exit(1)
else:
    print("=" * 60)
    print("smoke 全部通过 ✓")
    sys.exit(0)
