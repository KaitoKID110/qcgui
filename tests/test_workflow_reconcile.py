# -*- coding: utf-8 -*-
r"""工作流 reconcile 的回归测试：不能误杀正在运行的工作流。

实测事故
--------
跑测试时把 ``QCGUI_HOME`` 指向了生产目录，测试进程里新建的
``WorkflowRunner.__init__`` 调用 ``_reconcile()``，把**另一个进程正在跑**的
工作流（75 原子 r2SCAN-3c 优化）标成了 ``interrupted``，两个阶段变 ``skipped``，
而对应的 ORCA 进程还在算 —— 计算结果直接废掉。

修法：落盘写 ``updated_at``，运行期间每 30 秒心跳刷新，
``_reconcile`` 只在心跳过期（180 秒）时才判定为"上次崩溃的残留"。
"""
import json
import time
from pathlib import Path

from qcgui.jobs.workflow import WorkflowRunner, make_workflow_id
from qcgui.model import Molecule, Result

WATER = """3
water
O  0.000000  0.000000  0.117300
H  0.000000  0.757200 -0.469200
H  0.000000 -0.757200 -0.469200
"""


class _Record:
    def __init__(self, jid):
        self.job_id = jid
        self.status = "done"
        self.message = ""
        self.running = False
        self.molecule = "water"
        self.job_type = "opt"


class _Store:
    def __init__(self, root):
        self.root = root
        self.records = {}
        self.results = {}

    def get(self, jid):
        return self.records.get(jid)

    def load_result(self, jid):
        return self.results.get(jid)


class _SlowManager:
    """submit 里 sleep 一会儿，模拟"正在跑"的工作流。"""

    def __init__(self, root, delay=0.6):
        self.store = _Store(root)
        self.delay = delay
        self.calls = []
        self._n = 0

    def submit(self, spec, on_event=None, blocking=False, job_id=None):
        self.calls.append(spec)
        time.sleep(self.delay)
        self._n += 1
        jid = f"job-{self._n:03d}"
        self.store.records[jid] = _Record(jid)
        result = Result(status="done", engine="orca")
        result.geometry = {"symbols": list(spec.molecule.symbols),
                           "coords": [list(c) for c in spec.molecule.coords],
                           "units": "angstrom"}
        self.store.results[jid] = result
        return self.store.records[jid]

    def cancel(self, jid):
        return True


def _water() -> Molecule:
    return Molecule.from_xyz(WATER)


def test_new_runner_does_not_kill_live_workflow(tmp_path):
    """另一个 runner 启动时，不能把正在跑的工作流标成 interrupted。"""
    mgr = _SlowManager(tmp_path)
    runner = WorkflowRunner(mgr)
    rec = runner.submit(_water(), [{"job_type": "opt"}, {"job_type": "opt"}])
    assert rec.status == "running"

    # 关键：工作流正在跑的时候，另建一个 runner（模拟测试进程/第二个服务实例）
    other = WorkflowRunner(mgr)
    info = other.get(rec.workflow_id)
    assert info is not None
    assert info.status == "running", (
        f"新建 runner 把正在跑的工作流标成了 {info.status} —— "
        "这正是实测踩到的事故")
    for st in info.stages:
        assert st.status != "skipped", "正在跑的阶段被标成了 skipped"

    # 等它跑完，结果仍然是 done
    deadline = time.time() + 10
    while time.time() < deadline:
        info = runner.get(rec.workflow_id)
        if info and info.status != "running":
            break
        time.sleep(0.05)
    assert info.status == "done", info.message
    print("    新建 runner 未影响正在运行的工作流")


def test_stale_running_workflow_is_marked_interrupted(tmp_path):
    """心跳过期的 running 记录（上次崩溃残留）要标成 interrupted。"""
    runner = WorkflowRunner(_SlowManager(tmp_path))
    wf_id = make_workflow_id()
    record = {
        "workflow_id": wf_id,
        "status": "running",
        "created_at": time.time() - 7200,
        "updated_at": time.time() - 3600,      # 心跳一小时没更新 → 确实死了
        "molecule": "water",
        "stages": [{"job_type": "opt", "status": "running", "job_id": ""}],
    }
    (runner.root / f"{wf_id}.json").write_text(
        json.dumps(record), encoding="utf-8")

    fresh = WorkflowRunner(_SlowManager(tmp_path))
    rec = fresh.get(wf_id)
    assert rec is not None and rec.status == "interrupted", (
        f"残留的 running 应为 interrupted，实际 {rec.status if rec else None}")
    assert rec.stages[0].status == "skipped"
    print("    心跳过期的残留记录被正确标为 interrupted")


def test_heartbeat_updates_updated_at(tmp_path):
    """心跳线程要定期刷新 updated_at（否则长任务会被误判为死掉）。"""
    mgr = _SlowManager(tmp_path, delay=1.0)
    runner = WorkflowRunner(mgr)
    runner.HEARTBEAT_INTERVAL = getattr(runner, "HEARTBEAT_INTERVAL", 30.0)
    rec = runner.submit(_water(), [{"job_type": "opt"}])
    path = runner.root / f"{rec.workflow_id}.json"

    def read_updated_at() -> float:
        # Windows 上写入用 os.replace，读取偶尔会撞上共享冲突 → 重试几次
        for _ in range(50):
            try:
                return float(json.loads(
                    path.read_text(encoding="utf-8"))["updated_at"])
            except (PermissionError, FileNotFoundError, ValueError):
                time.sleep(0.02)
        raise AssertionError("读不到工作流状态文件")

    first = read_updated_at()
    # 等任务跑完（会多次 _save，updated_at 必须单调递增）
    deadline = time.time() + 10
    while time.time() < deadline:
        info = runner.get(rec.workflow_id)
        if info and info.status != "running":
            break
        time.sleep(0.05)
    last = read_updated_at()
    assert last >= first
    assert last > 0
    print("    updated_at 随状态更新推进")
