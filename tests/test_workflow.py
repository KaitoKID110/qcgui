# -*- coding: utf-8 -*-
r"""工作流（自动串链）的回归测试。

这些测试**不跑 ORCA**：用一个 stub manager 模拟"提交后立刻完成"，
这样能快速、确定地验证编排逻辑（顺序、几何血缘、失败处理、取消、持久化）。

真实 ORCA 的整链验证在报告里做过（opt → nmr，几何血缘正确、σ 可见），
不在单测里重复跑 —— 单测要的是快而确定。

为什么几何血缘是重点：如果某一步没有正确地从上一步取结果几何，那么
"优化完接着算 NMR"实际算的是**未优化结构**，而界面一切正常 ——
这正是本项目最怕的"看起来对其实错"。
"""
import json
import time
from pathlib import Path

from qcgui.jobs.workflow import (
    WorkflowRunner,
    molecule_from_record,
)
from qcgui.model import Result

_WATER = {
    "symbols": ["O", "H", "H"],
    "coords": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]],
}


class _Record:
    """stub 任务记录（只有工作流会用到的字段）。"""

    def __init__(self, job_id, status, message="", molecule="water", job_type="energy"):
        self.job_id = job_id
        self.status = status
        self.message = message
        self.molecule = molecule
        self.job_type = job_type
        self.running = False


class _Store:
    def __init__(self, root: Path):
        self.root = root
        self.records: dict = {}
        self.results: dict = {}

    def get(self, job_id):
        return self.records.get(job_id)

    def load_result(self, job_id):
        return self.results.get(job_id)


class _StubManager:
    """模拟 JobManager：submit 立刻完成，并把几何沿 x 平移 1.0。

    这样下一步如果**真的**用了上一步的结果几何，坐标会累积平移；
    没用的话就还是原始坐标 —— 一测便知。
    """

    def __init__(self, root: Path, fail_at: int | None = None):
        self.store = _Store(root)
        self.calls = []                  # 记录每次 submit 的 spec
        self.fail_at = fail_at
        self.cancelled = []
        self._n = 0

    def submit(self, spec, on_event=None, blocking=False, job_id=None):
        index = len(self.calls)
        self.calls.append(spec)
        self._n += 1
        jid = f"job-{self._n:03d}"
        record = _Record(jid, "done", molecule=spec.molecule.name,
                         job_type=spec.job_type.value)
        if self.fail_at is not None and index == self.fail_at:
            record.status = "failed"
            record.message = "stub 故意失败"
        self.store.records[jid] = record

        result = Result(status=record.status, engine="orca")
        # 几何沿 x 平移 1.0：用于验证"下一步用的是上一步的结果几何"
        result.geometry = {
            "symbols": list(spec.molecule.symbols),
            "coords": [[c[0] + 1.0, c[1], c[2]] for c in spec.molecule.coords],
            "units": "angstrom",
        }
        result.charge = spec.molecule.charge
        result.multiplicity = spec.molecule.multiplicity
        self.store.results[jid] = result
        return record

    def cancel(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return True


def _make_manager(tmp_path: Path, **kw) -> _StubManager:
    return _StubManager(tmp_path, **kw)


def _molecule():
    from qcgui.model import Atom, Molecule
    atoms = tuple(Atom(s, *c) for s, c in zip(_WATER["symbols"], _WATER["coords"]))
    return Molecule(atoms, 0, 1, "water")


def _wait(rec_getter, timeout=10.0):
    """等工作流结束（stub 很快，但仍在线程里）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = rec_getter()
        if rec is not None and rec.status != "running":
            return rec
        time.sleep(0.02)
    return rec_getter()


def test_stages_run_in_order_and_use_previous_geometry(tmp_path):
    """两步按顺序跑，且第二步用的是**第一步的结果几何**。"""
    mgr = _make_manager(tmp_path)
    runner = WorkflowRunner(mgr)

    rec = runner.submit(_molecule(), [{"job_type": "opt"}, {"job_type": "nmr"}])
    done = _wait(lambda: runner.get(rec.workflow_id))

    assert done.status == "done", done.message
    assert [c.job_type.value for c in mgr.calls] == ["opt", "nmr"]
    assert [s.status for s in done.stages] == ["done", "done"]
    assert all(s.job_id for s in done.stages), "每步都应有 job_id"

    # 第一次调用用的是原始坐标 x=0；第二次必须是 stub 结果（x=1）
    assert mgr.calls[0].molecule.coords[0][0] == 0.0
    assert mgr.calls[1].molecule.coords[0][0] == 1.0, (
        "第二步没有用上一步的结果几何 —— 那样会算未优化的结构"
    )
    print("    顺序正确，且第二步沿用了第一步的结果几何")


def test_failure_stops_the_chain_and_marks_rest_skipped(tmp_path):
    """某一步失败：整条链停下，剩余步骤标 skipped，并写清停在哪一步。"""
    mgr = _make_manager(tmp_path, fail_at=0)      # 第一步就失败
    runner = WorkflowRunner(mgr)

    rec = runner.submit(_molecule(),
                        [{"job_type": "opt"}, {"job_type": "nmr"}, {"job_type": "freq"}])
    done = _wait(lambda: runner.get(rec.workflow_id))

    assert done.status == "failed", done.status
    assert len(mgr.calls) == 1, f"失败后不该继续跑，实际跑了 {len(mgr.calls)} 步"
    assert done.stages[0].status == "failed"
    assert [s.status for s in done.stages[1:]] == ["skipped", "skipped"]
    assert "第 1 步" in done.message, done.message
    print("    失败即停，剩余步骤被跳过，消息说明了停在哪一步")


def test_unknown_job_type_is_rejected_before_anything_runs(tmp_path):
    """非法任务类型要在提交时就拒绝，不能跑一半才发现。"""
    mgr = _make_manager(tmp_path)
    runner = WorkflowRunner(mgr)
    try:
        runner.submit(_molecule(), [{"job_type": "opt"}, {"job_type": "不存在的类型"}])
    except ValueError as exc:
        assert "未知任务类型" in str(exc), str(exc)
        assert mgr.calls == [], "被拒绝的工作流不应提交任何任务"
        print("    非法任务类型在提交时就被拒绝")
    else:
        raise AssertionError("应当拒绝未知任务类型")


def test_empty_stages_is_rejected(tmp_path):
    mgr = _make_manager(tmp_path)
    runner = WorkflowRunner(mgr)
    try:
        runner.submit(_molecule(), [])
    except ValueError:
        print("    空步骤被拒绝")
    else:
        raise AssertionError("应当拒绝空步骤")


def test_workflow_state_is_persisted_and_readable(tmp_path):
    """工作流状态要落盘：关掉界面/重启后仍能查到做了什么。"""
    mgr = _make_manager(tmp_path)
    runner = WorkflowRunner(mgr)
    rec = runner.submit(_molecule(), [{"job_type": "opt"}])
    done = _wait(lambda: runner.get(rec.workflow_id))

    path = runner.root / f"{done.workflow_id}.json"
    assert path.is_file(), f"没有落盘: {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == "done"
    assert data["stages"][0]["job_type"] == "opt"
    assert data["stages"][0]["job_id"]

    # 新建一个 runner 模拟"服务重启"，应当能读到同一条记录
    runner2 = WorkflowRunner(mgr)
    again = runner2.get(done.workflow_id)
    assert again is not None and again.status == "done"
    print("    状态已落盘，重启后仍能读到")


def test_running_workflow_is_marked_interrupted_on_restart(tmp_path):
    """重启时残留的 running 必须改成 interrupted，不能永远显示"运行中"。"""
    mgr = _make_manager(tmp_path)
    runner = WorkflowRunner(mgr)
    # 手工造一个"上次没跑完"的记录
    wf_dir = runner.root
    leftover = {
        "workflow_id": "wf-20260101-000000-deadbeef",
        "status": "running",
        "created_at": time.time(),
        "molecule": "water",
        "stages": [
            {"job_type": "opt", "status": "done", "job_id": "job-001"},
            {"job_type": "nmr", "status": "running", "job_id": ""},
        ],
    }
    (wf_dir / "wf-20260101-000000-deadbeef.json").write_text(
        json.dumps(leftover), encoding="utf-8")

    runner2 = WorkflowRunner(mgr)     # 触发 reconcile
    rec = runner2.get("wf-20260101-000000-deadbeef")
    assert rec.status == "interrupted", rec.status
    assert "服务重启" in rec.message
    assert rec.stages[1].status == "skipped"
    print("    残留的 running 在重启时被标为 interrupted")


def test_molecule_from_record_requires_a_result_geometry(tmp_path):
    """上游没有结果几何时要明确报错，不能悄悄用输入结构。"""
    mgr = _make_manager(tmp_path)
    rec = mgr.submit.__self__ if False else None    # 占位，避免误用
    # 造一个没有 result 的任务
    mgr.store.records["job-x"] = _Record("job-x", "done")
    try:
        molecule_from_record(mgr, "job-x")
    except ValueError as exc:
        assert "结果几何" in str(exc), str(exc)
        print("    缺结果几何时明确报错")
    else:
        raise AssertionError("应当报错")
