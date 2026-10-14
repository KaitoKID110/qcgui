"""批量计算：把一批结构用同一方法跑掉，并给出相对能量。

典型用途是**中间体/构象的相对稳定性**：同一级别下比较若干结构，
相对最低者给出 ΔE。

设计上刻意做成 ``JobManager`` 之上的一层编排，而不是新造一个引擎任务类型：
- 每个结构仍是一个独立的、可单独重跑与追溯的任务（有各自的 job_id 与目录）
- 天然复用 worker 进程隔离、任务队列、持久化与取消
- 单个结构失败不会带塌整批
"""

from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from ..model import JobSpec, JobType, Molecule, Result
from ..units import HARTREE_TO_KCAL, HARTREE_TO_KJMOL

#: 可用于比较的能量量（需要该任务类型产出对应数据）
QUANTITIES: dict[str, str] = {
    "energy": "电子能",
    "enthalpy": "焓 H",
    "gibbs": "自由能 G",
    "zpe": "零点能 ZPE",
}

_THERMO_KEYS = {
    "enthalpy": "enthalpy_H_hartree",
    "gibbs": "gibbs_free_energy_G_hartree",
    "zpe": "zpe_hartree",
}


def quantity_of(result: Result, quantity: str = "energy") -> float | None:
    """从结果中取出用于比较的量（Hartree）。取不到返回 None。"""
    if quantity == "energy":
        return result.final_energy
    key = _THERMO_KEYS.get(quantity)
    if key is None:
        raise ValueError(f"未知的比较量 {quantity!r}；可选: {sorted(QUANTITIES)}")
    value = result.thermo.get(key)
    return float(value) if isinstance(value, (int, float)) else None


@dataclass
class BatchItem:
    """批次里的一个结构。"""

    name: str
    job_id: str | None = None
    status: str = "pending"
    n_atoms: int | None = None
    formula: str = ""
    value: float | None = None          # 用于比较的量（Hartree）
    relative_kj: float | None = None
    relative_kcal: float | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "done" and self.value is not None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BatchResult:
    """一批结构的相对能量结果。"""

    quantity: str = "energy"
    job_type: str = ""
    level_of_theory: str = ""
    items: list[BatchItem] = field(default_factory=list)
    reference_name: str | None = None
    reference_value: float | None = None
    created_at: float = field(default_factory=time.time)

    # ---------- 派生 ----------

    @property
    def finished(self) -> list[BatchItem]:
        return [it for it in self.items if it.ok]

    @property
    def failed(self) -> list[BatchItem]:
        return [it for it in self.items if it.status not in ("done", "pending") or not it.ok]

    @property
    def quantity_label(self) -> str:
        return QUANTITIES.get(self.quantity, self.quantity)

    def ranked(self) -> list[BatchItem]:
        """按相对能量升序（失败项排在最后）。"""
        return sorted(
            self.items,
            key=lambda it: (it.relative_kj is None, it.relative_kj if it.relative_kj is not None else 0.0),
        )

    # ---------- 序列化 ----------

    def to_dict(self) -> dict:
        data = asdict(self)
        data["quantity_label"] = self.quantity_label
        return data

    def save(self, directory: str | Path, stem: str = "batch") -> dict[str, Path]:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / f"{stem}.json"
        csv_path = directory / f"{stem}.csv"
        json_path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.write_csv(csv_path)
        return {"json": json_path, "csv": csv_path}

    def write_csv(self, path: str | Path) -> Path:
        path = Path(path)
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["结构", "状态", "原子数", "式量", "能量(Ha)", "相对(kJ/mol)", "相对(kcal/mol)", "任务ID", "说明"]
            )
            for item in self.ranked():
                writer.writerow(
                    [
                        item.name, item.status, item.n_atoms or "", item.formula,
                        f"{item.value:.10f}" if item.value is not None else "",
                        f"{item.relative_kj:.2f}" if item.relative_kj is not None else "",
                        f"{item.relative_kcal:.2f}" if item.relative_kcal is not None else "",
                        item.job_id or "", item.message,
                    ]
                )
        return path

    def summary_lines(self) -> list[str]:
        lines = [
            f"批量计算 · {self.job_type} · {self.level_of_theory}",
            f"比较量: {self.quantity_label}    结构数: {len(self.items)}"
            f"（成功 {len(self.finished)} / 失败 {len(self.failed)}）",
        ]
        if self.reference_name:
            lines.append(f"参考（最低）: {self.reference_name}")
        lines.append("")
        lines.append(f"{'结构':<24}{'相对 kJ/mol':>14}{'相对 kcal/mol':>16}{'状态':>10}")
        lines.append("-" * 66)
        for item in self.ranked():
            rel_kj = f"{item.relative_kj:.2f}" if item.relative_kj is not None else "—"
            rel_kcal = f"{item.relative_kcal:.2f}" if item.relative_kcal is not None else "—"
            lines.append(f"{item.name[:23]:<24}{rel_kj:>14}{rel_kcal:>16}{item.status:>10}")
        return lines


EventHandler = Callable[[str, dict], None]


class BatchRunner:
    """在 ``JobManager`` 之上把一批结构跑完并算相对能量。"""

    def __init__(self, manager, on_event: EventHandler | None = None):
        self.manager = manager
        self.on_event = on_event
        self._cancelled = False

    def cancel(self) -> None:
        """取消整批：停掉当前正在跑的那个任务，并阻止后续提交。"""
        self._cancelled = True
        for job_id in self.manager.running_jobs():
            self.manager.cancel(job_id)

    def build_specs(
        self,
        molecules: Sequence[Molecule],
        *,
        job_type: JobType = JobType.ENERGY,
        method: str = "B3LYP",
        basis: str = "def2-SVP",
        solvent: str | None = None,
        nprocs: int = 4,
        maxcore_mb: int = 2000,
        extra_keywords: tuple[str, ...] = (),
        extra_blocks: tuple[tuple[str, str], ...] = (),
    ) -> list[tuple[str, JobSpec]]:
        specs: list[tuple[str, JobSpec]] = []
        used: dict[str, int] = {}
        for index, molecule in enumerate(molecules):
            base = (molecule.name or f"mol{index + 1}").strip() or f"mol{index + 1}"
            used[base] = used.get(base, 0) + 1
            name = base if used[base] == 1 else f"{base}_{used[base]}"
            specs.append(
                (
                    name,
                    JobSpec(
                        molecule=molecule,
                        job_type=job_type,
                        method=method,
                        basis=basis,
                        solvent=solvent,
                        nprocs=nprocs,
                        maxcore_mb=maxcore_mb,
                        extra_keywords=tuple(extra_keywords),
                        extra_blocks=tuple(extra_blocks),
                        title=name,
                    ),
                )
            )
        return specs

    def run(
        self,
        molecules: Sequence[Molecule],
        *,
        job_type: JobType = JobType.ENERGY,
        method: str = "B3LYP",
        basis: str = "def2-SVP",
        solvent: str | None = None,
        nprocs: int = 4,
        maxcore_mb: int = 2000,
        quantity: str = "energy",
        extra_keywords: tuple[str, ...] = (),
        extra_blocks: tuple[tuple[str, str], ...] = (),
        on_event: EventHandler | None = None,
        on_item: Callable[[BatchItem], None] | None = None,
    ) -> BatchResult:
        """顺序跑完一批结构并返回相对能量。每个结构一个独立任务。"""
        self._cancelled = False
        specs = self.build_specs(
            molecules,
            job_type=job_type,
            method=method,
            basis=basis,
            solvent=solvent,
            nprocs=nprocs,
            maxcore_mb=maxcore_mb,
            extra_keywords=extra_keywords,
            extra_blocks=extra_blocks,
        )
        result = BatchResult(
            quantity=quantity,
            job_type=job_type.value,
            level_of_theory=f"{method}/{basis}".rstrip("/"),
            items=[BatchItem(name=name, n_atoms=spec.molecule.n_atoms,
                             formula=spec.molecule.formula)
                   for name, spec in specs],
        )

        for item, (name, spec) in zip(result.items, specs):
            if self._cancelled:
                item.status = "cancelled"
                item.message = "批次已取消"
                if on_item:
                    on_item(item)
                continue

            record = self.manager.submit(spec, on_event=on_event or self.on_event)
            item.job_id = record.job_id
            item.status = "running"
            if on_item:
                on_item(item)

            final = self.manager.wait(record.job_id)
            item.status = final.status if final else "failed"
            item.message = (final.message if final else "") or ""

            loaded = self.manager.store.load_result(record.job_id)
            if loaded is not None:
                value = quantity_of(loaded, quantity)
                if value is not None:
                    item.value = value
                elif item.status == "done":
                    item.message = item.message or f"结果中缺少「{QUANTITIES.get(quantity, quantity)}」"
            if on_item:
                on_item(item)

        _fill_relative(result)
        return result


def _fill_relative(result: BatchResult) -> None:
    """以最低者为参考，填入相对能量。"""
    values = [it.value for it in result.items if it.value is not None]
    if not values:
        return
    reference = min(values)
    result.reference_value = reference
    for item in result.items:
        if item.value is None:
            continue
        delta = item.value - reference
        item.relative_kj = delta * HARTREE_TO_KJMOL
        item.relative_kcal = delta * HARTREE_TO_KCAL
        if abs(delta) < 1e-12:
            result.reference_name = item.name


def collect_structures(paths: Iterable[str | Path], **kwargs) -> list[Molecule]:
    """从一组文件读入结构（.xyz / .cjson），保持输入顺序。"""
    return [Molecule.from_file(path, **kwargs) for path in paths]
