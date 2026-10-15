"""轨道成分分析（Multiwfn 菜单 8）的结果模型与解析。

回答的是"**这个轨道主要在哪几个原子上**"—— 光看轨道能级图看不出这个，
而对判断反应位点来说它常常比能级本身更有用。

和键级一样，Multiwfn 没有结构化输出，只能读 stdout；防护措施
（严格解析、整体拒绝、原始输出落盘、黄金文件测试）见
:mod:`qcgui.model.bondorder` 的模块文档。这里沿用同一套规矩。

实测输出（Multiwfn 3.8(dev)，咖啡因 HOMO）::

     Orbital:    51  Energy(a.u.):     -0.319356  Occ:  2.000000  Type: Alpha&Beta
     Printing threshold of absolute value:  >   0.50000 %

     Basis Type    Atom    Shell      Local       Cross term        Total
       20   Z        2(N )   10      3.40768 %      1.20955 %      4.61724 %
       ...
     Composition of each shell
     Shell    10 Type: P    in atom    2(N ) :     4.91595 %
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 支持的分区方式 → （菜单 8 的子菜单号, 名字, 落盘文件名）
ORBITAL_PARTITIONS: dict[str, tuple[str, str, str]] = {
    "mulliken": ("1", "Mulliken 布居", "orbcomp_mulliken.txt"),
    "stout": ("2", "Stout-Politzer 布居", "orbcomp_stout.txt"),
    "scpa": ("3", "Ros-Schuit (SCPA) 布居", "orbcomp_scpa.txt"),
}

#: 轨道选择：Multiwfn 支持 "a"=全部, "h"=HOMO, "l"=LUMO, "h-5", "l+3" ...
ORBITAL_CHOICES: dict[str, tuple[str, str]] = {
    "homo": ("h", "HOMO"),
    "lumo": ("l", "LUMO"),
    "homo-1": ("h-1", "HOMO−1"),
    "lumo+1": ("l+1", "LUMO+1"),
    "all": ("a", "全部轨道"),
}

_HEADER = re.compile(
    r"Orbital:\s*(\d+)\s+Energy\(a\.u\.\):\s*(-?[\d.]+)\s+Occ:\s*(-?[\d.]+)"
)

#: ``Shell    10 Type: P    in atom    2(N ) :     4.91595 %``
_SHELL_ROW = re.compile(
    r"^\s*Shell\s+(\d+)\s+Type:\s*(\w+)\s+in atom\s+(\d+)\s*\(\s*([A-Za-z]{1,3})\s*\)\s*:\s*"
    r"(-?[\d.]+)\s*%"
)


@dataclass
class AtomContribution:
    """某个原子对这个轨道贡献了多少（所有 shell 加总）。"""

    index: int          # 1 起，与 ORCA/Multiwfn 输入一致
    symbol: str
    percent: float
    shells: list[tuple[int, str, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "index": self.index, "symbol": self.symbol,
            "label": f"{self.symbol}{self.index}",
            "percent": round(self.percent, 4),
            "shells": [{"shell": s, "type": t, "percent": round(p, 4)}
                       for s, t, p in self.shells],
        }


@dataclass
class OrbitalComposition:
    """一个轨道的成分分析结果。"""

    orbital_index: int | None = None
    energy_au: float | None = None
    occupancy: float | None = None
    partition: str = "mulliken"
    partition_label: str = ""
    request: str = "homo"
    request_label: str = ""
    threshold: float | None = None
    atoms: list[AtomContribution] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.atoms)

    def sorted_atoms(self) -> list[AtomContribution]:
        return sorted(self.atoms, key=lambda a: a.percent, reverse=True)

    def to_dict(self) -> dict:
        top = self.sorted_atoms()[:3]
        return {
            "orbital_index": self.orbital_index,
            "energy_au": self.energy_au,
            "energy_ev": self.energy_au * 27.211386245988 if self.energy_au is not None else None,
            "occupancy": self.occupancy,
            "partition": self.partition,
            "partition_label": self.partition_label,
            "request": self.request,
            "request_label": self.request_label,
            "threshold": self.threshold,
            "count": len(self.atoms),
            # 最强的几个排在前面 —— "主要在哪几个原子上"就是这个意思
            "atoms": [a.to_dict() for a in self.sorted_atoms()],
            "top_label": "、".join(f"{a.symbol}{a.index} {a.percent:.1f}%" for a in top),
            "problems": list(self.problems),
        }


def parse_orbital_composition(text: str, partition: str = "mulliken",
                              request: str = "homo") -> OrbitalComposition:
    """从 Multiwfn 的 stdout 里解析轨道成分。

    **严格**：至少要见到 ``Orbital:`` 头和 ``Composition of each shell`` 段，
    并且按原子汇总出贡献；否则返回空结果并说明原因 —— 宁可交白卷。
    """
    result = OrbitalComposition(
        partition=partition,
        partition_label=ORBITAL_PARTITIONS.get(partition, (None, partition, None))[1],
        request=request,
        request_label=ORBITAL_CHOICES.get(request, (None, request))[1],
    )
    if not text:
        result.problems.append("Multiwfn 没有输出任何内容")
        return result

    header = _HEADER.search(text)
    if header is None:
        result.problems.append(
            "输出里没有找到轨道信息行（`Orbital: N Energy(a.u.): ... Occ: ...`）—— "
            "可能是 Multiwfn 版本不同，或这个波函数类型不支持")
        return result
    result.orbital_index = int(header.group(1))
    result.energy_au = float(header.group(2))
    result.occupancy = float(header.group(3))

    th = re.search(r"Printing threshold of absolute value:\s*>\s*([\d.]+)\s*%", text)
    if th:
        result.threshold = float(th.group(1))

    # 按原子汇总逐 shell 的贡献。这一段的格式是两个表里最规整的，
    # 而且直接对应"哪个原子贡献多少"，正是界面上要显示的东西。
    per_atom: dict[int, AtomContribution] = {}
    seen_shells = 0
    for line in text.splitlines():
        match = _SHELL_ROW.match(line)
        if match is None:
            continue
        shell, shell_type, atom_index, symbol, percent = match.groups()
        index = int(atom_index)
        entry = per_atom.setdefault(index, AtomContribution(index, symbol, 0.0))
        entry.percent += float(percent)
        entry.shells.append((int(shell), shell_type, float(percent)))
        seen_shells += 1

    if not seen_shells:
        result.problems.append(
            "没有解析出任何原子贡献（缺 'Composition of each shell' 段）—— "
            "格式可能变了。这里宁可交白卷也不给半份数据")
        return result

    result.atoms = list(per_atom.values())
    return result
