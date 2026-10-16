"""纯几何工具：键长/键角/二面角、成键判定、成键变化比较。

.. important::
   本模块**只做几何数学**，不 import 任何量化引擎 —— 界面进程可以直接用
   （项目的架构铁律：界面进程永远不 import 引擎）。

二面角的**约定**经过实测校准（这一步很容易搞错，所以把证据记下来）：

* ORCA 的 ``%geom Scan`` 里 ``D i j k l = v`` 用的是标准约定 ——
  顺式（cis/Z）为 0°，反式（trans/E）为 180°
* 校准方式一：7 个已知扫描值的收敛几何（110°…170°），
  ``ORCA值 + 本文档函数的值 = 180`` 逐点成立
* 校准方式二：RDKit 的参照 —— ``C/C=C\\CO``（Z）本文档函数给 0.00°，
  ``C/C=C/CO``（E）给 180.00°

    >>> from qcgui.model.geometry import dihedral
    >>> dihedral((1,0,0),(0,0,0),(0,0,1),(0,1,1))  # 仅为示例
    90.0
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: 共价半径（Å）。用于按距离判断成键；缺的元素退回 0.77（碳）。
COVALENT_RADII: dict[str, float] = {
    "H": 0.31, "He": 0.28, "Li": 1.28, "Be": 0.96, "B": 0.84, "C": 0.76,
    "N": 0.71, "O": 0.66, "F": 0.57, "Ne": 0.58, "Na": 1.66, "Mg": 1.41,
    "Al": 1.21, "Si": 1.11, "P": 1.07, "S": 1.05, "Cl": 1.02, "Ar": 1.06,
    "K": 2.03, "Ca": 1.76, "Br": 1.20, "I": 1.39,
}

#: 判定成键的距离容差系数：``d < k * (r_i + r_j)`` 即认为成键。
#: 1.25 是常用的经验值，能覆盖 C-C(1.54)、C-H(1.09)、C-O(1.43)、C=C(1.34)。
_BOND_TOLERANCE = 1.25


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a) -> float:
    return math.sqrt(_dot(a, a))


def distance(p, q) -> float:
    return math.dist(p, q)


def angle(p0, p1, p2) -> float:
    """键角 p0-p1-p2，度。"""
    v1, v2 = _sub(p0, p1), _sub(p2, p1)
    denom = _norm(v1) * _norm(v2)
    if denom < 1e-12:
        return 0.0
    return math.degrees(math.acos(max(-1.0, min(1.0, _dot(v1, v2) / denom))))


def dihedral(p0, p1, p2, p3) -> float:
    """二面角 p0-p1-p2-p3，度，范围 (-180, 180]。

    用标准（IUPAC / ORCA）约定：顺式 = 0°，反式 = ±180°。

    实现是通用的 Blondel–Karrer 形式。**不要**自己另写一个 ——
    这个约定极容易写反（本项目就写反过两次，把 Z 构型判成 E）。

    这里 `b0 = p0 - p1`（即 `-(p1 - p0)`）是标准形式的一部分：
    少写这个负号会让结果整体偏 180°，顺式就会被算成反式。
    """
    b0 = _sub(p0, p1)          # 注意：标准形式要求是 -(p1 - p0)
    b1 = _sub(p2, p1)
    b2 = _sub(p3, p2)
    b1_norm = _norm(b1)
    if b1_norm < 1e-12:
        return 0.0
    b1u = (b1[0] / b1_norm, b1[1] / b1_norm, b1[2] / b1_norm)
    v = _sub(b0, tuple(c * _dot(b0, b1u) for c in b1u))
    w = _sub(b2, tuple(c * _dot(b2, b1u) for c in b1u))
    x = _dot(v, w)
    y = _dot(_cross(b1u, v), w)
    return math.degrees(math.atan2(y, x))


def measure(symbols, coords, kind: str, atoms) -> float | None:
    """量一个内坐标。

    ``kind`` 取 ``"B"``（键长）/ ``"A"``（键角）/ ``"D"``（二面角）。
    原子编号越界或形状不对时返回 ``None``（调用方据此决定要不要报错）。
    """
    if not atoms:
        return None
    if max(atoms) >= len(coords) or min(atoms) < 0:
        return None
    points = [coords[i] for i in atoms]
    try:
        if kind == "B" and len(points) == 2:
            return distance(*points)
        if kind == "A" and len(points) == 3:
            return angle(*points)
        if kind == "D" and len(points) == 4:
            return dihedral(*points)
    except (TypeError, ValueError, IndexError):
        return None
    return None


# ------------------------------------------------------------------ 成键

def bond_set(symbols, coords, tolerance: float = _BOND_TOLERANCE) -> set[tuple[int, int]]:
    """按共价半径 + 距离判定成键，返回 ``{(i, j), ...}``（``i < j``）。"""
    bonds: set[tuple[int, int]] = set()
    n = len(symbols)
    for i in range(n):
        ri = COVALENT_RADII.get(symbols[i], 0.77)
        for j in range(i + 1, n):
            rj = COVALENT_RADII.get(symbols[j], 0.77)
            if distance(coords[i], coords[j]) < tolerance * (ri + rj):
                bonds.add((i, j))
    return bonds


@dataclass(frozen=True)
class ConnectivityChange:
    """两套几何之间的成键差异。"""

    broken: list[tuple[int, int]]
    formed: list[tuple[int, int]]

    @property
    def changed(self) -> bool:
        return bool(self.broken or self.formed)

    def describe(self, symbols) -> str:
        def label(pair):
            return f"{symbols[pair[0]]}{pair[0]}–{symbols[pair[1]]}{pair[1]}"

        parts = []
        if self.broken:
            parts.append("断裂 " + "、".join(label(p) for p in self.broken))
        if self.formed:
            parts.append("新成 " + "、".join(label(p) for p in self.formed))
        return "；".join(parts)


#: 判定"成键**变化**"时的回滞带（占截断半径的比例）。
#:
#: 为什么需要：用硬阈值时，键长刚好卡在截断半径附近的键会因**正常热振动**
#: 来回翻转。实测：拿仓库里 70 个真实结构各加 ±0.05 Å 抖动，硬阈值下有
#: **2 个**被翻转（``rig_meo_ben_R2.20``、``rigid_R1.45``）—— 也就是说
#: 这个守卫会误报。
#:
#: 加 10% 回滞带后，只有"**明确越界**"才算变化：断裂要求
#: ``d_after > cutoff × 1.1``，新成要求 ``d_before > cutoff × 1.1``。
#: 代价是判据略微变严 —— 但对本项目的用途（抓"醇被消成烯 + 水"这种
#: 键长变化两倍以上的剧变）完全够用，而误报的代价大得多：
#: 一个会乱叫的守卫，用户很快就会学会忽略它。
_BOND_HYSTERESIS = 0.10


def connectivity_change(symbols, coords_before, coords_after,
                        tolerance: float = _BOND_TOLERANCE,
                        hysteresis: float = _BOND_HYSTERESIS) -> ConnectivityChange:
    """比较前后两套几何的成键。

    这是抓"**算出来的分子已经不是你要的分子**"最可靠的手段：
    它不依赖能量、不依赖是否收敛，只看键接有没有变。

    实测踩过：一个带 ``%geom Scan`` 的优化任务，产物是 1,3-丁二烯 + 水
    （C–O 断裂、H 转移到 O），而任务状态是 ``done``、零警告 ——
    用户只能靠肉眼看出结构不对。

    带**回滞带**：只有明确越过阈值才算变化，避免边界上的键被热振动翻转
    （见 :data:`_BOND_HYSTERESIS`）。
    """
    before_pairs = bond_set(symbols, coords_before, tolerance)
    after_pairs = bond_set(symbols, coords_after, tolerance)

    def _cut(i: int, j: int) -> float:
        ri = COVALENT_RADII.get(symbols[i], 0.77)
        rj = COVALENT_RADII.get(symbols[j], 0.77)
        return tolerance * (1.0 + hysteresis) * (ri + rj)

    # 断裂：原来在成键范围内，现在**明确**超出（越过了回滞带）
    broken = [
        pair for pair in before_pairs
        if distance(coords_after[pair[0]], coords_after[pair[1]]) > _cut(*pair)
    ]
    # 新成：原来**明确**不在成键范围内，现在进了截断半径
    formed = [
        pair for pair in (after_pairs - before_pairs)
        if distance(coords_before[pair[0]], coords_before[pair[1]]) > _cut(*pair)
    ]
    return ConnectivityChange(broken=sorted(broken), formed=sorted(formed))


# ------------------------------------------------------------------ 扫描设置

#: ORCA ``%geom Scan`` 的坐标字母
SCAN_LETTERS = {"B": "键长", "A": "键角", "D": "二面角"}


@dataclass(frozen=True)
class ScanSpec:
    """从 ``%geom`` 块里解析出来的一条扫描设置。"""

    kind: str                  # B / A / D
    atoms: tuple[int, ...]
    start: float
    end: float
    points: int

    @property
    def label(self) -> str:
        return SCAN_LETTERS.get(self.kind, self.kind)

    @property
    def crosses_180(self) -> bool:
        """扫描区间是否包含 180°（二面角/键角在 180° 处是内坐标奇点）。"""
        if self.kind not in ("D", "A"):
            return False
        lo, hi = min(self.start, self.end), max(self.start, self.end)
        # 把区间按 360° 折回，检查是否覆盖 180°（含折返跨越）
        span = hi - lo
        if span >= 360:
            return True
        # 归一化后 180 是否落在 [lo, hi] 的某个 360 周期内
        base = math.floor((lo - 180) / 360) * 360
        for shift in (base, base + 360, base + 720):
            if lo <= 180 + shift <= hi:
                return True
        return False


#: 会移动原子的任务类型。单点能的"输出几何"就是输入几何，比成键没意义。
MOVING_JOB_TYPES = frozenset({"opt", "opt_freq", "ts", "ts_freq"})


def describe_connectivity_change(symbols, coords_before, coords_after) -> str | None:
    """成键变了就返回一句描述，没变返回 ``None``。

    抽出来是为了让**引擎和界面都能用**：引擎在算完时判一次，
    界面在读旧结果时还能再判一次 —— 因为检查是后加的，
    之前存下来的结果文件里没有这个结论（实测：那个消除成丁二烯的任务
    在历史库里仍然显示"结果可用"，正是这个原因）。

    这个检查很便宜（只比对坐标，不读大文件），所以读取时重算完全可接受。
    """
    change = connectivity_change(symbols, coords_before, coords_after)
    if not change.changed:
        return None
    return f"成键发生变化（{change.describe(symbols)}）"


# ------------------------------------------------------------ 结构叠加（Kabsch）
#
# 为什么需要：GaussView 里"把优化前和优化后叠起来看 + 报一个 RMSD"是常规操作。
# 没有它，两个构象的差别只能靠肉眼在两个画面上来回切换着看 —— 而"看起来叠上了"
# 恰恰是最不可靠的判断方式（本项目已有三次栽在"看着对"上）。
#
# **不引入 numpy**：这里只需要 3×3 的旋转，用四元数法（Horn 1987）把问题化归成
# 一个 4×4 对称矩阵的最大特征向量，自带的 Jacobi 迭代几十行就能算到机器精度。
# 引 numpy 会给界面进程加上一个几十 MB 的依赖，而它换来的只是这点算术 ——
# 与项目"前端零依赖 / 界面进程不加重依赖"的取向冲突。


class AlignmentError(ValueError):
    """两个结构无法叠加（原子数不同、元素顺序不同、坐标非法）。

    单独一个异常类型是为了让调用方能把"用户选错了两个结构"和
    其它 ``ValueError`` 区分开 —— 前者要给出可读的中文提示，
    而不是把它当成内部错误。
    """


def _as_coords(coords, what: str = "坐标") -> list[tuple[float, float, float]]:
    """把坐标统一成 ``[(x, y, z), ...]``，非法就明确报错。"""
    if coords is None:
        raise AlignmentError(f"{what}不能为空")
    out: list[tuple[float, float, float]] = []
    for index, point in enumerate(coords):
        try:
            x, y, z = (float(point[0]), float(point[1]), float(point[2]))
        except (TypeError, ValueError, IndexError) as exc:
            raise AlignmentError(
                f"{what}里第 {index} 个原子不是三个数：{point!r}"
            ) from exc
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            raise AlignmentError(f"{what}里第 {index} 个原子的坐标不是有限数：{point!r}")
        out.append((x, y, z))
    if not out:
        raise AlignmentError(f"{what}为空 —— 没有原子可以叠加")
    return out


def check_same_elements(symbols_a, symbols_b) -> None:
    """两个结构的**元素顺序必须逐个一致**，否则明确报错。

    为什么必须报错而不是"尽力对齐"：Kabsch 只做刚体变换，它默认第 i 个原子
    对应第 i 个原子。如果两个文件的原子顺序不同（非常常见：换了个建模工具、
    重新优化过、手改了 xyz），硬算出来的 RMSD 是**没有物理意义的数字** ——
    而且它看起来完全正常（通常偏大但不离谱），用户没有任何办法察觉。
    报错是唯一安全的做法。本项目的定位就是"自动找对应原子"不在范围内。
    """
    if symbols_a is None or symbols_b is None:
        raise AlignmentError(
            "叠加需要两个结构各自的元素顺序（symbols）—— 只给坐标无法保证对应关系"
        )
    a = [str(s).strip() for s in symbols_a]
    b = [str(s).strip() for s in symbols_b]
    if len(a) != len(b):
        raise AlignmentError(
            f"两个结构的原子数不同（{len(a)} vs {len(b)}）—— "
            f"叠加要求原子数相同且顺序一致，本功能不会自动找对应原子"
        )
    for index, (x, y) in enumerate(zip(a, b)):
        if x != y:
            raise AlignmentError(
                f"两个结构的元素顺序不一致：第 {index + 1} 个原子是 {x}，"
                f"另一个结构是 {y}。叠加要求顺序完全一致"
                f"（自动找对应原子不在本功能范围内）"
            )


def _centroid(points) -> tuple[float, float, float]:
    n = len(points)
    return (
        sum(p[0] for p in points) / n,
        sum(p[1] for p in points) / n,
        sum(p[2] for p in points) / n,
    )


def _jacobi_eigen(matrix, max_sweeps: int = 64, tol: float = 1e-18):
    """对称矩阵的 Jacobi 特征分解。

    返回 ``(eigenvalues, eigenvectors)``，其中 ``eigenvectors[k]`` 是第 k 个
    特征值对应的特征向量（已归一化）。只用于 4×4 的四元数矩阵 ——
    Jacobi 每次消掉一对非对角元，几十次扫描后非对角元平方和降到 1e-18 以下，
    对 4×4 来说这就是机器精度。
    """
    n = len(matrix)
    m = [[float(matrix[i][j]) for j in range(n)] for i in range(n)]
    v = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]

    for _ in range(max_sweeps):
        off = 0.0
        for i in range(n):
            for j in range(i + 1, n):
                off += m[i][j] * m[i][j]
        if off <= tol:
            break
        for p in range(n - 1):
            for q in range(p + 1, n):
                apq = m[p][q]
                if abs(apq) <= 1e-300:
                    continue
                theta = (m[q][q] - m[p][p]) / (2.0 * apq)
                # t 是旋转量；theta 为 0 时取 45°（任意选择，必定减小非对角元）
                t = (math.copysign(1.0, theta) / (abs(theta) + math.sqrt(theta * theta + 1.0))
                     if theta != 0.0 else 1.0)
                c = 1.0 / math.sqrt(t * t + 1.0)
                s = t * c
                # 左乘再右乘同一个旋转（G^T A G）—— 对称性因此保持
                for k in range(n):
                    mkp, mkq = m[k][p], m[k][q]
                    m[k][p] = c * mkp - s * mkq
                    m[k][q] = s * mkp + c * mkq
                for k in range(n):
                    mpk, mqk = m[p][k], m[q][k]
                    m[p][k] = c * mpk - s * mqk
                    m[q][k] = s * mpk + c * mqk
                for k in range(n):
                    vkp, vkq = v[k][p], v[k][q]
                    v[k][p] = c * vkp - s * vkq
                    v[k][q] = s * vkp + c * vkq

    values = [m[i][i] for i in range(n)]
    vectors = [[v[i][j] for i in range(n)] for j in range(n)]
    return values, vectors


def _rotation_from_quaternion(q) -> tuple[tuple[float, float, float], ...]:
    """单位四元数 → 旋转矩阵（行优先的 3×3）。"""
    w, x, y, z = q
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    if norm < 1e-300:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    w, x, y, z = w / norm, x / norm, y / norm, z / norm
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)),
        (2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)),
        (2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)),
    )


def _apply_rotation(rotation, point):
    return (
        rotation[0][0] * point[0] + rotation[0][1] * point[1] + rotation[0][2] * point[2],
        rotation[1][0] * point[0] + rotation[1][1] * point[1] + rotation[1][2] * point[2],
        rotation[2][0] * point[0] + rotation[2][1] * point[1] + rotation[2][2] * point[2],
    )


#: 单位旋转矩阵（供退化情形与测试使用）
IDENTITY_ROTATION = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def kabsch_align(coords_a, coords_b):
    """把结构 A 最优叠加到结构 B 上（Kabsch / 四元数法）。

    返回 ``(rotated_a, rotation, translation)``：

    * ``rotated_a``：把 A 的每个原子做刚体变换后的坐标
    * ``rotation``：3×3 旋转矩阵（行优先）
    * ``translation``：3 元素平移向量

    满足 ``rotated_a[i] == rotation @ coords_a[i] + translation``。

    只做**刚体**变换（旋转 + 平移，不缩放、不镜像）—— 化学结构的比较里缩放
    没有物理意义，而镜像会把一对对映体"叠"成 RMSD 0，那是彻底错误的结论。
    因此返回的 ``rotation`` 行列式恒为 +1；纯镜像的两个结构叠不到 0
    （这不是缺陷，是应有的行为）。

    .. note::
       元素顺序不在本函数的检查范围内（这里只有坐标）。调用方要用带
       ``symbols`` 的 :func:`overlay`，它会先做 :func:`check_same_elements`。
    """
    a = _as_coords(coords_a, "结构 A 的坐标")
    b = _as_coords(coords_b, "结构 B 的坐标")
    if len(a) != len(b):
        raise AlignmentError(
            f"两个结构的原子数不同（{len(a)} vs {len(b)}）—— 叠加要求原子数相同"
        )

    ca, cb = _centroid(a), _centroid(b)
    ac = [(p[0] - ca[0], p[1] - ca[1], p[2] - ca[2]) for p in a]
    bc = [(p[0] - cb[0], p[1] - cb[1], p[2] - cb[2]) for p in b]

    # 单原子没有方向信息：任何旋转都同样好，取单位阵（结果 RMSD 仍是 0）
    if len(a) < 2:
        rotation = IDENTITY_ROTATION
    else:
        # S[i][j] = Σ a_i · b_j，即 H = AᵀB；四元数矩阵 N 由 H 的分量拼出
        s = [[sum(p[i] * q[j] for p, q in zip(ac, bc)) for j in range(3)] for i in range(3)]
        (sxx, sxy, sxz), (syx, syy, syz), (szx, szy, szz) = s
        n = [
            [sxx + syy + szz, syz - szy, szx - sxz, sxy - syx],
            [syz - szy, sxx - syy - szz, sxy + syx, szx + sxz],
            [szx - sxz, sxy + syx, -sxx + syy - szz, syz + szy],
            [sxy - syx, szx + sxz, syz + szy, -sxx - syy + szz],
        ]
        values, vectors = _jacobi_eigen(n)
        best = max(range(4), key=lambda k: values[k])
        rotation = _rotation_from_quaternion(vectors[best])

    # 平移：让 A 的质心（旋转后）落到 B 的质心上
    ca_rot = _apply_rotation(rotation, ca)
    translation = (cb[0] - ca_rot[0], cb[1] - ca_rot[1], cb[2] - ca_rot[2])
    rotated_a = [
        tuple(c + t for c, t in zip(_apply_rotation(rotation, p), translation))
        for p in a
    ]
    return rotated_a, rotation, translation


def rmsd(coords_a, coords_b, align: bool = False,
         symbols_a=None, symbols_b=None) -> float:
    """两个结构的 RMSD（Å）。

    ``align=False``：直接比原始坐标 —— 这就是"把两个文件不用任何处理直接比较"，
    两个结构只要在空间里的摆放不同（旋转/平移），这个数就很大。
    ``align=True``：先做 Kabsch 最优叠加再比 —— 这才是"结构相似度"。

    两个数一起看才有意义：**对齐后变小**才说明差别只是摆放不同；
    对齐后仍很大，说明构象/成键真的不同。

    给了 ``symbols_a`` / ``symbols_b`` 就先检查元素顺序（见
    :func:`check_same_elements`）—— 顺序不一致时算出来的数字没有意义。
    """
    if symbols_a is not None or symbols_b is not None:
        check_same_elements(symbols_a, symbols_b)

    a = _as_coords(coords_a, "结构 A 的坐标")
    b = _as_coords(coords_b, "结构 B 的坐标")
    if len(a) != len(b):
        raise AlignmentError(
            f"两个结构的原子数不同（{len(a)} vs {len(b)}）—— RMSD 要求原子数相同"
        )
    if align:
        a = kabsch_align(a, b)[0]

    total = 0.0
    for p, q in zip(a, b):
        total += (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2
    return math.sqrt(total / len(a))


def overlay(symbols_a, coords_a, symbols_b, coords_b) -> dict:
    """两个结构的叠加结果 —— API 与界面共用的那一份计算。

    约定：**A 不动，B 被叠到 A 所在的参考系上**（"参考结构"与"被比较结构"）。
    这样界面里 A 保持用户看到的原样，B 被搬过来，用户不会失去参照。

    返回字典的字段即 API 的返回体（见 ``qcgui.web.app.api_job_overlay``）：

    * ``rmsd_before``：叠加前的原始 RMSD（≥ 0）
    * ``rmsd_after``：最优叠加后的 RMSD（≤ before，且只有刚体差别时 ≈ 0）
    * ``aligned_coords``：B 叠到 A 之后的坐标
    * ``rotation`` / ``translation``：作用在 B 上的刚体变换
    * ``n_atoms`` / ``symbols``：原子数与元素顺序（供画图与校验）
    """
    check_same_elements(symbols_a, symbols_b)

    a = _as_coords(coords_a, "结构 A 的坐标")
    b = _as_coords(coords_b, "结构 B 的坐标")
    aligned, rotation, translation = kabsch_align(b, a)

    before = rmsd(a, b, align=False)
    after = rmsd(a, aligned, align=False)
    # 浮点误差可能让 after 比理论最小值小到 -0（不可能为负，这里只做保护性夹紧）
    after = max(0.0, after)
    return {
        "rmsd_before": before,
        "rmsd_after": after,
        "aligned_coords": [list(c) for c in aligned],
        "rotation": [list(row) for row in rotation],
        "translation": list(translation),
        "n_atoms": len(a),
        "symbols": [str(s).strip() for s in symbols_a],
        "moved": "b",
    }


def parse_scan_blocks(blocks) -> list[ScanSpec]:
    """从 ``extra_blocks`` 里解析 ``%geom`` 的 ``Scan`` 段。

    ``blocks`` 形如 ``[["geom", "Scan\\n  D 1 2 3 4 = 0, 180, 19\\nend\\n..."]]``。
    解析不出来就跳过 —— 用户可能手写别的 ``%geom`` 内容。
    """
    specs: list[ScanSpec] = []
    for block in blocks or ():
        try:
            name, body = block[0], block[1]
        except (TypeError, IndexError, KeyError):
            continue
        if str(name).strip().lower() != "geom" or not isinstance(body, str):
            continue
        lines = [line.strip() for line in body.splitlines()]
        in_scan = False
        for line in lines:
            low = line.lower()
            if low.startswith("scan"):
                in_scan = True
                continue
            if not in_scan:
                continue
            if low in ("end", ""):
                if low == "end":
                    in_scan = False
                continue
            spec = _parse_scan_line(line)
            if spec is not None:
                specs.append(spec)
    return specs


def _parse_scan_line(line: str) -> ScanSpec | None:
    """解析 ``D 1 2 3 4 = 0, 180, 19`` 这样的一行。"""
    if "=" not in line:
        return None
    left, right = line.split("=", 1)
    tokens = left.replace(",", " ").split()
    if len(tokens) < 2:
        return None
    kind = tokens[0].upper()
    if kind not in SCAN_LETTERS:
        return None
    atoms: list[int] = []
    for token in tokens[1:]:
        try:
            atoms.append(int(token))
        except ValueError:
            return None
    numbers = [n.strip() for n in right.replace(",", " ").split() if n.strip()]
    if len(numbers) < 2:
        return None
    try:
        start, end = float(numbers[0]), float(numbers[1])
        points = int(float(numbers[2])) if len(numbers) > 2 else 0
    except ValueError:
        return None
    return ScanSpec(kind=kind, atoms=tuple(atoms), start=start, end=end, points=points)


def scan_warnings(symbols, coords, blocks) -> list[str]:
    """检查一个任务带的扫描设置，返回**给用户看**的问题清单。

    这些问题是实测过的，不是理论担忧：

    1. **区间包含 180°** —— 二面角在 180° 处是 ORCA 冗余内坐标的奇点，
       实测直接 ``ORCA_GSTEP ... Aborting run``，退出码 2。
    2. **原子编号越界 / 个数不对** —— 会生成无效输入。

    .. note::
       **没有**做"起点离实际值太远就报警"。虽然直觉上该报，但实测数据不支持：
       实际值 -117.9° 时，起点 0°（差 118°）与 90°（差 152°）的结果都跑歪了，
       而 110°（差 132°）那组 7 个点全部完好 —— 差距最大的不是失败的那次。
       三个数据点推不出因果，硬报只会制造误报、让用户学会忽略警告。
       "实际值是多少"作为**信息**给出（见 :func:`scan_actual_values`），不作为警告。
    """
    warnings: list[str] = []
    for spec in parse_scan_blocks(blocks):
        need = {"B": 2, "A": 3, "D": 4}[spec.kind]
        if len(spec.atoms) != need:
            warnings.append(
                f"{spec.label}扫描需要 {need} 个原子编号，收到 {len(spec.atoms)} 个"
                f"（{list(spec.atoms)}）—— 这条扫描设置会被 ORCA 拒绝。"
            )
            continue
        if max(spec.atoms) >= len(symbols):
            warnings.append(
                f"{spec.label}扫描的原子编号 {list(spec.atoms)} 超出范围"
                f"（分子只有 {len(symbols)} 个原子，编号从 0 起）—— 任务会失败。"
            )
            continue

        if spec.crosses_180:
            warnings.append(
                f"{spec.label}扫描区间 {spec.start}→{spec.end} 包含 180°。"
                f"二面角/键角在 180° 处是 ORCA 内坐标的奇点，实测会直接崩："
                f"``ORCA_GSTEP: Geometry optimization in internals failed ... "
                f"Aborting run``（退出码 2）。"
                f"建议改成不跨 180° 的区间，或拆成两段。"
            )
    return warnings


def scan_actual_values(symbols, coords, blocks) -> list[dict]:
    """给出每条扫描设置在**当前几何下的实际值**（纯信息，不是警告）。

    用户填起点时最需要知道的就这一个数 —— 但"填得离它远会怎样"没有被证实，
    所以这里只如实汇报，不做判断。
    """
    out: list[dict] = []
    for spec in parse_scan_blocks(blocks):
        actual = measure(symbols, coords, spec.kind, spec.atoms)
        if actual is None:
            continue
        out.append({
            "label": spec.label,
            "atoms": list(spec.atoms),
            "start": spec.start,
            "end": spec.end,
            "actual": actual,
            "unit": "Å" if spec.kind == "B" else "°",
        })
    return out
