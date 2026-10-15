"""P0 第 4 项：守卫的覆盖面。

两项待办：
1. 成键检测原来只对优化类任务生效 —— **扫描的每一帧也该查**
   （实测踩过：一个二面角扫描第 0 个点就把醇消成了丁二烯 + 水，
   19 个点全是消除产物，而外部看到的只是一条"像样的曲线"）
2. `_BOND_TOLERANCE = 1.25` 只在十几个分子上验证过 ——
   用仓库里 `examples/` 的**真实结构**批量回归，确认它既不会误判、
   也不会松到漏掉真正的成键变化
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.engines.orca import _scan_changed_frames  # noqa: E402
from qcgui.model.geometry import (  # noqa: E402
    _BOND_TOLERANCE,
    bond_set,
    connectivity_change,
)

EXAMPLES = REPO / "examples"


def _xyz_files():
    if not EXAMPLES.is_dir():
        return []
    return sorted(p for p in EXAMPLES.glob("*.xyz") if p.is_file())


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


def test_bond_detection_is_robust_to_thermal_noise():
    """阈值不能太紧：真实结构加一点热噪声后，**成键不能变**。

    这是"零误报"的基础 —— 如果 0.05 Å 的抖动就改变判定，
    那优化过程中的正常振动都会被误报成"分子变了"。
    用仓库里 `examples/` 的真实结构批量跑（不是十几个自造的）。
    """
    files = _xyz_files()
    assert files, f"{EXAMPLES} 里没有可用的 .xyz"
    rng = random.Random(20261015)
    checked = flipped = 0

    for path in files:
        try:
            symbols, coords = _read_xyz(path)
        except (ValueError, IndexError):
            continue
        if len(coords) < 2:
            continue
        checked += 1
        # 每个原子独立抖动 ±0.05 Å（约等于室温下的键长振幅上限）
        noisy = [[c[0] + rng.uniform(-0.05, 0.05),
                  c[1] + rng.uniform(-0.05, 0.05),
                  c[2] + rng.uniform(-0.05, 0.05)] for c in coords]
        # 要比的是**守卫实际用的判定**（connectivity_change，带回滞带），
        # 不是裸的 bond_set —— 后者是硬阈值，边界上的键本来就会因抖动翻转，
        # 而那正是回滞带要解决的问题。
        if connectivity_change(symbols, coords, noisy).changed:
            flipped += 1
            change = connectivity_change(symbols, coords, noisy)
            print(f"      ⚠ {path.name}: 抖动后守卫报了变化 —— {change.describe(symbols)}")

    assert flipped == 0, (
        f"{checked} 个真实结构里有 {flipped} 个被 0.05 Å 的抖动改变了成键判定 —— "
        f"阈值 {_BOND_TOLERANCE} 太紧，正常振动都会被误报"
    )
    print(f"    {checked} 个真实结构 × ±0.05 Å 抖动：成键判定零翻转（阈值 {_BOND_TOLERANCE}）")


def test_bond_detection_is_not_too_loose():
    """阈值也不能太松：把一根键**明确**拉开必须被判成断裂。

    注意配合回滞带看：判"断裂"要求超出 ``截断半径 × 1.1``。
    以 C–C 为例截断半径是 1.9 Å，所以 2.09 Å 之内属于**缓冲带**（不报），
    2.2 Å 以上才算明确断裂。这个取舍是有意的 —— 见 ``_BOND_HYSTERESIS``。
    """
    symbols = ["C", "C", "H", "H", "H", "H", "H", "H"]
    coords = [[0.0, 0.0, 0.0], [1.53, 0.0, 0.0],
              [-0.4, 0.9, 0.4], [-0.4, -0.9, 0.4], [-0.4, 0.0, -1.0],
              [1.93, 0.9, -0.4], [1.93, -0.9, -0.4], [1.93, 0.0, 1.0]]
    assert bond_set(symbols, coords) == {(0, 1), (0, 2), (0, 3), (0, 4),
                                         (1, 5), (1, 6), (1, 7)}, bond_set(symbols, coords)

    # 2.2 Å：明确超出缓冲带 → 必须报断裂
    stretched = [list(c) for c in coords]
    stretched[1] = [2.2, 0.0, 0.0]
    assert (0, 1) in connectivity_change(symbols, coords, stretched).broken, "2.2 Å 应当判为断裂"

    # 2.5 Å：更不用说
    stretched[1] = [2.5, 0.0, 0.0]
    assert (0, 1) in connectivity_change(symbols, coords, stretched).broken, "2.5 Å 应当判为断裂"

    # 真实的消除场景：C–O 从 1.43 拉到 3.4 Å、H 转移到 O —— 必须全报出来
    # 下标：0=C 1=C 2=O 3=H（甲基氢）…，所以 C–O 是 (1,2)，转移后 O–H 是 (2,3)
    mol = ["C", "C", "O", "H", "H", "H", "H"]
    before = [[0.0, 0.0, 0.0], [1.5, 0.0, 0.0], [2.9, 0.0, 0.0],
              [-0.6, 0.9, 0.0], [-0.6, -0.9, 0.0], [1.5, 0.9, 0.0], [3.86, 0.0, 0.0]]
    after = [list(c) for c in before]
    after[2] = [4.2, 2.0, 0.0]
    after[6] = [4.9, 2.0, 0.0]
    after[3] = [4.2, 2.6, 0.0]
    change = connectivity_change(mol, before, after)
    assert (1, 2) in change.broken, f"C–O 断裂必须报出：{change}"
    assert (2, 3) in change.formed, f"H 转移到 O 必须报出：{change}"
    print("    2.2/2.5 Å 拉伸与真实的消除场景都能报出（不漏报）")


# ------------------------------------------------------------------ 扫描逐帧

def _write_scan_job(job_dir: Path, symbols, before, frames):
    """造一个任务目录：jobspec.json（输入几何）+ 若干扫描帧。"""
    (job_dir / "jobspec.json").write_text(json.dumps({
        "molecule": {"symbols": symbols, "coords": before, "charge": 0,
                     "multiplicity": 1, "name": "scan"},
    }), encoding="utf-8")
    geometries = [{"value": float(i), "coords": f} for i, f in enumerate(frames)]
    return symbols, geometries


def test_scan_frames_are_checked_against_the_input_molecule():
    """扫描的每一帧都要和**输入分子**比，不是和第一帧比。

    为什么不能用第一帧当基准：实测那个消除任务**第 0 个点就已经变了**，
    拿它当参照会把"从头就是错的"误判成"没变化"。
    """
    symbols = ["C", "C", "O", "H", "H", "H", "H"]
    before = [[0.0, 0.0, 0.0], [1.5, 0.0, 0.0], [2.9, 0.0, 0.0],
              [-0.6, 0.9, 0.0], [-0.6, -0.9, 0.0], [1.5, 0.9, 0.0], [3.86, 0.0, 0.0]]
    # 第 0 帧就已经消除（C–O 断、H 转移）—— 正是实测踩到的形态
    broken = [list(c) for c in before]
    broken[2] = [4.2, 2.0, 0.0]
    broken[6] = [4.9, 2.0, 0.0]
    broken[3] = [4.2, 2.6, 0.0]

    job_dir = Path(tempfile.mkdtemp(prefix="qcgui_scanframes_"))
    symbols, geometries = _write_scan_job(job_dir, symbols, before, [broken, broken, broken])

    changed = _scan_changed_frames(job_dir, symbols, geometries)
    assert changed == [0, 1, 2], f"三帧都变了，应当全部标出：{changed}"
    print(f"    第 0 帧就已变化时，全部帧被标出：{changed}")

    # 用第一帧当基准就会漏掉全部（这就是不能用它的原因）
    from qcgui.model.geometry import connectivity_change
    naive = [i for i, g in enumerate(geometries)
             if connectivity_change(symbols, geometries[0]["coords"], g["coords"]).changed]
    assert naive == [], "前提：拿第一帧当基准确实一个都发现不了"
    print("    （反证：拿第一帧当基准 → 一个都发现不了）")


def test_scan_frames_stay_quiet_for_a_clean_scan():
    """正常的扫描不能被误报 —— 沿二面角转动不改变成键。"""
    symbols = ["C", "C", "O", "H", "H", "H", "H"]
    before = [[0.0, 0.0, 0.0], [1.5, 0.0, 0.0], [2.9, 0.0, 0.0],
              [-0.6, 0.9, 0.0], [-0.6, -0.9, 0.0], [1.5, 0.9, 0.0], [3.86, 0.0, 0.0]]

    import math
    frames = []
    for step in range(5):
        angle = math.radians(20 * step)
        # 绕 C0–C1 轴整体转动末端基团：键长全部保持
        moved = [list(c) for c in before]
        for idx in (2, 6):
            x, y, z = before[idx]
            moved[idx] = [x, y * math.cos(angle) - z * math.sin(angle),
                          y * math.sin(angle) + z * math.cos(angle)]
        frames.append(moved)

    job_dir = Path(tempfile.mkdtemp(prefix="qcgui_cleanscan_"))
    symbols, geometries = _write_scan_job(job_dir, symbols, before, frames)
    changed = _scan_changed_frames(job_dir, symbols, geometries)
    assert changed == [], f"正常转动不该被误报：{changed}"
    print("    正常二面角转动的 5 帧：零误报")


# ---------------------------------------------------------------- 脚本模式

def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
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
