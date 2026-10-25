"""键连判断与刚性基团（环系）识别 —— 纯标准库，不依赖 rdkit。

为什么需要自己做
----------------
"第一轮把苯基当作刚性"要实现，必须知道**哪些原子构成环**。本项目的
``Molecule`` 只存元素与坐标（不存键），所以这里用共价半径 + 距离做键连判断，
再用图论找环。

实测依据（ORCA 6.1.1，甲苯 C7H8，HF/STO-3G）
---------------------------------------------
* 笛卡尔约束 ``%geom Constraints { C 0:10 C }``：优化后环内 C–C 完全不变
  （漂移 0.0000 Å），环-甲基连接键照常优化（1.5200 → 1.5291 Å）；
* 片段优化（xyz 里标 ``C(1)`` + ``ConstrainFragments { 1 }`` +
  ``ConnectFragments { 1 2 O }``）：环内部刚性、但可整体转动（环原子位移 0.156 Å）。

本模块只负责"找出环系 + 环上氢"，具体约束写法在引擎里生成。
"""
from __future__ import annotations

from dataclasses import dataclass

from .molecule import Molecule

#: 共价半径（Å）。覆盖常见元素；未收录的回退 0.77 Å（接近碳）。
COVALENT_RADII: dict[str, float] = {
    "H": 0.31, "He": 0.28, "Li": 1.28, "Be": 0.96, "B": 0.84, "C": 0.76,
    "N": 0.71, "O": 0.66, "F": 0.57, "Ne": 0.58, "Na": 1.66, "Mg": 1.41,
    "Al": 1.21, "Si": 1.11, "P": 1.07, "S": 1.05, "Cl": 1.02, "Ar": 1.06,
    "K": 2.03, "Ca": 1.76, "Sc": 1.70, "Ti": 1.60, "V": 1.53, "Cr": 1.39,
    "Mn": 1.39, "Fe": 1.32, "Co": 1.26, "Ni": 1.24, "Cu": 1.32, "Zn": 1.22,
    "Ga": 1.22, "Ge": 1.20, "As": 1.19, "Se": 1.20, "Br": 1.20, "Kr": 1.16,
    "Rb": 2.20, "Sr": 1.95, "Y": 1.90, "Zr": 1.75, "Nb": 1.64, "Mo": 1.54,
    "Tc": 1.47, "Ru": 1.46, "Rh": 1.42, "Pd": 1.39, "Ag": 1.45, "Cd": 1.44,
    "In": 1.42, "Sn": 1.39, "Sb": 1.39, "Te": 1.38, "I": 1.39, "Xe": 1.40,
    "Cs": 2.44, "Ba": 2.15, "La": 2.07, "Ce": 2.04, "Hf": 1.75, "Ta": 1.70,
    "W": 1.62, "Re": 1.51, "Os": 1.44, "Ir": 1.41, "Pt": 1.36, "Au": 1.36,
    "Hg": 1.32, "Tl": 1.45, "Pb": 1.46, "Bi": 1.48,
}

#: 键连判断系数：d < k * (r_i + r_j) 视为成键。
#: 1.25 对 C–C/C–H 都合适（苯 C–C 1.39 vs r_C+r_C=1.52 → 比值 0.91）。
_BOND_FACTOR = 1.25

#: 只找"小环"（环大小上限）。苯环 6、萘环 6+6、常见杂环 5~7。
MAX_RING_SIZE = 8


@dataclass
class BondGraph:
    """简单的成键图：``neighbors[i]`` 是与原子 i 成键的原子下标集合。"""

    neighbors: list[set[int]]

    def bonded(self, i: int, j: int) -> bool:
        return j in self.neighbors[i]


def bond_graph(molecule: Molecule) -> BondGraph:
    """用共价半径 + 距离判断成键。"""
    coords = [(a.x, a.y, a.z) for a in molecule.atoms]
    radii = [COVALENT_RADII.get(a.symbol, 0.77) for a in molecule.atoms]
    neighbors: list[set[int]] = [set() for _ in coords]
    for i in range(len(coords)):
        for j in range(i + 1, len(coords)):
            dx = coords[i][0] - coords[j][0]
            dy = coords[i][1] - coords[j][1]
            dz = coords[i][2] - coords[j][2]
            dist2 = dx * dx + dy * dy + dz * dz
            cutoff = _BOND_FACTOR * (radii[i] + radii[j])
            if dist2 <= cutoff * cutoff:
                neighbors[i].add(j)
                neighbors[j].add(i)
    return BondGraph(neighbors)


def find_rings(molecule: Molecule, graph: BondGraph | None = None,
               max_size: int = MAX_RING_SIZE) -> list[tuple[int, ...]]:
    """找出分子里的环（每个环是原子下标的元组，升序，去重）。

    做法：对每个原子做限深 DFS 找回到起点的简单回路，长度不超过 ``max_size``。
    同一组原子只保留一次（用排序后的元组去重），并且**丢弃可分解成更小环的
    大环**（例如萘的 10 元外围不当作一个环）。
    """
    graph = graph or bond_graph(molecule)
    n = len(graph.neighbors)
    found: set[tuple[int, ...]] = set()

    def dfs(start: int, current: int, path: list[int]) -> None:
        if len(path) > max_size:
            return
        for nxt in sorted(graph.neighbors[current]):
            if nxt == start and len(path) >= 3:
                found.add(tuple(sorted(path)))
            elif nxt > start and nxt not in path:   # 只往更大的下标走，避免重复
                dfs(start, nxt, path + [nxt])

    for start in range(n):
        dfs(start, start, [start])

    # 去掉"可被更小环覆盖"的大环：若某个环的所有原子能被若干个更小的环并集覆盖，
    # 那它就是外围大环，不是独立的小环（例如萘的外围 10 元环）。
    rings = sorted(found, key=lambda r: (len(r), r))
    minimal: list[tuple[int, ...]] = []
    for ring in rings:
        atoms = set(ring)
        covered: set[int] = set()
        for small in minimal:
            if set(small) <= atoms:
                covered |= set(small)
        if atoms <= covered:
            continue
        minimal.append(ring)
    return minimal


def ring_groups(molecule: Molecule, graph: BondGraph | None = None) -> list[tuple[int, ...]]:
    """把每个环系（环 + 环上氢）作为一组返回，用于"刚性基团"约束。

    为什么把环上的氢也算进去：只冻结碳骨架而放开 C–H 键，氢会绕 C–C 轴旋出
    环平面，环就不再是刚性的。苯基要刚性，必须连同氢一起冻结。

    边界处理：两个环共享原子时（如萘），按"稠合环系"合并成一个大组 ——
    分成两组会让共享原子被约束两次，ORCA 的约束出现冗余，行为不可预期。
    """
    graph = graph or bond_graph(molecule)
    rings = find_rings(molecule, graph)
    if not rings:
        return []

    # 按共享原子合并成环系（连通分量）
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for ring in rings:
        for atom in ring:
            find(atom)
        for atom in ring[1:]:
            union(ring[0], atom)

    groups: dict[int, set[int]] = {}
    for ring in rings:
        groups.setdefault(find(ring[0]), set()).update(ring)

    # 加上与环原子成键的氢（只加 H —— 重原子取代基属于柔性部分，不应冻结）
    result: list[tuple[int, ...]] = []
    for root, atoms in groups.items():
        extended = set(atoms)
        for atom in atoms:
            for nbr in graph.neighbors[atom]:
                if molecule.atoms[nbr].symbol == "H":
                    extended.add(nbr)
        result.append(tuple(sorted(extended)))
    return sorted(result, key=lambda g: g[0])


def frozen_atoms_for_rings(molecule: Molecule) -> tuple[int, ...]:
    """返回"应当冻结"的原子下标（0 基）—— 所有环系的并集。"""
    frozen: set[int] = set()
    for group in ring_groups(molecule):
        frozen.update(group)
    return tuple(sorted(frozen))
