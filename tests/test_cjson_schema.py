# -*- coding: utf-8 -*-
r"""回归测试：cclib 规范的 cjson（elements 嵌在 atoms 下）必须能读。

实测来源：用户拖入的 ``FeH2O.cjson``（Avogadro 2 导出），结构是
**Fe(H2O)6^2+**，19 个原子，properties.totalCharge=2、
properties.totalSpinMultiplicity=5。

旧代码只认顶层 ``data["elements"]["number"]``，于是：
  1. 直接报"不是可识别的 cjson"，文件根本打不开；
  2. 就算绕过第一条，也会因为不认识 totalCharge/totalSpinMultiplicity
     把电荷+2、多重度5 静默当成 0/1 —— 算出来就是另一个物种。
"""
import json

from qcgui.model import Molecule


def _cclib_style() -> dict:
    """cclib chemical JSON 规范写法：elements 在 atoms 里面。"""
    return {
        "chemicalJson": 1,
        "atoms": {
            "elements": {"number": [26, 8, 1, 1]},
            "coords": {"3d": [0.0, 0.0, 0.0,
                              2.0, 0.0, 0.0,
                              2.9, 0.0, 0.0,
                              2.0, 0.9, 0.0]},
        },
        "properties": {
            "totalCharge": 2,
            "totalSpinMultiplicity": 5,
        },
    }


def test_cjson_with_elements_under_atoms_is_accepted():
    """规范位置（atoms.elements.number）必须认得。"""
    mol = Molecule.from_cjson_data(_cclib_style(), name="cclib")
    assert mol.n_atoms == 4, mol.n_atoms
    assert [a.symbol for a in mol.atoms] == ["Fe", "O", "H", "H"]
    print("    atoms.elements.number 能读，4 个原子")


def test_cjson_reads_total_charge_and_spin_multiplicity():
    """cclib 的 totalCharge / totalSpinMultiplicity 必须生效。

    这是**正确性问题**：Fe(H2O)6^2+ 高自旋 d6（电荷+2、多重度5）与
    中性单重态是完全不同的电子态，静默用 0/1 会算出错误结果。
    """
    mol = Molecule.from_cjson_data(_cclib_style(), name="cclib")
    assert mol.charge == 2, f"电荷被忽略：{mol.charge}"
    assert mol.multiplicity == 5, f"多重度被忽略：{mol.multiplicity}"
    print("    totalCharge=2 / totalSpinMultiplicity=5 都被采纳")


def test_cjson_explicit_charge_overrides_the_file():
    """调用方显式给的值要覆盖文件里的 —— 界面允许用户改。"""
    mol = Molecule.from_cjson_data(_cclib_style(), charge=3, multiplicity=6)
    assert mol.charge == 3
    assert mol.multiplicity == 6
    print("    显式 charge/multiplicity 覆盖文件值")


def test_cjson_legacy_top_level_elements_still_works():
    """顶层 elements 的老写法不能被这次修改弄坏（向后兼容）。"""
    data = {
        "elements": {"number": [6, 1, 1, 1, 1]},
        "atoms": {"coords": {"3d": [0.0, 0.0, 0.0,
                                    1.0, 0.0, 0.0,
                                    -1.0, 0.0, 0.0,
                                    0.0, 1.0, 0.0,
                                    0.0, -1.0, 0.0]}},
    }
    mol = Molecule.from_cjson_data(data)
    assert mol.n_atoms == 5
    assert mol.atoms[0].symbol == "C"
    assert mol.charge == 0 and mol.multiplicity == 1
    print("    老格式（顶层 elements）仍可用")


def test_cjson_error_message_says_what_it_actually_saw():
    """确实不是 cjson 时，报错要说清看到了什么，而不是一句笼统的话。"""
    try:
        Molecule.from_cjson_data({"hello": "world"})
    except ValueError as exc:
        msg = str(exc)
        assert "hello" in msg, f"报错没说明实际内容：{msg}"
        print("    非法 cjson 的报错带上了实际键：", msg[:60])
    else:
        raise AssertionError("应当报错")


def test_cjson_reads_a_bom_prefixed_file(tmp_path):
    """带 BOM 的 cjson 也要能读（记事本保存的 JSON 常带 BOM）。"""
    p = tmp_path / "bom.cjson"
    payload = json.dumps(_cclib_style())
    p.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
    mol = Molecule.from_cjson(p)
    assert mol.n_atoms == 4, mol.n_atoms
    assert mol.charge == 2
    print("    带 BOM 的文件也能读")
