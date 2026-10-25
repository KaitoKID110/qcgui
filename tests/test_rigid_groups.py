# -*- coding: utf-8 -*-
r"""刚性基团冻结（第一轮刚性近似 → 第二轮放开）的回归测试。

实测依据（ORCA 6.1.1，甲苯 C7H8，HF/STO-3G）
--------------------------------------------
* 笛卡尔约束 ``%geom Constraints { C 0:10 C }`` 让环内 C–C 完全不变
  （漂移 0.0000 Å，冻结原子位移 3.4e-7 Å），环-甲基连接键照常优化；
* 两阶段（冻结 9 圈 58.1 s → 放开 4 圈 28.4 s，合计 86.4 s）
  与单次普通优化（11 圈 90.4 s）耗时相当，略快约 4%。

这些测试钉住"能不能正确冻住"以及"不会冻错原子"：
1. 环识别（苯/甲苯/萘/链状分子）；
2. 环上的氢必须一起冻结（只冻碳骨架的话，氢会转出去，环就不刚了）；
3. 引擎生成的约束文本正确（连续下标用范围写法，不连续要分段）；
4. 越界下标必须报错，不能悄悄生成非法输入。
"""
import math

import pytest

from qcgui.engines.orca import OrcaEngine
from qcgui.model import JobSpec, JobType, Molecule
from qcgui.model.perceive import (
    bond_graph,
    find_rings,
    frozen_atoms_for_rings,
    ring_groups,
)


def _mol(symbols, coords, name="mol"):
    text = f"{len(symbols)}\n{name}\n" + "\n".join(
        f"{s} {c[0]:.6f} {c[1]:.6f} {c[2]:.6f}" for s, c in zip(symbols, coords))
    return Molecule.from_xyz(text + "\n")


def benzene() -> Molecule:
    r = 1.39
    cs = [(r * math.cos(math.radians(60 * k)),
           r * math.sin(math.radians(60 * k)), 0.0) for k in range(6)]
    hs = [((r + 1.087) * math.cos(math.radians(60 * k)),
           (r + 1.087) * math.sin(math.radians(60 * k)), 0.0) for k in range(6)]
    return _mol(["C"] * 6 + ["H"] * 6, cs + hs, "benzene")


def toluene() -> Molecule:
    """C0 接甲基，C1..C5 带 H（15 原子）。"""
    r, rcc = 1.39, 1.52
    cs = [(r * math.cos(math.radians(60 * k)),
           r * math.sin(math.radians(60 * k)), 0.0) for k in range(6)]
    hs = [((r + 1.087) * math.cos(math.radians(60 * k)),
           (r + 1.087) * math.sin(math.radians(60 * k)), 0.0) for k in range(1, 6)]
    cm = (r + rcc, 0.0, 0.0)
    mh = []
    for dz in (0.89, -0.89):
        mh.append((cm[0] - 0.42, cm[1], dz))
    mh.append((cm[0] + 1.09, cm[1], 0.0))
    # 符号顺序必须与坐标顺序一致：6 个环 C、5 个环 H、1 个甲基 C、3 个甲基 H
    symbols = ["C"] * 6 + ["H"] * 5 + ["C"] + ["H"] * 3
    return _mol(symbols, cs + hs + [cm] + mh, "toluene")


def naphthalene() -> Molecule:
    """C10H8：两个稠合苯环（用正十边形近似环骨架，够用于环识别）。"""
    r = 1.41
    cs = [(r * math.cos(math.radians(36 * k)),
           r * math.sin(math.radians(36 * k)), 0.0) for k in range(10)]
    hs = [((r + 1.08) * math.cos(math.radians(36 * k)),
           (r + 1.08) * math.sin(math.radians(36 * k)), 0.0)
          for k in (0, 2, 4, 6, 8)]
    hs += [((r + 1.08) * math.cos(math.radians(36 * k)),
            (r + 1.08) * math.sin(math.radians(36 * k)), 0.0)
           for k in (1, 3, 5, 7)]
    return _mol(["C"] * 10 + ["H"] * 8, cs + hs, "naphthalene")


# ---------------------------------------------------------------- 键连 / 环识别

def test_bond_graph_on_benzene():
    graph = bond_graph(benzene())
    # 每个碳：2 个碳邻居 + 1 个氢邻居
    for i in range(6):
        assert len(graph.neighbors[i]) == 3, (i, graph.neighbors[i])
    # 每个氢只连一个碳
    for i in range(6, 12):
        assert len(graph.neighbors[i]) == 1


def test_benzene_ring_and_hydrogens_frozen():
    mol = benzene()
    assert find_rings(mol) == [(0, 1, 2, 3, 4, 5)]
    frozen = frozen_atoms_for_rings(mol)
    assert frozen == tuple(range(12)), frozen
    print(f"    苯：冻结 {len(frozen)} 个原子（环 + 环上氢）")


def test_toluene_freezes_ring_but_not_methyl():
    """甲苯：冻结环+环上氢（0..10），甲基碳和甲基氢不冻。"""
    mol = toluene()
    frozen = frozen_atoms_for_rings(mol)
    assert frozen == tuple(range(11)), frozen
    assert 11 not in frozen, "甲基碳不该被冻结（它是柔性部分）"
    assert 12 not in frozen and 13 not in frozen and 14 not in frozen
    print(f"    甲苯：冻结 {frozen}，甲基（11..14）保持自由")


def test_naphthalene_fused_rings_merge_into_one_group():
    """稠合环要合并成一组：分两组会让共享原子被约束两次。"""
    mol = naphthalene()
    groups = ring_groups(mol)
    assert len(groups) == 1, groups
    assert len(groups[0]) == 18, len(groups[0])
    print(f"    萘：{len(groups)} 个环系 / {len(groups[0])} 个原子")


def test_chain_molecule_has_no_rings():
    """链状分子没有环 → 不冻结任何原子（不能凭空冻）。"""
    coords = [(i * 1.54, 0.0, 0.0) for i in range(6)]
    mol = _mol(["C"] * 6, coords, "hexane")
    assert find_rings(mol) == []
    assert frozen_atoms_for_rings(mol) == ()


# ---------------------------------------------------------------- 引擎生成的片段优化

def _spec(mol, groups=(), frozen=()):
    return JobSpec(molecule=mol, job_type=JobType.OPT, method="HF",
                   basis="STO-3G", rigid_groups=tuple(groups),
                   frozen_atoms=tuple(frozen))


def test_engine_labels_xyz_with_fragment_numbers():
    """坐标块里每个原子都要带片段号：C(1) … H(1) … C(2) …"""
    mol = benzene()
    text = OrcaEngine().build_input_text(_spec(mol, [tuple(range(12))]))
    xyz_block = text[text.index("* xyz"):]
    for idx, atom in enumerate(mol.atoms):
        expected_prefix = f"{atom.symbol}("
        assert expected_prefix in xyz_block, xyz_block[:200]
    assert "C(1)" in xyz_block or "H(1)" in xyz_block
    print("    坐标块带片段标签")


def test_engine_uses_fragment_optimization_not_cartesian():
    """必须是片段优化（ConstrainFragments/ConnectFragments），不是 { C i:j C }。"""
    mol = toluene()
    groups = ring_groups(mol)           # 苯基（含 H）
    text = OrcaEngine().build_input_text(_spec(mol, groups))
    assert "ConstrainFragments { 1 }" in text, text
    assert "ConnectFragments" in text
    assert "{ C 0:" not in text, "不该再生成笛卡尔固定约束"
    # 连接项：片段 1 连到自由片段 2，并优化连接坐标（O）
    connect = text[text.index("ConnectFragments"):]
    assert "{ 1 2 O" in connect, connect[:200]
    print("    使用片段优化：ConstrainFragments + ConnectFragments(O)")


def test_free_atoms_form_their_own_fragment():
    """没被指定为刚性片段的原子自成一个片段（ORCA 要求所有原子都属于片段）。"""
    mol = toluene()
    ring = tuple(ring_groups(mol)[0])
    text = OrcaEngine().build_input_text(_spec(mol, [ring]))
    xyz_block = text[text.index("* xyz"):]
    # 甲基 4 个原子（11..14）应标为片段 2
    for idx in range(11, 15):
        atom = mol.atoms[idx]
        line = next(ln for ln in xyz_block.splitlines()
                    if ln.startswith(f"{atom.symbol}(2)"))
        assert line, f"原子 {idx} 没有片段 2 标签"
    print("    自由原子标为片段 2")


def test_two_rigid_groups_are_both_constrained():
    """两个刚性片段：ConstrainFragments 要同时列 1 和 2。"""
    mol = toluene()
    ring = tuple(ring_groups(mol)[0])
    methyl = tuple(range(11, 15))
    text = OrcaEngine().build_input_text(_spec(mol, [ring, methyl]))
    assert "ConstrainFragments { 1 } { 2 }" in text, text
    print("    两个片段都被约束")


def test_no_rigid_groups_means_no_fragment_block():
    text = OrcaEngine().build_input_text(_spec(benzene()))
    assert "ConstrainFragments" not in text
    assert "* xyz 0 1" in text and "C(1)" not in text


def test_out_of_range_rigid_group_is_rejected():
    with pytest.raises(ValueError) as exc:
        OrcaEngine().build_input_text(_spec(benzene(), [(0, 1, 99)]))
    assert "越界" in str(exc.value)


def test_overlapping_rigid_groups_are_rejected():
    """同一原子不能属于两个刚性片段（会被约束两次）。"""
    with pytest.raises(ValueError) as exc:
        OrcaEngine().build_input_text(_spec(benzene(), [(0, 1, 2), (2, 3, 4)]))
    assert "重叠" in str(exc.value)


def test_cartesian_and_fragment_modes_cannot_be_mixed():
    with pytest.raises(ValueError) as exc:
        OrcaEngine().build_input_text(
            _spec(benzene(), [(0, 1, 2)], frozen=(5, 6)))
    assert "不能同时" in str(exc.value)


def test_jobspec_roundtrip_keeps_rigid_groups():
    spec = _spec(benzene(), [(0, 1, 2), (5, 6)])
    data = spec.to_dict()
    assert data["rigid_groups"] == [[0, 1, 2], [5, 6]]
    back = JobSpec.from_dict(data)
    assert back.rigid_groups == ((0, 1, 2), (5, 6))
    print("    rigid_groups 往返正确")


def test_fragments_are_connected_along_real_bonds():
    """片段连接要沿着真实成键，而不是随便挑最近的一对（影响初始 Hessian 质量）。"""
    mol = toluene()
    ring = tuple(ring_groups(mol)[0])
    text = OrcaEngine().build_input_text(_spec(mol, [ring]))
    connect = text[text.index("ConnectFragments"):text.index("end", text.index("ConnectFragments"))]
    # 环上 C0(0) 与甲基 C(11) 是真实成键，应被选为连接原子对
    assert "0 11" in connect, connect
    print(f"    连接原子对取自真实键：{connect.strip()}")

def p_xylene() -> Molecule:
    """对二甲苯：苯环 + 两个甲基（自由部分是**两个互不相连**的分量）。"""
    r, rch, rcc = 1.39, 1.087, 1.52
    ring = [("C", (r * math.cos(math.radians(60 * k)),
                   r * math.sin(math.radians(60 * k)), 0.0)) for k in range(6)]
    # 只有 C1..C4 带 H（C0、C3 接甲基）
    ring_h = [("H", ((r + rch) * math.cos(math.radians(60 * k)),
                     (r + rch) * math.sin(math.radians(60 * k)), 0.0))
              for k in (1, 2, 4, 5)]
    atoms = ring + ring_h
    for sign in (+1, -1):
        base = (sign * (r + rcc), 0.0, 0.0)
        atoms.append(("C", base))
        for dz in (0.89, -0.89):
            atoms.append(("H", (base[0], 0.0, dz)))
        atoms.append(("H", (base[0] + sign * 1.09, 0.0, 0.0)))
    symbols = ["C"] * 6 + ["H"] * 4 + ["C", "H", "H", "H"] + ["C", "H", "H", "H"]
    return _mol(symbols, [c for _s, c in atoms], "p-xylene")


def test_free_atoms_split_into_connected_components():
    """两个甲基互不相连 → 必须是两个独立片段（否则 ORCA 建内坐标会失败）。"""
    mol = p_xylene()
    groups = ring_groups(mol)
    engine = OrcaEngine()
    labels, _geom = engine._rigid_fragment_setup(mol, groups)
    from collections import Counter
    counts = Counter(labels.values())
    # 片段 1 = 环（6C + 4H = 10），剩下两个甲基各 4 个原子 → 片段 2、3
    assert counts[1] == 10, counts
    assert sorted(v for k, v in counts.items() if k != 1) == [4, 4], counts
    assert len(counts) == 3, counts
    print(f"    片段划分: {dict(sorted(counts.items()))}（环 + 两个甲基）")


def test_connect_fragments_covers_every_fragment():
    """ConnectFragments 必须把所有片段连成生成树（片段数-1 条连接）。"""
    mol = p_xylene()
    groups = ring_groups(mol)
    text = OrcaEngine().build_input_text(_spec(mol, groups))
    block = text[text.index("ConnectFragments"):]
    block = block[:block.index("end")]
    edges = [ln for ln in block.splitlines() if "{" in ln]
    assert len(edges) == 2, f"3 个片段应有 2 条连接，实际 {edges}"
    print(f"    连接: {[e.strip() for e in edges]}")
