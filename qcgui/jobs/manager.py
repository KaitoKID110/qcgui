"""任务管理器：在独立子进程里跑引擎，并把过程/结果转成事件与持久化记录。

.. important::
   本模块**不导入 qcgui.engines**。引擎只在 worker 子进程里被导入，
   这是"界面与引擎分离"的具体落实点。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from ..model import JobSpec, Result
from ..procs import terminate
from .store import JobRecord, JobStore

#: 事件回调：(job_id, event) -> None
EventHandler = Callable[[str, dict], None]


class JobManager:
    """提交、跟踪、取消计算任务。线程安全。"""

    def __init__(
        self,
        root: str | Path,
        python: str | None = None,
        on_event: EventHandler | None = None,
    ):
        self.store = JobStore(root)
        self.python = python or sys.executable
        self.on_event = on_event
        self._procs: dict[str, subprocess.Popen] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.RLock()
        #: 上次进程留下的"运行中"任务（见 _reconcile_orphans）
        self.recovered_jobs: list[str] = self._reconcile_orphans()

    def _reconcile_orphans(self) -> list[str]:
        """把上一次进程留下的"运行中"任务标成中断。

        任务状态只活在**内存**里（``_procs``），磁盘上的记录靠自己更新。服务重启
        或崩溃时，正在跑的任务不会再有谁来改它的状态 —— 它会**永远停在
        "运行中"**。用户看到的现象就是"算完了还显示在运行"，很容易误判成卡住。

        这里在启动时清算一次：标成 ``cancelled`` 并写明原因（用 cancelled 而不是
        failed，因为它不是算错，是被打断了；界面上也是琥珀色而不是红色）。
        """
        recovered: list[str] = []
        try:
            records = self.store.list(limit=100000)
        except Exception:  # noqa: BLE001 - 清算失败不该阻止服务启动
            return recovered
        for record in records:
            if record.status not in ("running", "queued"):
                continue
            try:
                self.store.update(
                    record.job_id,
                    status="cancelled",
                    message="服务重启，任务已中断（不是计算失败）—— 需要的话重新运行一次",
                    finished_at=time.time(),
                )
                recovered.append(record.job_id)
            except Exception:  # noqa: BLE001
                continue
        return recovered

    # ------------------------------------------------------------ 提交

    def submit(
        self,
        spec: JobSpec,
        on_event: EventHandler | None = None,
        blocking: bool = False,
        job_id: str | None = None,
    ) -> JobRecord:
        """提交任务。``blocking=True`` 时等它跑完再返回最终记录。"""
        record = self.store.create(spec, job_id=job_id)
        if blocking:
            self._execute(record, spec, on_event)
            return self.store.get(record.job_id) or record
        threading.Thread(
            target=self._execute,
            args=(record, spec, on_event),
            name=f"job-{record.job_id}",
            daemon=True,
        ).start()
        return record

    # ------------------------------------------------------------ 取消

    def cancel(self, job_id: str) -> bool:
        """取消任务 —— 直接杀 worker 进程树（连带 ORCA/MPI 子进程）。"""
        with self._lock:
            proc = self._procs.get(job_id)
            if proc is None:
                return False
            self._cancelled.add(job_id)
        terminate(proc)
        return True

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            proc = self._procs.get(job_id)
        return proc is not None and proc.poll() is None

    def running_jobs(self) -> list[str]:
        with self._lock:
            return [jid for jid, p in self._procs.items() if p.poll() is None]

    def wait(self, job_id: str, timeout: float = 3600.0, interval: float = 0.2) -> JobRecord | None:
        """等待任务结束，返回最终记录。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_running(job_id):
                record = self.store.get(job_id)
                if record is not None and not record.running:
                    return record
            time.sleep(interval)
        return self.store.get(job_id)

    # ------------------------------------------------------------ 内部

    def _execute(self, record: JobRecord, spec: JobSpec, on_event: EventHandler | None) -> None:
        job_id = record.job_id
        job_dir = Path(record.job_dir)
        job_dir.mkdir(parents=True, exist_ok=True)

        spec_path = job_dir / "jobspec.json"
        spec_path.write_text(spec.canonical_json(), encoding="utf-8")

        self.store.update(job_id, status="running", started_at=time.time())
        self._emit(job_id, {"event": "running", "job_id": job_id, "job_dir": str(job_dir)}, on_event)

        env = os.environ.copy()
        env["PYTHONPATH"] = str(_repo_root()) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONIOENCODING"] = "utf-8"

        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0

        try:
            proc = subprocess.Popen(
                [self.python, "-m", "qcgui.worker", str(spec_path), str(job_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(_repo_root()),
                env=env,
                creationflags=creationflags,
            )
        except OSError as exc:
            self.store.update(
                job_id,
                status="failed",
                message=f"无法启动 worker 进程: {exc}",
                finished_at=time.time(),
            )
            self._emit(job_id, {"event": "error", "message": str(exc)}, on_event)
            return

        with self._lock:
            self._procs[job_id] = proc

        result: Result | None = None
        worker_error = ""
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if not line.strip():
                    continue
                event = _decode(line)
                kind = event.get("event")
                if kind == "finished":
                    payload = event.get("result")
                    if isinstance(payload, dict):
                        result = Result.from_dict(payload)
                elif kind == "error":
                    worker_error = str(event.get("message", ""))
                    self.store.update(job_id, message=worker_error[:500])
                self._emit(job_id, event, on_event)
        finally:
            proc.wait()
            with self._lock:
                self._procs.pop(job_id, None)
                cancelled = job_id in self._cancelled
                self._cancelled.discard(job_id)

        # 结果优先取事件；事件丢了就从磁盘恢复（worker 是先落盘再汇报）
        if result is None:
            result = self.store.load_result(job_id)

        if result is not None:
            if cancelled:
                result.status = "cancelled"
                result.message = result.message or "用户取消"
            self.store.save_result(job_id, result)
            self.store.update(job_id, finished_at=time.time())
        else:
            status = "cancelled" if cancelled else "failed"
            message = "用户取消" if cancelled else (worker_error or "worker 未产出结果")
            self.store.update(
                job_id,
                status=status,
                message=message[:500],
                finished_at=time.time(),
            )
            self._emit(job_id, {"event": "failed", "status": status, "message": message}, on_event)

    def _emit(self, job_id: str, event: dict, on_event: EventHandler | None) -> None:
        for handler in (on_event, self.on_event):
            if handler is None:
                continue
            try:
                handler(job_id, event)
            except Exception:  # 回调异常不应影响任务
                pass


def _decode(line: str) -> dict:
    try:
        event = json.loads(line)
        if isinstance(event, dict):
            return event
    except ValueError:
        pass
    # 非协议行（引擎崩溃时的杂音）原样转给界面
    return {"event": "raw", "line": line}


def _repo_root() -> Path:
    """仓库根目录（``qcgui/jobs/manager.py`` 往上三层）。"""
    return Path(__file__).resolve().parents[2]
