"""结果侧几何与电荷的合法性检查（P0：`energy` 任务的坏几何会显示绿点）。

为什么需要这个模块
------------------
`Molecule.__post_init__` 有 `MIN_ATOM_GAP` 校验，但它**只在注入侧生效**。
`Result.geometry` 由引擎适配器（`engines/orca.py`）从 `orca_2json` 直接填入，
**从不经过 `Molecule`** —— 于是结果几何侧是**零校验**的。

两处既有的补算（成键变化、收敛检查）都挂在
`spec is not None and job_type in MOVING_JOB_TYPES` 上，所以：

* **单点任务（`energy`）不在白名单里** → 完全不设防；
* 而且 `jobs/store.py::load_spec()` 会**故意吞掉** `Molecule.__post_init__`
  抛出的 ValueError 返回 `None` → 即使放开白名单，补算仍被 `spec is not None` 跳过。

实测反例（`20261013-162908-bf2e781befd90603`，`job_type=energy`）：
`status=done`、`verdict=ok`、`warnings=[]`，但 C1–C12 相距 **0.300 Å**、
Mulliken 给出 `C0=+16.786 e` / `C11=−17.756 e`（同几何 Löwdin 只有 −0.257 / −0.147）。
界面把它当正常结果显示 —— 正是本项目最怕的「算错了却显示绿点」。

设计约束（关键）
----------------
**本模块的函数不接收 `JobSpec`、不接收 `job_type`、不 import 任何引擎** ——
只看 `symbols` / `coords` / 电荷数组。这样上面两处缺口**同时被绕开**，
而且本模块是纯标准库，界面进程可以直接用（架构第 1 条）。

阈值不是新造的数
----------------
`MIN_ATOM_GAP = 0.4` 复用注入侧同一个常量（`molecule.py`），其依据是
"最短的真实化学键是 H₂ 的 0.74 Å"。

`0.4–0.5 Å` 是**回滞带**（只 `minor` 提示）：106 个带几何的真实任务里，
这一档唯一的任务是 `done` 且电荷完全正常 —— 阈值取 0.5 就会误报 1 例。
这是 P0-4 的教训（硬阈值下 70 个真实结构里有 2 个会被热振动翻转成误报，
加 10% 回滞带后零翻转）。
"""

from __future__ import annotations

import math

from .molecule import MIN_ATOM_GAP
from .result import SEVERITY_BLOCKING, SEVERITY_MAJOR, SEVERITY_MINOR, Issue

#: 回滞带上界 (Å)：这一档只提示、不阻断。见模块文档。
CLOSE_CONTACT_GAP = 0.5

#: 电荷求和判据的容差。103 个真实任务的实测噪声上限是 8.7e-11，
#: 1e-6 留了 4 个数量级余量，不会误报。
SUM_TOLERANCE = 1e-6


def min_atom_gap(coords) -> float | None:
    """最小原子间距 (Å)。少于 2 个原子返回 None。"""
    pts = [tuple(float(v) for v in p) for p in coords]
    if len(pts) < 2:
        return None
    best = math.inf
    for i in range(len(pts)):
        xi, yi, zi = pts[i]
        for j in range(i + 1, len(pts)):
            xj, yj, zj = pts[j]
            dx, dy, dz = xi - xj, yi - yj, zi - zj
            d = math.sqrt(dx * dx + dy * dy + dz * dz)
            if d < best:
                best = d
    return best


def geometry_sanity_problems(symbols, coords, gap: float = MIN_ATOM_GAP) -> list[Issue]:
    """结果几何的合法性检查。

    `gap` 以下 → `blocking`（原子几乎重合，这个几何不可能算对）；
    `[gap, CLOSE_CONTACT_GAP)` → `minor`（回滞带，只提示）。
    """
    issues: list[Issue] = []
    if not coords:
        return issues
    if len(symbols) != len(coords):
        # 长度不一致优先报 —— 这比"原子太少"更是真问题
        issues.append(Issue(
            code="geometry_length_mismatch",
            severity=SEVERITY_BLOCKING,
            message=f"结果里元素数与坐标数不一致（{len(symbols)} vs {len(coords)}），"
                    f"几何不可用",
        ))
        return issues
    if len(coords) < 2:
        return issues

    best = min_atom_gap(coords)
    if best is None:
        return issues
    if best < gap:
        # 找出是哪一对，让用户能直接去图上找（只报第一对，避免刷屏）
        pair = _closest_pair(coords)
        label = ""
        if pair is not None:
            i, j = pair
            label = f"（{symbols[i]}{i} 与 {symbols[j]}{j}）"
        issues.append(Issue(
            code="geometry_atoms_overlapping",
            severity=SEVERITY_BLOCKING,
            message=f"结果几何里有两个原子几乎重合：相距 {best:.3f} Å{label}，"
                    f"小于 {gap} Å。这个几何算出来的能量与电荷不可用",
        ))
    elif best < CLOSE_CONTACT_GAP:
        issues.append(Issue(
            code="geometry_close_contact",
            severity=SEVERITY_MINOR,
            message=f"结果几何里最近的两个原子相距 {best:.3f} Å，"
                    f"小于 {CLOSE_CONTACT_GAP} Å —— 如果这不是你要的构型，"
                    f"请检查输入结构",
        ))
    return issues


def charge_sanity_problems(symbols, charge, raw, tolerance: float = SUM_TOLERANCE) -> list[Issue]:
    """电荷数组的合法性检查。

    `raw` 是 ``{"布居名": [q0, q1, ...]}``。判据：

    * 数组长度既不是 0 也不等于原子数 → `blocking`（对不上就是不干净的数据）；
    * ``abs(∑q − charge) > tolerance`` → `blocking`（求和不等于体系电荷）；
    * 某个 ``|q_i|`` 超过"价电子数 + 1" → `major`（启发式，只当旁证）。

    **注意**：最后那条**单独不足以判不可用** —— 106 个真实任务里它命中 2 个，
    与几何判据 100% 重合，所以它只配当旁证（见规格 §6.5）。
    """
    issues: list[Issue] = []
    n_atoms = len(symbols)
    for name, values in (raw or {}).items():
        if values is None:
            continue
        vals = list(values)
        if values and len(vals) not in (0, n_atoms):
            issues.append(Issue(
                code="charge_length_mismatch",
                severity=SEVERITY_BLOCKING,
                message=f"{name} 电荷有 {len(vals)} 个数，但结构有 {n_atoms} 个原子 —— "
                        f"结果与几何不对应",
            ))
            continue
        if not vals:
            continue
        total = sum(float(v) for v in vals)
        if charge is not None and abs(total - float(charge)) > tolerance:
            issues.append(Issue(
                code="charge_sum_mismatch",
                severity=SEVERITY_BLOCKING,
                message=f"{name} 电荷求和 = {total:.6f} e，但体系电荷是 {charge} e "
                        f"（差 {abs(total - float(charge)):.3g} e）—— 这个结果自相矛盾",
            ))
        worst, worst_atom = _worst_charge(symbols, vals)
        if worst_atom is not None and abs(worst) > _charge_limit(symbols[worst_atom]) :
            issues.append(Issue(
                code="charge_out_of_range",
                severity=SEVERITY_MAJOR,
                message=f"{name} 电荷里 {symbols[worst_atom]}{worst_atom} = {worst:+.3f} e，"
                        f"超出了该元素价电子数的合理范围 —— 通常是几何本身出了问题",
            ))
    return issues


# ------------------------------------------------------------------ 内部小工具

def _closest_pair(coords):
    pts = [tuple(float(v) for v in p) for p in coords]
    if len(pts) < 2:
        return None
    best, pair = math.inf, None
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = math.dist(pts[i], pts[j])
            if d < best:
                best, pair = d, (i, j)
    return pair


#: 粗略的价电子数（只用于"离谱"判断，不是严格化学量）。
_VALENCE = {"H": 1, "C": 4, "N": 5, "O": 6, "F": 7, "P": 5, "S": 6, "Cl": 7,
            "Br": 7, "I": 7, "B": 3, "Si": 4}


def _charge_limit(symbol: str) -> float:
    """该元素电荷的合理上界（价电子数 + 1，留给离子与布居误差的余量）。"""
    return _VALENCE.get(symbol, 8) + 1


def _worst_charge(symbols, vals):
    """绝对值最大的那个电荷 —— 报告时带上它更有用（用户能直接去图上核对）。"""
    worst, idx = 0.0, None
    for i, v in enumerate(vals):
        f = float(v)
        if idx is None or abs(f) > abs(worst):
            worst, idx = f, i
    return worst, idx
