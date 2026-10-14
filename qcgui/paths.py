"""应用数据目录。

默认放在用户数据区，而不是代码目录 —— 任务产物（含 ORCA 的 .gbw）
可能很大，不应污染仓库。可用 ``QCGUI_HOME`` 覆盖（测试与便携部署用）。
"""

from __future__ import annotations

import os
from pathlib import Path


def data_root() -> Path:
    """应用数据根目录。"""
    override = os.environ.get("QCGUI_HOME")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "qcgui"
    return Path(os.path.expanduser("~")) / ".qcgui"


def jobs_root() -> Path:
    """任务存储根目录。"""
    return data_root() / "jobs"


def ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def search_roots() -> list[str]:
    """可能装着量化工具的目录（**不绑定某一台机器**）。

    ORCA / Multiwfn / Avogadro 都是"解压即用"的绿色包，用户习惯把它们
    直接放在盘符根下（``D:\\orca_6_1_1\\``）或桌面。所以候选里必须有**盘符根**，
    否则换一台机器就探测不到 —— 这正是可移植性最常出问题的地方。

    只返回**确实存在**的目录，避免对不存在的盘枚举浪费时间。
    """
    roots: list[str] = []
    if os.name == "nt":
        for letter in "CDEFGH":
            root = f"{letter}:\\"
            if os.path.isdir(root):
                roots.append(root)
    for var in (r"%LOCALAPPDATA%\Programs", r"%USERPROFILE%", r"%USERPROFILE%\Desktop",
                r"%ProgramFiles%", r"%ProgramFiles(x86)%"):
        expanded = os.path.expandvars(var)
        if expanded and "%" not in expanded and os.path.isdir(expanded):
            roots.append(expanded)
    # 去重且保持顺序
    seen: set[str] = set()
    unique: list[str] = []
    for root in roots:
        key = os.path.normcase(os.path.normpath(root))
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique
