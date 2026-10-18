# -*- coding: utf-8 -*-
r"""MOL / SDF 导入的回归测试。

为什么这些测试重要
------------------
导入 .mol 有三处容易出**静默错误**的地方，每一处都值得钉住：

1. **电荷**：MOL 的电荷不在原子行，而在 ``M  CHG`` 行（V3000 是 ``CHG=``）。
   不解析它 → 带电分子被当成中性分子算，界面看不出异常。
2. **二维坐标**：绘图软件导出的 .mol 常常 z 全为 0。直接送去优化会得到
   不合理结构甚至不收敛，所以必须能识别出来并转三维。
3. **缺显式氢**：2D 文件通常只有重原子。缺氢直接算 = 原子数都不对。

另外，第一版测试数据我把苯环所有键都写成双键（化学上等价
``C1=C=C=C=C=C=1``，碳价态已饱和），RDKit 正确地没有补氢 —— 那是我数据错、
不是代码错。这里用**交替单双键**的真实形态。
"""
import math

import pytest

from qcgui.model.molfile import (
    MolParseError,
    looks_like_mol,
    parse_mol,
    split_sdf_records,
)


def mol_text(title, atoms, bonds, extra_lines=(), version_tag="V2000"):
    """按 V2000 定点列宽格式拼 .mol 文本。"""
    out = [title, "  test program", "",
           f"{len(atoms):3d}{len(bonds):3d}  0  0  0  0  0  0  0  0999 {version_tag}"]
    for sym, (x, y, z) in atoms:
        out.append(
            f"{x:10.4f}{y:10.4f}{z:10.4f} {sym:<3s} 0  0  0  0  0  0  0  0  0  0  0  0")
    for a, b, order in bonds:
        out.append(f"{a:3d}{b:3d}{order:3d}  0  0  0  0")
    out.extend(extra_lines)
    out.append("M  END")
    return "\n".join(out) + "\n"


def benzene_2d():
    """2D 苯（交替单双键、无显式氢）—— 典型绘图软件导出形态。"""
    r = 1.39
    atoms = [("C", (r * math.cos(math.radians(60 * k)),
                    r * math.sin(math.radians(60 * k)), 0.0)) for k in range(6)]
    bonds = [(k + 1, ((k + 1) % 6) + 1, 2 if k % 2 == 0 else 1) for k in range(6)]
    return mol_text("benzene", atoms, bonds)


V3000 = """methanol
  test     3D

  0  0  0  0  0  0  0  0  0  0999 V3000
M  V30 BEGIN CTAB
M  V30 COUNTS 2 1 0 0 0
M  V30 BEGIN ATOM
M  V30 1 C 0.000000 0.000000 0.000000 0
M  V30 2 O 1.430000 0.000000 0.000000 0
M  V30 END ATOM
M  V30 BEGIN BOND
M  V30 1 1 1 2
M  V30 END BOND
M  V30 END CTAB
M  END
"""


# ---------------------------------------------------------------- 解析：V2000

def test_parse_v2000_basic():
    block = parse_mol(benzene_2d(), name="benzene")
    assert block.n_atoms == 6
    assert block.n_bonds == 6
    assert block.version == "v2000"
    assert block.symbols == ["C"] * 6
    assert block.name == "benzene"
    # 2D 且没有显式氢：这两个标志决定上层要不要转三维/补氢
    assert block.is_2d is True
    assert block.has_explicit_h is False
    print(f"    V2000 解析：{block.n_atoms} 原子 / {block.n_bonds} 键，二维={block.is_2d}")


def test_parse_reads_charge_from_m_chg():
    """电荷来自 M  CHG 行；漏掉它会把离子当分子算。"""
    text = mol_text("ammonium", [("N", (0.0, 0.0, 0.0))], [],
                    ["M  CHG  1   1   1"])
    block = parse_mol(text)
    assert block.charge == 1, f"电荷应为 +1，实际 {block.charge}"


def test_parse_sums_multiple_m_chg_entries():
    """两个原子的电荷要累加（例如 -1 与 +2 的盐/两性离子）。"""
    text = mol_text("zwitterion",
                    [("N", (0.0, 0.0, 0.0)), ("O", (1.4, 0.0, 0.0))],
                    [(1, 2, 1)],
                    ["M  CHG  2   1   1   2  -1"])
    block = parse_mol(text)
    assert block.charge == 0, f"应为 1 + (-1) = 0，实际 {block.charge}"


def test_counts_line_without_leading_spaces():
    """有些软件不补前导空格：定点列解析失败时要能退回按空白切分。"""
    text = ("no-padding\n  prog\n\n"
            "2 1 0 0 0 0 0 0 0 0999 V2000\n"
            "0.0000 0.0000 0.0000 C 0 0 0 0 0 0 0 0 0 0 0 0\n"
            "1.4300 0.0000 0.0000 O 0 0 0 0 0 0 0 0 0 0 0 0\n"
            "1 2 1 0 0 0 0\nM  END\n")
    block = parse_mol(text)
    assert block.n_atoms == 2
    assert block.symbols == ["C", "O"]


def test_3d_structure_with_hydrogens_is_kept_as_3d():
    """带三维坐标和显式氢的结构要能被识别（上层就不该重建几何）。"""
    text = mol_text("water",
                    [("O", (0.0, 0.0, 0.1173)),
                     ("H", (0.0, 0.7572, -0.4692)),
                     ("H", (0.0, -0.7572, -0.4692))],
                    [(1, 2, 1), (1, 3, 1)])
    block = parse_mol(text)
    assert block.is_2d is False
    assert block.has_explicit_h is True
    assert block.n_atoms == 3


# ---------------------------------------------------------------- 解析：V3000

def test_parse_v3000():
    block = parse_mol(V3000)
    assert block.version == "v3000"
    assert block.symbols == ["C", "O"], block.symbols
    assert block.n_bonds == 1, block.n_bonds
    print("    V3000 解析正常")


def test_parse_v3000_atom_charge():
    """V3000 的电荷写在原子行的 CHG= 属性里。"""
    text = """charged
  test

  0  0  0  0  0  0  0  0  0  0999 V3000
M  V30 BEGIN CTAB
M  V30 COUNTS 1 0 0 0 0
M  V30 BEGIN ATOM
M  V30 1 N 0.000000 0.000000 0.000000 0 CHG=1
M  V30 END ATOM
M  V30 END CTAB
M  END
"""
    assert parse_mol(text).charge == 1


# ---------------------------------------------------------------- SDF

def test_sdf_records_are_split_and_first_is_used():
    sdf = benzene_2d() + "$$$$\n" + V3000 + "$$$$\n"
    assert len(split_sdf_records(sdf)) == 2
    block = parse_mol(sdf)
    assert block.n_records == 2, "要报告一共有几条记录，不能只默默取第一条"
    assert block.n_atoms == 6, "应当取第一条记录（苯）"


# ---------------------------------------------------------------- 错误处理

def test_truncated_atom_block_reports_clear_error():
    """原子数写着 6 但只给 2 个原子：要报清楚，不能崩在别处。"""
    text = ("broken\n  prog\n\n"
            "6 6 0 0 0 0 0 0 0 0999 V2000\n"
            "0.0 0.0 0.0 C 0 0 0 0 0 0 0 0 0 0 0 0\n"
            "1.4 0.0 0.0 C 0 0 0 0 0 0 0 0 0 0 0 0\n"
            "M  END\n")
    with pytest.raises(MolParseError) as exc:
        parse_mol(text)
    assert "原子" in str(exc.value)


def test_query_atom_is_rejected_with_explanation():
    """MOL 的查询原子（A/Q/*/L）无法映射成具体元素，要明确拒绝。"""
    text = ("query\n  prog\n\n"
            "1 0 0 0 0 0 0 0 0 0999 V2000\n"
            "0.0 0.0 0.0 A 0 0 0 0 0 0 0 0 0 0 0 0\n"
            "M  END\n")
    with pytest.raises(MolParseError) as exc:
        parse_mol(text)
    assert "元素" in str(exc.value)


def test_empty_text_is_rejected():
    with pytest.raises(MolParseError):
        parse_mol("   \n\n")


# ---------------------------------------------------------------- 格式猜测

def test_looks_like_mol_recognises_mol_but_not_xyz():
    assert looks_like_mol(benzene_2d()) is True
    assert looks_like_mol(V3000) is True
    xyz = "3\nwater\nO 0 0 0.1173\nH 0 0.7572 -0.4692\nH 0 -0.7572 -0.4692\n"
    assert looks_like_mol(xyz) is False, "XYZ 不能被误判成 MOL"


def test_from_text_dispatches_mol_by_extension_and_content():
    from qcgui.model import Molecule

    # 有扩展名：按 .mol 处理
    m = Molecule.from_text(benzene_2d(), name="benzene.mol")
    assert m.n_atoms == 6
    # 无扩展名但内容像 MOL：也要识别
    m2 = Molecule.from_text(V3000, name="v3000")
    assert m2.n_atoms == 2
    # XYZ 仍然按 XYZ 处理
    m3 = Molecule.from_text("3\nwater\nO 0 0 0\nH 0 0.76 -0.47\nH 0 -0.76 -0.47\n",
                            name="water.xyz")
    assert m3.n_atoms == 3


def test_from_file_loads_mol(tmp_path):
    from qcgui.model import Molecule

    path = tmp_path / "benzene.mol"
    path.write_text(benzene_2d(), encoding="utf-8")
    m = Molecule.from_file(path)
    assert m.n_atoms == 6
    assert m.name == "benzene"
