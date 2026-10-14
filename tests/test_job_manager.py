"""JobManager 测试：进程隔离、任务持久化、取消。

.. important::
   本模块**刻意不 import qcgui.engines** —— 它模拟界面进程的视角。
   进程隔离的结论由子进程证据给出，而不是靠本模块的自述。

    pytest tests/test_job_manager.py -v
    python tests/test_job_manager.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.jobs import JobManager, JobStore  # noqa: E402
from qcgui.model import JobSpec, JobType, Molecule  # noqa: E402

WATER_HF_DEF2SVP = -75.960983978583

WATER_XYZ = """3
water
O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
"""


class _Skipped(Exception):
    pass


def _skip(reason: str):
    try:
        import pytest

        pytest.skip(reason)
    except ImportError:
        raise _Skipped(reason)


def _orca_available() -> bool:
    from shutil import which

    return which("orca") is not None or bool(os.environ.get("QCGUI_ORCA_EXE"))


def _require_orca():
    if not _orca_available():
        _skip("本机没有 ORCA")


def test_startup_reconciles_orphaned_running_jobs():
    """服务重启后，上次留下的"运行中"任务必须被清算掉。

    任务状态只活在内存里（``_procs``），磁盘上的记录靠自己更新。进程被强杀或
    崩溃时，正在跑的任务永远不会再有人来改它 —— 它会**永远停在"运行中"**，
    用户看到的就是"算完了还显示在运行"。
    """
    root = Path(tempfile.mkdtemp(prefix="qcgui_orphan_"))
    store = JobStore(root)
    spec = _spec()
    running = store.create(spec, job_id="20200101-000000-orphanrun")
    store.update(running.job_id, status="running")
    queued = store.create(spec, job_id="20200101-000001-orphanqueue")
    store.update(queued.job_id, status="queued")
    done = store.create(spec, job_id="20200101-000002-reallydone")
    store.update(done.job_id, status="done", message="正常完成")

    # 新建一个 manager == 服务重启
    manager = JobManager(root)
    assert set(manager.recovered_jobs) == {running.job_id, queued.job_id}, manager.recovered_jobs

    assert store.get(running.job_id).status == "cancelled"
    assert "服务重启" in store.get(running.job_id).message
    assert store.get(queued.job_id).status == "cancelled"
    # 已完成的不能被顺手改掉
    assert store.get(done.job_id).status == "done"
    assert store.get(done.job_id).message == "正常完成"
    print(f"    2 个孤儿任务被清算为 cancelled，已完成的不受影响")


def test_reading_a_broken_jobspec_returns_none_instead_of_raising():
    """读历史 jobspec 不该因为**后加的校验规则**而抛异常。

    实测踩过：两条早先存下的任务（原子重合，那是加重叠校验之前的事）让
    ``/api/report.csv`` 整个 500 —— 一张表被两条坏记录打挂。
    """
    root = Path(tempfile.mkdtemp(prefix="qcgui_badspec_"))
    store = JobStore(root)
    record = store.create(_spec(), job_id="20200101-000000-badspec")
    # jobspec.json 是**引擎**生成输入时写的，store.create 只建目录和索引行；
    # 这里直接造一个"读不出来"的文件即可。
    path = store.job_dir(record.job_id) / "jobspec.json"
    # 造一个违反当前校验的 jobspec：两个原子重合
    path.write_text(
        '{"molecule": {"symbols": ["O", "H", "H"], '
        '"coords": [[0, 0, 0], [0.001, 0, 0], [0, 1, 0]], '
        '"charge": 0, "multiplicity": 1, "name": "bad"}, '
        '"job_type": "energy", "method": "HF", "basis": "STO-3G"}',
        encoding="utf-8",
    )
    assert store.load_spec(record.job_id) is None, "坏 jobspec 应当返回 None 而不是抛异常"
    # 完全不是 JSON 也不能炸
    path.write_text("这不是 JSON", encoding="utf-8")
    assert store.load_spec(record.job_id) is None
    print("    违反当前校验的 jobspec 与非法 JSON 都返回 None")


def _water() -> Molecule:
    return Molecule.from_xyz(WATER_XYZ, name="water")


def _spec(job_type: JobType = JobType.ENERGY) -> JobSpec:
    return JobSpec(
        molecule=_water(),
        job_type=job_type,
        method="HF",
        basis="def2-SVP",
        nprocs=2,
    )


def _root(name: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"qcgui_mgr_{name}_"))


# ---------------------------------------------------------------- 进程隔离

_ISOLATION_SCRIPT = r"""
import sys
sys.path.insert(0, r"{repo}")
from qcgui.jobs import JobManager
from qcgui.model import JobSpec, JobType, Molecule

mol = Molecule.from_xyz(open(r"{xyz}", encoding="utf-8").read())
spec = JobSpec(molecule=mol, job_type=JobType.ENERGY,
               method="HF", basis="def2-SVP", nprocs=2)
mgr = JobManager(r"{root}")
rec = mgr.submit(spec, blocking=True)

print("ENGINES_IMPORTED=%s" % ("qcgui.engines" in sys.modules))
print("ORCA_MODULE=%s" % ("qcgui.engines.orca" in sys.modules))
print("STATUS=%s" % rec.status)
print("ENERGY=%.10f" % (rec.final_energy if rec.final_energy is not None else 0.0))
"""


def test_gui_side_never_imports_engines():
    """在干净的子进程里跑一个任务，证明调用方地址空间没有引擎模块。"""
    _require_orca()
    xyz = Path(tempfile.mkdtemp(prefix="qcgui_iso_")) / "water.xyz"
    xyz.write_text(WATER_XYZ, encoding="utf-8")

    script = _ISOLATION_SCRIPT.format(repo=REPO, xyz=xyz, root=_root("iso"))
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO),
        timeout=300,
    )
    out = proc.stdout or ""
    assert proc.returncode == 0, f"子进程失败:\n{out}\n{proc.stderr}"

    values = dict(
        line.split("=", 1) for line in out.splitlines() if "=" in line
    )
    assert values.get("ENGINES_IMPORTED") == "False", (
        "调用方进程导入引擎了 —— 进程隔离被破坏\n" + out
    )
    assert values.get("ORCA_MODULE") == "False", out
    assert values.get("STATUS") == "done", out
    assert abs(float(values.get("ENERGY", 0)) - WATER_HF_DEF2SVP) < 1e-6, out
    print("    调用方未导入引擎，且任务在 worker 子进程中完成")


def test_manager_module_has_no_engine_dependency():
    """静态检查：manager/store 的源码里不出现引擎导入。"""
    for rel in ("qcgui/jobs/manager.py", "qcgui/jobs/store.py", "qcgui/jobs/__init__.py"):
        text = (REPO / rel).read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and "engines" in stripped:
                raise AssertionError(f"{rel} 里出现了引擎导入: {stripped}")
    print("    manager/store 源码中无引擎导入")


# ---------------------------------------------------------------- 正常流程

def test_run_persist_and_reload():
    _require_orca()
    root = _root("persist")
    mgr = JobManager(root)

    record = mgr.submit(_spec(JobType.ENERGY), blocking=True)
    assert record is not None
    assert record.status == "done", record.message
    assert record.final_energy is not None
    assert abs(record.final_energy - WATER_HF_DEF2SVP) < 1e-6
    assert record.started_at and record.finished_at
    assert record.duration and record.duration > 0

    job_dir = Path(record.job_dir)
    for name in ("jobspec.json", "status.json", "result.json"):
        assert (job_dir / name).is_file(), f"缺少 {name}"
    assert (job_dir / "water_energy.inp").is_file()

    # 模拟界面重启：全新的 store 实例应当能读回一切
    reopened = JobStore(root)
    again = reopened.get(record.job_id)
    assert again is not None and again.status == "done"
    assert abs(again.final_energy - WATER_HF_DEF2SVP) < 1e-6

    spec_back = reopened.load_spec(record.job_id)
    assert spec_back is not None
    assert spec_back.fingerprint() == record.fingerprint

    result = reopened.load_result(record.job_id)
    assert result is not None and result.ok
    assert result.geometry and len(result.geometry["symbols"]) == 3

    assert any(r.job_id == record.job_id for r in reopened.list())
    print(f"    任务 {record.job_id} 落盘并可从新实例读回")


def test_event_stream_is_delivered():
    _require_orca()
    root = _root("events")
    mgr = JobManager(root)
    events: list[tuple[str, dict]] = []
    mgr.submit(_spec(JobType.ENERGY), on_event=lambda jid, ev: events.append((jid, ev)),
               blocking=True)

    kinds = [ev["event"] for _, ev in events]
    assert "running" in kinds
    assert "finished" in kinds
    assert "log" in kinds, "没有收到任何引擎日志行"
    print(f"    收到 {len(events)} 个事件，类型: {sorted(set(kinds))}")


# ---------------------------------------------------------------- 取消

def test_cancel_kills_worker_and_children():
    _require_orca()
    root = _root("cancel")
    mgr = JobManager(root)

    # 优化 + 频率足够长，来得及取消
    record = mgr.submit(_spec(JobType.OPT_FREQ))
    deadline = time.time() + 10.0
    while not mgr.is_running(record.job_id) and time.time() < deadline:
        time.sleep(0.05)
    if not mgr.is_running(record.job_id):
        _skip("worker 未能及时启动，跳过取消测试")

    time.sleep(1.5)
    assert mgr.cancel(record.job_id), "cancel() 应返回 True"

    final = mgr.wait(record.job_id, timeout=60)
    assert final is not None
    assert final.status == "cancelled", f"期望 cancelled，得到 {final.status}: {final.message}"
    assert not mgr.is_running(record.job_id)
    print(f"    任务状态: {final.status}")

    # 连根杀：不允许留下 ORCA 孤儿进程
    if os.name == "nt":
        time.sleep(1.5)
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq orca.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace",
        ).stdout
        orphans = [l for l in out.splitlines() if l.strip().startswith('"orca.exe"')]
        assert not orphans, f"取消后仍有 ORCA 孤儿进程: {orphans}"
        print("    无 ORCA 孤儿进程残留")


# ---------------------------------------------------------------- 脚本模式

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
