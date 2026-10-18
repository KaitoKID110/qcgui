"""任务定义：一次计算请求的完整、不可变描述。

``JobSpec`` 必须能完整复现一次计算 —— 它会被序列化进任务目录，
并据此计算指纹，用于去重与结果追溯。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from .molecule import Molecule


class JobType(str, Enum):
    """计算任务类型。值即持久化用的稳定字符串。"""

    ENERGY = "energy"
    OPT = "opt"
    FREQ = "freq"
    OPT_FREQ = "opt_freq"
    TS = "ts"
    TS_FREQ = "ts_freq"
    NEB_TS = "neb_ts"
    IRC = "irc"
    SCAN = "scan"
    NMR = "nmr"

    @property
    def label(self) -> str:
        return _LABELS[self]


_LABELS: dict[JobType, str] = {
    JobType.ENERGY: "单点能",
    JobType.OPT: "几何优化",
    JobType.FREQ: "频率",
    JobType.OPT_FREQ: "优化 + 频率",
    JobType.TS: "过渡态优化",
    JobType.TS_FREQ: "过渡态优化 + 频率",
    JobType.NEB_TS: "NEB-TS 自动搜索过渡态",
    JobType.IRC: "内禀反应坐标",
    JobType.SCAN: "势能面扫描",
    JobType.NMR: "NMR 屏蔽常数",
}


@dataclass(frozen=True)
class JobSpec:
    """一次计算的完整描述。不可变，可序列化。"""

    molecule: Molecule
    job_type: JobType = JobType.ENERGY

    method: str = "B3LYP"
    basis: str = "def2-SVP"

    engine: str = "orca"
    nprocs: int = 4
    maxcore_mb: int = 2000

    #: SMD 隐式溶剂名（如 "water"、"acetonitrile"）；None 表示气相
    solvent: str | None = None
    #: 追加到关键字行的裸关键字
    extra_keywords: tuple[str, ...] = ()
    #: 引擎专属的块设置，形如 ``(("irc", "MAXITER 30"),)``。
    #: 由各引擎适配器决定渲染语法（ORCA 渲染为 ``%irc ... end``）。
    extra_blocks: tuple[tuple[str, str], ...] = ()
    #: 过渡态优化时先算精确 Hessian（ORCA: %geom Calc_Hess true）
    calc_hess: bool = False

    #: 几何来源：上游任务的 job_id。为空表示用 molecule 自带的坐标。
    #: 参考 ACH-Orca-Workbench 的"用父任务优化后的几何"设计。
    parent_job: str = ""

    #: NMR 参考物的屏蔽常数：``(("H", 33.713), ("C", 248.638), ...)``。
    #: 只有 NMR 任务会用；由 workflow 用**同一方法/基组现算**参考物后填入。
    #: 存成 tuple 而不是 dict，是为了保持 JobSpec 不可变/可哈希的一贯风格。
    nmr_reference: tuple[tuple[str, float], ...] = ()
    #: 参考方案标识（如 ``"tms"``），仅作追溯与界面展示。
    nmr_reference_scheme: str = ""
    #: 使用的配方 id（仅作追溯，不参与计算）
    recipe_id: str = ""

    title: str = ""

    def __post_init__(self) -> None:
        if self.nprocs < 1:
            raise ValueError(f"nprocs 必须 ≥ 1，得到 {self.nprocs}")
        if self.maxcore_mb < 0:
            raise ValueError(f"maxcore_mb 不能为负，得到 {self.maxcore_mb}")
        for value, what in ((self.method, "method"), (self.basis, "basis")):
            if value and not value.isascii():
                raise ValueError(f"{what} 含非 ASCII 字符: {value!r}（ORCA 输入必须是纯 ASCII）")

    # ---------- 展示 ----------

    @property
    def label(self) -> str:
        return self.job_type.label

    @property
    def level_of_theory(self) -> str:
        # 复合方法（r2SCAN-3c 等）没有独立基组，别留下孤零零的斜杠
        return f"{self.method}/{self.basis}" if self.basis else self.method

    def summary(self) -> str:
        bits = [self.level_of_theory, self.job_type.label]
        if self.solvent:
            bits.append(f"SMD({self.solvent})")
        return " · ".join(bits)

    # ---------- 序列化 ----------

    def to_dict(self) -> dict:
        return {
            "job_type": self.job_type.value,
            "engine": self.engine,
            "method": self.method,
            "basis": self.basis,
            "nprocs": self.nprocs,
            "maxcore_mb": self.maxcore_mb,
            "solvent": self.solvent,
            "extra_keywords": list(self.extra_keywords),
            "extra_blocks": [list(b) for b in self.extra_blocks],
            "calc_hess": self.calc_hess,
            "parent_job": self.parent_job,
            "nmr_reference": [list(item) for item in self.nmr_reference],
            "nmr_reference_scheme": self.nmr_reference_scheme,
            "recipe_id": self.recipe_id,
            "title": self.title,
            "molecule": {
                "name": self.molecule.name,
                "charge": self.molecule.charge,
                "multiplicity": self.molecule.multiplicity,
                "symbols": list(self.molecule.symbols),
                "coords": [list(c) for c in self.molecule.coords],
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JobSpec":
        mol = data["molecule"]
        from .molecule import Atom

        atoms = tuple(
            Atom(sym, float(c[0]), float(c[1]), float(c[2]))
            for sym, c in zip(mol["symbols"], mol["coords"])
        )
        molecule = Molecule(
            atoms,
            int(mol.get("charge", 0)),
            int(mol.get("multiplicity", 1)),
            mol.get("name", "mol"),
        )
        return cls(
            molecule=molecule,
            job_type=JobType(data.get("job_type", "energy")),
            method=data.get("method", "B3LYP"),
            basis=data.get("basis", "def2-SVP"),
            engine=data.get("engine", "orca"),
            nprocs=int(data.get("nprocs", 4)),
            maxcore_mb=int(data.get("maxcore_mb", 2000)),
            solvent=data.get("solvent"),
            extra_keywords=tuple(data.get("extra_keywords", ()) or ()),
            extra_blocks=tuple(
                (str(b[0]), str(b[1])) for b in (data.get("extra_blocks") or ())
            ),
            calc_hess=bool(data.get("calc_hess", False)),
            parent_job=data.get("parent_job", "") or "",
            nmr_reference=tuple(
                (str(item[0]), float(item[1]))
                for item in (data.get("nmr_reference") or ())
                if isinstance(item, (list, tuple)) and len(item) == 2
            ),
            nmr_reference_scheme=data.get("nmr_reference_scheme", "") or "",
            recipe_id=data.get("recipe_id", "") or "",
            title=data.get("title", ""),
        )

    # ---------- 指纹 ----------

    def canonical_json(self) -> str:
        """规范化 JSON：与浮点打印精度无关地稳定。"""
        data = self.to_dict()
        return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def fingerprint(self) -> str:
        """16 位十六进制指纹，用于任务去重与结果追溯。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()[:16]

    def identity(self) -> str:
        """指纹 + 人类可读摘要，便于任务目录命名。"""
        return f"{self.fingerprint()}"
