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

#: explorer.exe 在受限令牌下 DLL 初始化失败时的退出码（STATUS_DLL_INIT_FAILED）。
#: 实测本机 "打开结果文件夹" 一按就弹 "explorer.exe - 应用程序错误
#: (0xc0000142)"，窗口不出现 —— 必须识别这个码才能兜底。
_EXPLORER_DLL_INIT_FAILED = 0xC0000142

#: 等 explorer 交接给已有窗口的宽限时间（秒）。
#: 正常机器上 explorer.exe <dir> 会把请求转给已经常驻的 shell 进程然后退出，
#: 通常远小于这个值；只有真的崩了才会立刻带着错误码退出。
_EXPLORER_GRACE = 2.5


def _explorer_failed(code: int | None) -> bool:
    """explorer 的退出码是不是"起了但崩了"。

    只认 0xC0000142 这一种：正常交接退出是 0 或 1（1 = 交给已有实例），
    不能把它们当失败 —— 否则正常机器上每次都会误走兜底。
    """
    if code is None:
        return False
    return (code & 0xFFFFFFFF) == _EXPLORER_DLL_INIT_FAILED


def _open_with_explorer(folder: Path, select: Path | None = None):
    """用 explorer 打开目录（或选中文件）；失败返回 False。

    直接执行真实动作再看退出码，不做"能不能跑"的猜测 ——
    之前用 ``explorer /?`` 探测是错的：explorer 会真的开窗口然后挂住，
    超时被误判成"可用"，于是静默失败。
    """
    args = ["explorer"]
    if select is not None:
        args += ["/select,", str(select)]
    else:
        args += [str(folder)]
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return False
    try:
        code = proc.wait(timeout=_EXPLORER_GRACE)
    except subprocess.TimeoutExpired:
        # 还在跑 = 窗口开起来了，正常
        return True
    # 已经退出：只有 DLL 初始化失败才算失败
    return not _explorer_failed(code)


def browse_folder_tk(path: str | Path, title: str = "结果文件") -> ToolResult:
    """用自带的 Tk 浏览器打开目录（explorer.exe 不可用时的兜底）。

    必须用**独立进程**启动，不能在本进程里 tk.Tk() + mainloop()：
    tkinter 的事件循环要求在主线程，而 qcgui 的调用点在 Flask 的请求线程里，
    直接建窗口会**静默失败**（实测：接口返回 ok，窗口根本不出现）。

    所以这里起 ``qcgui_browse.py`` 的新进程 —— 它的主线程天然就是 Tk 线程。
    """
    folder = Path(path)
    if not folder.is_dir():
        return ToolResult(False, "explorer", f"目录不存在: {folder}")

    browser = Path(__file__).with_name("qcgui_browse.py")
    if not browser.is_file():
        return ToolResult(False, "explorer", f"缺少浏览器脚本: {browser}")

    # 用 pythonw（GUI 子系统）启动，避免多一个控制台窗口；找不到就退回 python
    exe = Path(sys.executable)
    if exe.name.lower().startswith("python"):
        pyw = exe.with_name("pythonw.exe")
        if pyw.is_file():
            exe = pyw

    try:
        proc = subprocess.Popen(
            [str(exe), str(browser), str(folder), title],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        return ToolResult(False, "explorer",
                          f"无法启动自带浏览器: {type(exc).__name__}: {exc}")

    return ToolResult(True, "explorer", f"已打开 {folder}", pid=proc.pid)


def open_in_file_manager(path: str | Path) -> ToolResult:
    """打开目录（或选中文件所在目录）。

    默认用**自带浏览器**（``qcgui_browse.py``），因为它在这个环境里稳定可用。
    想用系统资源管理器时，在 ``config.json`` 里设 ``"file_manager": "explorer"``。

    为什么默认不是 explorer.exe：实测本机由服务进程启动 explorer 会以
    ``0xC0000142``（DLL 初始化失败）退出并弹"应用程序错误"，窗口不出现；
    而同一个 explorer.exe 由非受限进程启动是正常的。这跟路径、权限都无关，
    是进程创建环境的限制 —— 与其让用户每次看到错误框，不如用自带的那套。
    另外自带浏览器不依赖 shell 集成，远程桌面/无桌面会话里也能列出文件。
    """
    target = Path(path)
    folder = target if target.is_dir() else target.parent
    if not folder.is_dir():
        return ToolResult(False, "explorer", f"目录不存在: {folder}")

    mode = ""
    try:
        from .config import load_config as _load_cfg
        mode = str((_load_cfg() or {}).get("file_manager") or "").strip().lower()
    except Exception:                    # noqa: BLE001 - 读不到配置就按默认走
        mode = ""

    system = platform.system()
    if system == "Windows" and mode != "explorer":
        # 默认路径：自带浏览器（独立进程，不依赖 shell 集成）
        return browse_folder_tk(folder)

    if system == "Windows":
        # 显式要求用系统资源管理器
        select = target if target.is_file() else None
        if _open_with_explorer(folder, select):
            return ToolResult(True, "explorer", f"已打开 {folder}")
        # 资源管理器起不来 → 兜底到自带浏览器，别让按钮点了没反应
        return browse_folder_tk(folder)

    try:
        if system == "Darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except OSError as exc:
        return ToolResult(False, "explorer", f"打开失败: {exc}")
    return ToolResult(True, "explorer", f"已打开 {folder}")
