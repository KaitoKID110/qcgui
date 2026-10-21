# -*- coding: utf-8 -*-
r"""Molden 核电荷修复（ECP 赝势）的回归测试。

问题回顾
--------
含 ECP 的体系（如 Au 用 def2-ECP），ORCA 的 ``orca_2mkl -molden`` 在
``[Atoms]`` 里写**完整核电荷**，但 molden 里的电子数只有**价电子**。
Multiwfn 按 ``Σ核电荷 − 电子数`` 判断净电荷，于是实测报：

    The net charge of this system is quite large ( 61)!
    Probably ECP is employed while you forgot to modify the atomic indices
    in [atoms] field of the .molden file to actual nuclear charges...

实测数字（Au 配合物 61 原子）：ΣZ=309、NEL=248、真实电荷 +1 → 309−248=61。
把 Au 的第三列改成 19（79−60）后：249−248=+1，Multiwfn 报告净电荷 1
且警告消失（用 Multiwfn 真跑验证过）。

这些测试钉住三点：
1. 能从 ORCA .out 正确读出 ECP 元素与芯电子数、电子数、真实电荷；
2. 修复后 ``Σ核电荷 − NEL`` 等于真实电荷；
3. **对不上就绝不写文件**（宁可不修，也不给 Multiwfn 一个看似正常的错输入）；
4. 幂等：对已修好的文件再跑一次不会减第二次。
"""
import io

from qcgui.molden import (
    diagnose_molden,
    parse_ecp_cores,
    parse_electron_info,
    parse_molden_atoms,
    repair_molden_file,
    repair_molden_text,
    sum_nuclear_charges,
)

OUT_SNIPPET = """
-----------------
BASIS SET IN INPUT FORMAT
-----------------
Group 1, Type Au ECP Def2-ECP (replacing 60 core electrons, lmax=3)
  Atom   1Au   ECP group =>   1

Total Charge           Charge          ....    1
Number of Electrons    NEL             ....  248
"""


def molden_text(atom_lines):
    return (
        "[Molden Format]\n"
        "[Title]\n"
        " test\n"
        "[Atoms] AU\n"
        + "".join(atom_lines)
        + "[GTO]\n1 0\n"
    )


def au_complex_molden():
    """简化版 Au 配合物：Au + 2 N + 2 C，Au 用完整核电荷 79。"""
    return molden_text([
        "  Au   1   79      0.000000     0.000000     0.000000\n",
        "  N    2    7      1.900000     0.000000     0.000000\n",
        "  N    3    7     -1.900000     0.000000     0.000000\n",
        "  C    4    6      0.000000     1.500000     0.000000\n",
        "  C    5    6      0.000000    -1.500000     0.000000\n",
    ])


#: 上面这个简化体系：ΣZ = 79+7+7+6+6 = 105；NEL 对应 +1 电荷 → 104
def _nel_for(charge=1):
    return 105 - 60 - charge     # Au 去掉 60 个芯电子


# ---------------------------------------------------------------- 解析

def test_parse_ecp_cores_from_orca_out():
    cores = parse_ecp_cores(OUT_SNIPPET)
    assert cores == {"Au": 60}, cores


def test_parse_electron_info_from_orca_out():
    nel, charge = parse_electron_info(OUT_SNIPPET)
    assert nel == 248, nel
    assert charge == 1, charge


def test_bare_out_without_ecp_gives_empty_cores():
    """没 ECP 的输出不该凭空给出芯电子数。"""
    plain = "Number of Electrons    NEL             ....   42\n"
    assert parse_ecp_cores(plain) == {}
    assert parse_electron_info(plain) == (42, None)


def test_parse_molden_atoms_and_sum():
    rows, start, end = parse_molden_atoms(au_complex_molden())
    assert start >= 0 and end > start
    assert len(rows) == 5
    assert sum_nuclear_charges(rows) == 105


# ---------------------------------------------------------------- 诊断

def test_diagnose_reports_the_large_charge_mismatch():
    """正是用户看到的那个症状：Σ核电荷 − 电子数 = 61。"""
    mismatch = diagnose_molden(au_complex_molden(), _nel_for(1), 1)
    assert mismatch is not None
    assert mismatch.implied_charge == 61, mismatch.implied_charge
    print(f"    诊断出 {mismatch.describe()}")


def test_diagnose_is_clean_for_non_ecp_systems():
    """普通体系（无 ECP）不该报问题。"""
    water = molden_text([
        "  O    1    8      0.000000     0.000000     0.117300\n",
        "  H    2    1      0.000000     0.757200    -0.469200\n",
        "  H    3    1      0.000000    -0.757200    -0.469200\n",
    ])
    assert diagnose_molden(water, 10, 0) is None


# ---------------------------------------------------------------- 修复

def test_repair_subtracts_core_electrons_and_matches_real_charge():
    repaired, report = repair_molden_text(
        au_complex_molden(), {"Au": 60}, _nel_for(1), 1)
    assert report.changed is True
    rows, _, _ = parse_molden_atoms(repaired)
    au_row = next(r for r in rows if r[0] == "Au")
    assert au_row[2] == "19", f"Au 应改成 79−60=19，实际 {au_row[2]}"
    # 其它元素不动
    assert sum_nuclear_charges(rows) == 105 - 60
    assert sum_nuclear_charges(rows) - _nel_for(1) == 1
    assert diagnose_molden(repaired, _nel_for(1), 1) is None
    print(f"    {report.message()}")


def test_repair_keeps_other_lines_untouched():
    original = au_complex_molden()
    repaired, report = repair_molden_text(original, {"Au": 60}, _nel_for(1), 1)
    assert report.changed
    # 行数不变、非原子行内容不变
    assert len(repaired.splitlines()) == len(original.splitlines())
    for before, after in zip(original.splitlines(), repaired.splitlines()):
        if before.strip().startswith("Au"):
            continue
        assert before == after, f"不该改动这一行: {before!r}"


def test_repair_refuses_when_still_inconsistent():
    """改完仍对不上真实电荷 → 不写（宁可不修，也不给错的输入）。"""
    # 故意给错误的 ECP 芯电子数（应为 60，给 20）
    repaired, report = repair_molden_text(
        au_complex_molden(), {"Au": 20}, _nel_for(1), 1)
    assert report.changed is False
    assert repaired == au_complex_molden()
    assert any("仍不一致" in n for n in report.notes), report.notes


def test_repair_without_ecp_does_nothing():
    water = molden_text([
        "  O    1    8      0.000000     0.000000     0.117300\n",
        "  H    2    1      0.000000     0.757200    -0.469200\n",
    ])
    repaired, report = repair_molden_text(water, {}, 10, 0)
    assert report.changed is False
    assert repaired == water


def test_repair_file_is_idempotent(tmp_path):
    """对已修好的文件再跑一次不会再减一次芯电子（幂等）。"""
    molden = tmp_path / "x.molden.input"
    molden.write_text(au_complex_molden(), encoding="utf-8")
    out = tmp_path / "x.out"
    out.write_text(
        "Group 1, Type Au ECP Def2-ECP (replacing 60 core electrons, lmax=3)\n"
        f"Total Charge           Charge          ....    1\n"
        f"Number of Electrons    NEL             ....  {_nel_for(1)}\n",
        encoding="utf-8")

    first = repair_molden_file(molden)
    assert first.changed is True
    content_after_first = molden.read_text(encoding="utf-8")

    second = repair_molden_file(molden)
    assert second.changed is False, "第二次运行不该再改文件"
    assert "已经一致" in " ".join(second.notes), second.notes
    assert molden.read_text(encoding="utf-8") == content_after_first
    # 备份文件应保留原始内容
    backup = molden.with_suffix(molden.suffix + ".orig")
    assert backup.is_file()
    assert "Au   1   79" in backup.read_text(encoding="utf-8")
    print("    幂等 + 保留 .orig 备份")


def test_repair_file_without_out_cannot_diagnose(tmp_path):
    """没有 ORCA .out 就读不到 ECP/电子数 —— 不能瞎改。"""
    molden = tmp_path / "x.molden.input"
    molden.write_text(au_complex_molden(), encoding="utf-8")
    report = repair_molden_file(molden)
    assert report.changed is False
    assert any(".out" in n for n in report.notes), report.notes
