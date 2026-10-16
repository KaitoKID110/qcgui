"""结果侧几何/电荷守卫的测试（P0：`energy` 任务的坏几何会显示绿点）。

缺陷反例（实测，来自真实任务库）：
`20261013-162908-bf2e781befd90603`（`rig_meo_ben_R1.70`，`job_type=energy`）
`status=done`、`verdict=ok`、`warnings=[]`，但 C1–C12 相距 **0.300 Å**、
Mulliken `C0=+16.786 e` / `C11=−17.756 e`（同几何 Löwdin 只有 −0.257 / −0.147 e）。

为什么这个缺陷存在：`Result.geometry` 由引擎适配器直接填入，**从不经过
`Molecule.__post_init__` 的 `MIN_ATOM_GAP` 校验**；而两处既有补算都挂在
`spec is not None and job_type in MOVING_JOB_TYPES` 上，单点任务不在白名单里。
所以本模块的守卫**只看几何与电荷数组，不看 spec、不看 job_type**。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.model.charges import (  # noqa: E402
    CLOSE_CONTACT_GAP,
    charge_sanity_problems,
    geometry_sanity_problems,
    min_atom_gap,
)
from qcgui.model.molecule import MIN_ATOM_GAP  # noqa: E402
from qcgui.model.result import (  # noqa: E402
    SEVERITY_BLOCKING,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
)


def _codes(issues):
    return {i.code for i in issues}


# --------------------------------------------------------------- 几何

def test_the_real_regression_case_is_now_blocked():
    """实测反例：两个碳相距 0.300 Å → 必须 blocking（修复前是绿点）。"""
    symbols = ["C", "C"]
    coords = [[0.0, 0.0, 0.0], [0.300, 0.0, 0.0]]
    issues = geometry_sanity_problems(symbols, coords)
    assert _codes(issues) == {"geometry_atoms_overlapping"}, issues
    assert issues[0].severity == SEVERITY_BLOCKING
    assert "0.300" in issues[0].message
    # 文案要指出是哪一对，用户才能直接去图上核对
    assert "C0" in issues[0].message and "C1" in issues[0].message


def test_second_real_regression_case_is_blocked_too():
    """同类第二个反例 `20261013-162908-fa309825c4dab3ca`：0.150 Å。"""
    issues = geometry_sanity_problems(["C", "C"], [[0, 0, 0], [0.150, 0, 0]])
    assert "geometry_atoms_overlapping" in _codes(issues)


def test_hysteresis_band_only_warns_never_blocks():
    """0.4–0.5 Å 是回滞带：**只提示**，不是不可用。

    实测依据：106 个带几何的真实任务里，这一档唯一的任务（0.450 Å）是 `done`
    且电荷完全正常 —— 阈值取 0.5 就会误报 1 例。
    """
    issues = geometry_sanity_problems(["C", "C"], [[0, 0, 0], [0.450, 0, 0]])
    assert _codes(issues) == {"geometry_close_contact"}, issues
    assert issues[0].severity == SEVERITY_MINOR
    assert not any(i.severity == SEVERITY_BLOCKING for i in issues)

    # 边界：正好 0.4 不进 blocking；正好 0.5 不进回滞带
    assert "geometry_atoms_overlapping" not in _codes(
        geometry_sanity_problems(["C", "C"], [[0, 0, 0], [MIN_ATOM_GAP, 0, 0]]))
    assert geometry_sanity_problems(
        ["C", "C"], [[0, 0, 0], [CLOSE_CONTACT_GAP, 0, 0]]) == []


def test_normal_geometries_are_silent():
    """正常运行不得误报 —— 真实水分子与苯环。"""
    water = ["O", "H", "H"]
    water_coords = [[0.0, 0.0, 0.1173], [0.0, 0.7572, -0.4692], [0.0, -0.7572, -0.4692]]
    assert geometry_sanity_problems(water, water_coords) == []
    # 水的最短原子间距是 O–H 键长，约 0.958 Å —— 远在 0.5 Å 回滞带之上
    gap = min_atom_gap(water_coords)
    assert 0.9 < gap < 1.0, gap


def test_too_few_atoms_and_mismatched_lengths():
    """少于 2 个原子无从判断；元素数与坐标数不一致要报出来。"""
    assert geometry_sanity_problems(["O"], [[0.0, 0.0, 0.0]]) == []
    assert geometry_sanity_problems([], []) == []
    # 长度不一致必须优先于"原子太少"被报出来 —— 那才是真问题
    issues = geometry_sanity_problems(["C", "C"], [[0.0, 0.0, 0.0]])
    assert _codes(issues) == {"geometry_length_mismatch"}
    assert issues[0].severity == SEVERITY_BLOCKING
    issues = geometry_sanity_problems(["C"], [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    assert _codes(issues) == {"geometry_length_mismatch"}


def test_threshold_is_reused_not_invented():
    """阈值必须复用注入侧的 MIN_ATOM_GAP，不许另造一个数。"""
    assert MIN_ATOM_GAP == 0.4
    assert CLOSE_CONTACT_GAP > MIN_ATOM_GAP


# --------------------------------------------------------------- 电荷

def test_charge_sum_mismatch_is_blocked():
    issues = charge_sanity_problems(["O", "H", "H"], 0,
                                    {"mulliken": [0.5, 0.5, 0.5]})
    assert "charge_sum_mismatch" in _codes(issues)
    assert all(i.severity == SEVERITY_BLOCKING for i in issues if i.code == "charge_sum_mismatch")


def test_charge_sum_tolerance_admits_real_numerical_noise():
    """103 个真实任务的求和噪声上限 8.7e-11 —— 不许误报。"""
    assert charge_sanity_problems(["O", "H", "H"], 0,
                                  {"loewdin": [8.7e-11, -4.3e-11, -4.4e-11]}) == []
    # 但真差出来必须报
    assert "charge_sum_mismatch" in _codes(
        charge_sanity_problems(["O", "H", "H"], 0, {"loewdin": [0.1, 0.0, 0.0]}))


def test_charged_system_sum_uses_the_actual_charge():
    """带电体系：求和要跟体系电荷比，不是跟 0 比。"""
    assert charge_sanity_problems(["O", "H", "H"], -1,
                                  {"mulliken": [-0.6, -0.2, -0.2]}) == []
    assert "charge_sum_mismatch" in _codes(
        charge_sanity_problems(["O", "H", "H"], -1, {"mulliken": [0.6, 0.2, 0.2]}))


def test_charge_length_mismatch_is_blocked():
    """103/103 真实任务的电荷个数都等于原子数。"""
    issues = charge_sanity_problems(["O", "H", "H"], 0, {"mulliken": [0.0, 0.0]})
    assert _codes(issues) == {"charge_length_mismatch"}
    assert issues[0].severity == SEVERITY_BLOCKING


def test_the_real_regression_charges_are_flagged_but_only_as_major():
    """反例的 Mulliken 电荷 +16.786 / −17.756 e 必须报出来。

    但**只给 major**：106 个真实任务里这条判据命中 2 个，与几何判据 100% 重合，
    所以它只配当旁证，不能单独判不可用（规格 §6.5）。
    """
    symbols = ["C"] * 12
    vals = [0.0] * 12
    vals[0] = 16.786
    vals[11] = -17.756
    vals[1] = 0.97
    issues = charge_sanity_problems(symbols, 0, {"mulliken": vals})
    codes = _codes(issues)
    assert "charge_out_of_range" in codes, issues
    oob = [i for i in issues if i.code == "charge_out_of_range"]
    assert oob and oob[0].severity == SEVERITY_MAJOR
    # 报告的是绝对值最大的那个（C11 −17.756，比 C0 +16.786 更大）
    assert "C11" in oob[0].message


def test_null_and_empty_charge_arrays_are_tolerated():
    """解析不到电荷不是错 —— 不该因为"没有数据"就报错。"""
    assert charge_sanity_problems(["O", "H", "H"], 0, {}) == []
    assert charge_sanity_problems(["O", "H", "H"], 0, {"loewdin": []}) == []
    assert charge_sanity_problems(["O", "H", "H"], 0, {"loewdin": None}) == []


def test_guard_does_not_need_spec_or_job_type():
    """本模块的签名里不许出现 spec / job_type —— 那是两处缺口的根源。"""
    import inspect

    from qcgui.model import charges

    for fn in (charges.geometry_sanity_problems, charges.charge_sanity_problems):
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"spec", "job_type", "jobspec"}), (fn.__name__, params)
