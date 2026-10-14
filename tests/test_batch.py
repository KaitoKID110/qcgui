"""批量计算（相对能量）测试。

.. important::
   本模块同样**不 import qcgui.engines** —— 批量走 JobManager → worker 子进程。

    pytest tests/test_batch.py -v
    python tests/test_batch.py
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.jobs import BatchRunner, JobManager  # noqa: E402
from qcgui.jobs.batch import QUANTITIES, BatchItem, BatchResult, _fill_relative, quantity_of  # noqa: E402
from qcgui.model import JobSpec, JobType, Molecule, Result  # noqa: E402


class _Skipped(Exception):
    pass


def _skip(reason: str):
    try:
        import pytest

        pytest.skip(reason)
    except ImportError:
        raise _Skipped(reason)


def _require_orca():
    from shutil import which

    if not (which("orca") or os.environ.get("QCGUI_ORCA_EXE")):
        _skip("本机没有 ORCA")


def _water(r: float, angle: float = 104.5, name: str = "water") -> Molecule:
    """按指定 O-H 键长构造水分子。"""
    half = math.radians(angle / 2.0)
    dx, dy = r * math.sin(half), r * math.cos(half)
    xyz = (
        f"3\n{name}\n"
        f"O   0.000000   0.000000   0.000000\n"
        f"H  {dx:10.6f} {dy:10.6f}   0.000000\n"
        f"H {-dx:10.6f} {dy:10.6f}   0.000000\n"
    )
    return Molecule.from_xyz(xyz)


# ---------------------------------------------------------------- 纯逻辑

def test_fill_relative_uses_lowest_as_reference():
    batch = BatchResult(
        quantity="energy",
        items=[
            BatchItem(name="a", status="done", value=-76.0),
            BatchItem(name="b", status="done", value=-76.1),
            BatchItem(name="c", status="failed", value=None),
        ],
    )
    _fill_relative(batch)
    assert batch.reference_name == "b"
    assert batch.reference_value == -76.1
    assert batch.items[1].relative_kj == 0.0
    assert abs(batch.items[0].relative_kj - 0.1 * 2625.4996394799) < 1e-6
    assert batch.items[2].relative_kj is None
    # 排序时失败项排在最后
    assert [it.name for it in batch.ranked()] == ["b", "a", "c"]


def test_quantity_of_reads_thermo():
    result = Result(final_energy=-76.0, thermo={"gibbs_free_energy_G_hartree": -75.9})
    assert quantity_of(result, "energy") == -76.0
    assert quantity_of(result, "gibbs") == -75.9
    assert quantity_of(result, "enthalpy") is None
    try:
        quantity_of(result, "nonsense")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("未知比较量应当报错")
    assert "energy" in QUANTITIES


def test_batch_result_csv_and_ranking():
    batch = BatchResult(
        quantity="energy", job_type="energy", level_of_theory="HF/def2-SVP",
        items=[
            BatchItem(name="high", status="done", value=-75.9, relative_kj=52.5, relative_kcal=12.5),
            BatchItem(name="low", status="done", value=-76.0, relative_kj=0.0, relative_kcal=0.0),
        ],
        reference_name="low",
    )
    out = Path(tempfile.mkdtemp(prefix="qcgui_batch_csv_")) / "b.csv"
    batch.write_csv(out)
    text = out.read_text(encoding="utf-8-sig")
    assert "low" in text and "high" in text
    lines = [l for l in text.splitlines() if l.strip()]
    assert lines[1].startswith("low"), "CSV 应按相对能量升序"


# ---------------------------------------------------------------- 真跑 ORCA

def test_batch_relative_energies_end_to_end():
    _require_orca()
    root = Path(tempfile.mkdtemp(prefix="qcgui_batch_"))
    manager = JobManager(root / "jobs")
    runner = BatchRunner(manager)

    # HF/def2-SVP 水的平衡键长约 0.94 Å，故 0.96 Å 那份应当最低
    molecules = [
        _water(0.90, name="water_090"),
        _water(0.96, name="water_096"),
        _water(1.10, name="water_110"),
    ]

    seen: list[str] = []
    batch = runner.run(
        molecules,
        job_type=JobType.ENERGY,
        method="HF",
        basis="def2-SVP",
        nprocs=2,
        on_item=lambda item: seen.append(item.status),
    )

    assert len(batch.items) == 3
    assert not batch.failed, [(it.name, it.status, it.message) for it in batch.items]
    assert batch.reference_name == "water_096", [
        (it.name, it.value, it.relative_kj) for it in batch.items
    ]

    by_name = {it.name: it for it in batch.items}
    assert by_name["water_096"].relative_kj == 0.0
    assert by_name["water_090"].relative_kj > 0
    assert by_name["water_110"].relative_kj > 0
    assert by_name["water_110"].relative_kj > by_name["water_090"].relative_kj

    # 相对值必须与绝对值自洽
    for item in batch.items:
        expected = (item.value - batch.reference_value) * 2625.4996394799
        assert abs(item.relative_kj - expected) < 1e-6

    # 每个结构都是独立、可追溯的任务
    assert all(it.job_id for it in batch.items)
    assert len({it.job_id for it in batch.items}) == 3
    records = {r.job_id for r in manager.store.list()}
    assert {it.job_id for it in batch.items} <= records

    saved = batch.save(root)
    assert saved["json"].is_file() and saved["csv"].is_file()
    loaded = saved["json"].read_text(encoding="utf-8")
    assert "water_096" in loaded

    print("    相对能量 (kJ/mol): " + ", ".join(
        f"{it.name}={it.relative_kj:.2f}" for it in batch.ranked()
    ))
    print("    参考（最低）: " + str(batch.reference_name))


def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed, skipped = [], []
    for name, func in tests:
        print(f"[ RUN  ] {name}")
        try:
            func()
            print(f"[  OK  ] {name}")
        except _Skipped as exc:
            skipped.append(name)
            print(f"[ SKIP ] {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, exc))
            print(f"[ FAIL ] {name}: {type(exc).__name__}: {exc}")
    print("-" * 60)
    print(f"通过 {len(tests) - len(failed) - len(skipped)} / 失败 {len(failed)} / 跳过 {len(skipped)}")
    for name, exc in failed:
        print(f"  失败: {name} -> {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
