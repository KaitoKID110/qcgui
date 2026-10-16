"""叠加 / RMSD 的**独立**数学核对（2026-10-16 收口时补）。

为什么已经有 ``tests/test_overlay.py`` 还要再来一份
----------------------------------------------------

``test_overlay.py`` 里"**已知**旋转"这四个字其实是自证的：它构造 B 时用的
四元数→旋转矩阵公式和 ``qcgui/model/geometry.py`` 的
``_rotation_from_quaternion`` 是同一套写法。两处一起写错（把 R 和 Rᵀ 搞反、
四元数取共轭、4×4 矩阵的符号写错）时，那批用例可以**照样全绿** ——
而界面会给出一个方向相反的数，用户没有任何办法察觉。

所以这个文件里每个"期望值"都来自与被测代码**不同的来源**：

1. **Rodrigues 公式**（本文件自己实现，与四元数无关）造旋转，
   并验证恢复出来的矩阵**就是**那个已知矩阵的转置；
2. 两原子构型的**闭式手算解** ``RMSD = |ΔL| / 2``；
3. 对称笼形分子（等边三角形 / 正四面体）**单原子径向扰动**的手算闭式解
   ``RMSD = t·√(N-1) / N``（推导见 :func:`test_hand_value_for_one_perturbed_atom`）；
4. **SO(3) 上的暴力搜索 + 局部精修**求全局最优 —— 完全不用特征分解，
   用来证明 Kabsch 给的那个数**确实是全局最小**（而不是某个局部极小）。

被测代码只要把旋转转置、把 4×4 矩阵符号写反、或只收敛到局部极小，
上面四条里至少有一条会挂。

    pytest tests/test_overlay_math.py -v
    python tests/test_overlay_math.py
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.model import Molecule  # noqa: E402
from qcgui.model.geometry import kabsch_align, overlay, rmsd  # noqa: E402

#: 手算值是闭式精确解，只受浮点舍入影响
TOL_HAND = 1e-12
#: 最优叠加后的 RMSD（两个结构真的只差刚体变换时）
TOL_ALIGNED = 1e-9
#: 暴力搜索是数值方法，容差给宽一点
TOL_BRUTE = 1e-6


# ------------------------------------------------------------------ 独立工具
#
# 下面每个函数都**故意不用** geometry.py 里的任何东西。

def rodrigues(axis, degrees):
    """Rodrigues 旋转公式（自己实现，与四元数无关）。"""
    x, y, z = axis
    norm = math.sqrt(x * x + y * y + z * z)
    x, y, z = x / norm, y / norm, z / norm
    angle = math.radians(degrees)
    c, s = math.cos(angle), math.sin(angle)
    one_c = 1.0 - c
    return (
        (c + x * x * one_c, x * y * one_c - z * s, x * z * one_c + y * s),
        (y * x * one_c + z * s, c + y * y * one_c, y * z * one_c - x * s),
        (z * x * one_c - y * s, z * y * one_c + x * s, c + z * z * one_c),
    )


def transpose(matrix):
    return tuple(tuple(matrix[j][i] for j in range(3)) for i in range(3))


def apply_transform(rotation, translation, points):
    """把已知的旋转 + 平移施加到坐标上。"""
    return [
        [sum(rotation[i][k] * point[k] for k in range(3)) + translation[i]
         for i in range(3)]
        for point in points
    ]


def centroid(points):
    n = len(points)
    return tuple(sum(p[i] for p in points) / n for i in range(3))


def rmsd_from_definition(a, b) -> float:
    """按定义直接算 RMSD = sqrt( mean_i |a_i - b_i|² )（独立复算一遍定义）。"""
    total = 0.0
    for p, q in zip(a, b):
        total += sum((p[i] - q[i]) ** 2 for i in range(3))
    return math.sqrt(total / len(a))


def rotation_vector_to_matrix(v):
    """旋转向量（轴 × 角）→ 旋转矩阵；用 Rodrigues，不用四元数。"""
    theta = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if theta < 1e-14:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    return rodrigues(v, math.degrees(theta))


def _cost(v, centered_a, centered_b) -> float:
    """固定旋转下的最小二乘和（平移取质心对齐时的解析最优）。"""
    rotation = rotation_vector_to_matrix(v)
    total = 0.0
    for p, q in zip(centered_b, centered_a):
        for i in range(3):
            rotated = sum(rotation[i][k] * p[k] for k in range(3))
            total += (rotated - q[i]) ** 2
    return total


def brute_force_aligned_rmsd(a, b, samples: int = 4000, seed: int = 20261016,
                             starts: int = 8) -> float:
    """SO(3) 上暴力搜索 + 局部精修求全局最优的叠加 RMSD。

    原理（和 Kabsch 完全不同的一条路）：固定旋转 R 后，最优平移必然把
    质心对齐，于是

        RMSD²(R) = (1/N) Σ |R·(b_i - c_b) - (a_i - c_a)|²

    只剩"在 SO(3) 上最小化这个二次型"。这里对旋转向量做
    **随机多起点采样 + 坐标下降 + 步长折半**，完全不碰特征分解。
    """
    ca, cb = centroid(a), centroid(b)
    ac = [[p[i] - ca[i] for i in range(3)] for p in a]
    bc = [[p[i] - cb[i] for i in range(3)] for p in b]

    rng = random.Random(seed)
    candidates: list[tuple[float, tuple[float, float, float]]] = []
    for _ in range(samples):
        x, y, z = (rng.gauss(0.0, 1.0) for _ in range(3))
        norm = math.sqrt(x * x + y * y + z * z)
        theta = rng.uniform(0.0, math.pi)
        v = (x / norm * theta, y / norm * theta, z / norm * theta)
        candidates.append((_cost(v, ac, bc), v))
    candidates.sort(key=lambda item: item[0])

    best = candidates[0][0]
    for _, start in candidates[:starts]:
        v = list(start)
        cost = _cost(v, ac, bc)
        step = 0.2
        while step > 1e-13:
            moved = False
            for k in range(3):
                for delta in (step, -step):
                    trial = list(v)
                    trial[k] += delta
                    trial_cost = _cost(trial, ac, bc)
                    if trial_cost < cost - 1e-18:
                        v, cost, moved = trial, trial_cost, True
            if not moved:
                step *= 0.5
        if cost < best:
            best = cost
    return math.sqrt(best / len(a))


def load_example(name: str):
    """读 examples/ 里的真实分子。"""
    path = REPO / "examples" / name
    if not path.is_file():
        raise AssertionError(f"缺少测试用的真实结构：{path}")
    molecule = Molecule.from_xyz_file(path)
    return list(molecule.symbols), [list(c) for c in molecule.coords]


# ------------------------------------------------------------------ 1. 已知旋转
#
# 独立的旋转来源：Rodrigues 公式，完全不经过四元数。

#: 绕 (1,1,1) 方向转 37°（随便挑的非对称角度，避开 90°/180° 这种
#: "转置了也对"的特殊情况）
AXIS_37 = (1.0, 1.0, 1.0)
ROT_37 = rodrigues(AXIS_37, 37.0)
SHIFT_37 = (2.5, -1.75, 0.5)


def test_known_rodrigues_rotation_aligns_to_zero():
    """真实分子 + 已知 Rodrigues 旋转 37° + 平移 → 对齐后 RMSD < 1e-9。"""
    symbols, coords = load_example("acetic_acid.xyz")
    moved = apply_transform(ROT_37, SHIFT_37, coords)

    raw = rmsd(coords, moved)
    aligned = rmsd(coords, moved, align=True)
    assert raw > 1.0, f"对齐前应当明显不为 0，得到 {raw}"
    assert aligned < TOL_ALIGNED, f"对齐后应当 ≈ 0，得到 {aligned}"

    # 反着叠也必须一样（RMSD 对称）
    assert abs(rmsd(moved, coords, align=True) - aligned) < TOL_ALIGNED
    print(f"    Rodrigues 37°+(2.5,-1.75,0.5)：{raw:.6f} Å → {aligned:.3e} Å"
          f"（乙酸 {len(symbols)} 原子）")


def test_recovered_rotation_is_the_known_one():
    """恢复出来的旋转必须是**已知旋转的转置**（约定：B 被搬到 A 上）。

    这一条是"旋转方向没搞反"的直接证据 —— 方向反了的话，叠合后的 RMSD
    依然会是 0，只有这个矩阵能区分（自证测试抓不到）。
    """
    symbols, coords = load_example("acetic_acid.xyz")
    moved = apply_transform(ROT_37, SHIFT_37, coords)
    data = overlay(symbols, coords, symbols, moved)

    expected = transpose(ROT_37)
    worst = max(abs(data["rotation"][i][j] - expected[i][j])
                for i in range(3) for j in range(3))
    assert worst < 1e-9, f"恢复的旋转不是已知旋转的转置，最大偏差 {worst}"

    # 平移也要对得上：把 B 变换回去必须落在 A 上
    worst_point = 0.0
    for point_b, point_a in zip(moved, coords):
        back = [sum(data["rotation"][i][k] * point_b[k] for k in range(3))
                + data["translation"][i] for i in range(3)]
        worst_point = max(worst_point,
                          max(abs(back[i] - point_a[i]) for i in range(3)))
    assert worst_point < TOL_ALIGNED, f"R·b + t 没有落回 A 上，最大偏差 {worst_point}"

    # 行列式必须是 +1（纯旋转，不含反射）
    r = data["rotation"]
    det = (r[0][0] * (r[1][1] * r[2][2] - r[1][2] * r[2][1])
           - r[0][1] * (r[1][0] * r[2][2] - r[1][2] * r[2][0])
           + r[0][2] * (r[1][0] * r[2][1] - r[1][1] * r[2][0]))
    assert abs(det - 1.0) < 1e-12, f"det = {det}，应当为 +1"
    print(f"    R = R_已知ᵀ（最大偏差 {worst:.2e}）；R·b + t 落回 A"
          f"（{worst_point:.2e} Å）；det(R) = {det:.12f}")


def test_rodrigues_self_check():
    """先证明这个"独立参照"本身是对的：RᵀR = I、det = +1、角度对。"""
    for axis, degrees in (((1, 1, 1), 37.0), ((0, 0, 1), 90.0), ((1, -2, 0.5), 123.4)):
        r = rodrigues(axis, degrees)
        for i in range(3):
            for j in range(3):
                dot = sum(r[k][i] * r[k][j] for k in range(3))
                assert abs(dot - (1.0 if i == j else 0.0)) < 1e-14
        det = (r[0][0] * (r[1][1] * r[2][2] - r[1][2] * r[2][1])
               - r[0][1] * (r[1][0] * r[2][2] - r[1][2] * r[2][0])
               + r[0][2] * (r[1][0] * r[2][1] - r[1][1] * r[2][0]))
        assert abs(det - 1.0) < 1e-14
        # 轴上的向量不动
        norm = math.sqrt(sum(c * c for c in axis))
        unit = [c / norm for c in axis]
        rotated = [sum(r[i][k] * unit[k] for k in range(3)) for i in range(3)]
        assert max(abs(rotated[i] - unit[i]) for i in range(3)) < 1e-14
    print("    Rodrigues 参照自检：RᵀR = I、det = +1、转轴不变")


# ------------------------------------------------------------------ 2. 手算：两原子
#
# 两原子构型的叠加有**闭式解**：把两条线段叠起来（中点重合、方向相反或相同），
# 两个端点各偏移 |L1 - L2| / 2，于是
#
#     RMSD = sqrt( [ (ΔL/2)² + (ΔL/2)² ] / 2 ) = |L1 - L2| / 2
#
# 注意这里**不允许缩放**：若允许缩放，RMSD 会是 0 —— 所以这条同时验证了
# "只做刚体变换"。

def test_hand_value_two_atom_bond_length():
    """手算：只有键长不同（无旋转）时，重叠后 RMSD = |ΔL| / 2。"""
    cases = [
        (1.00, 1.10),   #  0.050
        (2.00, 1.00),   #  0.500
        (1.4200, 1.0900),  # 0.165（C-O vs C-H 量级）
    ]
    for length_a, length_b in cases:
        expected = abs(length_a - length_b) / 2.0
        a = [[0.0, 0.0, 0.0], [length_a, 0.0, 0.0]]
        b = [[0.0, 0.0, 0.0], [length_b, 0.0, 0.0]]

        got = rmsd(a, b, align=True)
        assert abs(got - expected) < TOL_HAND, \
            f"L1={length_a} L2={length_b}：手算 {expected}，实际 {got}"

        # 手算值与摆放无关：把 B 转到别处（已知 Rodrigues）结果必须一样
        moved = apply_transform(rot_arbitrary(seed=length_a + length_b),
                                (7.0, -3.0, 1.25), b)
        got_moved = rmsd(a, moved, align=True)
        assert abs(got_moved - expected) < TOL_HAND, \
            f"旋转平移后：手算 {expected}，实际 {got_moved}"

        assert abs(rmsd_from_definition([x for x in a],
                                        [x for x in a]) - 0.0) < 1e-15
        print(f"    键长 {length_a} → {length_b} Å：手算 |ΔL|/2 = {expected:.12f}，"
              f"代码 {got:.12f}")


def rot_arbitrary(seed: float):
    """随便造一个已知的旋转矩阵（由 seed 决定，便于复现）。"""
    rng = random.Random(int(seed * 1000) + 7)
    axis = (rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1))
    if all(abs(c) < 1e-9 for c in axis):
        axis = (1.0, 1.0, 1.0)
    return rodrigues(axis, rng.uniform(5.0, 175.0))


# ------------------------------------------------------------------ 3. 手算：一个原子被扰动
#
# 手算过程（**这是本文件的核心**，报告里原样引用）
# ---------------------------------------------------------------------------
# 取 N 个原子，质心在原点，每个原子到质心的距离都是 1（等边三角形 /
# 正四面体，坐标就是 N 个单位向量 u_i，且 Σu_i = 0）。
#
# 把第 0 个原子**沿半径方向**推出去 t：b_0 = (1 + t)·u_0，其余不动。
#
# * 质心：B 的质心 = t·u_0 / N，A 的质心是原点 → 去质心后
#       b_0' = (1 + t - t/N)·u_0 = (1 + (N-1)t/N)·u_0
#       b_i' = u_i - (t/N)·u_0      （i = 1..N-1）
#   A 去质心后不动：a_i' = u_i = -(1/N)·u_0 + w_i，其中 w_i ⊥ u_0
#   （|w_i|² = 1 - 1/N²，且 Σ_{i≠0} w_i = 0）
# * 最优旋转在 N=3 / N=4 时被对称性限制成"绕 u_0 转 θ"（两个构型都有一个
#   含 u_0 的镜面，最优旋转可以取与它交换的那个）：
#       f(θ) = Σ|R_θ b_i' - a_i'|²
#            = ((N-1)t/N)²                          ← 原子 0 的径向残余
#              + (N-1)(t/N)²                        ← 其余原子的径向偏移
#              + Σ_{i≠0}|R_θ w_i - w_i|²            ← 切向部分（交叉项因正交为 0）
#   Σ_{i≠0}|R_θ w_i - w_i|² = (N-1)·2|w|²(1-cosθ) ≥ 0，在 θ = 0 取 0。
# * 于是 f_min = t²[(N-1)² + (N-1)]/N² = t²(N-1)N/N² = t²(N-1)/N，
#       RMSD = sqrt(f_min / N) = t·√(N-1) / N。
# ---------------------------------------------------------------------------


def _cage(n: int):
    """质心在原点、半径 1 的对称笼：等边三角形（n=3）/ 正四面体（n=4）。"""
    if n == 3:
        return [[1.0, 0.0, 0.0],
                [-0.5, math.sqrt(3.0) / 2.0, 0.0],
                [-0.5, -math.sqrt(3.0) / 2.0, 0.0]]
    if n == 4:
        s = 1.0 / math.sqrt(3.0)
        return [[s, s, s], [s, -s, -s], [-s, s, -s], [-s, -s, s]]
    raise ValueError(n)


def test_hand_value_for_one_perturbed_atom():
    """手算：对称笼里一个原子径向推出 t → 重叠后 RMSD = t·√(N-1)/N。"""
    for n in (3, 4):
        base = _cage(n)
        # 前提自检：质心在原点、每个原子到质心距离为 1
        assert max(abs(c) for c in centroid(base)) < 1e-15
        assert max(abs(math.dist(p, (0.0, 0.0, 0.0)) - 1.0) for p in base) < 1e-15

        for t in (0.01, 0.13, 0.5):
            perturbed = [list(p) for p in base]
            perturbed[0] = [c * (1.0 + t) for c in base[0]]
            expected = t * math.sqrt(n - 1) / n

            got = rmsd(base, perturbed, align=True)
            assert abs(got - expected) < 1e-12, \
                f"N={n} t={t}：手算 {expected}，实际 {got}"

            # 手算值与摆放无关：B 先做已知旋转 + 平移，结果必须一样
            moved = apply_transform(rot_arbitrary(seed=t + n), (3.0, 2.0, -1.0),
                                    perturbed)
            got_moved = rmsd(base, moved, align=True)
            assert abs(got_moved - expected) < 1e-12, \
                f"N={n} t={t} 旋转平移后：手算 {expected}，实际 {got_moved}"
            print(f"    N={n} t={t:.2f}：手算 t·√(N-1)/N = {expected:.12f}，"
                  f"代码 {got:.12f}，旋转平移后 {got_moved:.12f}")


# ------------------------------------------------------------------ 4. 独立全局最优
#
# Kabsch 是解析解，前提是"取最大特征值"这一步做对了。这里的暴力搜索
# 是另一条完全不同的路（随机多起点 + 局部精修，不用特征分解），用来证明
# 代码给出的数**就是全局最小**，而不是某个局部极小。

def _brute_cases():
    symbols, coords = load_example("acetic_acid.xyz")
    rot = rodrigues((0.3, -1.0, 0.7), 113.0)
    shifted = apply_transform(rot, (4.0, -2.0, 7.5), coords)
    # 纯刚体：暴力搜索和 Kabsch 都必须给 0
    yield "乙酸（纯刚体 113°）", coords, shifted

    # 一个原子被扰动 0.13 Å：两个方法都必须给出同一个非零数
    perturbed = [list(p) for p in shifted]
    perturbed[2] = [perturbed[2][0] + 0.13, perturbed[2][1] - 0.07,
                    perturbed[2][2] + 0.11]
    yield "乙酸（第 3 个原子扰动 0.13 Å）", coords, perturbed

    # 手算过的笼形扰动：暴力搜索要能独立复现 t·√(N-1)/N
    base = _cage(4)
    perturbed = [list(p) for p in base]
    perturbed[0] = [c * 1.13 for c in base[0]]
    yield "正四面体（t = 0.13）", base, perturbed



def test_brute_force_so3_confirms_global_optimum():
    """暴力搜索 SO(3) 得到的全局最优必须与 Kabsch 一致（且不更小）。"""
    for label, a, b in _brute_cases():
        brute = brute_force_aligned_rmsd(a, b)
        kabsch = rmsd(a, b, align=True)
        assert brute >= kabsch - TOL_BRUTE, \
            f"{label}：暴力搜索找到更小的 RMSD（{brute} < Kabsch {kabsch}）" \
            f"——Kabsch 给的不是全局最优"
        assert abs(brute - kabsch) < TOL_BRUTE, \
            f"{label}：暴力搜索 {brute} vs Kabsch {kabsch}，两者不一致"
        print(f"    {label}：Kabsch {kabsch:.12f} vs 暴力搜索 {brute:.12f}"
              f"（差 {abs(brute - kabsch):.2e}）")


def test_aligned_coords_match_the_definition():
    """``overlay`` 返回的坐标必须真的满足定义（独立复算 RMSD）。"""
    symbols, coords = load_example("caffeine.xyz")
    moved = apply_transform(rodrigues((0.0, 1.0, 0.0), 61.0), (1.5, 2.5, -3.5),
                            coords)
    data = overlay(symbols, coords, symbols, moved)

    assert data["moved"] == "b"
    assert data["n_atoms"] == len(coords)
    # 用定义独立复算一遍：从 aligned_coords 直接算 RMSD
    by_definition = rmsd_from_definition(coords, data["aligned_coords"])
    assert abs(by_definition - data["rmsd_after"]) < 1e-12, \
        f"读数 {data['rmsd_after']} 与按定义复算的 {by_definition} 不一致"
    assert data["rmsd_after"] <= data["rmsd_before"] + 1e-15
    print(f"    overlaid {data['n_atoms']} 原子：读数 {data['rmsd_after']:.3e}，"
          f"按定义复算 {by_definition:.3e}，对齐前 {data['rmsd_before']:.4f}")


def test_rmsd_is_invariant_under_common_relabelling():
    """把两个结构**同时**按同一个顺序重排，RMSD 不许变（对应关系是 i↔i）。"""
    symbols, coords = load_example("caffeine.xyz")
    b = apply_transform(rodrigues((1.0, 0.0, 0.0), 23.0), (0.5, -0.5, 2.0), coords)
    order = list(range(len(coords)))
    random.Random(3).shuffle(order)

    a2 = [coords[i] for i in order]
    b2 = [b[i] for i in order]
    baseline = rmsd(coords, b, align=True)
    shuffled = rmsd(a2, b2, align=True)
    assert abs(shuffled - baseline) < TOL_ALIGNED, \
        f"同步重排后 RMSD 变了：{baseline} → {shuffled}"

    # 同样地，单原子扰动的手算值也必须不受同步重排影响
    perturbed = [list(p) for p in coords]
    perturbed[4] = [perturbed[4][0] + 0.2, perturbed[4][1], perturbed[4][2]]
    p2 = [perturbed[i] for i in order]
    assert abs(rmsd(a2, p2, align=True) - rmsd(coords, perturbed, align=True)) < TOL_ALIGNED
    print(f"    同步重排 24 原子：{baseline:.12f} Å 保持不变")


# ---------------------------------------------------------------- 脚本模式

def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, func in tests:
        print(f"[ RUN  ] {name}")
        try:
            func()
            print(f"[  OK  ] {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, exc))
            print(f"[ FAIL ] {name}: {type(exc).__name__}: {exc}")
    print("-" * 60)
    print(f"通过 {len(tests) - len(failed)} / 失败 {len(failed)}")
    for name, exc in failed:
        print(f"  失败: {name} -> {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
