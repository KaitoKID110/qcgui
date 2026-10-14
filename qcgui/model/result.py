"""计算结果：与引擎无关的规范化模型。

上层（界面、导出、比对）只认这个对象，**不关心跑的是 ORCA、Psi4 还是高斯**。
每个引擎适配器负责把自己那套输出翻译成这里的字段。

约定：
- 能量单位 Hartree，长度单位 Å，频率单位 cm⁻¹，偶极矩单位 Debye。
- 缺失的字段保持 ``None`` / 空容器，绝不用 0 冒充。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: 状态取值
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_UNKNOWN = "unknown"


@dataclass
class Result:
    """一次计算的结构化结果。"""

    # --- 出处 ---
    engine: str = ""
    engine_version: str = ""
    spec_fingerprint: str = ""
    job_dir: str = ""

    # --- 状态 ---
    status: str = STATUS_UNKNOWN
    message: str = ""

    #: 不影响成败、但会让人误读结果的警告。
    #: 典型场景：用户请求了 NBO 分析，ORCA 因为没设 NBOEXE 把它**静默跳过**，
    #: 任务照常 TERMINATED NORMALLY —— 界面上会是绿点，但一个 NBO 数都没有。
    warnings: list[str] = field(default_factory=list)

    # --- 方法与任务 ---
    job_type: str = ""
    method: str = ""
    charge: int | None = None
    multiplicity: int | None = None

    # --- 能量 ---
    final_energy: float | None = None
    energy_components: dict[str, float] = field(default_factory=dict)

    # --- 结构 ---
    #: {"symbols": [...], "coords": [[x, y, z], ...], "units": "angstrom"}
    geometry: dict | None = None

    # --- 性质 ---
    dipole: dict | None = None
    loewdin_charges: list[float] = field(default_factory=list)
    mulliken_charges: list[float] = field(default_factory=list)
    mayer_charges: list[float] = field(default_factory=list)
    mayer_bond_orders: dict = field(default_factory=dict)
    #: 谐振频率 (cm⁻¹)。含平动/转动对应的 0.0 模态。
    frequencies: list[float] = field(default_factory=list)
    #: 热化学，规范键见 engines.orca._THERMO_FIELDS（Hartree / K / atm / amu）
    thermo: dict = field(default_factory=dict)
    #: 分子轨道：{"unit","orbitals":[{index,energy,occupancy,symmetry}],
    #:           "homo","lumo","gap_ev","point_group","n_electrons"}
    orbitals: dict = field(default_factory=dict)
    #: IRC 反应路径：帧数、逐帧能量、TS 帧、两端结构、能垒
    irc: dict = field(default_factory=dict)

    # --- 规模与耗时 ---
    n_atoms: int | None = None
    n_basis_functions: int | None = None
    n_electrons: int | None = None
    timings: dict = field(default_factory=dict)
    scf_timings: dict = field(default_factory=dict)

    # --- 产出文件（角色 → 路径） ---
    files: dict[str, str] = field(default_factory=dict)

    # ---------- 便捷属性 ----------

    @property
    def ok(self) -> bool:
        return self.status == STATUS_DONE

    @property
    def n_imaginary_frequencies(self) -> int:
        """虚频个数。极小点应为 0，过渡态应为 1。"""
        return sum(1 for f in self.frequencies if f < 0)

    def charges(self) -> list[float]:
        """优先返回 Löwdin 电荷，其次 Mayer。"""
        return self.loewdin_charges or self.mayer_charges

    # ---------- 序列化 ----------

    def to_dict(self) -> dict:
        data = asdict(self)
        # 派生量：asdict 只序列化字段，属性必须显式带上，
        # 否则界面拿不到虚频数（判断过渡态的关键指标）
        data["n_imaginary_frequencies"] = self.n_imaginary_frequencies
        data["ok"] = self.ok
        return data

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")
        return p

    @classmethod
    def from_dict(cls, data: dict) -> "Result":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def load(cls, path: str | Path) -> "Result":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ---------- 展示 ----------

    def summary_lines(self) -> list[str]:
        lines = [f"引擎      : {self.engine} {self.engine_version}".rstrip()]
        lines.append(f"任务      : {self.job_type}   方法: {self.method}")
        lines.append(f"状态      : {self.status}" + (f"  ({self.message})" if self.message else ""))
        if self.final_energy is not None:
            lines.append(f"最终能量  : {self.final_energy:.10f} Hartree")
        if self.frequencies:
            imag = self.n_imaginary_frequencies
            lines.append(f"频率      : {len(self.frequencies)} 个，其中虚频 {imag} 个")
        if self.n_basis_functions is not None:
            lines.append(f"基函数    : {self.n_basis_functions}   电子数: {self.n_electrons}")
        if self.timings:
            total = self.timings.get("SUM") or self.timings.get("TOTAL")
            if total is not None:
                lines.append(f"总耗时    : {total:.1f} s")
        return lines
