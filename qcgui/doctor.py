"""首次运行诊断：这台机器能不能跑起来？缺什么？怎么补？

换一台 Windows 机器部署时，最难受的不是报错，而是**报错说不清缺什么**。
所以这里把所有前置条件一次查完，每条都给"下一步该做什么"。

.. important::
   本模块**不 import 任何量化引擎**（架构铁律）。ORCA 的相关信息全部通过
   子进程 ``python -m qcgui.worker --capabilities`` 拿。

用法::

    python main.py --doctor
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

#: 必需与可选依赖：(import 名, pip 名, 是否必需, 缺了会怎样)
DEPENDENCIES: tuple[tuple[str, str, bool, str], ...] = (
    ("flask", "Flask", True, "界面与接口完全无法启动"),
    ("werkzeug", "Werkzeug", True, "同上（Flask 的依赖）"),
    ("rdkit", "rdkit", False, "「从 SMILES 生成结构」不可用；xyz/cjson 文件照常"),
    ("PIL", "Pillow", False, "tools/plot_scan.py 导图片、结果面板的「红外谱图」不可用"),
    ("tkinter", "tkinter", False, "桌面界面 --tk 与启动失败弹窗不可用；网页界面不受影响"),
)

MIN_PYTHON = (3, 10)


class Check:
    """一条检查结果。"""

    def __init__(self, name: str, ok: bool, detail: str, fix: str = "",
                 required: bool = True):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.fix = fix
        self.required = required

    def line(self) -> str:
        mark = "✓" if self.ok else ("✗" if self.required else "·")
        text = f"  [{mark}] {self.name}: {self.detail}"
        if not self.ok and self.fix:
            text += f"\n        → {self.fix}"
        return text


def check_python() -> Check:
    version = sys.version_info
    ok = version[:2] >= MIN_PYTHON
    detail = f"{platform.python_version()} ({'64' if sys.maxsize > 2**32 else '32'} 位) · {sys.executable}"
    fix = ""
    if not ok:
        fix = f"需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 或更新（当前 {platform.python_version()}）"
    elif sys.maxsize <= 2**32:
        fix = ""  # 32 位能跑，但大基组会内存不够，下面会提示
    return Check("Python", ok, detail, fix)


def check_dependencies() -> list[Check]:
    """逐个 import 检查依赖 —— 直接 import 比查元数据更接近真实情况。"""
    out: list[Check] = []
    for module, pip_name, required, consequence in DEPENDENCIES:
        try:
            __import__(module)
            out.append(Check(pip_name, True, "已安装", required=required))
        except Exception as exc:  # noqa: BLE001
            out.append(Check(
                pip_name, False, f"缺失（{type(exc).__name__}）—— {consequence}",
                fix=("标准库缺失，需要装带 tkinter 的 Python 发行版"
                     if module == "tkinter" else f"python -m pip install '{pip_name}'"),
                required=required,
            ))
    return out


def check_engines() -> list[Check]:
    """通过 worker 子进程问引擎是否可用（界面进程不得 import 引擎）。"""
    from .paths import data_root

    out: list[Check] = []
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "qcgui.worker", "--capabilities"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
            cwd=str(Path(__file__).resolve().parents[1]),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except Exception as exc:  # noqa: BLE001
        return [Check("量化引擎", False, f"探测失败：{type(exc).__name__}: {exc}",
                      fix="先用 python -m qcgui.worker --capabilities 手动跑一次看回溯")]

    if proc.returncode != 0:
        return [Check("量化引擎", False, f"worker 退出码 {proc.returncode}",
                      fix=(proc.stderr or proc.stdout or "")[-200:])]

    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return [Check("量化引擎", False, "worker 输出无法解析",
                      fix=(proc.stdout or "")[-200:])]

    for engine in payload.get("engines", []):
        name = engine.get("display_name") or engine.get("name")
        if engine.get("available"):
            detail = engine.get("detail") or engine.get("exe") or ""
            source = engine.get("source") or ""
            out.append(Check(name, True,
                             f"{detail}" + (f"（来源：{source}）" if source else "")))
        else:
            out.append(Check(
                name, False, str(engine.get("detail") or "不可用"),
                fix="装好 ORCA 后，任选一种：① 界面「设置」里填 orca.exe 的完整路径；"
                    "② 把 orca.exe 所在目录加入 PATH；"
                    "③ 设环境变量 QCGUI_ORCA_EXE 指向它",
            ))
    if not out:
        out.append(Check("量化引擎", False, "没有任何已注册的引擎"))
    return out


def check_tools() -> list[Check]:
    """外部工具（Multiwfn / Avogadro / obabel）—— 都只是可选。"""
    from .config import tool_status

    out: list[Check] = []
    for name, info in tool_status().items():
        out.append(Check(
            {"multiwfn": "Multiwfn", "avogadro": "Avogadro2", "obabel": "obabel"}.get(name, name),
            bool(info.get("available")),
            info.get("path") or "未找到（可选，不影响计算）",
            required=False,
        ))
    return out


def check_storage() -> Check:
    """数据目录能不能写 —— 便携部署（U 盘、只读目录）最容易卡在这里。"""
    from .paths import data_root, jobs_root

    root = data_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".qcgui_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:  # noqa: BLE001
        return Check("数据目录", False, f"{root} 不可写（{type(exc).__name__}）",
                     fix="把 QCGUI_HOME 环境变量指到一个可写目录，例如 set QCGUI_HOME=D:\\qcgui_data")
    return Check("数据目录", True, f"{root}（任务存在 {jobs_root()}）")


def run_doctor(stream=None) -> int:
    """跑全套检查并打印。返回 0 = 能算，1 = 有必需项缺失。"""
    stream = stream or sys.stdout
    from .paths import data_root

    print("=" * 72, file=stream)
    print("qcgui 环境诊断", file=stream)
    print(f"  系统: {platform.platform()}", file=stream)
    print(f"  数据目录: {data_root()}", file=stream)
    print("=" * 72, file=stream)

    checks: list[Check] = [check_python()]
    deps = check_dependencies()

    print("\n【运行时】", file=stream)
    for check in checks:
        print(check.line(), file=stream)
    print(check_storage().line(), file=stream)

    print("\n【Python 依赖】", file=stream)
    for check in deps:
        print(check.line(), file=stream)

    print("\n【量化引擎】", file=stream)
    engine_checks = check_engines()
    for check in engine_checks:
        print(check.line(), file=stream)

    print("\n【外部工具（都可选）】", file=stream)
    tool_checks = check_tools()
    for check in tool_checks:
        print(check.line(), file=stream)

    storage = check_storage()
    optional = deps + tool_checks
    blockers = [c for c in checks + deps + engine_checks if not c.ok and c.required]
    if not storage.ok:
        blockers.append(storage)

    print("\n" + "=" * 72, file=stream)
    if blockers:
        print(f"结论：**不能算** —— {len(blockers)} 项必需条件没满足：", file=stream)
        for check in blockers:
            print(f"  - {check.name}: {check.detail}", file=stream)
        print("\n按上面每条后面的 → 处理即可。", file=stream)
        return 1
    missing = [c for c in optional if not c.ok]
    if missing:
        names = "、".join(c.name for c in missing)
        print(f"结论：**可以算**。可选功能缺失：{names}（上面标 · 的项）。", file=stream)
    else:
        print("结论：**全部就绪**。", file=stream)
    return 0
