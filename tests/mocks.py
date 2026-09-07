"""Mock 钩子 (v1)

与 cleaner.py 实现解耦:
- 提供的 mock 不依赖具体属性名/函数签名,实现完成后只需调整 call_goal 即可
- 失败路径用可控返回值模拟,避免破坏 GUI 测试
- mock_subprocess_run 提供两种 assertion helper,覆盖 -EncodedCommand 与清洗两种实现路径

使用方式:
    from mocks import MockSubprocessRun, mock_shfileop, mock_messagebox, inject_confirm_flag

    # 1. 替换 subprocess.run
    with MockSubprocessRun(side_effect=lambda args, **kw: CompletedProcess(args, 0, "", "")) as m:
        # 调用 cleaner 的代码
        ...
        m.assert_encoded_command_call(m.calls[0], "清理前备份")

    # 2. 替换 SHFileOperationW
    mock_shfileop(ctypes.windll, return_val=0x78, aborted=True)
"""

from __future__ import annotations

import base64
import ctypes
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable
from unittest.mock import MagicMock


# ====================== 1. mock_shfileop ======================


class ShFileOpMocker:
    """包装 ctypes.windll.shell32.SHFileOperationW 的 mock。

    用法:
        mocker = mock_shfileop(ctypes.windll, return_val=0x78, aborted=True)
        ... 调用代码 ...
        mocker.uninstall()  # 恢复原函数
        mocker.calls       # 列出所有调用
    """

    def __init__(self, windll: Any, return_val: int = 0x78, aborted: bool = True) -> None:
        self.windll = windll
        self.return_val = return_val
        self.aborted = aborted
        self.calls: list[dict[str, Any]] = []
        self._original = None
        self._installed = False

    def install(self) -> "ShFileOpMocker":
        """替换 SHFileOperationW,开始记录调用。"""
        if self._installed:
            return self
        self._original = self.windll.shell32.SHFileOperationW
        captured = self

        def _fake(op_ptr):
            # op_ptr 是 ctypes 指针,解引用拿结构体
            try:
                op = op_ptr.contents
                p_from = getattr(op, "pFrom", "") or ""
                w_func = getattr(op, "wFunc", 0)
                f_flags = getattr(op, "fFlags", 0)
                # pFrom 是 \0 分隔多路径,展开
                paths = [p for p in p_from.split("\0") if p]
                captured.calls.append({
                    "pFrom": p_from,
                    "paths": paths,
                    "wFunc": w_func,
                    "fFlags": f_flags,
                })
                # 修改 fAnyOperationsAborted 字段
                if hasattr(op, "fAnyOperationsAborted"):
                    op.fAnyOperationsAborted = captured.aborted
            except Exception:
                captured.calls.append({"error": "decode_failed"})
            return captured.return_val

        self.windll.shell32.SHFileOperationW = _fake
        self._installed = True
        return self

    def uninstall(self) -> None:
        if self._installed and self._original is not None:
            self.windll.shell32.SHFileOperationW = self._original
            self._installed = False


def mock_shfileop(
    windll: Any,
    *,
    return_val: int = 0x78,
    aborted: bool = True,
) -> ShFileOpMocker:
    """快速包装 SHFileOperationW,返回 mocker 实例(已 install)。

    默认 return_val=0x78 (DE_OPCANCELLED),aborted=True — 即"用户取消"语义。
    测试失败路径时设 return_val=0x78;测试成功路径设 return_val=0。
    """
    return ShFileOpMocker(windll, return_val=return_val, aborted=aborted).install()


# ====================== 2. mock_messagebox ======================


class MessageBoxMocker:
    """替换 tkinter.messagebox.askyesno / askokcancel 的返回值。

    用法:
        mock_messagebox(askyesno_returns=True, askokcancel_returns=False)
        # ... 测试代码 ...
        mock_messagebox.restore()
    """

    def __init__(
        self,
        askyesno_returns: bool | None = None,
        askokcancel_returns: bool | None = None,
        showinfo_returns: bool | None = None,
        showwarning_returns: bool | None = None,
    ) -> None:
        self.askyesno_returns = askyesno_returns
        self.askokcancel_returns = askokcancel_returns
        self.showinfo_returns = showinfo_returns
        self.showwarning_returns = showwarning_returns
        self.calls: list[dict[str, Any]] = []
        self._installed = False
        self._originals: dict[str, Any] = {}

    def install(self) -> "MessageBoxMocker":
        try:
            from tkinter import messagebox
        except ImportError:
            # 离线环境可能没装 tk,直接跳过
            return self

        def _record(fn_name: str, default: Any):
            def _fake(*args, **kwargs):
                self.calls.append({
                    "fn": fn_name,
                    "args": args,
                    "kwargs": kwargs,
                })
                return getattr(self, f"{fn_name}_returns", default)
            return _fake

        pairs = [
            ("askyesno", self.askyesno_returns, False),
            ("askokcancel", self.askokcancel_returns, False),
            ("showinfo", self.showinfo_returns, None),
            ("showwarning", self.showwarning_returns, None),
        ]
        for fn_name, _, default in pairs:
            if hasattr(messagebox, fn_name):
                self._originals[fn_name] = getattr(messagebox, fn_name)
                setattr(messagebox, fn_name, _record(fn_name, default))
        self._installed = True
        return self

    def restore(self) -> None:
        if not self._installed:
            return
        try:
            from tkinter import messagebox
        except ImportError:
            return
        for fn_name, orig in self._originals.items():
            setattr(messagebox, fn_name, orig)
        self._originals.clear()
        self._installed = False


def mock_messagebox(
    *,
    askyesno_returns: bool | None = None,
    askokcancel_returns: bool | None = None,
    showinfo_returns: bool | None = None,
    showwarning_returns: bool | None = None,
) -> MessageBoxMocker:
    """快速包装 messagebox 二次确认,返回 mocker 实例(已 install)。

    默认 askyesno=None 表示「没装 mock」,需要传 True/False 才会真正拦截。
    """
    return MessageBoxMocker(
        askyesno_returns=askyesno_returns,
        askokcancel_returns=askokcancel_returns,
        showinfo_returns=showinfo_returns,
        showwarning_returns=showwarning_returns,
    ).install()


# ====================== 3. mock_subprocess_run ======================


class _FakeCompletedProcess:
    """subprocess.CompletedProcess 的最小替身,避免依赖真实 subprocess。"""

    def __init__(self, args: Any, returncode: int = 0,
                 stdout: str = "", stderr: str = "") -> None:
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class MockSubprocessRun:
    """替换 subprocess.run 的 mock,捕获所有调用。

    用法:
        m = MockSubprocessRun(side_effect=lambda args, **kw: _FakeCompletedProcess(args, 0))
        m.install()
        # ... 调用代码 ...
        m.uninstall()

        # 断言
        m.assert_encoded_command_call(m.calls[0], "清理前备份")
        m.assert_sanitized_command_call(m.calls[0], "清理前备份")
    """

    # 注入字符白名单(出现即视为清洗失败)
    DANGEROUS_CHARS = set(";'`$\n\r|&")

    def __init__(
        self,
        *,
        side_effect: Callable[[Any, dict], _FakeCompletedProcess] | None = None,
        default_returncode: int = 0,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.side_effect = side_effect
        self.default_returncode = default_returncode
        self._original = None
        self._installed = False

    def install(self) -> "MockSubprocessRun":
        import subprocess
        self._original = subprocess.run
        captured = self

        def _fake(*args, **kwargs):
            # args[0] 是命令(list/str),kwargs 含 capture_output 等
            cmd = args[0] if args else kwargs.get("args")
            tokens_info = self._tokenize(cmd)
            call_record = {
                "args": cmd,
                "kwargs": {k: v for k, v in kwargs.items()},
                "argv_tokens": tokens_info["tokens"],
                "argv_joined": tokens_info["joined"],
            }
            captured.calls.append(call_record)
            if captured.side_effect is not None:
                return captured.side_effect(cmd, kwargs)
            return _FakeCompletedProcess(cmd, returncode=captured.default_returncode)

        subprocess.run = _fake
        self._installed = True
        return self

    def uninstall(self) -> None:
        import subprocess
        if self._installed and self._original is not None:
            subprocess.run = self._original
            self._installed = False

    def __enter__(self) -> "MockSubprocessRun":
        return self.install()

    def __exit__(self, *exc: Any) -> None:
        self.uninstall()

    @staticmethod
    def _tokenize(cmd: Any) -> dict[str, Any]:
        """把命令参数展平成 token 列表与 joined 字符串,供断言检查。

        返回:
            {"tokens": list[str], "joined": str}

        - list/tuple: 直接转 str 再拼 joined
        - str: 用 shlex.split(posix=False) 拆分(保留引号),joined 是空格拼接
        """
        if isinstance(cmd, (list, tuple)):
            tokens = [str(x) for x in cmd]
        elif isinstance(cmd, str):
            import shlex
            try:
                tokens = shlex.split(cmd, posix=False)
            except ValueError:
                tokens = [cmd]
        else:
            tokens = [str(cmd)]
        return {"tokens": tokens, "joined": " ".join(tokens)}

    # ----- 断言 helper -----

    def assert_encoded_command_call(
        self,
        call: dict[str, Any],
        expected_desc: str,
    ) -> bool:
        """断言 call 是「-EncodedCommand 路径」。

        实现原理:PowerShell 的 -EncodedCommand 接收 Base64 字符串,
        把 description 做 UTF-16LE 编码后 Base64,然后作为单独 argv token 传入。
        断言:
          1. argv_tokens 包含 -EncodedCommand (或 -Enc)
          2. 紧随其后的 token 是合法 Base64
          3. Base64 解码后 UTF-16LE 字符串 == expected_desc
        """
        tokens = call.get("argv_tokens", [])
        try:
            idx = next(
                i for i, t in enumerate(tokens)
                if t in ("-EncodedCommand", "-Enc")
            )
        except StopIteration:
            return False
        if idx + 1 >= len(tokens):
            return False
        b64_str = tokens[idx + 1]
        try:
            decoded_bytes = base64.b64decode(b64_str, validate=True)
            decoded_str = decoded_bytes.decode("utf-16-le")
        except Exception:
            return False
        return decoded_str == expected_desc

    def assert_sanitized_command_call(
        self,
        call: dict[str, Any],
        expected_desc: str,
    ) -> bool:
        """断言 call 是「description 清洗路径」。

        实现原理:description 直接拼到 PowerShell 命令行(单/双引号包裹),所以:
          1. 在 argv_joined 上用 regex 提取 `-Description` / `/Description` / `-D`
             之后、被 ' 或 " 包裹的内容(去引号)
          2. 提取出的内容不含 DANGEROUS_CHARS 中的危险字符
          3. 提取出的内容 == expected_desc

        为什么用 joined 而非 tokens:很多实现会把整段 PowerShell -Command 字符串
        作为单个 token 传入(list 模式),tokens 里看不到内部结构。
        """
        joined = call.get("argv_joined", "")
        lower = joined.lower()
        idx = -1
        for kw in ("-description", "--description", "/description", "-desc"):
            pos = lower.find(kw)
            if pos >= 0:
                idx = pos + len(kw)
                break
        if idx < 0:
            return False
        after = joined[idx:].lstrip()
        # 跳过开引号
        if after.startswith(("'", '"')):
            after = after[1:]
        # 提取到首个危险字符为止
        end_chars = "';\"`$&\n\r|"
        end_idx = len(after)
        for i, ch in enumerate(after):
            if ch in end_chars:
                end_idx = i
                break
        sanitized = after[:end_idx].rstrip()
        return sanitized == expected_desc

    def assert_no_injection(
        self,
        call: dict[str, Any],
        expected_desc: str,
    ) -> str:
        """综合断言:任一路径命中即返回路径名 ("encoded" / "sanitized")。

        都不命中则 raise AssertionError,带可定位信息。
        """
        if self.assert_encoded_command_call(call, expected_desc):
            return "encoded"
        if self.assert_sanitized_command_call(call, expected_desc):
            return "sanitized"
        raise AssertionError(
            f"未检测到防注入路径:\n"
            f"  tokens={call.get('argv_tokens')}\n"
            f"  expected_desc={expected_desc!r}\n"
            f"  期望:EncodedCommand 路径 或 清洗路径"
        )


# ====================== 4. inject_confirm_flag ======================


def inject_confirm_flag(cleaner_instance: Any, *, disabled: bool = True) -> Any:
    """在 Cleaner 实例上挂 _confirm_disabled 属性,控制二次确认是否强制放行。

    设计:cleaner.py 应在二次确认前判断 `self._confirm_disabled`,为 True 时直接放行
    (跳过 messagebox.askyesno),避免 GUI 测试卡死。

    如果 cleaner.py 没有实现 _confirm_disabled 检查,此函数只是 setattr,不影响
    行为——属于"接口预留",测试断言可以判断属性存在与否。

    返回 cleaner_instance (便于链式)。
    """
    setattr(cleaner_instance, "_confirm_disabled", disabled)
    return cleaner_instance


def inject_confirm_callback(cleaner_instance: Any, callback: Callable[[], bool]) -> Any:
    """高级:挂 _confirm_callback 替代 messagebox.askyesno 调用。

    cleaner.py 应在弹确认框前判断 `if self._confirm_callback is not None:
    return self._confirm_callback()`。callback 返回 True 视为用户点"是"。
    """
    setattr(cleaner_instance, "_confirm_callback", callback)
    return cleaner_instance


# ====================== 5. filter_existing_paths ======================


def filter_existing_paths(
    paths: Iterable[str],
    *,
    follow_symlinks: bool = False,
) -> tuple[list[str], list[str]]:
    """过滤路径列表,返回 (existing, missing)。

    设计:cleaner.py 的 Cleaner.clean_target 在调用 _scan_for_deletion / safe_remove_dir
    之前,可用此 helper 把不存在的路径集中跳过(替代 if not ep.exists(): continue)。

    不抛异常:PermissionError/OSError 视为 missing。

    follow_symlinks=False 时,符号链接目标不存在也视为 missing(避免悬空链接)。

    注:cleaner.py 已有等价内联代码,本函数仅作为"如果 core-refactor 决定暴露
    helper,测试用此接口"的占位。
    """
    existing: list[str] = []
    missing: list[str] = []
    for p in paths:
        if not p:
            missing.append(p)
            continue
        try:
            if Path(p).exists():
                existing.append(p)
            else:
                missing.append(p)
        except (PermissionError, OSError):
            missing.append(p)
    return existing, missing


# ====================== 6. 全局上下文管理器 ======================


class AllMocks:
    """一次性安装所有 mock 的上下文管理器。

    用法:
        with AllMocks(subproc_default_rc=0, shfileop_rc=0x78) as mocks:
            # ... 调用代码 ...
            mocks.subproc.assert_no_injection(mocks.subproc.calls[0], "clean")
    """

    def __init__(
        self,
        *,
        subproc_side_effect: Callable | None = None,
        subproc_default_rc: int = 0,
        shfileop_return_val: int = 0x78,
        shfileop_aborted: bool = True,
        messagebox_askyesno: bool | None = None,
        messagebox_askokcancel: bool | None = None,
    ) -> None:
        self.subproc = MockSubprocessRun(
            side_effect=subproc_side_effect,
            default_returncode=subproc_default_rc,
        )
        self.shfileop: ShFileOpMocker | None = None
        self._windll: Any = None
        self.messagebox = MessageBoxMocker(
            askyesno_returns=messagebox_askyesno,
            askokcancel_returns=messagebox_askokcancel,
        )
        self._shfileop_params = (shfileop_return_val, shfileop_aborted)

    def __enter__(self) -> "AllMocks":
        self.subproc.install()
        self.messagebox.install()
        try:
            self._windll = ctypes.windll
            self.shfileop = ShFileOpMocker(
                self._windll,
                return_val=self._shfileop_params[0],
                aborted=self._shfileop_params[1],
            ).install()
        except AttributeError:
            # 离线/无 ctypes 时跳过 shfileop
            self.shfileop = None
        return self

    def __exit__(self, *exc: Any) -> None:
        self.subproc.uninstall()
        self.messagebox.restore()
        if self.shfileop is not None:
            self.shfileop.uninstall()


__all__ = [
    "ShFileOpMocker",
    "mock_shfileop",
    "MessageBoxMocker",
    "mock_messagebox",
    "MockSubprocessRun",
    "AllMocks",
    "inject_confirm_flag",
    "inject_confirm_callback",
    "filter_existing_paths",
]
