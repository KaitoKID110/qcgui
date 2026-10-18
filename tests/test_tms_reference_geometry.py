# -*- coding: utf-8 -*-
r"""TMS 参考物几何与优化健壮性的回归测试。

为什么要有这些
--------------
上一版内置 TMS 几何把**甲基氢的方向算反了**（H 朝 Si 而不是背向 Si），
最小原子间距远低于正常成键距离，结果**所有级别**的参考物优化都跑满默认
圈数不收敛 —— 而报错只说"没有收敛"，看不出根因。

所以这里钉住三条：
1. 内置几何**没有原子冲突**（最小间距必须 >= 1.0 Å）；
2. 参考物优化带 ``%geom MaxIter 100``（大基组/杂化泛函下留足圈数）；
3. 参考物"跑完但没收敛"时必须**明确失败**，不能拿半成品几何算参考值。
"""
import math

from qcgui.jobs.reference_cache import REFERENCE_VERSION
from qcgui.model.nmr_reference import REFERENCE_MOLECULES


def _tms_atoms():
    info = REFERENCE_MOLECULES["tms"]
    lines = info["xyz"].strip().splitlines()
    n = int(lines[0])
    atoms = []
    for line in lines[2:2 + n]:
        parts = line.split()
        atoms.append((parts[0], (float(parts[1]), float(parts[2]), float(parts[3]))))
    return atoms


def test_tms_geometry_has_no_atom_clash():
    """内置 TMS 几何不允许有原子冲突（这是上一版不收敛的根因）。

    实测教训：H 朝向 Si 时最小间距会掉到 1.0 Å 以下，优化必然挣扎。
    """
    atoms = _tms_atoms()
    assert len(atoms) == 17, len(atoms)
    best = min(
        math.dist(atoms[i][1], atoms[j][1])
        for i in range(len(atoms)) for j in range(i + 1, len(atoms))
    )
    assert best >= 1.0, (
        f"TMS 内置几何有原子冲突：最小间距仅 {best:.3f} Å。"
        "这种几何会让参考物优化不收敛（上一版就是这么坏的）"
    )
    print(f"    TMS 最小原子间距 {best:.3f} Å（无冲突）")


def test_tms_bond_lengths_are_physical():
    """Si–C 与 C–H 键长要落在合理范围，防止几何整体缩放错。"""
    atoms = _tms_atoms()
    si = next(c for s, c in atoms if s == "Si")
    cs = [c for s, c in atoms if s == "C"]
    hs = [c for s, c in atoms if s == "H"]
    assert len(cs) == 4 and len(hs) == 12
    si_c = [math.dist(si, c) for c in cs]
    assert all(1.75 <= d <= 2.00 for d in si_c), f"Si–C 键长异常: {si_c}"
    # 每个 C 都应该有 3 个 ~1.09 Å 的 H
    for c in cs:
        near = sorted(math.dist(c, h) for h in hs)[:3]
        assert all(1.00 <= d <= 1.20 for d in near), f"C–H 键长异常: {near}"
    print(f"    Si–C ≈ {sum(si_c)/4:.3f} Å，C–H ≈ 1.09 Å")


def test_reference_version_was_bumped_for_the_geometry_fix():
    """几何修了，缓存版本必须升档 —— 否则会继续用旧几何算出的参考值。"""
    assert REFERENCE_VERSION >= 3, (
        f"REFERENCE_VERSION={REFERENCE_VERSION}，TMS 几何修正后应至少为 3"
    )
    print(f"    REFERENCE_VERSION = {REFERENCE_VERSION}（旧缓存会失效）")


def test_reference_opt_uses_larger_max_cycles():
    """参考物优化必须显式放宽优化圈数（大基组下默认 50 圈常不够）。"""
    import inspect

    from qcgui.jobs import workflow as wf_mod
    src = inspect.getsource(wf_mod.WorkflowRunner._ensure_reference)
    assert "MaxIter 100" in src, "参考物优化没有放宽 MaxIter"
    print("    参考物优化带 %geom MaxIter 100")


def test_unconverged_reference_is_rejected_not_used():
    """参考物"跑完但没收敛"时必须明确失败，不能拿半成品几何算参考值。"""
    import inspect

    from qcgui.jobs import workflow as wf_mod
    src = inspect.getsource(wf_mod.WorkflowRunner._ensure_reference)
    assert "没有收敛" in src, "没有检查参考物优化的收敛状态"
    assert "参考值不可信" in src, "未收敛时没有说明为什么不能用"
    print("    未收敛的参考物会被明确拒绝")
