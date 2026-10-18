"""工作流：把多个任务串成一条链，每步自动用上一步的结果几何。

为什么需要
----------
"优化完结构接着算 NMR"是一类非常常见的组合。以前只能手工做两次：
先跑 opt，跑完再到「几何来源」里选那个任务、改成 NMR、再跑一次。
中间一旦忘了选几何来源，**算的就是没优化的结构**，而界面上一切正常
—— 这正是本项目最怕的"看起来对其实错"。

设计要点
--------
* **服务端执行**，不靠浏览器：关掉标签页工作流照样往下跑。
* 每一步都是**普通的任务记录**（``JobManager.submit``），所以：
  能在任务列表里看到、能点开看结果、能单独取消、结果照常持久化。
* 每步之间用 ``molecule_from_record`` 取上一步的**结果几何**
  （等价于界面上的「几何来源」），电荷/多重度也沿用上一步。
* 任何一步失败/取消 → 整条链停下，剩余标记 ``skipped``，
  并在工作流记录里写清**停在哪一步、为什么**。
* 状态落到 ``<jobs_root>/workflows/<id>.json``，重启后能查到；
  重启时仍在 ``running`` 的工作流会被标成 ``interrupted``
  （和任务库的孤儿回收同理，不能让它永远显示"运行中"）。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..model import Atom, JobSpec, JobType, Molecule
from .manager import JobManager

#: 事件回调：``(workflow_id, event)``。事件的 ``event`` 字段形如
#: ``workflow_started`` / ``workflow_stage`` / ``workflow_done`` / ``workflow_failed``。
WorkflowEventHandler = Callable[[str, dict], None]

_STATUS_RUNNING = "running"
_STATUS_DONE = "done"
_STATUS_FAILED = "failed"
_STATUS_CANCELLED = "cancelled"
_STATUS_INTERRUPTED = "interrupted"

_TERMINAL = (_STATUS_DONE, _STATUS_FAILED, _STATUS_CANCELLED, _STATUS_INTERRUPTED)


def make_workflow_id() -> str:
    """工作流 id：时间戳 + 短随机串（可排序、可读）。"""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return f"wf-{stamp}-{uuid.uuid4().hex[:8]}"


def molecule_from_record(manager: JobManager, job_id: str) -> Molecule:
    """取某个已完成任务的结果几何，作为下一步的起点。

    用**结果几何**而不是输入几何 —— 优化类任务的输入是未优化的坐标，
    拿它继续下一步等于白优化。这也是"几何来源"的含义。
    """
    record = manager.store.get(job_id)
    if record is None:
        raise ValueError(f"上游任务不存在: {job_id}")
    result = manager.store.load_result(job_id)
    if result is None or not result.geometry:
        raise ValueError(f"上游任务 {job_id} 没有可用的结果几何")
    geometry = result.geometry
    atoms = tuple(
        Atom(str(s), float(c[0]), float(c[1]), float(c[2]))
        for s, c in zip(geometry["symbols"], geometry["coords"])
    )
    base = (record.molecule or "mol").split()[0]
    return Molecule(
        atoms,
        int(result.charge if result.charge is not None else 0),
        int(result.multiplicity if result.multiplicity is not None else 1),
        f"{base}_{record.job_type}",
    )


@dataclass
class StageRecord:
    """工作流里的一步。"""

    job_type: str
    status: str = "pending"
    job_id: str = ""
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "job_type": self.job_type,
            "status": self.status,
            "job_id": self.job_id,
            "message": self.message,
        }


@dataclass
class WorkflowRecord:
    """一条工作流的持久化状态。"""

    workflow_id: str
    stages: list[StageRecord] = field(default_factory=list)
    status: str = _STATUS_RUNNING
    message: str = ""
    created_at: float = 0.0
    finished_at: float | None = None
    #: 初始结构的描述（给界面显示用）
    molecule: str = ""
    charge: int = 0
    multiplicity: int = 1
    #: 共享的计算设置（方法/基组/溶剂/核数…），每一步都可以覆盖
    shared: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "status": self.status,
            "message": self.message,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "molecule": self.molecule,
            "charge": self.charge,
            "multiplicity": self.multiplicity,
            "shared": dict(self.shared),
            "stages": [s.to_dict() for s in self.stages],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowRecord":
        rec = cls(
            workflow_id=str(data.get("workflow_id") or make_workflow_id()),
            status=str(data.get("status") or _STATUS_RUNNING),
            message=str(data.get("message") or ""),
            created_at=float(data.get("created_at") or 0.0),
            finished_at=data.get("finished_at"),
            molecule=str(data.get("molecule") or ""),
            charge=int(data.get("charge") or 0),
            multiplicity=int(data.get("multiplicity") or 1),
            shared=dict(data.get("shared") or {}),
        )
        rec.stages = [
            StageRecord(
                job_type=str(s.get("job_type") or ""),
                status=str(s.get("status") or "pending"),
                job_id=str(s.get("job_id") or ""),
                message=str(s.get("message") or ""),
            )
            for s in (data.get("stages") or [])
        ]
        return rec

    @property
    def current_index(self) -> int:
        """正在跑（或最后跑过）的那一步的下标；没有则 -1。"""
        for i, st in enumerate(self.stages):
            if st.status == "running":
                return i
        for i in range(len(self.stages) - 1, -1, -1):
            if self.stages[i].status in ("done", "failed", "cancelled"):
                return i
        return -1


class WorkflowRunner:
    """顺序执行一条工作流。线程安全，可被 Web 层直接调用。"""

    def __init__(self, manager: JobManager, on_event: WorkflowEventHandler | None = None,
                 root: str | Path | None = None) -> None:
        self.manager = manager
        self._on_event = on_event
        self._lock = threading.Lock()
        #: 工作流记录目录（默认与任务库同级）
        jobs_root = Path(manager.store.root)
        self.root = Path(root) if root is not None else jobs_root / "workflows"
        self.root.mkdir(parents=True, exist_ok=True)
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._reconcile()

    # ------------------------------------------------------------ 持久化

    def _path(self, wf_id: str) -> Path:
        return self.root / f"{wf_id}.json"

    def _save(self, rec: WorkflowRecord) -> None:
        try:
            self._path(rec.workflow_id).write_text(
                json.dumps(rec.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            # 落盘失败不该让工作流停下；内存里的状态仍然是对的
            pass

    def _reconcile(self) -> None:
        """重启后把"仍在 running"的工作流标成 interrupted。

        和任务库的孤儿回收同理：进程没了，那个"运行中"就是假的，
        不能让界面永远显示在跑。
        """
        for path in sorted(self.root.glob("wf-*.json")):
            try:
                rec = WorkflowRecord.from_dict(
                    json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                continue
            if rec.status == _STATUS_RUNNING:
                rec.status = _STATUS_INTERRUPTED
                rec.message = "服务重启，工作流中断（不是计算失败）—— 需要的话重新运行一次"
                for st in rec.stages:
                    if st.status in ("running", "pending"):
                        st.status = "skipped"
                rec.finished_at = time.time()
                self._save(rec)

    def get(self, wf_id: str) -> WorkflowRecord | None:
        path = self._path(wf_id)
        if not path.is_file():
            return None
        try:
            return WorkflowRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def list_recent(self, limit: int = 20) -> list[WorkflowRecord]:
        out: list[WorkflowRecord] = []
        for path in sorted(self.root.glob("wf-*.json"), reverse=True):
            try:
                out.append(WorkflowRecord.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError):
                continue
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------ 提交 / 取消

    def submit(
        self,
        molecule: Molecule,
        stages: list[dict],
        parent_job: str = "",
    ) -> WorkflowRecord:
        """提交一条工作流。

        ``stages`` 每项形如 ``{"job_type": "opt"}``，可带 ``method`` / ``basis`` /
        ``solvent`` / ``nprocs`` / ``extra_keywords`` / ``extra_blocks`` / ``maxcore_mb``
        覆盖共享设置。

        返回**刚创建的工作流记录**（``status=running``），执行在后台线程继续。
        调用方应当立刻轮询/订阅进度，而不是等这个函数返回。
        """
        if not stages:
            raise ValueError("工作流至少要有一个步骤")

        parsed: list[tuple[StageRecord, dict]] = []
        for raw in stages:
            if not isinstance(raw, dict):
                raise ValueError("每个步骤必须是对象")
            raw_type = str(raw.get("job_type") or "").strip()
            if not raw_type:
                raise ValueError("步骤缺少 job_type")
            try:
                job_type = JobType(raw_type)
            except ValueError:
                raise ValueError(f"未知任务类型: {raw_type}") from None
            overrides = {k: v for k, v in raw.items() if k != "job_type"}
            parsed.append((StageRecord(job_type=job_type.value), overrides))

        wf_id = make_workflow_id()
        rec = WorkflowRecord(
            workflow_id=wf_id,
            status=_STATUS_RUNNING,
            created_at=time.time(),
            molecule=molecule.name,
            charge=int(molecule.charge),
            multiplicity=int(molecule.multiplicity),
            stages=[st for st, _ in parsed],
        )
        with self._lock:
            self._cancel[wf_id] = threading.Event()
        self._save(rec)
        self._emit(wf_id, {"event": "workflow_started",
                           "workflow_id": wf_id,
                           "stages": [st.job_type for st, _ in parsed],
                           "molecule": molecule.name})

        thread = threading.Thread(
            target=self._run,
            args=(rec, molecule, parsed, parent_job.strip(), wf_id),
            name=f"workflow-{wf_id}",
            daemon=True,
        )
        with self._lock:
            self._threads[wf_id] = thread
        thread.start()
        return rec

    def cancel(self, wf_id: str) -> bool:
        """取消工作流：停掉正在跑的步骤，剩余步骤标 skipped。"""
        with self._lock:
            event = self._cancel.get(wf_id)
        rec = self.get(wf_id)
        if rec is None or rec.status in _TERMINAL:
            return False
        if event is not None:
            event.set()
        # 正在跑的那一步也一起杀（连带 ORCA/MPI 子进程）
        for st in rec.stages:
            if st.status == "running" and st.job_id:
                self.manager.cancel(st.job_id)
                break
        return True

    # ------------------------------------------------------------ 执行

    def _emit(self, wf_id: str, event: dict) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(wf_id, event)
        except Exception:      # 回调异常不影响工作流本身
            pass

    def _run(self, rec: WorkflowRecord, molecule: Molecule,
             parsed: list[tuple[StageRecord, dict]], parent_job: str,
             wf_id: str) -> None:
        """顺序执行每一步。每步都等上一步真的跑完。"""
        current = molecule
        # 第一步如果指定了上游任务，也用其结果几何（和界面上「几何来源」一致）
        if parent_job:
            try:
                current = molecule_from_record(self.manager, parent_job)
            except ValueError as exc:
                self._fail(rec, wf_id, 0, f"上游任务几何不可用: {exc}")
                return

        with self._lock:
            cancel_event = self._cancel.get(wf_id) or threading.Event()

        for index, (stage, overrides) in enumerate(parsed):
            if cancel_event.is_set():
                self._finish_cancelled(rec, wf_id, index)
                return

            stage.status = "running"
            self._save(rec)
            self._emit(wf_id, {"event": "workflow_stage",
                               "workflow_id": wf_id,
                               "index": index,
                               "job_type": stage.job_type,
                               "status": "running"})

            try:
                spec = self._build_spec(current, stage.job_type, overrides,
                                        parent_job if index == 0 else "")
            except (ValueError, TypeError) as exc:
                self._fail(rec, wf_id, index, f"参数不合法: {exc}")
                return

            record = self.manager.submit(spec, blocking=True)

            stage.job_id = record.job_id
            stage.status = record.status
            stage.message = record.message or ""
            self._save(rec)
            self._emit(wf_id, {"event": "workflow_stage",
                               "workflow_id": wf_id,
                               "index": index,
                               "job_type": stage.job_type,
                               "status": stage.status,
                               "job_id": stage.job_id,
                               "message": stage.message})

            if record.status != "done":
                self._fail(rec, wf_id, index,
                           f"第 {index + 1} 步（{stage.job_type}）没有成功："
                           f"{record.status} {record.message or ''}".strip())
                return

            # 下一步从上一步的**结果几何**继续
            try:
                current = molecule_from_record(self.manager, record.job_id)
            except ValueError as exc:
                self._fail(rec, wf_id, index,
                           f"第 {index + 1} 步没有可用的结果几何：{exc}")
                return

        rec.status = _STATUS_DONE
        rec.finished_at = time.time()
        rec.message = "全部步骤完成"
        self._save(rec)
        self._emit(wf_id, {"event": "workflow_done",
                           "workflow_id": wf_id,
                           "status": _STATUS_DONE,
                           "stages": [s.to_dict() for s in rec.stages]})

    def _build_spec(self, molecule: Molecule, job_type: str,
                    overrides: dict, parent_job: str) -> JobSpec:
        shared = {}
        for key in ("method", "basis", "nprocs", "maxcore_mb", "solvent",
                    "calc_hess", "engine"):
            if key in overrides and overrides[key] not in (None, ""):
                shared[key] = overrides[key]
        extra_keywords = overrides.get("extra_keywords") or ()
        if isinstance(extra_keywords, str):
            extra_keywords = tuple(extra_keywords.split())
        shared["extra_keywords"] = tuple(str(k) for k in extra_keywords)
        raw_blocks = overrides.get("extra_blocks") or ()
        blocks: list[tuple[str, str]] = []
        for item in raw_blocks:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                blocks.append((str(item[0]), str(item[1])))
        shared["extra_blocks"] = tuple(blocks)
        if "nprocs" in shared:
            shared["nprocs"] = int(shared["nprocs"])
        if "maxcore_mb" in shared:
            shared["maxcore_mb"] = int(shared["maxcore_mb"])
        return JobSpec(molecule=molecule, job_type=JobType(job_type),
                       parent_job=parent_job, **shared)

    def _fail(self, rec: WorkflowRecord, wf_id: str, index: int, message: str) -> None:
        """某一步失败：整条链停下，剩余步骤标 skipped。"""
        if 0 <= index < len(rec.stages):
            if rec.stages[index].status not in ("done", "failed", "cancelled"):
                rec.stages[index].status = "failed"
        for st in rec.stages[index + 1:]:
            if st.status == "pending":
                st.status = "skipped"
        rec.status = _STATUS_FAILED
        rec.message = message
        rec.finished_at = time.time()
        self._save(rec)
        self._emit(wf_id, {"event": "workflow_failed",
                           "workflow_id": wf_id,
                           "status": _STATUS_FAILED,
                           "message": message,
                           "stages": [s.to_dict() for s in rec.stages]})

    def _finish_cancelled(self, rec: WorkflowRecord, wf_id: str, index: int) -> None:
        for i, st in enumerate(rec.stages):
            if i == index and st.status == "running":
                st.status = "cancelled"
            elif st.status == "pending":
                st.status = "skipped"
        rec.status = _STATUS_CANCELLED
        rec.message = "已取消"
        rec.finished_at = time.time()
        self._save(rec)
        self._emit(wf_id, {"event": "workflow_cancelled",
                           "workflow_id": wf_id,
                           "status": _STATUS_CANCELLED,
                           "stages": [s.to_dict() for s in rec.stages]})
