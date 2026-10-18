"""MDL MOL / SDF 文件解析（纯标准库，不依赖 rdkit）。

为什么单独一个模块
------------------
``molecule.py`` 刻意只依赖标准库（引擎层不该被迫装 rdkit）。MOL 格式的解析
细节不少（V2000 定点列宽 / V3000 线格式 / M CHG 电荷 / SDF 多记录），
放在单独模块里既保持 molecule.py 干净，也方便单独测。

格式要点（实测踩过的坑）
------------------------
* V2000 的 counts 行是**定点列宽**（前 3 位原子数、再 3 位键数），不是空格分隔；
  但有些软件输出不带前导空格，所以固定列解析失败时要能退回按空白切分。
* 电荷不在原子行里，而在 ``M  CHG n idx q idx q ...`` 行（原子编号从 1 开始）；
  V3000 则写在原子行的 ``CHG=`` 属性里。不解析它 → 带电分子会被当成中性分子算，
  而界面上看不出异常。
* 很多 .mol 由绘图软件导出，**坐标是 2D 的（z 全为 0）**。直接把 2D 结构送去
  优化会得到不合理结果甚至不收敛，所以这里要标出 ``is_2d`` 交给上层处理。
* 2D 文件通常也**不含显式氢**（只有重原子）。缺氢直接算 = 原子数都不对。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 真实元素的符号表（来自 molecule.SYMBOLS，索引即原子序数）。
#: **必须用真实元素校验，不能只按"大写字母开头"猜**：MOL 的查询原子
#: ``A``(any) / ``Q`` / ``L`` / ``*`` 都符合那种形状，但它们不是具体元素。
from .molecule import SYMBOLS as _SYMBOLS

_REAL_ELEMENTS = frozenset(s for s in _SYMBOLS if s)

#: 2D 判定阈值：所有原子的 |z| 都小于它就认为是 2D 结构（单位 Å）
FLAT_Z_TOLERANCE = 1e-3


@dataclass
class MolBlock:
    """解析出来的单个分子记录。"""

    symbols: list[str]
    coords: list[tuple[float, float, float]]
    charge: int = 0
    name: str = ""
    #: 版本："v2000" / "v3000"
    version: str = "v2000"
    #: 键的数量（只用于展示/提示，不参与计算）
    n_bonds: int = 0
    #: 是否是二维结构（z 全为 0）
    is_2d: bool = False
    #: 文件里是否有显式氢原子
    has_explicit_h: bool = False
    #: SDF 里一共有几条记录（.mol 恒为 1）
    n_records: int = 1
    #: 解析过程中的提示（给用户看的）
    notes: list[str] = field(default_factory=list)

    @property
    def n_atoms(self) -> int:
        return len(self.symbols)

    def to_xyz(self) -> str:
        """导出为 XYZ 文本（便于调试/落盘）。"""
        lines = [str(self.n_atoms), self.name or "mol"]
        for sym, (x, y, z) in zip(self.symbols, self.coords):
            lines.append(f"{sym} {x:.6f} {y:.6f} {z:.6f}")
        return "\n".join(lines) + "\n"


class MolParseError(ValueError):
    """MOL/SDF 内容不合法。"""


def _looks_like_element(token: str) -> bool:
    """是否是一个**真实元素**符号（区分大小写）。"""
    return (token or "").strip() in _REAL_ELEMENTS


def split_sdf_records(text: str) -> list[str]:
    """把 SDF 按 ``$$$$`` 切成多条 MOL 记录。"""
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    records = [chunk for chunk in body.split("$$$$") if chunk.strip()]
    return records


def parse_mol(text: str, name: str | None = None) -> MolBlock:
    """解析单条 MOL 记录（自动识别 V2000 / V3000）。"""
    if not isinstance(text, str):
        raise MolParseError(f"MOL 内容必须是文本，收到 {type(text).__name__}")
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    records = split_sdf_records(body)
    if not records:
        raise MolParseError("MOL 文件是空的")
    block = records[0]
    n_records = len(records)
    lines = block.split("\n")

    if len(lines) < 4:
        raise MolParseError("MOL 内容不完整：至少需要 4 行（3 行头部 + counts 行）")

    title = lines[0].strip()
    counts_line = lines[3]
    if "V3000" in counts_line.upper() or any("V30" in ln for ln in lines[:8]):
        parsed = _parse_v3000(lines)
        version = "v3000"
    else:
        parsed = _parse_v2000(lines)
        version = "v2000"

    symbols, coords, charge, n_bonds = parsed
    if not symbols:
        raise MolParseError("MOL 里没有原子（原子数为 0）")

    flat = all(abs(c[2]) <= FLAT_Z_TOLERANCE for c in coords)
    notes: list[str] = []
    if flat:
        notes.append("坐标是二维的（所有 z≈0）")
    if not any(s == "H" for s in symbols):
        notes.append("文件里没有显式氢原子")

    return MolBlock(
        symbols=symbols,
        coords=coords,
        charge=int(charge),
        name=(name or title or "mol").strip() or "mol",
        version=version,
        n_bonds=int(n_bonds),
        is_2d=flat,
        has_explicit_h=any(s == "H" for s in symbols),
        n_records=n_records,
        notes=notes,
    )


def _parse_v2000(lines: list[str]) -> tuple[list[str], list[tuple[float, float, float]], int, int]:
    counts = lines[3]
    n_atoms = n_bonds = 0
    # 先按定点列宽解析（标准 MOL）
    try:
        n_atoms = int(counts[0:3])
        n_bonds = int(counts[3:6])
    except (ValueError, IndexError):
        parts = counts.split()
        if len(parts) < 2:
            raise MolParseError(
                f"counts 行读不出原子数/键数: {counts!r}"
            ) from None
        try:
            n_atoms, n_bonds = int(parts[0]), int(parts[1])
        except ValueError:
            raise MolParseError(
                f"counts 行里的原子数/键数不是整数: {counts!r}"
            ) from None

    if n_atoms <= 0:
        raise MolParseError(f"原子数为 {n_atoms}，无法解析")

    symbols: list[str] = []
    coords: list[tuple[float, float, float]] = []
    for i in range(n_atoms):
        idx = 4 + i
        if idx >= len(lines):
            raise MolParseError(
                f"原子数写的是 {n_atoms}，但文件在第 {i + 1} 个原子处就结束了"
            )
        line = lines[idx]
        atom = _parse_v2000_atom(line, i + 1)
        symbols.append(atom[0])
        coords.append(atom[1])

    # 电荷：M  CHG n idx q idx q ...
    charge = 0
    for line in lines[4 + n_atoms:]:
        if line.startswith("M  CHG") or line.startswith("M CHG"):
            parts = line.split()
            # 形如 ['M', 'CHG', 'n', idx, q, ...]
            try:
                count = int(parts[2])
            except (ValueError, IndexError):
                continue
            for k in range(count):
                try:
                    q = int(parts[4 + 2 * k])
                except (ValueError, IndexError):
                    break
                charge += q
        elif line.startswith("M  END") or line.startswith("M END"):
            break

    return symbols, coords, charge, n_bonds


def _parse_v2000_atom(line: str, number: int) -> tuple[str, tuple[float, float, float]]:
    """解析一行 V2000 原子。

    标准格式是定点列：``%10.4f%10.4f%10.4f %-3s``。
    但有些软件不补前导空格，因此定点失败时退回按空白切分。
    """
    # 定点列优先
    if len(line) >= 34:
        try:
            x = float(line[0:10])
            y = float(line[10:20])
            z = float(line[20:30])
            sym = line[31:34].strip()
            if _looks_like_element(sym):
                return sym, (x, y, z)
        except ValueError:
            pass
    parts = line.split()
    if len(parts) < 4:
        raise MolParseError(f"第 {number} 个原子行读不出坐标/元素: {line!r}")
    try:
        x, y, z = float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        raise MolParseError(
            f"第 {number} 个原子行的坐标不是数字: {line!r}"
        ) from None
    sym = parts[3].strip()
    # 同分异构/查询原子（A、Q、*、L）无法映射到具体元素
    if not _looks_like_element(sym):
        raise MolParseError(
            f"第 {number} 个原子的元素符号无法识别: {sym!r}"
            f"（MOL 里的 A/Q/*/L 等查询原子不支持，请先转成具体元素）"
        )
    return sym, (x, y, z)


def _parse_v3000(lines: list[str]) -> tuple[list[str], list[tuple[float, float, float]], int, int]:
    symbols: list[str] = []
    coords: list[tuple[float, float, float]] = []
    charge = 0
    n_bonds = 0
    in_atoms = False
    in_bonds = False
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("M  V30") and not stripped.startswith("M V30"):
            continue
        payload = stripped[stripped.find("V30") + 3:].strip()
        upper = payload.upper()
        if upper.startswith("BEGIN ATOM"):
            in_atoms = True
            continue
        if upper.startswith("END ATOM"):
            in_atoms = False
            continue
        if upper.startswith("BEGIN BOND"):
            in_bonds = True
        if upper.startswith("END BOND"):
            in_bonds = False
        if upper.startswith("COUNTS"):
            parts = payload.split()
            try:
                n_bonds = int(parts[2])
            except (ValueError, IndexError):
                pass
            continue
        if in_bonds:
            n_bonds = max(n_bonds, 0)
            continue
        if not in_atoms:
            continue
        parts = payload.split()
        if len(parts) < 5:
            continue
        # 形如: <idx> <element> <x> <y> <z> [aamap] [prop=value ...]
        sym = parts[1]
        if not _looks_like_element(sym):
            # 不是原子行（可能是属性行），跳过
            continue
        try:
            x, y, z = float(parts[2]), float(parts[3]), float(parts[4])
        except ValueError:
            raise MolParseError(
                f"V3000 原子行坐标不是数字: {stripped!r}"
            ) from None
        symbols.append(sym)
        coords.append((x, y, z))
        for token in parts[5:]:
            if token.upper().startswith("CHG="):
                try:
                    charge += int(token.split("=", 1)[1])
                except ValueError:
                    pass
    if not symbols:
        raise MolParseError("V3000 记录里没找到任何原子（BEGIN ATOM 块为空或格式不认识）")
    return symbols, coords, charge, n_bonds


def looks_like_mol(text: str) -> bool:
    """判断一段文本是不是 MOL/SDF（用于 ``Molecule.from_text`` 的格式猜测）。

    只看特征，不做完整解析：第 4 行形如 counts 行，或者出现 V2000/V3000 标记。
    """
    if not text:
        return False
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    head = body.split("\n")[:6]
    blob = "\n".join(head).upper()
    if "V2000" in blob or "V3000" in blob:
        return True
    if "$$$$" in body:
        return True
    if len(head) >= 4:
        counts = head[3]
        parts = counts.split()
        # counts 行通常是 2~11 个整数
        if 2 <= len(parts) <= 13 and all(re.fullmatch(r"-?\d+", p) for p in parts[:2]):
            return True
    return False
