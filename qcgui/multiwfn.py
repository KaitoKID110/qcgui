"""Multiwfn 集成：键级分析。

Multiwfn 是本项目用得上的**外部**分析工具（不带 ORCA 的许可限制），
它补上了当前最缺的一块能力 —— **化学解释**：

* Mayer / Wiberg / Mulliken / 模糊(FBO) / Laplacian(LBO) **键级**
* 轨道成分（Multiken / Stout-Politzer / SCPA 布居）
* 模糊原子空间分析、概念 DFT……

这些**都不需要买 NBO**。本机 3.8(dev) 实测可用。

架构说明（重要）：

* 本模块属于**引擎侧**，只在 ``python -m qcgui.worker`` 子进程里被调用，
  界面进程绝不 import 它（架构第 1 条）。
* Multiwfn **没有结构化输出**，只能读它的 stdout —— 这是本项目
  对"只用结构化输出"的唯一一次例外，理由与防护措施见
  :mod:`qcgui.model.bondorder` 的模块文档。
  这里额外做一件事：**把原始输出原样落盘**，解析结果可追溯。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .external import resolve_tool
from .model.bondorder import (
    BOND_ORDER_KINDS,
    BondOrderResult,
    parse_bond_orders,
)

#: Multiwfn 启动很慢（要读波函数、建积分），给它足够的余量
DEFAULT_TIMEOUT = 900


def multiwfn_available() -> tuple[bool, str]:
    exe, how = resolve_tool("multiwfn")
    if not exe:
        return False, "没有找到 Multiwfn（可设置 QCGUI_MULTIWfn 或放进 PATH）"
    return True, exe


def _menu_script(kind: str) -> str:
    """拼出 Multiwfn 的批处理菜单脚本。

    菜单号是**真跑 Multiwfn 抓主菜单确认**的：
    主菜单 ``9`` = Bond order analysis，子菜单里 ``1`` Mayer、``3`` Wiberg、
    ``4`` Mulliken、``7`` FBO、``8`` LBO。
    最后用 ``q`` 正常退出 —— 不给它 ``q`` 会一直等在输入上，把任务挂死。
    """
    sub = BOND_ORDER_KINDS.get(kind, ("1", "", ""))[0]
    return f"9\n{sub}\nq\n"


def run_bond_order_analysis(
    molden_path: str | Path,
    job_dir: str | Path,
    kind: str = "mayer",
    timeout: int = DEFAULT_TIMEOUT,
) -> BondOrderResult:
    """在某个波函数文件上跑一次键级分析。

    ``molden_path`` 一般来自 :func:`qcgui.orcatools.make_molden`
    （``orca_2mkl -molden`` 生成的 ``.molden.input``）——
    Multiwfn 读不了 ORCA 的 ``.gbw``，必须先转成 molden。

    原始输出落盘到 ``job_dir/bondorder_<kind>.txt``，解析结果可追溯到它。
    """
    molden = Path(molden_path)
    target = Path(job_dir)
    label = BOND_ORDER_KINDS.get(kind, (None, kind, None))[1]
    result = BondOrderResult(kind=kind, kind_label=label)

    exe, how = resolve_tool("multiwfn")
    if not exe:
        result.problems.append(
            "没有找到 Multiwfn —— 键级分析需要它。"
            "把 Multiwfn 放进 PATH，或在设置里填它的路径。")
        return result
    if not molden.is_file():
        result.problems.append(f"找不到波函数文件：{molden.name}（先跑 orca_2mkl -molden）")
        return result

    script = _menu_script(kind)
    try:
        proc = subprocess.run(
            [exe, str(molden)],
            input=script,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(target),
        )
    except subprocess.TimeoutExpired:
        result.problems.append(f"Multiwfn 超过 {timeout} 秒还没结束，已放弃")
        return result
    except OSError as exc:
        result.problems.append(f"启动 Multiwfn 失败：{exc}")
        return result

    output = (proc.stdout or "") + (proc.stderr or "")

    # 原始输出落盘：解析结果必须能追溯回它。写不进去也不致命，
    # 但要如实说一声 —— 免得以后有人以为有留痕却找不到。
    raw_name = BOND_ORDER_KINDS.get(kind, (None, None, f"bondorder_{kind}.txt"))[2]
    try:
        (target / raw_name).write_text(output, encoding="utf-8")
    except OSError as exc:
        result.problems.append(f"原始输出没能落盘（{exc}），下面是从内存里解析的")

    parsed = parse_bond_orders(output, kind=kind)
    parsed.problems = result.problems + parsed.problems
    return parsed
