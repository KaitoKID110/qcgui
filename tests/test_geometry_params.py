"""几何参数表：一次列出所有键长 / 键角 / 二面角。

对应功能（GaussView 的 Table 窗口那种）：把所有内坐标列成一张表，
点一行就能在 3D 里定位到原子。这里测的是**数据层** ——
``qcgui.model.geometry.geometry_parameters``；表格的接线由
``tests/test_web_browser.py`` 在真浏览器里验（"数据对了 ≠ 用户看得见"）。

    pytest tests/test_geometry_params.py -v
    python tests/test_geometry_params.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.model.geometry import (  # noqa: E402
    angle,
    bond_set,
    distance,
    geometry_parameters,
    measure,
)

EXAMPLES = REPO / "examples"

# 真实的水分子几何（ORCA 优化过的样子）：O–H 0.9578 Å、H–O–H 104.48°
WATER = {
    "symbols": ["O", "H", "H"],
    "coords": [[0.0, 0.0, 0.1173], [0.0, 0.7572, -0.4692], [0.0, -0.7572, -0.4692]],
}

# 理想正四面体甲烷：C–H 1.0900 Å、H–C–H 109.47°
_T = 1.09 / 3 ** 0.5
METHANE = {
    "symbols": ["C", "H", "H", "H", "H"],
    "coords": [
        [0.0, 0.0, 0.0],
        [_T, _T, _T], [-_T, -_T, _T], [_T, -_T, -_T], [-_T, _T, -_T],
    ],
}

# 过氧化氢：O–O 1.45、两根 O–H 各 0.97，二面角正好 90°
H2O2 = {
    "symbols": ["O", "O", "H", "H"],
    "coords": [[0.0, 0.0, 0.0], [0.0, 0.0, 1.45], [0.97, 0.0, 0.0], [0.0, 0.97, 1.45]],
}


def _read_xyz(path: Path):
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    count = int(lines[0].split()[0])
    symbols, coords = [], []
    for line in lines[2:2 + count]:
        parts = line.split()
        if len(parts) < 4:
            continue
        symbols.append(parts[0])
        coords.append([float(x) for x in parts[1:4]])
    return symbols, coords


def _example_files():
    if not EXAMPLES.is_dir():
        return []
    return sorted(p for p in EXAMPLES.glob("*.xyz") if p.is_file())


# ------------------------------------------------------------------ 小分子

def test_water_has_two_bonds_and_one_angle():
    """水：2 个 O–H（0.96±0.02 Å）+ 1 个 H–O–H（104.5±1°），没有二面角。

    数值和单位都要对 —— 表里显示"0.958 Å"和"104.48 °"才是用户要的东西。
    """
    data = geometry_parameters(WATER["symbols"], WATER["coords"])

    bonds = data["bonds"]
    assert len(bonds) == 2, f"水应当有 2 个键长，得到 {bonds}"
    assert sorted(b["atoms"] for b in bonds) == [[0, 1], [0, 2]], bonds
    for bond in bonds:
        assert abs(bond["value"] - 0.96) < 0.02, bond
        assert bond["unit"] == "Å", bond
    assert bonds[0]["label"] == "O0–H1", bonds[0]

    angles = data["angles"]
    assert len(angles) == 1, f"水应当有 1 个键角，得到 {angles}"
    assert angles[0]["atoms"] == [1, 0, 2], angles[0]
    assert abs(angles[0]["value"] - 104.5) < 1.0, angles[0]
    assert angles[0]["unit"] == "°", angles[0]
    assert angles[0]["label"] == "H1–O0–H2", angles[0]

    assert data["dihedrals"] == [], data["dihedrals"]
    print(f"    水：{len(bonds)} 键长（{bonds[0]['value']:.3f} Å）"
          f" + 1 键角（{angles[0]['value']:.2f}°）")


def test_methane_has_six_tetrahedral_angles():
    """甲烷：4 个键长 + C(4,2)=6 个键角，全部 109.47°±0.5°，没有二面角。"""
    data = geometry_parameters(METHANE["symbols"], METHANE["coords"])

    assert len(data["bonds"]) == 4, data["bonds"]
    for bond in data["bonds"]:
        assert abs(bond["value"] - 1.09) < 0.005, bond

    angles = data["angles"]
    assert len(angles) == 6, f"甲烷应当有 6 个键角，得到 {len(angles)}"
    for entry in angles:
        assert abs(entry["value"] - 109.47) < 0.5, entry
        assert entry["atoms"][1] == 0, f"中心原子应当是碳（下标 0）：{entry}"

    assert data["dihedrals"] == [], "甲烷没有二面角"
    print(f"    甲烷：4 键长 + {len(angles)} 键角，全部 "
          f"{angles[0]['value']:.2f}° ±0.5°")


def test_hydrogen_peroxide_gives_the_dihedral():
    """过氧化氢：3 个键长、2 个键角、**1 个二面角**（H–O–O–H = 90°）。

    二面角是本功能的重点之一，而且这个约定写反过两次（顺式/反式差 180°），
    所以专门用一个"手工算得出来"的构型钉住它。
    """
    data = geometry_parameters(H2O2["symbols"], H2O2["coords"])

    assert len(data["bonds"]) == 3, data["bonds"]
    assert len(data["angles"]) == 2, data["angles"]

    dihedrals = data["dihedrals"]
    assert len(dihedrals) == 1, f"应当只有 1 个二面角，得到 {dihedrals}"
    entry = dihedrals[0]
    assert entry["atoms"] == [2, 0, 1, 3], entry
    assert abs(entry["value"] - 90.0) < 0.01, entry
    assert entry["unit"] == "°", entry
    assert entry["label"] == "H2–O0–O1–H3", entry
    print(f"    过氧化氢：二面角 {entry['label']} = {entry['value']:.2f}°")


def test_cis_and_trans_dihedrals_are_not_swapped():
    """顺式 = 0°、反式 = 180°：写反的话这里会差整整 180°。

    用 O–O–H 骨架（键长 0.97/1.45，两端氢相距 1.45 以上不成键），
    所以每组刚好只有 1 个二面角，测的就是它。
    """
    def skeleton(h3):
        return geometry_parameters(
            ["O", "O", "H", "H"],
            [[0.0, 0.0, 0.0], [0.0, 0.0, 1.45], [0.97, 0.0, 0.0], h3],
        )

    cis = skeleton([0.97, 0.0, 1.45])      # 两端同侧
    trans = skeleton([-0.97, 0.0, 1.45])   # 两端异侧
    assert len(cis["bonds"]) == 3, cis["bonds"]
    assert len(cis["dihedrals"]) == 1 and len(trans["dihedrals"]) == 1, (cis, trans)
    assert abs(cis["dihedrals"][0]["value"]) < 1e-6, cis["dihedrals"]
    assert abs(abs(trans["dihedrals"][0]["value"]) - 180.0) < 1e-6, trans["dihedrals"]
    print("    顺式 0.00° / 反式 180.00°，没有搞反")


def test_ring_dihedral_degeneracy_is_skipped():
    """三元环里"键两侧取到同一个原子"是退化情形，不能当成二面角列出来。"""
    data = geometry_parameters(
        ["C", "C", "C"],
        [[0.0, 0.0, 0.0], [1.5, 0.0, 0.0], [0.75, 1.3, 0.0]],
    )
    assert len(data["bonds"]) == 3 and len(data["angles"]) == 3, data
    assert data["dihedrals"] == [], f"三元环不该有二面角：{data['dihedrals']}"


# ------------------------------------------------------------------ 坏输入

def test_empty_and_single_atom_do_not_crash():
    """空输入 / 单原子 / 原子离得很远：不崩，三组都是空表。

    界面里"还没载入结构"和"载入了一个原子"都是会真实发生的状态，
    这里不能抛异常（否则点开面板就报错）。
    """
    for symbols, coords in (
        ([], []),
        (["C"], [[0.0, 0.0, 0.0]]),
        (["C", "C"], [[0.0, 0.0, 0.0], [6.0, 0.0, 0.0]]),
        (None, None),
    ):
        data = geometry_parameters(symbols, coords)
        assert data == {"bonds": [], "angles": [], "dihedrals": []}, data

    # 原子全部重合：值会退化（角度 0、键长 0），但绝不能抛异常
    degenerate = geometry_parameters(["O", "H", "H"], [[0.0, 0.0, 0.0]] * 3)
    assert isinstance(degenerate["angles"], list), degenerate
    print("    空/单原子/重合原子都不崩，返回空表或退化值")


def test_atom_count_mismatch_is_rejected():
    """原子数与坐标数不一致必须**拒绝**，而不是返回一张错位的表。

    错位的表最坏的地方在于"看着像回事"：编号整体错开一位，
    用户拿去跟 3D 图对，怎么都对不上。
    """
    for symbols, coords in (
        (["C", "H"], [[0.0, 0.0, 0.0]]),
        (["C"], [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]),
        ([], [[0.0, 0.0, 0.0]]),
    ):
        try:
            geometry_parameters(symbols, coords)
        except ValueError as exc:
            assert "不一致" in str(exc), exc
        else:
            raise AssertionError(f"原子数不匹配却没报错：{symbols} / {coords}")

    # 坐标不是三个数字同样要拒绝（不能悄悄按 0 算）
    for bad in ([["a", "b", "c"]], [[1.0, 2.0]], [[1.0, 2.0, 3.0, 4.0]], [["1.0"]]):
        try:
            geometry_parameters(["C"], bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"坏坐标没报错：{bad}")
    print("    原子数不匹配 / 坏坐标一律 ValueError")


# ------------------------------------------------------------------ 表本身

def test_rows_are_sorted_unique_and_labels_match():
    """按值升序、同一组内不重复、下标从 0 起且与 label 对得上。"""
    for name, mol in (("水", WATER), ("甲烷", METHANE), ("过氧化氢", H2O2)):
        data = geometry_parameters(mol["symbols"], mol["coords"])
        for kind in ("bonds", "angles", "dihedrals"):
            rows = data[kind]
            values = [r["value"] for r in rows]
            assert values == sorted(values), f"{name} 的 {kind} 没有按值排序：{values}"
            seen = set()
            for row in rows:
                key = tuple(row["atoms"])
                assert key not in seen, f"{name} 的 {kind} 里有重复行：{row}"
                seen.add(key)
                assert len(set(row["atoms"])) == len(row["atoms"]), f"原子有重复：{row}"
                for index in row["atoms"]:
                    assert 0 <= index < len(mol["symbols"]), f"下标越界：{row}"
                expected = "–".join(f"{mol['symbols'][i]}{i}" for i in row["atoms"])
                assert row["label"] == expected, f"label 不对：{row} vs {expected}"
                assert row["unit"] in ("Å", "°"), row
    print("    三组都按值升序、无重复、label 与 0 起下标一致")


def test_values_equal_the_calibrated_helpers():
    """表里的数必须**就是** measure() 给的那个数（不另写一套数学）。"""
    symbols, coords = H2O2["symbols"], H2O2["coords"]
    data = geometry_parameters(symbols, coords)

    pairs = sorted(bond_set(symbols, coords))
    # 表里是按**值**排的，所以按原子对比较集合（顺序由 bond_set 决定不了表头顺序）
    assert sorted(tuple(b["atoms"]) for b in data["bonds"]) == [tuple(p) for p in pairs], \
        data["bonds"]
    for bond in data["bonds"]:
        i, j = bond["atoms"]
        assert abs(bond["value"] - distance(coords[i], coords[j])) < 1e-12, bond
    for entry in data["angles"]:
        assert abs(entry["value"] - measure(symbols, coords, "A", entry["atoms"])) < 1e-12, entry
    for entry in data["dihedrals"]:
        assert abs(entry["value"] - measure(symbols, coords, "D", entry["atoms"])) < 1e-12, entry

    # 手工验证一个键角：H2–O0–O1（表里按升序存成 (1, 0, 2)）
    got = [e for e in data["angles"] if set(e["atoms"]) == {0, 1, 2}]
    expected = angle(coords[1], coords[0], coords[2])
    assert got and abs(got[0]["value"] - expected) < 1e-12, data["angles"]
    print("    所有值与 bond_set / distance / angle / dihedral / measure 逐一相符")


def test_non_bonded_contacts_are_not_listed():
    """拉开之后的"接触"不是键：不该被列进表里（表只列化学上说得通的量）。"""
    symbols = ["C", "C"]
    data = geometry_parameters(symbols, [[0.0, 0.0, 0.0], [2.5, 0.0, 0.0]])
    assert data == {"bonds": [], "angles": [], "dihedrals": []}, data
    print("    2.5 Å 的 C…C 不算键，表里不出现")


def test_real_example_structures_stay_consistent():
    """用仓库里 examples/ 的真实结构批量回归：不崩，且不变量全部成立。

    自造的几个小分子容易"刚好对"，真实结构（含环、含多重键）更容易暴露
    编号/重复/越界这类问题。
    """
    files = _example_files()
    assert files, f"{EXAMPLES} 里没有可用的 .xyz"
    checked = 0
    for path in files:
        try:
            symbols, coords = _read_xyz(path)
        except (ValueError, IndexError):
            continue
        if len(coords) < 2:
            continue
        checked += 1
        data = geometry_parameters(symbols, coords)
        pairs = bond_set(symbols, coords)
        assert len(data["bonds"]) == len(pairs), f"{path.name}: 键长条数不对"
        for kind in ("bonds", "angles", "dihedrals"):
            rows = data[kind]
            assert [r["value"] for r in rows] == sorted(r["value"] for r in rows), \
                f"{path.name} 的 {kind} 没排序"
            keys = [tuple(r["atoms"]) for r in rows]
            assert len(set(keys)) == len(keys), f"{path.name} 的 {kind} 有重复"
            for row in rows:
                assert all(0 <= i < len(symbols) for i in row["atoms"]), f"{path.name}: 下标越界"
                assert row["unit"] == ("Å" if kind == "bonds" else "°"), row
                if kind == "angles":
                    assert 0.0 <= row["value"] <= 180.0, f"{path.name}: 键角越界 {row}"
                if kind == "dihedrals":
                    assert -180.0 < row["value"] <= 180.0, f"{path.name}: 二面角越界 {row}"
    assert checked >= 3, f"只跑到 {checked} 个真实结构，覆盖太少"
    print(f"    {checked} 个真实结构：条数/排序/下标/范围全部一致")


def test_geometry_module_imports_no_engine():
    """架构铁律：这个模块（以及用它算表的界面进程）不 import 任何量子引擎。

    用独立子进程验，避免被本进程里其它测试已经 import 的引擎模块干扰。
    """
    code = (
        "import sys; import qcgui.model.geometry as g;"
        "print(','.join(sorted(m for m in sys.modules if m.startswith('qcgui.engines'))))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", f"几何模块拉进了引擎：{proc.stdout.strip()}"
    print("    geometry 模块不依赖任何引擎（子进程实测）")


# ------------------------------------------------------------------ 脚本模式

def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = skipped = 0
    for name, func in tests:
        print(f"[ RUN  ] {name}")
        try:
            func()
            print(f"[  OK  ] {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[ FAIL ] {name}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n通过 {passed} / 失败 {failed} / 跳过 {skipped}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_main())
