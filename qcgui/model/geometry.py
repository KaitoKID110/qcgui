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
