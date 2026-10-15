"""P0 第 2、3 项：结构化输出静默失败、同秒重复提交共用 job_id。

两条都属于"做错了不报错"：
* ``orca_2json`` 失败被彻底吞掉 → ``done`` 但能量与几何全空、零警告（审计 E3）
* 同一秒内提交相同参数 → 同一个 job_id → 两个 ORCA 争同一目录互毁（审计 E1，高危）
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.jobs import JobStore  # noqa: E402
from qcgui.jobs.store import make_job_id  # noqa: E402
from qcgui.model import JobSpec, JobType, Molecule  # noqa: E402

WATER_XYZ = "3\nwater\nO 0 0 0.1173\nH 0 0.7572 -0.4692\nH 0 -0.7572 -0.4692\n"


def _spec():
    return JobSpec(molecule=Molecule.from_xyz(WATER_XYZ), job_type=JobType.ENERGY,
                   method="HF", basis="STO-3G")


# ------------------------------------------------------------ E1：job_id 碰撞

def test_same_second_submissions_get_distinct_ids():
    """**核心用例**：同一秒内两次相同提交不能共用 job_id。

    这是审计里的高危项（E1）。旧行为：``make_job_id`` 只精确到秒，
    两次提交拿到同一个 id，于是两个 worker 争同一个 ``.out`` 互相截断、
    索引只剩一行、``cancel()`` 只杀得掉一个留下孤儿进程。

    先确认"不加防护就会撞"（证明这个用例测的是真问题），再确认 store 会岔开。
    """
    store = JobStore(Path(tempfile.mkdtemp(prefix="qcgui_ids_")) / "jobs")
    spec = _spec()

    # 前提：id 生成确实只精确到秒 —— 同一秒内完全相同
    assert make_job_id(spec) == make_job_id(spec), "前提不成立：id 生成本来就不同"

    first = store.create(spec)
    second = store.create(spec)
    third = store.create(spec)

    ids = [first.job_id, second.job_id, third.job_id]
    assert len(set(ids)) == 3, f"同秒三次提交拿到了重复 id：{ids}"
    # 目录必须各不相同，否则两个 ORCA 会互相截断
    dirs = {store.job_dir(i) for i in ids}
    assert len(dirs) == 3, f"任务目录重复：{dirs}"
    # 索引里应当是 3 行，不是 1 行
    assert len(store.list(limit=100)) == 3, "INSERT OR REPLACE 把前一条覆盖掉了"
    # 第一个仍是"时间戳+指纹"的原格式，后缀只在冲突时加
    assert first.job_id.startswith(make_job_id(spec)), first.job_id
    assert second.job_id.startswith(first.job_id), second.job_id
    print(f"    三个 id: {[i.split('-', 2)[-1] for i in ids]}")

    # 指纹仍然相同 —— "同参数可复跑"的原意保住了（只是不再覆盖前一次）
    fps = {r.fingerprint for r in store.list(limit=100)}
    assert len(fps) == 1, fps
    print("    指纹一致（同参数可复跑），但 id 与目录各不相同")


def test_explicit_job_id_is_not_renamed():
    """调用方显式指定的 id 原样保留 —— 去重只针对自动生成的那种。"""
    store = JobStore(Path(tempfile.mkdtemp(prefix="qcgui_ids2_")) / "jobs")
    record = store.create(_spec(), job_id="my-fixed-id")
    assert record.job_id == "my-fixed-id", record.job_id
    print("    显式 id 原样保留")


# ------------------------------------------------- E3：orca_2json 静默失败

def test_json_export_failure_is_reported():
    """``orca_2json`` 失败必须报出来，而不是交出一个"成功但没数据"的结果。

    直接调 ``_export_json``：用一个**必然失败**的假 orca_2json
    （Windows 下不可执行的 .bat / 不存在的路径），检查它返回失败说明。
    """
    import os
    from qcgui.engines.orca import OrcaEngine

    job_dir = Path(tempfile.mkdtemp(prefix="qcgui_json_"))
    engine = OrcaEngine()

    # 假的 orca_2json：一个必然以非零码退出的东西
    if os.name == "nt":
        fake = job_dir / "orca_2json.bat"
        fake.write_text("@echo off\r\nexit /b 2\r\n", encoding="ascii")
    else:
        fake = job_dir / "orca_2json"
        fake.write_text("#!/bin/sh\nexit 2\n", encoding="ascii")
        fake.chmod(0o755)

    original = engine.resolve_tool
    engine.resolve_tool = lambda name: str(fake) if name == "orca_2json" else original(name)
    try:
        (job_dir / "w.gbw").write_bytes(b"not really a gbw")
        failures = engine._export_json(job_dir, "w")
    finally:
        engine.resolve_tool = original

    assert failures, "假 orca_2json 必然失败，却没报出任何失败说明"
    assert any("退出码" in f for f in failures), failures
    print(f"    导出失败被记录：{failures[0]}")

    # 没装这个工具时不算失败（会走别的解析路径）
    engine.resolve_tool = lambda name: None
    try:
        assert engine._export_json(job_dir, "w") == []
    finally:
        engine.resolve_tool = original
    print("    未安装配套工具时不误报")


def test_export_issue_survives_the_later_issue_build():
    """回归：导出问题不能被后面那批 issues 的赋值覆盖掉。

    写这段时踩过：``parse()`` 里后面有 ``result.issues = (...)``，
    一次赋值就把前面 append 的导出问题整个抹掉了 —— 用 += 才对。
    """
    import inspect
    from qcgui.engines import orca as orca_mod

    source = inspect.getsource(orca_mod.OrcaEngine.parse)
    assert "result.issues +=" in source, "parse() 里必须是 += 追加"
    assert "result.issues = (" not in source, "不能用赋值把已记录的问题覆盖掉"
    print("    parse() 用的是 += 追加（不会被覆盖）")


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
