"""分子模型：元素、坐标、电荷、多重度，以及与常见格式的互转。

设计约束：
- 只依赖标准库。引擎层不应被迫依赖 rdkit / numpy。
- **坐标一律以 Å（埃）为规范单位**；从 ORCA 读入的 Bohr 坐标在解析层换算。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path

#: Bohr → Å
BOHR_TO_ANGSTROM = 0.529177210903
#: Å → Bohr
ANGSTROM_TO_BOHR = 1.0 / BOHR_TO_ANGSTROM

#: 坐标的合理上限（Å）。真实分子远小于此；超过说明数据源有问题
#: （NaN / inf / 单位搞错 / 解析错位），应当在构造时就被拦下。
MAX_COORD_ANGSTROM = 1.0e4

#: 两个原子允许的最近距离（Å）。最短的真实化学键是 H₂ 的 0.74 Å，
#: 比这近得多只可能是数据源坏了。
MIN_ATOM_GAP = 0.4

# 索引即原子序数，0 位占位。内嵌以避免引擎层依赖 rdkit。
SYMBOLS: tuple[str, ...] = (
    "", "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne",
    "Na", "Mg", "Al", "Si", "P", "S", "Cl", "Ar", "K", "Ca",
    "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
    "Ga", "Ge", "As", "Se", "Br", "Kr", "Rb", "Sr", "Y", "Zr",
    "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn",
    "Sb", "Te", "I", "Xe", "Cs", "Ba", "La", "Ce", "Pr", "Nd",
    "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb",
    "Lu", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
    "Tl", "Pb", "Bi", "Po", "At", "Rn", "Fr", "Ra", "Ac", "Th",
    "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf", "Es", "Fm",
    "Md", "No", "Lr", "Rf", "Db", "Sg", "Bh", "Hs", "Mt", "Ds",
    "Rg", "Cn", "Nh", "Fl", "Mc", "Lv", "Ts", "Og",
)

_Z_OF: dict[str, int] = {s: z for z, s in enumerate(SYMBOLS) if s}


def symbol_from_z(z: int) -> str:
    """原子序数 → 元素符号。"""
    z = int(z)
    if not 1 <= z < len(SYMBOLS):
        raise ValueError(f"不支持的原子序数: {z}")
    return SYMBOLS[z]


def z_from_symbol(symbol: str) -> int:
    """元素符号 → 原子序数。"""
    key = symbol.strip()
    key = key[:1].upper() + key[1:].lower() if key else key
    if key not in _Z_OF:
        raise ValueError(f"无法识别的元素符号: {symbol!r}")
    return _Z_OF[key]


@dataclass(frozen=True)
class Atom:
    """单个原子，坐标为 Å。"""

    symbol: str
    x: float
    y: float
    z: float

    @property
    def z_number(self) -> int:
        return z_from_symbol(self.symbol)

    def to_xyz_line(self) -> str:
        return f"{self.symbol:<3s} {self.x:>16.10f} {self.y:>16.10f} {self.z:>16.10f}"

    def to_orca_line(self) -> str:
        return f"{self.symbol:<3s} {self.x:>14.8f} {self.y:>14.8f} {self.z:>14.8f}"


@dataclass(frozen=True)
class Molecule:
    """一个分子（或任意原子集合）。不可变。"""

    atoms: tuple[Atom, ...]
    charge: int = 0
    multiplicity: int = 1
    name: str = "mol"

    def __post_init__(self) -> None:
        if not self.atoms:
            raise ValueError("分子至少需要一个原子")
        if self.multiplicity < 1:
            raise ValueError(f"多重度必须 ≥ 1，得到 {self.multiplicity}")
        for index, atom in enumerate(self.atoms):
            # 元素符号必须是认识的：否则会一路带到 ORCA 才报错，用户看不出是哪一步坏的
            z_from_symbol(atom.symbol)
            for value in (atom.x, atom.y, atom.z):
                if not math.isfinite(value):
                    raise ValueError(
                        f"第 {index + 1} 个原子的坐标不是有限数（{value}）"
                    )
                if abs(value) > MAX_COORD_ANGSTROM:
                    raise ValueError(
                        f"第 {index + 1} 个原子的坐标 {value:.3g} 超出合理范围"
                        f"（|坐标| 应小于 {MAX_COORD_ANGSTROM:.0f} Å）"
                    )
        # 原子重叠：最短的真实化学键是 H2 的 0.74 Å，比这近得多只可能是
        # 数据源坏了（例如 RDKit 嵌入在某些高对称分子上塌缩成两个原子重合）。
        # ORCA 遇到这种情况只报 "Zero distance encountered between atoms"，很难排查。
        for i in range(len(self.atoms)):
            xi, yi, zi = self.atoms[i].x, self.atoms[i].y, self.atoms[i].z
            for j in range(i + 1, len(self.atoms)):
                atom = self.atoms[j]
                dx, dy, dz = atom.x - xi, atom.y - yi, atom.z - zi
                if dx * dx + dy * dy + dz * dz < MIN_ATOM_GAP**2:
                    raise ValueError(
                        f"第 {i + 1} 个原子（{self.atoms[i].symbol}）与第 {j + 1} 个原子"
                        f"（{atom.symbol}）几乎重合（相距 < {MIN_ATOM_GAP} Å）"
                    )

    # ---------- 基本信息 ----------

    @property
    def n_atoms(self) -> int:
        return len(self.atoms)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(a.symbol for a in self.atoms)

    @property
    def coords(self) -> tuple[tuple[float, float, float], ...]:
        return tuple((a.x, a.y, a.z) for a in self.atoms)

    @property
    def formula(self) -> str:
        counts: dict[str, int] = {}
        for a in self.atoms:
            counts[a.symbol] = counts.get(a.symbol, 0) + 1
        # 常见有机写法：C、H 在前
        order = sorted(counts, key=lambda s: (s not in ("C", "H"), s))
        return "".join(f"{s}{counts[s] if counts[s] > 1 else ''}" for s in order)

    # ---------- 输出 ----------

    def to_xyz(self, comment: str | None = None) -> str:
        head = self.name if comment is None else comment
        lines = [str(self.n_atoms), head]
        lines.extend(a.to_xyz_line() for a in self.atoms)
        return "\n".join(lines) + "\n"

    def orca_xyz_block(self) -> str:
        """ORCA 输入文件里的内联坐标块。"""
        lines = [f"* xyz {self.charge} {self.multiplicity}"]
        lines.extend(a.to_orca_line() for a in self.atoms)
        lines.append("*")
        return "\n".join(lines)

    # ---------- 读取 ----------

    @classmethod
    def from_xyz(
        cls,
        text: str,
        charge: int = 0,
        multiplicity: int = 1,
        name: str | None = None,
    ) -> "Molecule":
        """解析 XYZ 文本（首行原子数，次行注释，其后为元素+坐标）。

        ``name`` 为 None 时采用注释行（XYZ 约定俗成的分子名），再退回 ``"mol"``。
        """
        lines = text.splitlines()
        i = 0
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines):
            raise ValueError("XYZ 内容为空")

        try:
            n = int(lines[i].split()[0])
        except (ValueError, IndexError) as exc:
            raise ValueError(f"XYZ 第一行应为原子数，实际为 {lines[i]!r}") from exc

        if name is None:
            comment = lines[i + 1].strip() if i + 1 < len(lines) else ""
            name = comment[:60] or "mol"

        atoms: list[Atom] = []
        for line in lines[i + 2 : i + 2 + n]:
            parts = line.split()
            if len(parts) < 4:
                raise ValueError(f"XYZ 原子行格式错误: {line!r}")
            try:
                atoms.append(Atom(parts[0], float(parts[1]), float(parts[2]), float(parts[3])))
            except ValueError as exc:
                raise ValueError(f"XYZ 坐标无法解析: {line!r}") from exc

        if len(atoms) != n:
            raise ValueError(f"XYZ 声明 {n} 个原子，实际读到 {len(atoms)} 个")
        return cls(tuple(atoms), int(charge), int(multiplicity), name)

    @classmethod
    def from_xyz_file(
        cls,
        path: str | Path,
        charge: int = 0,
        multiplicity: int = 1,
        name: str | None = None,
    ) -> "Molecule":
        p = Path(path)
        return cls.from_xyz(
            p.read_text(encoding="utf-8", errors="replace"),
            charge,
            multiplicity,
            name or p.stem,
        )

    @classmethod
    def from_cjson(
        cls,
        path: str | Path,
        charge: int | None = None,
        multiplicity: int | None = None,
        name: str | None = None,
    ) -> "Molecule":
        """读取 cclib/IQmol 风格 cjson —— 旧版 GUI 的主力格式。

        用 ``utf-8-sig`` 读：有些编辑器（如 Windows 记事本）会给 JSON 加 BOM，
        而 ``json.loads`` 遇到 BOM 会直接报 "Unexpected UTF-8 BOM"。
        顺手把 BOM 吃掉，不让用户碰到这种与内容无关的失败。
        """
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        return cls.from_cjson_data(data, charge, multiplicity, name or p.stem)

    @classmethod
    def from_cjson_data(
        cls,
        data: dict,
        charge: int | None = None,
        multiplicity: int | None = None,
        name: str | None = None,
    ) -> "Molecule":
        """从已解析的 cjson 字典构造（不落盘，供 Web 上传等场景使用）。

        ``elements`` 的位置有两种写法，都要认：

        * **cclib chemical JSON 规范**把元素放在 **``atoms.elements.number``**
          （Avogadro 2 导出的就是这个；实测 ``FeH2O.cjson`` 只在这里有）；
        * 早年有些工具把它放在**顶层** ``elements.number``。

        只认后者会让规范文件全部解析失败，而且报错只说"缺少
        atoms.coords.3d / elements.number"，看不出其实是键的位置不同。
        """
        atoms_node = data.get("atoms") if isinstance(data, dict) else None
        coords = None
        numbers = None

        # 坐标：规范在 atoms.coords.3d；也容忍顶层 coords
        for node in (atoms_node, data):
            if isinstance(node, dict):
                c = node.get("coords")
                if isinstance(c, dict) and "3d" in c:
                    coords = c["3d"]
                    break
                if isinstance(c, list):
                    coords = c
                    break

        # 元素：规范在 atoms.elements.number；也容忍顶层 elements
        for node in (atoms_node, data):
            if isinstance(node, dict):
                e = node.get("elements")
                if isinstance(e, dict) and "number" in e:
                    numbers = e["number"]
                    break
                if isinstance(e, list):
                    numbers = e
                    break

        if coords is None or numbers is None:
            # 报错时把"实际看到了什么"带上，省得用户猜
            seen = []
            if isinstance(atoms_node, dict):
                seen.append("atoms." + "/".join(sorted(atoms_node)) if atoms_node else "atoms")
            if isinstance(data, dict):
                seen.append("顶层 " + "/".join(sorted(data)))
            raise ValueError(
                "不是可识别的 cjson（缺少坐标或元素编号）。"
                "需要 atoms.coords.3d 与 atoms.elements.number（cclib 规范），"
                "或顶层 coords / elements。实际看到：" + "；".join(seen)
            )

        numbers = [int(z) for z in numbers]
        coords = [float(v) for v in coords]

        atoms = tuple(
            Atom(symbol_from_z(z), coords[3 * i], coords[3 * i + 1], coords[3 * i + 2])
            for i, z in enumerate(numbers)
            if 3 * i + 2 < len(coords)
        )
        if len(atoms) != len(numbers):
            raise ValueError(
                f"元素数与坐标数不一致：需要 {len(numbers)} 组坐标，"
                f"实际只有 {len(coords) // 3} 组"
            )

        if charge is None:
            charge = _first_int(
                data,
                ("charge",),
                ("properties", "charge"),
                # cclib 规范的写法（Avogadro 2 导出即此）
                ("properties", "totalCharge"),
                ("totalCharge",),
            ) or 0
        if multiplicity is None:
            multiplicity = _first_int(
                data,
                ("multiplicity", "spin_multiplicity"),
                ("properties", "multiplicity"),
                # cclib 规范的写法
                ("properties", "totalSpinMultiplicity"),
                ("totalSpinMultiplicity",),
            ) or 1
        return cls(atoms, int(charge), int(multiplicity), name or "mol")

    @classmethod
    def from_mol(
        cls,
        text: str,
        name: str | None = None,
        charge: int | None = None,
        multiplicity: int | None = None,
    ) -> "Molecule":
        """解析 MDL MOL / SDF 文本（V2000 与 V3000 都支持）。

        * 只取 SDF 的**第一条**记录（多记录时上层会提示用户）；
        * 电荷来自 ``M  CHG`` / V3000 的 ``CHG=``，不是靠猜；
        * 不校验/不使用键信息（本项目的计算只需要坐标与元素）；
        * **二维坐标不做特殊处理** —— 上层应当先用 cheminfo 生成三维，
          因为把 z 全为 0 的平面结构直接送去优化会得到不合理结果。
        """
        from .molfile import parse_mol

        block = parse_mol(text, name=name)
        atoms = tuple(
            Atom(sym, float(c[0]), float(c[1]), float(c[2]))
            for sym, c in zip(block.symbols, block.coords)
        )
        return cls(
            atoms,
            int(block.charge if charge is None else charge),
            int(1 if multiplicity is None else multiplicity),
            (name or block.name or "mol")[:60],
        )

    @classmethod
    def from_mol_file(cls, path: str | Path, **kwargs) -> "Molecule":
        p = Path(path)
        text = p.read_text(encoding="utf-8", errors="replace")
        return cls.from_mol(text, name=kwargs.pop("name", p.stem or "mol"), **kwargs)

    @classmethod
    def from_text(
        cls,
        text: str,
        name: str | None = None,
        charge: int | None = None,
        multiplicity: int | None = None,
    ) -> "Molecule":
        """按内容猜测格式（JSON → cjson，否则按 XYZ）。

        ``charge`` / ``multiplicity`` 为 None 时：cjson 用文件里记录的值，
        纯 XYZ 用默认值（0 / 1）。注意不能把 None 直接透传下去 ——
        ``int(None)`` 会抛 TypeError。
        """
        from .molfile import looks_like_mol

        stripped = text.lstrip()
        if stripped.startswith("{"):
            return cls.from_cjson_data(
                json.loads(text), charge=charge, multiplicity=multiplicity, name=name
            )
        # MOL/SDF：先看文件名扩展名（最可靠），再看内容特征。
        # 注意不能只靠内容判断 —— 某些 XYZ 文件第 4 行也恰好是一串整数。
        suffix = Path(name).suffix.lower() if name else ""
        if suffix in (".mol", ".sdf", ".mdl") or looks_like_mol(text):
            return cls.from_mol(
                text, name=name, charge=charge, multiplicity=multiplicity
            )
        return cls.from_xyz(
            text,
            charge=0 if charge is None else int(charge),
            multiplicity=1 if multiplicity is None else int(multiplicity),
            name=name,
        )

    @classmethod
    def from_file(cls, path: str | Path, **kwargs) -> "Molecule":
        """按扩展名自动选择读取方式。"""
        p = Path(path)
        suffix = p.suffix.lower()
        if suffix in (".cjson", ".json"):
            return cls.from_cjson(p, **kwargs)
        if suffix in (".mol", ".sdf", ".mdl"):
            return cls.from_mol_file(p, **kwargs)
        return cls.from_xyz_file(p, **kwargs)

    # ---------- 变换 ----------

    def with_charge_multiplicity(self, charge: int, multiplicity: int) -> "Molecule":
        return replace(self, charge=int(charge), multiplicity=int(multiplicity))

    def with_atoms(self, atoms: tuple[Atom, ...]) -> "Molecule":
        return replace(self, atoms=atoms)

    def __str__(self) -> str:  # pragma: no cover - 仅用于展示
        return f"<Molecule {self.name} {self.formula} charge={self.charge} mult={self.multiplicity}>"


def _first_int(data: dict, *paths) -> int | None:
    for path in paths:
        node = data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if node is not None:
            try:
                return int(node)
            except (TypeError, ValueError):
                continue
    return None
