"""外部工具调用：Avogadro2 可视化、Multiwfn 分析、obabel 格式转换。

移植自旧版 ``psi4gui/core/external.py``，但修掉了三个问题：

1. **路径写死**。旧版把 ``C:\\Users\\<用户名>\\Desktop\\Multiwfn_2026...`` 写进常量，
   换个使用者就不存在了。现改为自动探测 + 配置文件覆盖（见 ``config.py``）。
2. **阻塞**。旧版 ``_run_multiwfn`` 最后 ``proc.wait()`` 会一直等到 Multiwfn 退出；
   当年它是在界面线程里调的，等于界面卡死。现在一律立即返回。
3. **靠盲喂 stdin**。旧版往 Multiwfn 的 stdin 里按固定 ``sleep`` 塞 [回车, 路径, "0"]，
   时序一抖就失败。实测 Multiwfn 支持**命令行直接带文件**（``Multiwfn water.xyz``），
   于是改为直接传参，配合独立控制台窗口，用户可正常交互。

例外：``run_multiwfn_batch`` 是有意阻塞的，用于非交互批量分析与自动化测试。
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import load_config, resolve_tool

#: Windows 下新建独立控制台，让交互式程序有自己的窗口
_CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


@dataclass
class ToolResult:
    """外部工具调用结果。"""

    ok: bool
    tool: str
    message: str
    command: list[str] = field(default_factory=list)
    pid: int | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "message": self.message,
            "command": self.command,
            "pid": self.pid,
        }


def _resolve(name: str) -> tuple[str | None, str]:
    return resolve_tool(name, load_config())


def _missing(tool: str, source: str) -> ToolResult:
    return ToolResult(
        ok=False,
        tool=tool,
        message=f"找不到 {tool} 可执行文件（{source}）。可在设置里指定路径。",
    )


def _check_file(path: str | Path) -> Path | None:
    p = Path(path)
    return p if p.is_file() else None


# ------------------------------------------------------------------ Avogadro2

def view_with_avogadro(file_path: str | Path) -> ToolResult:
    """用 Avogadro2 打开结构文件（立即返回，不等待）。"""
    exe, source = _resolve("avogadro")
    if not exe:
        return _missing("Avogadro2", source)

    path = _check_file(file_path)
    if path is None:
        return ToolResult(False, "avogadro", f"文件不存在: {file_path}")

    command = [exe, str(path)]
    try:
        proc = subprocess.Popen(command, cwd=str(path.parent))
    except OSError as exc:
        return ToolResult(False, "avogadro", f"启动 Avogadro2 失败: {exc}", command)
    return ToolResult(True, "avogadro", f"已用 Avogadro2 打开 {path.name}", command, proc.pid)


# ------------------------------------------------------------------- Multiwfn

def run_multiwfn(file_path: str | Path) -> ToolResult:
    """用 Multiwfn 打开文件。

    直接以文件为命令行参数（实测 Multiwfn 会打印 ``Loaded ... successfully!``），
    并在独立控制台里运行，用户可以正常交互。**立即返回，不等待其退出。**
    """
    exe, source = _resolve("multiwfn")
    if not exe:
        return _missing("Multiwfn", source)

    path = _check_file(file_path)
    if path is None:
        return ToolResult(False, "multiwfn", f"文件不存在: {file_path}")

    command = [exe, str(path)]
    kwargs: dict = {"cwd": str(path.parent)}
    if platform.system() == "Windows":
        kwargs["creationflags"] = _CREATE_NEW_CONSOLE
    try:
        proc = subprocess.Popen(command, **kwargs)
    except OSError as exc:
        return ToolResult(False, "multiwfn", f"启动 Multiwfn 失败: {exc}", command)
    return ToolResult(
        True, "multiwfn", f"已在 Multiwfn 中载入 {path.name}", command, proc.pid
    )


def run_multiwfn_batch(
    file_path: str | Path,
    commands: str,
    timeout: float = 120.0,
) -> tuple[ToolResult, str]:
    """非交互地跑一段 Multiwfn 命令，返回 ``(结果, 完整输出)``。

    用于批量分析与自动化测试 —— **这里是有意阻塞的**。
    """
    exe, source = _resolve("multiwfn")
    if not exe:
        return _missing("Multiwfn", source), ""

    path = _check_file(file_path)
    if path is None:
        return ToolResult(False, "multiwfn", f"文件不存在: {file_path}"), ""

    command = [exe, str(path)]
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(path.parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding=sys.getfilesystemencoding() or "utf-8",
            errors="replace",
        )
    except OSError as exc:
        return ToolResult(False, "multiwfn", f"启动 Multiwfn 失败: {exc}", command), ""

    try:
        output, _ = proc.communicate(input=commands, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        output, _ = proc.communicate()
        return (
            ToolResult(False, "multiwfn", f"Multiwfn 超过 {timeout:.0f}s 未结束，已终止", command),
            output or "",
        )
    ok = proc.returncode == 0
    message = "Multiwfn 执行完成" if ok else f"Multiwfn 退出码 {proc.returncode}"
    return ToolResult(ok, "multiwfn", message, command), output or ""


# -------------------------------------------------------------------- obabel

def convert_with_obabel(src: str | Path, dst: str | Path, timeout: float = 60.0) -> ToolResult:
    """用 obabel 做格式转换（例如 cif → xyz）。"""
    exe, source = _resolve("obabel")
    if not exe:
        return _missing("Open Babel", source)

    source_path = _check_file(src)
    if source_path is None:
        return ToolResult(False, "obabel", f"文件不存在: {src}")

    dest = Path(dst)
    command = [exe, str(source_path), "-O", str(dest)]
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, errors="replace"
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolResult(False, "obabel", f"obabel 调用失败: {exc}", command)

    if dest.is_file():
        return ToolResult(True, "obabel", f"已转换出 {dest.name}", command)
    detail = (proc.stderr or proc.stdout or "").strip()[:200]
    return ToolResult(False, "obabel", f"转换失败: {detail}", command)


# ------------------------------------------------------------------ 文件管理器

def open_in_file_manager(path: str | Path) -> ToolResult:
    """在系统文件管理器里打开目录（或选中文件所在目录）。"""
    target = Path(path)
    folder = target if target.is_dir() else target.parent
    if not folder.is_dir():
        return ToolResult(False, "explorer", f"目录不存在: {folder}")

    try:
        if platform.system() == "Windows":
            if target.is_file():
                subprocess.Popen(["explorer", "/select,", str(target)])
            else:
                os.startfile(str(folder))  # noqa: S606 - Windows 专用
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except OSError as exc:
        return ToolResult(False, "explorer", f"打开失败: {exc}")
    return ToolResult(True, "explorer", f"已打开 {folder}")
