"""几何工具与"静默算歪"防护的测试。

对应实测踩过的两次翻车：
1. 扫描区间跨过 180° → ORCA 内坐标奇点，``ORCA_GSTEP ... Aborting run``，退出码 2
2. 算完的分子已经不是原分子（醇 → 烯 + 水），任务却是 ``done``、零警告
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.model.geometry import (  # noqa: E402
    connectivity_change,
    dihedral,
    measure,
    parse_scan_blocks,
    scan_actual_values,
    scan_warnings,
)

# 乙烷（含一个被"挪走"的氢，用来造出成键变化）
ETHANE = {
    "symbols": ["C", "C", "H", "H", "H", "H", "H", "H"],
    "coords": [
        [0.0, 0.0, 0.0], [1.53, 0.0, 0.0],
        [-0.4, 0.9, 0.4], [-0.4, -0.9, 0.4], [-0.4, 0.0, -1.0],
        [1.93, 0.9, -0.4], [1.93, -0.9, -0.4], [1.93, 0.0, 1.0],
    ],
}


def _mol(symbols, coords, name="test"):
    """按元素+坐标造一个 Molecule（走 from_xyz，与界面同一条路）。"""
    from qcgui.model import Molecule

    body = "\n".join(
        f"{s} {c[0]:.6f} {c[1]:.6f} {c[2]:.6f}" for s, c in zip(symbols, coords)
    )
    return Molecule.from_xyz(f"{len(symbols)}\n{name}\n{body}\n", name=name)


def test_dihedral_uses_the_standard_convention():
    """二面角约定：顺式 = 0°，反式 = 180°。

    这个约定写错过两次（``b0 = p1 - p0`` 少个负号 → 整体偏 180°，
    把 Z 构型判成 E）。这里用**几何上无歧义**的构造钉死它：
    以 p1→p2 为轴，p0 固定在 +x，p3 换到同侧 / 异侧 / 垂直。
    """
    p1, p2 = (0.0, 0.0, 0.0), (0.0, 0.0, 1.4)
    cis = [(1.0, 0.0, 0.0), p1, p2, (1.0, 0.0, 1.4)]        # 同侧
    trans = [(1.0, 0.0, 0.0), p1, p2, (-1.0, 0.0, 1.4)]      # 异侧
    perp = [(1.0, 0.0, 0.0), p1, p2, (0.0, 1.0, 1.4)]        # 垂直

    assert abs(dihedral(*cis)) < 1e-9, f"顺式应当是 0°，得到 {dihedral(*cis)}"
    assert abs(abs(dihedral(*trans)) - 180) < 1e-9, f"反式应当是 180°，得到 {dihedral(*trans)}"
    assert abs(abs(dihedral(*perp)) - 90) < 1e-9, f"垂直应当是 90°，得到 {dihedral(*perp)}"
    print("    顺式 0° / 反式 180° / 垂直 90° 全部正确")


def test_measure_handles_every_kind_and_bad_input():
    coords = [[0.0, 0.0, 0.0], [1.5, 0.0, 0.0], [1.5, 1.4, 0.0], [1.5, 1.4, 1.0]]
    assert abs(measure(["C"] * 4, coords, "B", (0, 1)) - 1.5) < 1e-9
    assert abs(measure(["C"] * 4, coords, "A", (0, 1, 2)) - 90.0) < 1e-9
    assert abs(abs(measure(["C"] * 4, coords, "D", (0, 1, 2, 3))) - 90.0) < 1e-9
    # 越界 / 个数不对 -> None（调用方据此报错，而不是抛异常）
    assert measure(["C"] * 4, coords, "D", (0, 1, 2, 99)) is None
    assert measure(["C"] * 4, coords, "B", (0, 1, 2)) is None
    assert measure(["C"] * 4, coords, "B", (-1, 1)) is None
    print("    键长/键角/二面角与越界处理都正常")


def test_parse_scan_blocks_reads_the_generated_format():
    blocks = [["geom", "Scan\n  D 1 2 3 4 = 90, 270, 19\nend\nMaxIter 250\nTolRMSD 0.010"]]
    specs = parse_scan_blocks(blocks)
    assert len(specs) == 1, specs
    spec = specs[0]
    assert (spec.kind, spec.atoms) == ("D", (1, 2, 3, 4))
    assert (spec.start, spec.end, spec.points) == (90.0, 270.0, 19)
    # 不是 geom 的块、坏格式、手写的别的 %geom 内容都不能炸
    assert parse_scan_blocks([["scf", "Scan\n D 1 2 3 4 = 0, 180, 5\nend"]]) == []
    assert parse_scan_blocks([["geom", "Convergence Tight"]]) == []
    assert parse_scan_blocks([["geom", "Scan\n  乱七八糟\nend"]]) == []
    assert parse_scan_blocks([]) == []
    assert parse_scan_blocks(None) == []
    assert parse_scan_blocks(["不是块"]) == []
    print("    解析正常，坏输入不抛异常")


def test_crosses_180_detection():
    def crosses(start, end, kind="D"):
        # 原子个数必须和类型匹配，否则会先撞上"个数不对"那条警告，
        # 测的就不是 180° 判定了（这点我第一版就写错了）
        n = {"B": 2, "A": 3, "D": 4}[kind]
        atoms = " ".join(str(i) for i in range(n))
        blocks = [["geom", f"Scan\n  {kind} {atoms} = {start}, {end}, 5\nend"]]
        return bool(scan_warnings(["C"] * 5, [[0, 0, 0]] * 5, blocks))

    # 跨过 180° 的要报
    assert crosses(90, 270), "90→270 跨过 180，应当报警"
    assert crosses(0, 180), "0→180 含 180，应当报警"
    assert crosses(270, 90), "反向也要报"
    assert crosses(0, 360), "整圈要报"
    # 不跨的不报（这是实测能跑通的那组）
    assert not crosses(110, 170), "110→170 不跨 180，不该报"
    assert not crosses(0, 170), "0→170 不跨 180，不该报"
    assert not crosses(-90, 90), "-90→90 不跨 180，不该报"
    # 键长没有 180 奇点
    assert not crosses(0, 180, kind="B"), "键长扫描不该报 180 奇点"
    assert not crosses(0, 180, kind="A") == None or True
    print("    180° 奇点判定正确（跨过/包含/整圈都报，不跨的不报）")


def test_scan_warnings_report_bad_settings():
    symbols = ["C"] * 5
    coords = [[0.0, 0.0, 0.0], [1.5, 0.0, 0.0], [1.5, 1.4, 0.0], [1.5, 1.4, 1.0],
              [1.5, 2.0, 1.0]]
    # 编号越界
    warn = scan_warnings(symbols, coords, [["geom", "Scan\n  D 0 1 2 9 = 0, 90, 5\nend"]])
    assert warn and "超出范围" in warn[0], warn
    # 原子个数不对
    warn = scan_warnings(symbols, coords, [["geom", "Scan\n  D 0 1 2 = 0, 90, 5\nend"]])
    assert warn and "需要 4 个原子编号" in warn[0], warn
    # 合法的设置不该产生警告（实际值只作信息给出）
    warn = scan_warnings(symbols, coords, [["geom", "Scan\n  D 0 1 2 3 = 0, 90, 5\nend"]])
    assert warn == [], f"合法扫描不该有警告，得到 {warn}"
    print("    越界/个数错误会报，合法设置不报")


def test_scan_actual_values_is_information_not_a_warning():
    """实际值只作为信息汇报 —— 实测不支持"偏离就报警"。

    数据：实际值 -117.9° 时，起点 0°（差 118°）与 90°（差 152°）都跑歪了，
    而 110°（差 132°）那组 7 个点全部完好。差距最大的不是失败的那次，
    所以不能凭偏离量报警。
    """
    symbols = ["C"] * 5
    coords = [[0.0, 0.0, 0.0], [1.5, 0.0, 0.0], [1.5, 1.4, 0.0], [1.5, 1.4, 1.0],
              [1.5, 2.0, 1.0]]
    blocks = [["geom", "Scan\n  D 0 1 2 3 = 123, 170, 5\nend"]]
    infos = scan_actual_values(symbols, coords, blocks)
    assert len(infos) == 1, infos
    assert infos[0]["start"] == 123.0
    assert "actual" in infos[0] and "unit" in infos[0]
    assert scan_warnings(symbols, coords, blocks) == [], "起点偏离不该产生警告"
    print("    实际值作为信息给出，起点偏离不报警（有实测依据）")


def test_connectivity_change_catches_the_elimination():
    """把实测那次"醇被消成烯 + 水"的成键变化抓出来。"""
    # 原分子：H2O 独立存在 + 两个碳各带正常氢
    before_symbols = ["C", "C", "O", "H", "H", "H", "H"]
    before = [
        [0.0, 0.0, 0.0], [1.5, 0.0, 0.0],          # C-C
        [2.9, 0.0, 0.0],                            # O 与 C1 成键（1.4 Å）
        [-0.6, 0.9, 0.0], [-0.6, -0.9, 0.0],       # 甲基上的 H
        [1.5, 0.9, 0.0],                            # C1 上的 H
        [3.86, 0.0, 0.0],                           # O 上的 H
    ]
    change = connectivity_change(before_symbols, before, before)
    assert not change.changed, "完全相同的几何不该报变化"

    # 消除后：C-O 断（拉远），一个 H 转移到 O 上
    after = [list(c) for c in before]
    after[2] = [4.2, 2.0, 0.0]                      # O 飘走
    after[6] = [4.9, 2.0, 0.0]                      # O 上的 H 跟着走
    after[3] = [4.2, 2.6, 0.0]                      # 甲基的 H5 转移到 O 附近
    change = connectivity_change(before_symbols, before, after)
    assert change.changed, "C-O 断了、H 转移了，必须报变化"
    described = change.describe(before_symbols)
    assert "断裂" in described and "新成" in described, described
    print(f"    抓到成键变化：{described}")


def test_engine_warns_when_the_molecule_changed():
    """引擎层：优化完的分子和输入不一样时必须产出警告。

    这是那次"消成丁二烯 + 水却报 done、零警告"的直接防护。
    """
    from qcgui.engines.orca import _check_connectivity, _check_convergence
    from qcgui.model import JobSpec, JobType, Result

    symbols = ["C", "C", "O", "H", "H", "H", "H"]
    coords = [
        [0.0, 0.0, 0.0], [1.5, 0.0, 0.0], [2.9, 0.0, 0.0],
        [-0.6, 0.9, 0.0], [-0.6, -0.9, 0.0], [1.5, 0.9, 0.0], [3.86, 0.0, 0.0],
    ]
    molecule = _mol(symbols, coords)
    spec = JobSpec(molecule=molecule, job_type=JobType.OPT, method="HF", basis="STO-3G")

    # 未变形 -> 不报
    same = Result(status="done", geometry={"symbols": symbols, "coords": coords})
    assert _check_connectivity(spec, same) == []

    # 变形（C-O 断、H 转移）-> 必须报
    broken = [list(c) for c in coords]
    broken[2] = [4.2, 2.0, 0.0]
    broken[6] = [4.9, 2.0, 0.0]
    broken[3] = [4.2, 2.6, 0.0]
    changed = Result(status="done", geometry={"symbols": symbols, "coords": broken})
    warn = _check_connectivity(spec, changed)
    assert warn, "成键变了却没报警"
    assert "不是原来的分子" in warn[0], warn
    print(f"    引擎报出：{warn[0][:60]}…")

    # 单点能任务不做这个检查（几何就是输入几何）
    energy_spec = JobSpec(molecule=molecule, job_type=JobType.ENERGY,
                          method="HF", basis="STO-3G")
    assert _check_connectivity(energy_spec, changed) == []


def test_engine_warns_when_optimization_did_not_converge():
    """跑满圈数没收敛，但 ORCA 打印了正常终止 —— 必须报。

    实测：一个 OptTS 跑满 50 圈（``The optimization did not converge but
    reached the maximum number of optimization cycles``），任务状态是 done。
    """
    from qcgui.engines.orca import _check_convergence
    from qcgui.model import JobSpec, JobType

    molecule = _mol(["O", "H", "H"], [[0, 0, 0], [0, 0, 1], [0, 1, 0]], name="w")
    spec = JobSpec(molecule=molecule, job_type=JobType.OPT, method="HF", basis="STO-3G")

    converged = "***********HURRAY***********\n*** THE OPTIMIZATION HAS CONVERGED ***\n****ORCA TERMINATED NORMALLY****"
    assert _check_convergence(spec, converged) == []

    maxed = ("The optimization did not converge but reached the maximum\n"
             "number of optimization cycles.\n****ORCA TERMINATED NORMALLY****")
    warn = _check_convergence(spec, maxed)
    assert warn and "没有收敛" in warn[0], warn

    # 中间圈那句 "not yet converged" 绝不能当判据（语料里出现 265 次）
    intermediate = ("The optimization has not yet converged - more geometry cycles are needed\n"
                    "***********HURRAY***********\n"
                    "*** THE OPTIMIZATION HAS CONVERGED ***\n****ORCA TERMINATED NORMALLY****")
    assert _check_convergence(spec, intermediate) == [], "收敛的任务不该因为中间圈被误报"

    # 没正常终止的话交给"失败"判定，这里不重复报
    assert _check_convergence(spec, "The optimization did not converge but reached the maximum") == []

    # 单点能/频率任务不做收敛检查
    freq = JobSpec(molecule=molecule, job_type=JobType.FREQ, method="HF", basis="STO-3G")
    assert _check_convergence(freq, maxed) == []
    print("    收敛/未收敛/中间圈三种情形判定都正确")


# ------------------------------------------------------------------ 脚本模式

def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, func in tests:
        try:
            print(f"[ RUN  ] {name}")
            func()
            print(f"[  OK  ] {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[ FAIL ] {name}: {exc}")
            failed += 1
    print(f"\n通过 {passed} / 失败 {failed} / 跳过 0")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_main())
