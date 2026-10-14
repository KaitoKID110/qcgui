"""ORCA 配套工具（`orca_plot` / `orca_2mkl` / `orca_2json`）。

为什么不放在 ``engines/`` 里：这些是**后处理工具**，不是计算引擎本身。
界面层需要在已有的 ``.gbw`` 上补导 cube、molden，如果为此 import 引擎模块，
就破坏了"界面进程不 import 任何量化引擎"这条线。所以单独一层，两边都能用。

这里也是**全项目唯一**决定"ORCA 装在哪"的地方。
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: 环境变量：显式指定 orca 可执行文件
ENV_ORCA_EXE = "QCGUI_ORCA_EXE"

#: ORCA 官方安装脚本会设的环境变量
ENV_ORCA_DIR = "ORCA_DIR"


def _search_roots() -> list[str]:
    """可能装着 ORCA 的目录（实现见 :func:`qcgui.paths.search_roots`）。"""
    from .paths import search_roots

    return search_roots()


def _orca_globs() -> list[str]:
    """候选路径模式。

    ``orca*`` / ``ORCA*`` 覆盖 ``orca_6_1_1``、``ORCA61``、``orca`` 这些常见命名；
    ``Desktop`` 是很多人解压安装包的地方。
    """
    patterns: list[str] = []
    for root in _search_roots():
        for name in ("orca*", "ORCA*", "orca", "ORCA"):
            patterns.append(os.path.join(root, name, "orca.exe"))
        patterns.append(os.path.join(root, "Programs", "ORCA*", "orca.exe"))
        patterns.append(os.path.join(root, "Desktop", "orca*", "orca.exe"))
        patterns.append(os.path.join(root, "Desktop", "ORCA*", "orca.exe"))
    return patterns


def configured_orca_path() -> str:
    """配置文件里填的 ORCA 路径（没填就返回空串）。

    单独抽出来是为了让诊断信息能说清"这个路径是从哪来的"——
    对可移植性来说，"为什么没找到 ORCA"比"没找到"重要得多。
    """
    try:
        from .config import load_config

        value = load_config().get("orca_path") or ""
        return str(value).strip()
    except Exception:  # noqa: BLE001 - 配置坏了不该让整个程序起不来
        return ""


def orca_executable() -> str | None:
    """定位 orca 可执行文件。

    顺序：环境变量 → 配置文件 → PATH → 常见安装位置。

    配置文件排在 PATH 前面，是为了让"我在界面里指了路径"这件事一定生效 ——
    否则机器上碰巧有另一个 ORCA 在 PATH 里时，用户会以为设置没保存。
    """
    explicit = os.environ.get(ENV_ORCA_EXE)
    if explicit and Path(explicit).is_file():
        return str(Path(explicit))

    configured = configured_orca_path()
    if configured and Path(configured).is_file():
        return str(Path(configured))

    found = shutil.which("orca") or shutil.which("orca.exe")
    if found:
        return found

    # ORCA 官方 shell 脚本会设 ORCA_DIR 指向安装目录
    orca_dir = os.environ.get(ENV_ORCA_DIR)
    if orca_dir:
        candidate = Path(orca_dir) / "orca.exe"
        if candidate.is_file():
            return str(candidate)

    for pattern in _orca_globs():
        for match in sorted(glob.glob(pattern)):
            if Path(match).is_file():
                return match
    return None


def orca_source() -> str:
    """``orca_executable()`` 的结果是从哪儿来的（诊断用）。"""
    explicit = os.environ.get(ENV_ORCA_EXE)
    if explicit and Path(explicit).is_file():
        return f"环境变量 {ENV_ORCA_EXE}"
    configured = configured_orca_path()
    if configured and Path(configured).is_file():
        return "设置里填的路径"
    if shutil.which("orca") or shutil.which("orca.exe"):
        return "PATH"
    orca_dir = os.environ.get(ENV_ORCA_DIR)
    if orca_dir and (Path(orca_dir) / "orca.exe").is_file():
        return f"环境变量 {ENV_ORCA_DIR}"
    if orca_executable():
        return "自动探测（常见安装位置）"
    return "未找到"



def find_tool(name: str) -> str | None:
    """定位 ORCA 的配套工具：优先与 orca 同目录，其次 PATH。"""
    exe = orca_executable()
    if exe:
        suffix = ".exe" if os.name == "nt" else ""
        for candidate in (Path(exe).with_name(name + suffix),
                          Path(exe).with_name(name.capitalize() + suffix)):
            if candidate.is_file():
                return str(candidate)
    return shutil.which(name) or shutil.which(name + ".exe")


@dataclass
class ToolRun:
    """一次外部工具调用的结果。"""

    ok: bool
    tool: str
    message: str
    command: list[str] = field(default_factory=list)
    output: str = ""
    artifacts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "message": self.message,
            "command": self.command,
            "artifacts": self.artifacts,
        }


def _run(command: list[str], cwd: Path, stdin_text: str = "", timeout: float = 600.0):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        command,
        cwd=str(cwd),
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


# ---------------------------------------------------------------- 轨道 cube

def mo_cube_name(gbw: Path, mo_index: int, operator: int = 0) -> str:
    """orca_plot 的命名规则：``<gbw 基名>.mo<编号><a|b>.cube``。"""
    return f"{gbw.stem}.mo{mo_index}{'a' if operator == 0 else 'b'}.cube"


def export_mo_cube(
    job_dir: str | Path,
    gbw: str | Path,
    mo_index: int,
    operator: int = 0,
    grid: int = 50,
    timeout: float = 600.0,
) -> ToolRun:
    """把一个分子轨道导成 cube 文件（基于已有的 .gbw，不重跑 SCF）。

    ``orca_plot`` 是交互式程序，这里喂命令序列：
    2=轨道号 → 3=自旋 → 4=网格点数 → 11=生成 → 12=退出。
    实测序列正确时它不打印任何错误并直接产出 cube；
    若喂入非法输入它会**死循环**，所以每个数字都必须有效（超时兜底）。
    """
    exe = find_tool("orca_plot")
    if not exe:
        return ToolRun(False, "orca_plot", "找不到 orca_plot（orca 安装目录下应自带）")

    job_dir = Path(job_dir)
    gbw_path = Path(gbw)
    if not gbw_path.is_file():
        gbw_path = job_dir / Path(gbw).name
    if not gbw_path.is_file():
        return ToolRun(False, "orca_plot", f"找不到波函数文件: {gbw}")

    grid = max(20, min(int(grid), 200))
    commands = f"2\n{int(mo_index)}\n3\n{int(operator)}\n4\n{grid}\n11\n12\n"
    command = [exe, gbw_path.name, "-i"]
    try:
        proc = _run(command, job_dir, commands, timeout)
    except subprocess.TimeoutExpired:
        return ToolRun(False, "orca_plot", f"orca_plot 超过 {timeout:.0f}s 未结束（可能输入未被接受）", command)

    produced = job_dir / mo_cube_name(gbw_path, int(mo_index), int(operator))
    output = (proc.stdout or "") + (proc.stderr or "")

    # orca_plot 对越界编号会报 Invalid MO，但**仍然留下一个（无意义的）文件**，
    # 所以不能只看文件在不在 —— 必须查它自己说的话。
    if "Invalid MO requested" in output:
        return ToolRun(
            False, "orca_plot",
            f"轨道编号 {mo_index} 越界（orca_plot 报 Invalid MO）",
            command, output[-600:],
        )

    if produced.is_file() and produced.stat().st_size > 0:
        return ToolRun(
            True, "orca_plot",
            f"已生成轨道 {mo_index} 的 cube（{produced.stat().st_size / 1048576:.1f} MB，网格 {grid}³）",
            command, output[-2000:], [str(produced)],
        )
    tail = output[-800:]
    return ToolRun(False, "orca_plot", f"未生成 cube 文件。输出末尾：{tail}", command)


# ---------------------------------------------------------------- Molden

def make_molden(job_dir: str | Path, gbw: str | Path, timeout: float = 300.0) -> ToolRun:
    """用 ``orca_2mkl`` 把 .gbw 转成 Molden，供 Multiwfn 等外部程序读取。"""
    exe = find_tool("orca_2mkl")
    if not exe:
        return ToolRun(False, "orca_2mkl", "找不到 orca_2mkl")

    job_dir = Path(job_dir)
    gbw_path = Path(gbw)
    if not gbw_path.is_file():
        gbw_path = job_dir / Path(gbw).name
    if not gbw_path.is_file():
        return ToolRun(False, "orca_2mkl", f"找不到波函数文件: {gbw}")

    # orca_2mkl 要的是不带 .gbw 后缀的基名 —— 传全名它会拼成 xxx.gbw.gbw
    base = gbw_path.stem
    command = [exe, base, "-molden"]
    try:
        proc = _run(command, job_dir, "", timeout)
    except subprocess.TimeoutExpired:
        return ToolRun(False, "orca_2mkl", "orca_2mkl 超时", command)

    candidates = [
        job_dir / (base + ".molden.input"),
        job_dir / (base + ".molden"),
    ]
    for path in candidates:
        if path.is_file() and path.stat().st_size > 0:
            return ToolRun(True, "orca_2mkl", f"已生成 {path.name}", command,
                           (proc.stdout or "")[-1000:], [str(path)])

    found = sorted(job_dir.glob("*.molden*"))
    if found:
        return ToolRun(True, "orca_2mkl", f"已生成 {found[0].name}", command,
                       (proc.stdout or "")[-1000:], [str(found[0])])
    return ToolRun(False, "orca_2mkl",
                   f"未找到 Molden 文件。输出末尾：{((proc.stdout or '') + (proc.stderr or ''))[-600:]}",
                   command)


# ---------------------------------------------------------------- 自检

def probe() -> dict:
    """报告 ORCA 及其配套工具是否就位。"""
    exe = orca_executable()
    tools = {name: find_tool(name) for name in ("orca_plot", "orca_2mkl", "orca_2json")}
    return {
        "orca": exe,
        "tools": tools,
        "ok": bool(exe) and all(tools.values()),
        "python": sys.executable,
    }
