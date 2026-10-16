"""任务持久化：磁盘目录 + SQLite 索引。

目录布局::

    <root>/
      index.sqlite
      <job_id>/
        jobspec.json    输入指纹，可完整复现
        status.json     状态机快照（人可读）
        result.json     结构化结果
        *.inp / *.out / *.gbw / *.property.json   ← 引擎产出

任务目录里的一切在界面重启后仍然可用 —— 这是"断点续算"与结果追溯的基础。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..model import JobSpec, Result

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    status       TEXT NOT NULL,
    engine       TEXT,
    job_type     TEXT,
    method       TEXT,
    molecule     TEXT,
    job_dir      TEXT,
    created_at   REAL,
    started_at   REAL,
    finished_at  REAL,
    message      TEXT,
    final_energy REAL,
    n_imag_freq  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint);
"""


@dataclass
class JobRecord:
    """一个任务的状态快照。"""

    job_id: str
    fingerprint: str = ""
    status: str = "queued"
    engine: str = ""
    job_type: str = ""
    method: str = ""
    molecule: str = ""
    job_dir: str = ""
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    message: str = ""
    final_energy: float | None = None
    n_imag_freq: int | None = None

    @property
    def running(self) -> bool:
        return self.status in ("queued", "running")

    @property
    def duration(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.time()
        return end - self.started_at

    def to_dict(self) -> dict:
        data = asdict(self)
        # 派生量：asdict 只序列化字段，属性必须显式带上，
        # 否则界面上的"耗时"永远是空的
        data["duration"] = self.duration
        data["running"] = self.running
        return data


def make_job_id(spec: JobSpec, when: float | None = None) -> str:
    """时间戳 + 指纹：可排序、可追溯、同参数可复跑。"""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when or time.time()))
    return f"{stamp}-{spec.fingerprint()}"


class JobStore:
    """任务索引与结果存储。所有方法都可在多线程下调用。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "index.sqlite"
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ---------- 底层 ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    # ---------- 写 ----------

    def _unique_job_id(self, spec: JobSpec) -> str:
        """自动生成一个**当前不存在**的 job_id。

        ``make_job_id`` 只精确到秒，同一秒内提交相同参数会得到同一个 id。
        实测后果很严重（审计 E1，高危）：

        * 两个 worker 争同一个 ``.out``（引擎用 ``"w"`` 打开）互相截断
        * ``INSERT OR REPLACE`` 只留一行索引
        * ``_procs`` 字典键相同 → ``cancel()`` 只杀得掉一个，另一个变孤儿进程

        这里在冲突时加后缀 —— 保住"可排序、同参数可复跑"的原意（首次仍是
        时间戳+指纹），同时杜绝两个任务共享一个目录。

        只对**自动生成**的 id 去重；调用方显式指定的 id 原样保留（那是他的责任，
        测试也需要能钉住某个 id）。
        """
        base = make_job_id(spec)
        for n in range(1, 1000):
            candidate = base if n == 1 else f"{base}-{n}"
            if self.get(candidate) is None and not self.job_dir(candidate).exists():
                return candidate
        # 极端情况（999 次都撞上）：加个纳秒尾巴兜底
        return f"{base}-{time.time_ns()}"

    def create(self, spec: JobSpec, job_id: str | None = None) -> JobRecord:
        job_id = job_id or self._unique_job_id(spec)
        job_dir = self.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)

        record = JobRecord(
            job_id=job_id,
            fingerprint=spec.fingerprint(),
            status="queued",
            engine=spec.engine,
            job_type=spec.job_type.value,
            method=spec.level_of_theory,
            molecule=f"{spec.molecule.name} {spec.molecule.formula}",
            job_dir=str(job_dir),
            created_at=time.time(),
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO jobs
                   (job_id, fingerprint, status, engine, job_type, method, molecule,
                    job_dir, created_at, started_at, finished_at, message,
                    final_energy, n_imag_freq)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.job_id, record.fingerprint, record.status, record.engine,
                    record.job_type, record.method, record.molecule, record.job_dir,
                    record.created_at, record.started_at, record.finished_at,
                    record.message, record.final_energy, record.n_imag_freq,
                ),
            )
        self._write_status(record)
        return record

    def update(self, job_id: str, **fields) -> JobRecord | None:
        """更新若干列（列名即 JobRecord 字段名）。"""
        allowed = set(JobRecord.__dataclass_fields__) - {"job_id"}  # type: ignore[attr-defined]
        changes = {k: v for k, v in fields.items() if k in allowed}
        if not changes:
            return self.get(job_id)
        assignments = ", ".join(f"{k} = ?" for k in changes)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE jobs SET {assignments} WHERE job_id = ?",
                (*changes.values(), job_id),
            )
        record = self.get(job_id)
        if record:
            self._write_status(record)
        return record

    def save_result(self, job_id: str, result: Result) -> Path:
        """落盘结构化结果，并回填摘要字段。"""
        job_dir = self.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        path = result.save(job_dir / "result.json")
        # 结论也写进 message：任务列表只显示 message，不写进去的话
        # 用户看到绿点"done"就以为一切正常，不会去点开结果面板。
        #
        # 前缀区分严重度，界面据此上不同颜色的徽章：
        #   ⛔ 结果不可用（算出来的分子都变了）
        #   ⚠ 可用但有限制（某项分析被跳过 / 优化没到位）
        message = result.message
        if not message:
            verdict = result.verdict
            if verdict == "unusable":
                message = "⛔ " + result.verdict_reason
            elif verdict == "limited":
                message = "⚠ " + result.verdict_reason
        self.update(
            job_id,
            status=result.status,
            message=message[:500],
            final_energy=result.final_energy,
            n_imag_freq=len(result.frequencies) and result.n_imaginary_frequencies,
        )
        return path

    def _write_status(self, record: JobRecord) -> None:
        path = self.job_dir(record.job_id) / "status.json"
        try:
            path.write_text(
                json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    # ---------- 读 ----------

    def get(self, job_id: str) -> JobRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _row_to_record(row) if row else None

    def list(self, limit: int = 200) -> list[JobRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def count(self) -> int:
        """任务总数。

        为什么需要它：``/api/jobs`` 有 ``limit``，界面据此只显示前 N 条。
        以前界面既不知道总量、也不说"被截断了" —— 实测两个"结果不可用"的任务
        排在第 84/86 名，而界面上限是 80，它们因此在列表里**完全看不见**。
        有了总数，界面才能如实写出"只显示了前 300 条，另有 M 条未显示"。
        """
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
        return int(row["n"]) if row else 0

    def load_spec(self, job_id: str) -> JobSpec | None:
        """读回任务定义；文件缺失**或读不出来**都返回 ``None``。

        "读不出来"是真实存在的：校验规则是后来才加的，早先存下的 jobspec 可能
        不满足它（例如原子重合是在加了重叠校验之前存进去的）。**读历史数据不该
        被新规则炸掉** —— 校验的职责在接收用户输入那一侧，不在读盘这一侧。

        实测踩过：一个历史任务的 jobspec 让 ``/api/report.csv`` 整个 500
        （一张表被一条坏记录打挂，其余 100 条一条都导不出来）。
        """
        path = self.job_dir(job_id) / "jobspec.json"
        if not path.is_file():
            return None
        try:
            return JobSpec.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def load_result(self, job_id: str) -> Result | None:
        """读回结果；文件缺失或损坏都返回 ``None``（理由同 ``load_spec``）。"""
        path = self.job_dir(job_id) / "result.json"
        if not path.is_file():
            return None
        try:
            return Result.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None


def _row_to_record(row: sqlite3.Row) -> JobRecord:
    return JobRecord(**{k: row[k] for k in row.keys()})
