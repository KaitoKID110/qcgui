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

#: "谱图没有真实强度"这句话**只有一份**：界面上的提示、PNG 图上的警告、任务结论的
#: 理由全都用它。同一件事写三遍，早晚会分叉，而这句话正是"别把峰高读成强度"的
#: 唯一提醒。
from .spectrum import NO_INTENSITY_NOTE

#: 状态取值
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_UNKNOWN = "unknown"

#: 问题严重度
SEVERITY_BLOCKING = "blocking"   # 结果不可用：拿到的不是你要的东西
SEVERITY_MAJOR = "major"         # 结果可能不可用：几何可能没到位
SEVERITY_MINOR = "minor"         # 主结果可用，只是某一项附带分析缺失

#: 结论取值
VERDICT_OK = "ok"                # 可以用
VERDICT_LIMITED = "limited"      # 可以用，但有前提
VERDICT_UNUSABLE = "unusable"    # 不能用

_VERDICT_LABELS = {
    VERDICT_OK: "结果可用",
    VERDICT_LIMITED: "结果可用，但有限制",
    VERDICT_UNUSABLE: "结果不可用",
}

#: ORCA 明确说了"没收敛"的措辞。注意 ``The optimization has not yet converged``
#: 是**每个中间圈都会打印**的（真实语料里出现 265 次），绝不能当判据。
NONCONVERGED_MARKERS = (
    "did not converge but reached the maximum",
    "reached the maximum number of optimization cycles",
)
#: ORCA 报告收敛的措辞（两者总是一起出现）
CONVERGED_MARKERS = ("THE OPTIMIZATION HAS CONVERGED", "HURRAY")


def describe_convergence_problem(text: str) -> str | None:
    """输出文本里有没有"优化没收敛"的证据？有就返回说明，正常返回 ``None``。

    放在模型层（而不是引擎里）是为了让**界面也能用** —— 收敛检查是后来加的，
    之前存下的结果没有这个结论；而读取时补算要读输出文件，界面又不能 import 引擎。

    判据是**证据式**的：只在有明确证据时报，绝不猜。

    * 有 ``did not converge ... reached the maximum`` → "跑满步数未收敛"
    * 有 ``THE OPTIMIZATION HAS CONVERGED`` / ``HURRAY`` → 正常
    * 两者都没有 → "没有收敛公告"

    这个三分法在真实任务库上核过：43 个几何优化类任务里 36 个有收敛公告、
    1 个明确跑满步数，其余全是被取消/失败的 —— 不产生误报。
    """
    if not text:
        return None
    if any(marker in text for marker in NONCONVERGED_MARKERS):
        return ("优化**没有收敛**：跑满了最大优化圈数就停了（ORCA 仍打印了正常终止）。"
                "这个几何还没到位，别直接拿去做单点能或频率 —— 加大优化圈数或换个起点重算。")
    if any(marker in text for marker in CONVERGED_MARKERS):
        return None
    return ("优化过程**没有报告收敛**（输出里找不到 ``THE OPTIMIZATION HAS CONVERGED``），"
            "但 ORCA 正常结束了。这个几何可能还没到位，别直接拿去做单点能或频率。")


@dataclass
class Issue:
    """一条影响"这个结果能不能用"的问题。

    为什么要有严重度：``warnings`` 是一串给人看的字符串，分不出
    "某项分析被跳过"（主结果照样能用）和"算出来的分子已经不是你要的了"
    （结果根本不能用）。结论判定需要后者的信息。
    """

    #: 机器可读标识，便于统计与测试（不要靠匹配 message 文本）
    code: str
    severity: str
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message}


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

    #: 结构化的问题（带严重度）。``warnings`` 是它的可读投影。
    #: 界面上的"任务结论"由它算出来，见 :attr:`verdict`。
    issues: list[Issue] = field(default_factory=list)

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
    #: 对应的红外强度 (km/mol)，与 ``frequencies`` **一一对应**（同长度同顺序）。
    #: 目前引擎层还没解析它（ORCA 输出里的 ``IR SPECTRUM`` 表），所以实际任务
    #: 这里是空的 —— 谱图遇到空强度会按 1.0 画并在界面上标注"无强度谱"。
    #: 留好这个字段是为了让"补上强度解析"只动引擎层一处，界面不用改。
    ir_intensities: list[float] = field(default_factory=list)
    #: 热化学，规范键见 engines.orca._THERMO_FIELDS（Hartree / K / atm / amu）
    thermo: dict = field(default_factory=dict)
    #: 分子轨道：{"unit","orbitals":[{index,energy,occupancy,symmetry}],
    #:           "homo","lumo","gap_ev","point_group","n_electrons"}
    orbitals: dict = field(default_factory=dict)
    #: IRC 反应路径：帧数、逐帧能量、TS 帧、两端结构、能垒
    irc: dict = field(default_factory=dict)
    #: NMR 屏蔽常数。ORCA 不把它写进 orca_2json 的 property.json，
    #: 只能从 .out 的 ``CHEMICAL SHIELDING SUMMARY`` 定宽表解析
    #: （与 Multiwfn 定宽表同类的**第二处**文本解析例外，见 engines/orca.py）。
    #: 形如 {"source": "orca_out_text", "quantity": "isotropic_shielding_ppm",
    #:      "rows": [{"index": 0, "element": "O", "isotropic": 365.694,
    #:               "anisotropy": 4.029}]}
    nmr: dict = field(default_factory=dict)
    #: 几何约束信息：``{"frozen_atoms": [...], "n_frozen": N, "note": "..."}``。
    #: 第一轮"刚性基团冻结"必须记录下来 —— 否则看结果时无法知道
    #: 哪些原子被固定在初始坐标、根本没参与优化。
    constraints: dict = field(default_factory=dict)
    #: 对称性信息：检测到的点群、是否把坐标完美化、完美化后的坐标等。
    #: 结构见 ``engines/orca.py::_fill_symmetry``。
    symmetry: dict = field(default_factory=dict)

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
    def lacks_ir_intensity(self) -> bool:
        """频率任务，但拿不到 IR 强度 → 谱图只有峰位、没有峰高。

        为什么这件事必须影响**任务结论**（而不只是画图）：ORCA 的 IR 强度只写在
        ``.out`` 的 ``IR SPECTRUM`` 定宽表里，结构化输出（``orca_2json`` 的
        ``THERMOCHEMISTRY_Energies``）里**没有**这个字段。按项目铁律 2
        （结果只从结构化输出取），本轮没有去解析文本，所以 ``ir_intensities``
        实际是空的，画出来的是一条**等高峰谱**。用户很容易把那些等高的峰读成
        真实红外强度 —— 那就是"算错了还显示绿点"。所以结论降级为"可用但有限制"。

        空 ``frequencies``（非频率任务）不算：没有谱可画，谈不上这个限制。
        长度对不上按"没有"处理，与 :func:`qcgui.model.spectrum.peaks_from_frequencies`
        的口径一致（数据不齐时宁可不画强度，也不要错位）。
        """
        return bool(self.frequencies) and len(self.ir_intensities) != len(self.frequencies)

    @property
    def verdict(self) -> str:
        """这个结果能不能用：``ok`` / ``limited`` / ``unusable``。

        这是本项目最该被看见的一个判断。已有三次教训都属于同一类 ——
        **算错了却显示绿色成功**（静默跳过的 NBO、消除成丁二烯却报 done 的扫描、
        跑满 50 圈没收敛的过渡态）。用户不会去翻日志，只会看那个绿点。

        判据偏保守：只要有任何警告而又没有结构化信息，也算"有限制"。
        """
        if self.status != STATUS_DONE:
            return VERDICT_UNUSABLE
        severities = {issue.severity for issue in self.issues}
        if SEVERITY_BLOCKING in severities:
            return VERDICT_UNUSABLE
        if severities & {SEVERITY_MAJOR, SEVERITY_MINOR}:
            return VERDICT_LIMITED
        if self.lacks_ir_intensity:
            # 频率/热化学本身没问题，坏的是"谱图峰高"这一项 —— 有限制，不是可用
            return VERDICT_LIMITED
        if self.warnings:
            # 有警告但没标严重度（老的结果文件）：保守处理
            return VERDICT_LIMITED
        return VERDICT_OK

    @property
    def verdict_label(self) -> str:
        return _VERDICT_LABELS.get(self.verdict, self.verdict)

    @property
    def verdict_reason(self) -> str:
        """一句话说清为什么是这个结论（给用户看的）。"""
        if self.status == STATUS_FAILED:
            return self.message or "计算没有正常结束"
        if self.status == STATUS_CANCELLED:
            return "任务被取消，结果不完整"
        if self.status not in (STATUS_DONE,):
            return f"任务状态为 {self.status or '未知'}"

        blocking = [i for i in self.issues if i.severity == SEVERITY_BLOCKING]
        if blocking:
            return blocking[0].message
        major = [i for i in self.issues if i.severity == SEVERITY_MAJOR]
        if major:
            return major[0].message
        if self.lacks_ir_intensity:
            # 比 minor/warnings 更该先说：它直接决定"图上那条谱能不能按强度读"。
            # 同时说清哪些部分仍然有效，免得用户以为整个任务白算了。
            return f"{NO_INTENSITY_NOTE}（频率与热化学本身有效）"
        minor = [i for i in self.issues if i.severity == SEVERITY_MINOR]
        if minor:
            return f"附带分析有缺失（{len(minor)} 项），主结果本身没受影响"
        if self.warnings:
            return self.warnings[0]
        return "全部检查通过：没有跳过项、成键未变化、优化已收敛"

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
        # 结论也带上：界面不用自己重新推导一套判据（那样两边迟早会不一致）
        data["verdict"] = self.verdict
        data["verdict_label"] = self.verdict_label
        data["verdict_reason"] = self.verdict_reason
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
        payload = {k: v for k, v in data.items() if k in known}
        # issues 必须还原成 Issue 对象：JSON 里是 dict，直接塞回去会让
        # verdict 之类的属性在 `issue.severity` 上炸（读旧结果就中招）。
        raw = payload.get("issues") or []
        restored: list[Issue] = []
        for item in raw:
            if isinstance(item, Issue):
                restored.append(item)
            elif isinstance(item, dict):
                restored.append(Issue(
                    code=str(item.get("code", "unknown")),
                    severity=str(item.get("severity", SEVERITY_MINOR)),
                    message=str(item.get("message", "")),
                ))
        payload["issues"] = restored
        return cls(**payload)

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
