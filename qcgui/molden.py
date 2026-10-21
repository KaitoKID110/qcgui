"""修复 ORCA 生成的 Molden 文件里的核电荷，供 Multiwfn 正确读取。

为什么需要
----------
Multiwfn 加载 molden 时按 ``Σ([Atoms] 第三列) − 总电子数`` 判断分子净电荷。
用到 ECP（赝势）时，ORCA 的 ``orca_2mkl -molden`` 在 ``[Atoms]`` 里写的是
**完整核电荷**，而 molden 里的电子数只有**价电子** —— 于是 Multiwfn 算出
一个巨大的净电荷并报警：

    The net charge of this system is quite large (61)!
    Probably ECP is employed while you forgot to modify the atomic indices
    in [atoms] field of the .molden file to actual nuclear charges...

实测（Au 配合物，61 原子）：ΣZ=309、NEL=248、真实电荷 +1 →
309−248=61，正好是 Au 的 60 个芯电子 + 1。把 Au 的第三列从 79 改成 19
（79−60）后：249−248=+1，与真实电荷一致。

所以修法是：**把用了 ECP 的元素的第三列改成有效核电荷（Z − 芯电子数）**。
芯电子数从 ORCA 的 .out 里读（``ECP PARAMETER INFORMATION`` 段明确写着
"replacing 60 core electrons"），不靠硬编码的元素表 —— 不同 ECP/基组
替换的芯电子数不同，猜错了反而更糟。

安全性
------
* 找不到 .out 或读不到 ECP 信息 → **不改动**文件，只在报告里说明；
* 改完后做一致性校验：``Σ(新第三列) − NEL`` 必须等于 ORCA 报告的真实电荷，
  不相等就不写文件（宁可报错，也不给 Multiwfn 一个"看起来正常其实错"的输入）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: ORCA 输出里 ECP 定义行，例如：
#: ``Group 1, Type Au ECP Def2-ECP (replacing 60 core electrons, lmax=3)``
_ECP_LINE = re.compile(
    r"Type\s+([A-Z][a-z]?)\s+ECP\s+\S+\s*\(\s*replacing\s+(\d+)\s+core\s+electron",
    re.IGNORECASE)

#: 电子数。ORCA 写作 ``Number of Electrons    NEL             ....  248``
_NEL_LINE = re.compile(
    r"Number\s+of\s+Electrons\s+NEL\s*\.{2,}\s*(-?\d+)", re.IGNORECASE)

#: 分子总电荷。ORCA 写作 ``Total Charge           Charge          ....    1``
_CHARGE_LINE = re.compile(
    r"Total\s+Charge\s+Charge\s*\.{2,}\s*(-?\d+)", re.IGNORECASE)


@dataclass
class MoldenMismatch:
    """一句"这个 molden 的核电荷列和电子数对不上"的诊断。"""

    sum_charges: int
    n_electrons: int
    implied_charge: int
    real_charge: int | None = None

    def describe(self) -> str:
        real = "" if self.real_charge is None else f"，真实电荷 {self.real_charge:+d}"
        return (f"Σ核电荷 {self.sum_charges} − 电子数 {self.n_electrons} = "
                f"净电荷 {self.implied_charge:+d}{real}")


@dataclass
class RepairReport:
    """修复结果。``changed`` 为 False 时文件应保持原样。"""

    changed: bool = False
    path: str = ""
    ecp_cores: dict[str, int] = field(default_factory=dict)
    n_electrons: int | None = None
    real_charge: int | None = None
    before: MoldenMismatch | None = None
    after: MoldenMismatch | None = None
    notes: list[str] = field(default_factory=list)

    def message(self) -> str:
        if not self.changed:
            return "未修改：" + "；".join(self.notes or ["无需修复"])
        detail = ""
        if self.before and self.after:
            detail = f"（{self.before.describe()} → {self.after.describe()}）"
        ecp = "、".join(f"{el} 去掉 {n} 个芯电子"
                        for el, n in sorted(self.ecp_cores.items()))
        return f"Molden 核电荷已按 ECP 修正：{ecp}{detail}"


def parse_ecp_cores(out_text: str) -> dict[str, int]:
    """从 ORCA .out 里读出「元素 → 被 ECP 替换的芯电子数」。

    ORCA 在 basis 段会打印每个 ECP 的型号与替换芯电子数，例如：
        Group 1, Type Au ECP Def2-ECP (replacing 60 core electrons, lmax=3)
    同一种元素出现多次也没关系（去重）。
    """
    cores: dict[str, int] = {}
    for match in _ECP_LINE.finditer(out_text or ""):
        element = match.group(1)
        try:
            n = int(match.group(2))
        except ValueError:
            continue
        if n > 0:
            cores[element] = n
    return cores


def parse_electron_info(out_text: str) -> tuple[int | None, int | None]:
    """读出 (总电子数, 分子电荷)。读不到返回 (None, None) 或部分 None。"""
    text = out_text or ""
    nel = None
    charge = None
    match = _NEL_LINE.search(text)
    if match:
        try:
            nel = int(match.group(1))
        except ValueError:
            pass
    match = _CHARGE_LINE.search(text)
    if match:
        try:
            charge = int(match.group(1))
        except ValueError:
            pass
    return nel, charge


def parse_molden_atoms(text: str) -> tuple[list[list[str]], int, int]:
    """解析 molden 的 ``[Atoms]`` 段。

    返回 ``(原子行按空白切分后的列表, 段起始行号, 段结束行号)``。
    ``[Atoms]`` 每一行格式：``元素 序号 核电荷 x y z``。
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("[atoms]"):
            start = i
            break
    if start is None:
        return [], -1, -1
    rows: list[list[str]] = []
    end = start + 1
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("["):
            end = i
            break
        if not stripped:
            end = i + 1
            continue
        rows.append(stripped.split())
        end = i + 1
    return rows, start, end


def sum_nuclear_charges(rows: list[list[str]]) -> int:
    """``[Atoms]`` 第三列之和（Multiwfn 就是用这个算净电荷的）。"""
    total = 0
    for row in rows:
        if len(row) < 3:
            continue
        try:
            total += int(float(row[2]))
        except ValueError:
            continue
    return total


def diagnose_molden(text: str, n_electrons: int | None,
                    real_charge: int | None) -> MoldenMismatch | None:
    """核电荷总和与电子数是否对得上真实电荷。对得上就返回 None。"""
    rows, _, _ = parse_molden_atoms(text)
    if not rows or n_electrons is None:
        return None
    total = sum_nuclear_charges(rows)
    implied = total - int(n_electrons)
    if real_charge is not None and implied == int(real_charge):
        return None
    return MoldenMismatch(total, int(n_electrons), implied, real_charge)


def repair_molden_text(text: str, ecp_cores: dict[str, int],
                       n_electrons: int | None = None,
                       real_charge: int | None = None) -> tuple[str, RepairReport]:
    """把 ECP 元素的核电荷列改成有效核电荷，返回 ``(新文本, 报告)``。

    ``ecp_cores`` 为空、或没有 ``[Atoms]``、或改完仍对不上真实电荷时，
    **原文本原样返回**（``report.changed`` 为 False）—— 宁可不动，
    也不要把文件改成一个"看起来正常其实不对"的状态。
    """
    report = RepairReport(ecp_cores=dict(ecp_cores or {}),
                         n_electrons=n_electrons, real_charge=real_charge)
    if not ecp_cores:
        report.notes.append("没有 ECP 信息（该计算可能没用赝势，不需要修）")
        return text, report

    rows, start, end = parse_molden_atoms(text)
    if not rows or start < 0:
        report.notes.append("molden 里没有 [Atoms] 段")
        return text, report

    lines = text.splitlines()
    report.before = diagnose_molden(text, n_electrons, real_charge)

    new_rows: list[list[str]] = []
    touched: dict[str, int] = {}
    for row in rows:
        row = list(row)
        if len(row) >= 3 and row[0] in ecp_cores:
            try:
                z = int(float(row[2]))
            except ValueError:
                z = None
            if z is not None:
                z_eff = z - int(ecp_cores[row[0]])
                if z_eff > 0:
                    row[2] = str(z_eff)
                    touched[row[0]] = touched.get(row[0], 0) + 1
        new_rows.append(row)

    if not touched:
        report.notes.append("molden 里没有出现 ORCA 报告的那些 ECP 元素")
        return text, report

    # 只替换**需要改的行**，其它行连空白都不动。
    # （早先的写法把整个 [Atoms] 用 "  ".join() 重排，导致未修改的原子行空白
    #   也变了，产出无谓的 diff，也不利于人工对照。）
    new_lines = list(lines)
    row_iter = iter(new_rows)
    for idx in range(start + 1, min(end, len(new_lines)) + 1):
        if idx >= len(new_lines):
            break
        if not new_lines[idx].strip():
            continue
        try:
            row = next(row_iter)
        except StopIteration:
            break
        if len(row) < 3 or row[0] not in ecp_cores:
            continue          # 不动这一行
        # 原位替换第 3 列，保留原有空白/格式
        match = re.match(r"^(\s*\S+\s+\S+\s+)(\S+)(.*)$", new_lines[idx])
        if match:
            new_lines[idx] = match.group(1) + row[2] + match.group(3)

    repaired = "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")

    # 一致性校验：改完必须与真实电荷吻合，否则不写
    report.after = diagnose_molden(repaired, n_electrons, real_charge)
    if report.after is not None:
        report.notes.append(
            "修正后核电荷与真实电荷仍不一致，出于安全考虑不改动文件："
            + report.after.describe())
        return text, report

    report.changed = True
    return repaired, report


def repair_molden_file(molden_path: str | Path,
                       out_path: str | Path | None = None,
                       write: bool = True) -> RepairReport:
    """修复磁盘上的 molden 文件（默认原地覆盖）。

    ``out_path`` 默认取同目录下的 ``*.out``（ORCA 输出）——
    ECP 与电子数信息都从那里读，这是最权威的来源。
    """
    path = Path(molden_path)
    report = RepairReport(path=str(path))
    if not path.is_file():
        report.notes.append(f"文件不存在: {path}")
        return report
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report.notes.append(f"读不了文件: {exc}")
        return report

    outer_notes: list[str] = []
    out_file = Path(out_path) if out_path else next(
        iter(sorted(path.parent.glob("*.out"))), None)
    out_text = ""
    if out_file and Path(out_file).is_file():
        try:
            out_text = Path(out_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            out_text = ""
    else:
        outer_notes.append(
            "找不到 ORCA 的 .out（ECP 芯电子数与真实电荷都从那里读），"
            "因此无法判断是否需要修复，文件保持原样")

    ecp_cores = parse_ecp_cores(out_text)
    nel, charge = parse_electron_info(out_text)

    # 幂等：已经与真实电荷一致就早退。
    # 否则对修过的文件再减一次芯电子（Au 19−60 < 0 会被跳过，靠运气才不出错），
    # 这类"减两次"的错误在没有校验时是静默的。
    if nel is not None:
        existing = diagnose_molden(text, nel, charge)
        if existing is None:
            report.n_electrons = nel
            report.real_charge = charge
            report.notes.append("核电荷与电子数已经一致，无需修复")
            return report

    repaired, report = repair_molden_text(text, ecp_cores, nel, charge)
    # 把外层收集到的说明（例如"找不到 .out"）并进报告，别被新报告覆盖掉
    report.notes = list(outer_notes) + list(report.notes)
    report.path = str(path)
    if report.changed and write:
        try:
            # 备份一次原文件，方便对照/回退
            backup = path.with_suffix(path.suffix + ".orig")
            if not backup.exists():
                backup.write_text(text, encoding="utf-8")
            path.write_text(repaired, encoding="utf-8")
        except OSError as exc:
            report.changed = False
            report.notes.append(f"写回失败: {exc}")
    return report
