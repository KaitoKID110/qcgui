"""ORCA 引擎适配器。

数据流：``JobSpec`` → ``.inp``（ASCII，无 BOM）→ ``orca.exe`` → ``.out`` / ``.gbw``
→ ``orca_2json`` → JSON → ``Result``。

**结果只来自结构化 JSON**；``.out`` 文本仅用于判定是否正常终止与向界面转播日志。
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from ..model import (
    SEVERITY_BLOCKING,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    Issue,
    JobSpec,
    JobType,
    Result,
)
from ..model.molecule import BOHR_TO_ANGSTROM
from ..model.trajectory import read_xyz_frames
from ..orcatools import find_tool, orca_executable
from ..procs import terminate
from ..units import HARTREE_TO_EV, HARTREE_TO_KCAL
from .base import Engine, EngineError, LineHandler, RunOutcome, register

#: 环境变量：显式指定 orca 可执行文件
ENV_ORCA_EXE = "QCGUI_ORCA_EXE"

#: 任务类型 → ORCA 简单关键字
_JOB_KEYWORDS: dict[JobType, tuple[str, ...]] = {
    JobType.ENERGY: (),
    JobType.OPT: ("Opt",),
    JobType.FREQ: ("Freq",),
    JobType.OPT_FREQ: ("Opt", "Freq"),
    JobType.TS: ("OptTS",),
    JobType.TS_FREQ: ("OptTS", "Freq"),
    JobType.IRC: ("IRC",),
    # ORCA 的 NMR 屏蔽常数：``! ... NMR`` 会算 GIAO 屏蔽张量。
    # 实测水 HF/STO-3G 能正常终止，结果只在 .out 里（见 _fill_nmr）。
    JobType.NMR: ("NMR",),
}

#: 目前只声明**已经实测跑通**的任务类型
_CAPABILITIES = frozenset(_JOB_KEYWORDS)

_TERMINATION_MARKER = "ORCA TERMINATED NORMALLY"


#: 用户请求了、但 ORCA 会**静默跳过**的分析。
#:
#: 这类情况最危险：ORCA 打一行提示后继续跑完并正常终止，任务状态是"成功"，
#: 界面上是绿点 —— 但用户要的数据一个都没有。必须显式报出来。
#:
#: 形如 ``(输出里的跳过标记, 触发它的输入关键字, 给人看的说明)``。
#:
#: 注意**不要**把 ``Warning: ... skipping pre-diagonalization`` 这类收进来 ——
#: 那是 SCF 收敛策略的正常输出，不是"跳过了你要的分析"，报出来只会变成噪声。
_SILENT_SKIPS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "Skipping NBO-Analysis",
        ("NBO",),
        "你请求了 NBO 分析，但 ORCA 把它跳过了 —— 没有产生任何 NBO 数据。"
        "原因：缺少环境变量 NBOEXE（需要指向 NBO 6/7 的可执行文件），"
        "而 NBO 是需单独获取的第三方程序。本次结果里没有 NPA 电荷、"
        "没有 E(2) 二级微扰、没有 Wiberg 键级。",
    ),
)


def _requested_analyses(spec: JobSpec) -> set[str]:
    """把输入里出现过的关键字/块名收成一个集合，用来判断用户"要了什么"。"""
    tokens: set[str] = set()
    for word in (spec.method, spec.basis, *spec.extra_keywords):
        if word:
            tokens.add(str(word).strip().upper())
    for name, _ in spec.extra_blocks or ():
        tokens.add(str(name).strip().upper())
    return tokens


def _check_connectivity(spec: JobSpec, result: Result) -> list[Issue]:
    """算完的分子**还是不是你要的那个分子**？

    严重度 **blocking**：这个几何不能当作目标分子的结构用，
    所以整个结果判为不可用。

    实测踩过：一个带 ``%geom Scan`` 的优化任务，产物是 1,3-丁二烯 + 水
    （C–O 断裂、H 转移到 O），而任务状态是 ``done``、**零警告** ——
    用户只能靠肉眼看结构才发现不对。

    这里按共价半径判成键、前后一比，就能自动抓到。它不依赖能量、
    不依赖是否收敛、也不依赖"为什么会跑歪"，所以对任何原因导致的
    结构变化都有效。

    只对会移动原子的任务类型做（单点能的输出几何就是输入几何）。
    """
    from ..model.geometry import connectivity_change

    if spec.job_type not in (JobType.OPT, JobType.OPT_FREQ, JobType.TS, JobType.TS_FREQ):
        return []
    geometry = getattr(result, "geometry", None)
    if not geometry:
        return []
    symbols = list(geometry.get("symbols") or spec.molecule.symbols)
    coords_after = geometry.get("coords")
    if not coords_after or len(symbols) != spec.molecule.n_atoms:
        return []
    try:
        change = connectivity_change(symbols, spec.molecule.coords, coords_after)
    except (TypeError, ValueError, KeyError, IndexError):
        return []
    if not change.changed:
        return []
    name = spec.molecule.name or "原分子"
    return [Issue(
        code="connectivity_changed",
        severity=SEVERITY_BLOCKING,
        message=(
            f"算出来的**已经不是原来的分子**了：成键发生变化（{change.describe(symbols)}）。"
            f"这个几何不能当作 {name} 的结构用 —— 检查一下任务设置（尤其扫描的原子编号与范围），"
            f"或者用别的起点重算。"
        ),
    )]


def _check_convergence(spec: JobSpec, text: str) -> list[str]:
    """优化/扫描是不是真的收敛了。

    ORCA 会在**没收敛**的情况下也打印 ``ORCA TERMINATED NORMALLY``，
    于是任务显示绿点、几何其实没到位。实测：一个 ``OptTS`` 跑满 50 圈没收敛
    （``The optimization did not converge but reached the maximum number of
    optimization cycles``），任务状态是 ``done``。

    判据是**证据式**的（只在有明确证据时报警，绝不猜）：

    * 有 ``did not converge ... reached the maximum`` → 报"跑满步数未收敛"
    * 有 ``THE OPTIMIZATION HAS CONVERGED`` / ``HURRAY`` → 正常，不报
    * 两者都没有 → 报"没有收敛公告"

    这个三分法在真实任务库上核过：43 个几何优化类任务里，
    36 个有收敛公告、1 个明确跑满步数，其余全是被取消/失败的
    （不会走到这里，因为没正常终止时就返回了）—— 不产生误报。

    注意 ``The optimization has not yet converged`` 是**每个中间圈都会打印**
    的（语料里出现 265 次），绝不能拿它当判据。

    判据本身在 :func:`qcgui.model.result.describe_convergence_problem` —— 放在模型层
    是为了让界面读旧结果时也能用同一套措辞（界面不能 import 引擎）。
    """
    from ..model.result import describe_convergence_problem

    if spec.job_type not in (JobType.OPT, JobType.OPT_FREQ, JobType.TS, JobType.TS_FREQ):
        return []
    if _TERMINATION_MARKER not in text:
        return []          # 没正常终止的话，"失败"判定已经覆盖了
    problem = describe_convergence_problem(text)
    if not problem:
        return []
    code = "not_converged" if "跑满" in problem else "no_convergence_notice"
    return [Issue(code=code, severity=SEVERITY_MAJOR, message=problem)]


def _collect_issues(text: str, spec: JobSpec) -> list[Issue]:
    """找出"任务成功、但某项分析被悄悄跳过"的情况。

    严重度是 **minor**：主结果（能量/几何）照样能用，缺的只是某一项附带分析。
    用户仍然需要知道 —— 否则他会以为 NBO 数据在里面。
    """
    tokens = _requested_analyses(spec)
    found: list[Issue] = []
    for marker, triggers, message in _SILENT_SKIPS:
        if marker in text and tokens & set(triggers):
            found.append(Issue(code="analysis_skipped", severity=SEVERITY_MINOR,
                               message=message))
    return found


@register
class OrcaEngine(Engine):
    """ORCA 6.x 适配器。"""

    name = "orca"
    display_name = "ORCA"

    # ---------------------------------------------------------- 可用性

    def resolve_executable(self) -> str | None:
        """定位 orca 可执行文件（统一走 orcatools，避免两处各写一套）。"""
        return orca_executable()

    def resolve_tool(self, tool: str) -> str | None:
        """定位 ORCA 配套工具（如 orca_2json）。"""
        return find_tool(tool)

    def available(self) -> tuple[bool, str]:
        exe = self.resolve_executable()
        if exe:
            return True, exe
        return False, f"未找到 orca 可执行文件；请加入 PATH 或设置 {ENV_ORCA_EXE}"

    def capabilities(self) -> frozenset[JobType]:
        return _CAPABILITIES

    # ---------------------------------------------------------- 生成输入

    def build_input_text(self, spec: JobSpec) -> str:
        """构造 ORCA 输入文本。

        .. warning::
           必须是纯 ASCII。ORCA 遇到 BOM 或任何非 ASCII 字符会直接报
           ``non-ASCII character(s) found`` 并以退出码 50 终止。
        """
        keywords = [k for k in (spec.method, spec.basis) if k]
        keywords.extend(_JOB_KEYWORDS[spec.job_type])
        keywords.extend(k for k in spec.extra_keywords if k)
        # ORCA 对重复关键字直接报 INPUT ERROR（不是忽略），必须去重兜住
        keywords = _dedupe_keywords(keywords)

        lines: list[str] = []
        if spec.title:
            # 标题只是注释行，但 ORCA 会因为非 ASCII 直接拒绝整个输入
            # （non-ASCII character(s) found）。起了个中文名的分子不该算不了，
            # 所以这里把非 ASCII 剔掉 —— 界面上显示的名字仍然是原文。
            lines.append(f"# {_ascii_title(spec.title)}")
        lines.append("! " + " ".join(keywords))
        lines.append("")

        if spec.nprocs > 1:
            lines += ["%pal", f"  nprocs {spec.nprocs}", "end"]
        if spec.maxcore_mb:
            lines.append(f"%maxcore {spec.maxcore_mb}")
        if spec.solvent:
            lines += [
                "%cpcm",
                "  smd true",
                f'  SMDsolvent "{spec.solvent}"',
                "end",
            ]
        if spec.calc_hess and spec.job_type in (JobType.TS, JobType.TS_FREQ):
            lines += ["%geom", "  Calc_Hess true", "end"]
        for name, body in spec.extra_blocks:
            lines.append("%" + str(name).lstrip("%"))
            for row in str(body).splitlines():
                lines.append("  " + row.strip() if row.strip() else "")
            lines.append("end")

        lines.append("")
        lines.append(spec.molecule.orca_xyz_block())
        lines.append("")
        return "\n".join(lines)

    def generate_input(self, spec: JobSpec, job_dir: Path) -> Path:
        job_dir = Path(job_dir)
        job_dir.mkdir(parents=True, exist_ok=True)
        text = self.build_input_text(spec)
        try:
            text.encode("ascii")
        except UnicodeEncodeError as exc:  # pragma: no cover - 防御性
            raise EngineError(f"ORCA 输入含非 ASCII 字符：{exc}") from exc

        inp = job_dir / f"{_safe_stem(spec)}.inp"
        inp.write_text(text, encoding="ascii", newline="\n")

        # 顺带落盘 JobSpec，保证任务目录自解释、可复现
        spec_path = job_dir / "jobspec.json"
        spec_path.write_text(spec.canonical_json(), encoding="utf-8")
        return inp

    # ---------------------------------------------------------- 运行

    def run(
        self,
        spec: JobSpec,
        job_dir: Path,
        on_line: LineHandler | None = None,
        cancel: object | None = None,
    ) -> RunOutcome:
        exe = self.resolve_executable()
        if not exe:
            raise EngineError(f"未找到 orca 可执行文件；请设置 {ENV_ORCA_EXE}")

        job_dir = Path(job_dir)
        inp = _find_input(job_dir, spec)
        out_path = job_dir / f"{inp.stem}.out"

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        with open(out_path, "w", encoding="utf-8", errors="replace") as fh:
            proc = subprocess.Popen(
                [exe, inp.name],
                cwd=str(job_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
            assert proc.stdout is not None

            pump = threading.Thread(
                target=_pump_lines,
                args=(proc.stdout, fh, on_line),
                daemon=True,
            )
            pump.start()

            cancelled = False
            try:
                while proc.poll() is None:
                    if cancel is not None and _is_set(cancel):
                        cancelled = True
                        terminate(proc)
                        break
                    time.sleep(0.25)
            finally:
                code = proc.wait()
                pump.join(timeout=5.0)

        return RunOutcome(
            exit_code=0 if cancelled else (code or 0),
            cancelled=cancelled,
            out_file=str(out_path),
        )

    # ---------------------------------------------------------- 解析

    def parse(self, spec: JobSpec, job_dir: Path) -> Result:
        job_dir = Path(job_dir)
        inp = _find_input(job_dir, spec)
        base = inp.stem
        out_path = job_dir / f"{base}.out"
        gbw_path = job_dir / f"{base}.gbw"

        result = Result(
            engine=self.name,
            spec_fingerprint=spec.fingerprint(),
            job_dir=str(job_dir),
            job_type=spec.job_type.value,
            method=spec.level_of_theory,
            charge=spec.molecule.charge,
            multiplicity=spec.molecule.multiplicity,
            n_atoms=spec.molecule.n_atoms,
        )
        result.files["input"] = str(inp)
        if out_path.is_file():
            result.files["output"] = str(out_path)
        if gbw_path.is_file():
            result.files["wavefunction"] = str(gbw_path)

        text = out_path.read_text(encoding="utf-8", errors="replace") if out_path.is_file() else ""
        normal = _TERMINATION_MARKER in text

        # --- 结构化输出（唯一的数据来源） ---
        export_failures: list[str] = []
        if gbw_path.is_file():
            export_failures = self._export_json(job_dir, base)

        main_json = _load_json(job_dir / f"{base}.json")
        prop_json = _load_json(job_dir / f"{base}.property.json")
        if prop_json:
            result.files["json"] = str(job_dir / f"{base}.property.json")
        elif main_json:
            result.files["json"] = str(job_dir / f"{base}.json")

        # 导出失败 + 什么都没拿到 = 结果里不会有能量和几何。
        # 以前这种情况状态仍是 done、零警告，用户以为算成功了（审计 E3）。
        if export_failures and not (main_json or prop_json):
            result.issues.append(Issue(
                code="json_export_failed",
                severity=SEVERITY_BLOCKING,
                message=(
                    "结构化输出导出失败（" + "；".join(export_failures) + "），"
                    "而且没有任何可用的 JSON —— 本次结果**没有能量、也没有几何**。"
                    "任务虽然正常结束，但这个结果拿不到数据，请检查 ORCA 安装是否完整"
                    "（尤其 orca_2json 能否单独运行）。"
                ),
            ))
        elif export_failures:
            result.issues.append(Issue(
                code="json_export_partial",
                severity=SEVERITY_MAJOR,
                message=(
                    "结构化输出导出有失败（" + "；".join(export_failures) + "），"
                    "但有部分数据可用。结果可能缺少某些字段，重要结论建议核对原始输出。"
                ),
            ))

        if main_json:
            header = main_json.get("ORCA Header") or {}
            if isinstance(header, dict):
                result.engine_version = str(
                    header.get("version") or header.get("Version") or ""
                ) or result.engine_version
            self._fill_orbitals(result, main_json)

        if prop_json:
            self._fill_from_property(result, prop_json)
            status = (prop_json.get("Calculation_Status") or {}).get("Status", "")
            if isinstance(status, str) and "NORMAL" in status.upper():
                normal = True

        # IRC 的能量剖面不在 JSON 里，而在 ORCA 写的多帧轨迹文件中
        if spec.job_type is JobType.IRC:
            self._fill_irc(result, job_dir, base)

        # NMR 屏蔽常数**不在** orca_2json 的 property.json 里（实测：
        # 顶层只有 Calculation_Info/Status/Timings/Geometries），
        # 只在 .out 的定宽表里。这是本项目"结果只从结构化输出取"的
        # **第二处**明确例外（第一处是 Multiwfn 定宽表）。
        # 解析失败时留空并记 issue，绝不编数据。
        if spec.job_type is JobType.NMR:
            self._fill_nmr(result, text, spec)

        # --- 状态判定 ---
        if normal:
            result.status = "done"
        else:
            result.status = "failed"
            result.message = _first_error(text) or "ORCA 未正常终止"

        # 任务成功也可能"少了东西"。分三档，严重度决定界面上的"结论"：
        #   minor    —— 某项附带分析被静默跳过（主结果可用）
        #   major    —— 优化没到位 / 导出不完整（几何可能不能用）
        #   blocking —— 算出来的分子已经变了、或导出失败且没有数据
        #
        # 注意是 **+=** 不是 =：上面的结构化输出检查已经往 issues 里放过东西了，
        # 用赋值会把它们整个覆盖掉。
        result.issues += (
            _collect_issues(text, spec)
            + _check_connectivity(spec, result)
            + _check_convergence(spec, text)
        )
        # warnings 是给界面直接显示的可读投影，保持兼容
        result.warnings = [issue.message for issue in result.issues]

        return result

    def parse_scan_profile(self, job_dir: str | Path) -> dict | None:
        """松弛扫描的曲线数据；不是扫描任务时返回 ``None``。"""
        return parse_scan_profile(Path(job_dir))

    # --- 内部 ---
    def _fill_orbitals(self, result: Result, main_json: dict) -> None:
        """取分子轨道能量 / 占据数 / 对称性，并算 HOMO–LUMO 间隙。

        这些 ``orca_2json`` 直接给出，**不需要额外算一次**。
        轨道编号是 0 起的，与 ``orca_plot`` 的编号一致（实测：水的 STO-3G
        7 个轨道，MO=6 可导、MO=7 报 ``Invalid MO requested``）。
        """
        molecule = (main_json or {}).get("Molecule") or {}
        block = molecule.get("MolecularOrbitals") or {}
        mos = block.get("MOs") or []
        if not mos:
            return

        orbitals: list[dict] = []
        for index, mo in enumerate(mos):
            energy = _scalar(mo.get("OrbitalEnergy"))
            occupancy = _scalar(mo.get("Occupancy"))
            orbitals.append(
                {
                    "index": index,
                    "energy": float(energy) if isinstance(energy, (int, float)) else None,
                    "occupancy": float(occupancy) if isinstance(occupancy, (int, float)) else None,
                    "symmetry": str(mo.get("OrbitalSymLabel") or ""),
                }
            )

        occupied = [o for o in orbitals if (o["occupancy"] or 0) > 1e-6]
        virtual = [
            o for o in orbitals
            if (o["occupancy"] or 0) <= 1e-6 and o["energy"] is not None
        ]
        homo = occupied[-1] if occupied else None
        lumo = virtual[0] if virtual else None

        entry: dict = {
            "unit": str(block.get("EnergyUnit") or "Eh"),
            "count": len(orbitals),
            "orbitals": orbitals,
            "point_group": str(molecule.get("PointGroup") or ""),
            "n_electrons": int(round(sum(o["occupancy"] or 0 for o in orbitals))),
            "homo": homo["index"] if homo else None,
            "lumo": lumo["index"] if lumo else None,
        }
        if homo and homo["energy"] is not None:
            entry["homo_energy"] = homo["energy"]
            entry["homo_ev"] = homo["energy"] * HARTREE_TO_EV
        if lumo and lumo["energy"] is not None:
            entry["lumo_energy"] = lumo["energy"]
            entry["lumo_ev"] = lumo["energy"] * HARTREE_TO_EV
        if "homo_energy" in entry and "lumo_energy" in entry:
            gap = entry["lumo_energy"] - entry["homo_energy"]
            entry["gap_hartree"] = gap
            entry["gap_ev"] = gap * HARTREE_TO_EV

        result.orbitals = entry

    def _export_json(self, job_dir: Path, base: str) -> list[str]:
        """调用 ``orca_2json`` 生成结构化 JSON，**返回失败说明**。

        以前是彻底静默的：导出失败时结果里能量与几何全空、状态却是 ``done``、
        零警告（审计 E3）。实测真实二进制在缺 ``.property.txt`` 时会以 exit=2
        退出 —— 那种"绿点但没数据"比直接报错危险得多。

        返回空列表有两种含义：没装这个配套工具（会走别的解析路径），或者全部成功。
        调用方靠"有没有导出结果文件"来区分。
        """
        tool = self.resolve_tool("orca_2json")
        if not tool:
            return []
        failures: list[str] = []
        gbw = job_dir / f"{base}.gbw"
        for args in ([str(gbw)], [base, "-property"]):
            try:
                proc = subprocess.run(
                    [tool, *args],
                    cwd=str(job_dir),
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                failures.append(f"{' '.join(args)}：{type(exc).__name__}")
                continue
            if proc.returncode != 0:
                failures.append(f"{' '.join(args)} 退出码 {proc.returncode}")
        return failures

    def _fill_from_property(self, result: Result, prop: dict) -> None:
        info = prop.get("Calculation_Info") or {}
        status = prop.get("Calculation_Status") or {}
        if not result.engine_version:
            result.engine_version = str(status.get("version", "") or "")

        result.n_atoms = _as_int(info.get("NumOfAtoms")) or result.n_atoms
        result.n_basis_functions = _as_int(info.get("NumOfBasisFuncts"))
        result.n_electrons = _as_int(info.get("NumOfElectrons"))
        if _as_int(info.get("Charge")) is not None:
            result.charge = _as_int(info.get("Charge"))
        if _as_int(info.get("Mult")) is not None:
            result.multiplicity = _as_int(info.get("Mult"))

        timings = prop.get("Calculation_Timings") or {}
        if isinstance(timings, dict):
            result.timings = {k: v for k, v in timings.items() if isinstance(v, (int, float))}

        geometries = prop.get("Geometries") or []
        if not geometries:
            return
        geom = geometries[-1]

        energy_block = _first(geom.get("Energy"))
        if isinstance(energy_block, dict):
            value = _scalar(energy_block.get("totalEnergy"))
            if isinstance(value, (int, float)):
                result.final_energy = float(value)

        geometry = _extract_geometry(geom.get("Geometry"))
        if geometry:
            result.geometry = geometry

        dipole_block = _first(geom.get("Dipole_Moment"))
        if isinstance(dipole_block, dict):
            total = _floats(dipole_block.get("dipoleTotal"))
            magnitude = _scalar(dipole_block.get("dipoleMagnitude"))
            result.dipole = {
                "total": total,
                "magnitude": float(magnitude) if isinstance(magnitude, (int, float)) else None,
            }

        loewdin = _first(geom.get("Loewdin_Population_Analysis"))
        if isinstance(loewdin, dict):
            result.loewdin_charges = _floats(loewdin.get("AtomicCharges"))

        mayer = _first(geom.get("Mayer_Population_Analysis"))
        if isinstance(mayer, dict):
            result.mayer_charges = _floats(mayer.get("QA") or mayer.get("AtomicCharges") or [])
            result.mayer_bond_orders = {
                "values": _floats(mayer.get("BondOrders") or []),
                "components": mayer.get("components"),
                "threshold": _scalar(mayer.get("bondThresh")),
            }

        mulliken = _first(geom.get("Mulliken_Population_Analysis"))
        if isinstance(mulliken, dict):
            result.mulliken_charges = _floats(mulliken.get("AtomicCharges"))

        scf_timings = geom.get("SCF_Timings")
        if isinstance(scf_timings, dict):
            result.scf_timings = {
                k: v for k, v in scf_timings.items() if isinstance(v, (int, float))
            }

        # 频率与热化学：ORCA 6 把它们放在 THERMOCHEMISTRY_Energies 里
        thermo = _first(geom.get("THERMOCHEMISTRY_Energies"))
        if isinstance(thermo, dict):
            result.frequencies = _floats(thermo.get("FREQ") or [])
            result.thermo = _extract_thermo(thermo)
            if isinstance(thermo.get("elEnergy"), (int, float)):
                result.energy_components.setdefault("电子能", float(thermo["elEnergy"]))

        if not result.frequencies:
            result.frequencies = _extract_frequencies(geom)

    def _fill_nmr(self, result: Result, text: str, spec: JobSpec | None = None) -> None:
        r"""从 ORCA 输出解析 ``CHEMICAL SHIELDING SUMMARY`` 定宽表。

        为什么必须解析文本：实测 ``! HF STO-3G NMR`` 跑完后，``orca_2json``
        产出的 ``property.json`` **完全没有 NMR 数据**（顶层只有
        Calculation_Info / Calculation_Status / Calculation_Timings /
        Geometries），屏蔽常数只出现在 .out 里：

            --------------------------------
            CHEMICAL SHIELDING SUMMARY (ppm)
            --------------------------------

              Nucleus  Element    Isotropic     Anisotropy
              -------  -------  ------------   ------------
                  0       O          365.694          4.029
                  1       H           33.679         15.151

        **量纲注意**：这里给的是**绝对屏蔽常数 σ(ppm)**，不是化学位移 δ。
        δ = σ(参考物) − σ(样品)，需要 TMS 之类参考物单独计算。qcgui 不内置
        参考值，所以界面上必须写 σ 并说明换算方式 —— 直接叫"化学位移"会
        让人误以为能和文献数值直接比。
        """
        import re as _re

        lines = text.splitlines()
        start = None
        for i, line in enumerate(lines):
            if "CHEMICAL SHIELDING SUMMARY" in line.upper():
                start = i
                break
        if start is None:
            from ..model import Issue, SEVERITY_MAJOR
            result.issues.append(Issue(
                code="nmr_summary_missing",
                severity=SEVERITY_MAJOR,
                message="NMR 任务没在输出里找到 CHEMICAL SHIELDING SUMMARY —— "
                        "屏蔽常数没算出来或输出被截断，界面不给数值。",
            ))
            return

        row_re = _re.compile(
            r"^\s*(\d+)\s+([A-Za-z]{1,3})\s+"
            r"(-?\d+\.\d+)\s+(-?\d+\.\d+)\s*$"
        )
        rows = []
        seen_header = False
        for line in lines[start:start + 300]:
            if "Nucleus" in line and "Element" in line:
                seen_header = True
                continue
            if not seen_header:
                continue
            if line.strip() and set(line.strip()) <= set("-"):
                continue
            m = row_re.match(line)
            if m:
                rows.append({
                    "index": int(m.group(1)),
                    "element": m.group(2),
                    "isotropic": float(m.group(3)),
                    "anisotropy": float(m.group(4)),
                })
                continue
            if line.strip() and rows:
                break

        if not rows:
            from ..model import Issue, SEVERITY_MAJOR
            result.issues.append(Issue(
                code="nmr_summary_unparsed",
                severity=SEVERITY_MAJOR,
                message="找到了 CHEMICAL SHIELDING SUMMARY 标题，但没解析出数据行 —— "
                        "ORCA 输出格式可能变了，界面不给数值。",
            ))
            return

        # 元素顺序对一下分子：错位的话数值会张冠李戴，宁可判失败
        symbols = list((result.geometry or {}).get("symbols") or [])
        if symbols and len(symbols) == len(rows):
            bad = [r["element"] for i, r in enumerate(rows)
                   if i < len(symbols) and r["element"] != symbols[i]]
            if bad:
                from ..model import Issue, SEVERITY_MAJOR
                result.issues.append(Issue(
                    code="nmr_element_mismatch",
                    severity=SEVERITY_MAJOR,
                    message="NMR 表里的元素顺序和结构对不上（%s…），"
                            "数值可能错位，界面不给结论。" % bad[:3],
                ))
                return

        # ---- δ = σ(参考) − σ(样品) ----
        #
        # 参考值必须由**同一方法/基组现算**提供（workflow 会先跑参考物的
        # 优化 + NMR）。没有参考值的元素一律不给 δ —— 拿别的核的参考值顶上
        # 会得到完全错误的数字，那比"没有"更糟。
        references: dict[str, float] = {}
        scheme = ""
        if spec is not None:
            references = {str(el): float(v) for el, v in (spec.nmr_reference or ())}
            scheme = spec.nmr_reference_scheme or ""
        missing: list[str] = []
        if references:
            from ..model.nmr_reference import compute_deltas
            rows, missing = compute_deltas(rows, references)

        note = "绝对屏蔽常数 σ，不是化学位移 δ；δ = σ(参考) − σ(样品)，需单独算参考物"
        if references:
            # 说明里不要用 markdown 星号：前端会对内容做转义，星号会原样显示出来。
            # 「某元素没有参考物」单独放在 elements_without_reference 里由界面渲染，
            # 否则和 note 叠加会出现两遍（实测踩过）。
            tag = scheme.upper() if scheme else "参考物"
            note = (f"δ = σ({tag}) − σ(样品)，参考物用同一方法/基组现算；"
                    "未做经验标定，换泛函/基组后 δ 会偏移")

        payload = {
            "source": "orca_out_text",
            "quantity": "isotropic_shielding_ppm",
            "note": note,
            "n_nuclei": len(rows),
            "rows": rows,
        }
        if references:
            payload["reference_scheme"] = scheme
            payload["references"] = dict(references)
            if missing:
                payload["elements_without_reference"] = list(missing)
        result.nmr = payload

    def _fill_irc(self, result: Result, job_dir: Path, base: str) -> None:
        """解析 IRC 轨迹。

        ORCA 把轨迹写成多帧 XYZ，**每帧注释行带该帧能量**
        （``Coordinates from ORCA-job <name> E -227.777590200722``）。
        因此能量剖面来自这个结构化产物，而不是去正则匹配人读的输出表格。
        """
        full = job_dir / f"{base}_IRC_Full_trj.xyz"
        frames = read_xyz_frames(full) if full.is_file() else []
        if not frames:
            return

        energies = [f["energy"] for f in frames]
        known = [e for e in energies if e is not None]
        entry: dict = {
            "n_frames": len(frames),
            "frame_energies": energies,
            "files": {},
        }

        if known and len(known) == len(energies):
            ts_index = max(range(len(energies)), key=lambda i: energies[i])
            ts_energy = energies[ts_index]
            first, last = energies[0], energies[-1]
            entry.update(
                {
                    "ts_frame_index": ts_index,
                    "ts_energy": ts_energy,
                    "endpoint_energies": [first, last],
                    "barriers_kcal": [
                        (ts_energy - first) * HARTREE_TO_KCAL,
                        (ts_energy - last) * HARTREE_TO_KCAL,
                    ],
                }
            )

        for role, path in (
            ("full_trajectory", full),
            ("forward_trajectory", job_dir / f"{base}_IRC_F_trj.xyz"),
            ("backward_trajectory", job_dir / f"{base}_IRC_B_trj.xyz"),
            ("forward_endpoint", job_dir / f"{base}_IRC_F.xyz"),
            ("backward_endpoint", job_dir / f"{base}_IRC_B.xyz"),
        ):
            if path.is_file():
                entry["files"][role] = str(path)
                result.files[role] = str(path)

        for role in ("forward_endpoint", "backward_endpoint"):
            path = entry["files"].get(role)
            if not path:
                continue
            end_frames = read_xyz_frames(Path(path))
            if end_frames:
                frame = end_frames[-1]
                entry[role] = {
                    "symbols": frame["symbols"],
                    "coords": frame["coords"],
                    "units": "angstrom",
                    "energy": frame["energy"],
                }

        result.irc = entry


# ---------------------------------------------------------------- 辅助函数


def _safe_stem(spec: JobSpec) -> str:
    """任务文件名：分子名 + 任务类型，均为 ASCII 安全字符。"""
    raw = f"{spec.molecule.name}_{spec.job_type.value}"
    cleaned = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in raw)
    cleaned = cleaned.strip("._") or "job"
    ascii_only = cleaned.encode("ascii", "ignore").decode("ascii") or "job"
    return ascii_only[:80]


def _ascii_title(title: str, limit: int = 200) -> str:
    """把标题里的非 ASCII 字符剔掉，保证 ORCA 输入是纯 ASCII。

    注意只清洗**标题**（注释行）。关键字、坐标、块设置里的非 ASCII 仍然会
    让输入生成失败 —— 那些是真正的输入，不该被悄悄改掉。
    """
    cleaned = str(title).encode("ascii", "ignore").decode("ascii").strip()
    return (cleaned or "job")[:limit]


def _find_input(job_dir: Path, spec: JobSpec | None = None) -> Path:
    """定位任务目录里的输入文件。"""
    candidates = sorted(Path(job_dir).glob("*.inp"))
    if not candidates:
        raise EngineError(f"{job_dir} 中没有 .inp 输入文件")
    if spec is not None:
        expected = Path(job_dir) / f"{_safe_stem(spec)}.inp"
        if expected.is_file():
            return expected
    return candidates[0]


def _pump_lines(stream, fh, on_line: LineHandler | None) -> None:
    """把子进程输出同时写入文件与回调。"""
    try:
        for line in stream:
            fh.write(line)
            fh.flush()
            if on_line is not None:
                try:
                    on_line(line.rstrip("\n"))
                except Exception:  # 回调异常不应影响计算
                    pass
    except (ValueError, OSError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _is_set(cancel: object) -> bool:
    checker = getattr(cancel, "is_set", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return False


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _first(value):
    """取列表首元素；非列表则原样返回。"""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _scalar(value):
    """剥掉单元素列表的层层包裹，直到拿到标量。"""
    while isinstance(value, (list, tuple)) and len(value) == 1:
        value = value[0]
    return value


def _floats(value) -> list[float]:
    """``[[a],[b]]`` 或 ``[a,b]`` → ``[a, b]``（float）。"""
    out: list[float] = []
    if not isinstance(value, (list, tuple)):
        return out
    for item in value:
        scalar = _scalar(item)
        if isinstance(scalar, (int, float)):
            out.append(float(scalar))
    return out


def _as_int(value) -> int | None:
    scalar = _scalar(value)
    if isinstance(scalar, bool):
        return None
    if isinstance(scalar, int):
        return scalar
    if isinstance(scalar, float):
        return int(scalar)
    return None


def _extract_geometry(node) -> dict | None:
    """把 ORCA 的 Cartesians 转成 Å 单位的规范几何。"""
    if not isinstance(node, dict):
        return None
    coords = node.get("Coordinates")
    if not isinstance(coords, dict):
        return None
    cartesians = coords.get("Cartesians")
    if not isinstance(cartesians, (list, tuple)) or not cartesians:
        return None

    units = str(coords.get("Units", "") or "").lower()
    factor = 1.0
    if units.startswith(("a.u", "au", "bohr")):
        factor = BOHR_TO_ANGSTROM

    symbols: list[str] = []
    xyz: list[list[float]] = []
    for row in cartesians:
        if not isinstance(row, (list, tuple)) or len(row) < 4:
            return None
        symbols.append(str(row[0]))
        xyz.append([float(row[1]) * factor, float(row[2]) * factor, float(row[3]) * factor])
    return {"symbols": symbols, "coords": xyz, "units": "angstrom"}


def _extract_frequencies(geom: dict) -> list[float]:
    """从几何块里找振动频率。ORCA 各版本的键名不完全一致，故按名匹配。"""
    if not isinstance(geom, dict):
        return []
    for key, value in geom.items():
        if "frequen" not in str(key).lower():
            continue
        if isinstance(value, (list, tuple)):
            freqs = _floats(value)
            if freqs:
                return freqs
        if isinstance(value, dict):
            for sub in value.values():
                freqs = _floats(sub)
                if freqs:
                    return freqs
    return []


#: ORCA 热化学字段 → Result.thermo 的规范键
#:
#: .. note::
#:   ORCA 的 ``entropyS`` 字段实际是 **T·S**（Hartree），不是熵本身。
#:   实测：水在 298.15 K 下该值 0.0214106 Ha，而 H−G 之差恰为 0.0214106 Ha；
#:   换算得 S = 188.6 J/(mol·K)，与文献值 188.8 一致。故下面另给真熵。
_THERMO_FIELDS: dict[str, str] = {
    "temperature": "temperature_K",
    "pressure": "pressure_atm",
    "totalMass": "total_mass_amu",
    "numOfFreqs": "n_frequencies",
    "isLinear": "is_linear",
    "freqScalingFactor": "frequency_scaling_factor",
    "elEnergy": "electronic_energy_hartree",
    "zpe": "zpe_hartree",
    "innerEnergyU": "inner_energy_U_hartree",
    "enthalpyH": "enthalpy_H_hartree",
    "freeEnergyG": "gibbs_free_energy_G_hartree",
    "entropyS": "entropy_TS_hartree",
    "transEnergy": "translational_energy_hartree",
    "rotEnergy": "rotational_energy_hartree",
    "vibEnergy": "vibrational_energy_hartree",
    "qTrans": "q_translational",
    "qRot": "q_rotational",
    "qVib": "q_vibrational",
    "qEl": "q_electronic",
    "spinDegeneracy": "spin_degeneracy",
}


def _extract_thermo(thermo: dict) -> dict:
    """把 ORCA 的热化学块翻译成规范键；单位一律 Hartree / K / atm / amu。"""
    out: dict = {}
    for src, dst in _THERMO_FIELDS.items():
        if src in thermo:
            out[dst] = _scalar(thermo[src])

    ts = out.get("entropy_TS_hartree")
    temperature = out.get("temperature_K")
    if isinstance(ts, (int, float)) and isinstance(temperature, (int, float)) and temperature:
        out["entropy_S_hartree_per_K"] = ts / temperature
    return out


_ERROR_HINTS = ("ERROR", "Error:", "error:")


def _dedupe_keywords(words) -> list[str]:
    """去掉简单输入行里的重复关键字（大小写不敏感，保留首次出现的顺序）。

    ORCA 对重复关键字是**直接报错**而不是忽略：

        INPUT ERROR
        UNRECOGNIZED OR DUPLICATED KEYWORD(S) IN SIMPLE INPUT LINE
                        FREQ

    引擎本身会按 ``job_type`` 加任务关键字（``Opt`` / ``Freq`` / ``OptTS`` …），
    如果配方或用户又在 ``extra_keywords`` 里写一遍，整个任务就起不来 ——
    实测踩过：内置的 OPT+FREQ / FREQ / TS 三个配方都带了重复的 ``Freq``。
    """
    seen: set[str] = set()
    unique: list[str] = []
    for word in words:
        token = str(word).strip()
        key = token.upper()
        if not token or key in seen:
            continue
        seen.add(key)
        unique.append(token)
    return unique


def _first_error(text: str) -> str:
    """从输出里挑出第一条像错误的信息，供界面展示。"""
    for line in text.splitlines():
        stripped = line.strip()
        if any(hint in stripped for hint in _ERROR_HINTS) and stripped:
            return stripped[:300]
    # 退而求其次：最后一行非空内容
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()[:300]
    return ""


# ---------------------------------------------------------------- 扫描曲线


#: 松弛扫描的能量文件，按优先级。**必须用 scf 那个** —— 它和 ORCA 报告的
#: FINAL SINGLE POINT ENERGY 一致；``relaxscanact.dat`` 是另一个量，实测和
#: 前者差约 1.2 kcal/mol，拿它算势垒会偏。
_SCAN_ENERGY_GLOBS = ("*relaxscanscf.dat", "*relaxscanact.dat")

#: ORCA 扫描步标题里的坐标词 → (中文名, 单位)
_SCAN_COORDINATES = {
    "bond": ("键长", "Å"),
    "angle": ("键角", "°"),
    "dihedral": ("二面角", "°"),
}

#: 判定"重原子之间成键"的距离上限（Å），用于画键长曲线
_SCAN_BOND_LIMIT = 1.8

_SCAN_STEP_RE = re.compile(r"(Bond|Angle|Dihedral)\s*\([^)]*\)\s*:\s*(-?[\d.]+)", re.I)


def _scan_energy_file(job_dir: Path) -> Path | None:
    for pattern in _SCAN_ENERGY_GLOBS:
        hits = sorted(job_dir.glob(pattern))
        if hits:
            return hits[0]
    return None


def _read_scan_energies(path: Path) -> list[tuple[float, float]]:
    rows: list[tuple[float, float]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            rows.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return rows


def _read_scan_frames(out_path: Path) -> tuple[str, list[tuple[float, list]]]:
    """取每个扫描点的坐标值与该点**收敛后**的几何。

    每个 ``RELAXED SURFACE SCAN STEP n`` 标题里带着该点的坐标值，例如
    ``Dihedral (  2,   1,   0,   3)  : 120.00000000``。标题之后是这一步的
    优化过程 —— **取下一个标题之前的最后一个坐标块**，那就是收敛后的结构。
    （已用独立的约束优化结果校验过：0° 处 C–C 得 1.4954 Å，对照值 1.4953。）
    """
    lines = out_path.read_text(encoding="utf-8", errors="replace").splitlines()
    kind = "dihedral"
    steps: list[list] = []
    for i, line in enumerate(lines):
        if "RELAXED SURFACE SCAN STEP" not in line:
            continue
        for j in range(i, min(i + 6, len(lines))):
            m = _SCAN_STEP_RE.search(lines[j])
            if m:
                kind = m.group(1).lower()
                steps.append([float(m.group(2)), i, None])
                break

    for k, step in enumerate(steps):
        end = steps[k + 1][1] if k + 1 < len(steps) else len(lines)
        last = None
        for i in range(step[1], end):
            if "CARTESIAN COORDINATES (ANGSTROEM)" not in lines[i]:
                continue
            atoms = []
            for line in lines[i + 2:]:
                parts = line.split()
                if len(parts) == 4 and re.fullmatch(r"[A-Za-z]{1,2}", parts[0]):
                    try:
                        atoms.append((parts[0], float(parts[1]), float(parts[2]),
                                      float(parts[3])))
                    except ValueError:
                        break
                elif atoms:
                    break
            if atoms:
                last = atoms
        step[2] = last

    return kind, [(s[0], s[2]) for s in steps if s[2]]


def _scan_bond_series(frames: list[tuple[float, list]]) -> list[dict]:
    """把重原子之间的键长随扫描坐标的变化整理成曲线。"""
    if not frames:
        return []
    sample = frames[0][1]
    pairs = []
    for i in range(len(sample)):
        for j in range(i + 1, len(sample)):
            if sample[i][0] == "H" or sample[j][0] == "H":
                continue
            if math.dist(sample[i][1:], sample[j][1:]) < _SCAN_BOND_LIMIT:
                pairs.append((i, j, f"{sample[i][0]}{i + 1}–{sample[j][0]}{j + 1}"))
    series = []
    for i, j, label in pairs:
        values = []
        for _, atoms in frames:
            if len(atoms) <= max(i, j):
                values.append(None)
            else:
                values.append(math.dist(atoms[i][1:], atoms[j][1:]))
        series.append({"label": label, "i": i, "j": j, "unit": "angstrom",
                       "values": values})
    return series


def _scan_changed_frames(job_dir: Path, symbols, geometries) -> list[int]:
    """扫描里哪些帧的成键和起始结构不一样（返回 0 起的下标）。

    参照物优先用**输入分子**（``jobspec.json`` 里那一份），而不是第一帧 ——
    扫描的第一帧本身就可能已经跑歪了，拿它当基准会把"从头就是错的"
    误判成"没变化"（实测那个消除任务正是第 0 个点就已经变了）。

    读不到 jobspec 时才退回用第一帧。

    检查很便宜（只比坐标、不读大文件），所以对**每一帧**都做。
    """
    from ..model.geometry import connectivity_change

    if not symbols or len(geometries) < 2:
        return []
    coords = [g.get("coords") for g in geometries]
    if any(not c for c in coords):
        return []

    reference = None
    spec_path = job_dir / "jobspec.json"
    if spec_path.is_file():
        try:
            data = json.loads(spec_path.read_text(encoding="utf-8"))
            mol = data.get("molecule") or {}
            ref_symbols = list(mol.get("symbols") or [])
            ref_coords = mol.get("coords") or []
            if ref_symbols == list(symbols) and len(ref_coords) == len(symbols):
                reference = ref_coords
        except (OSError, ValueError, KeyError, TypeError):
            reference = None
    if reference is None:
        reference = coords[0]

    changed: list[int] = []
    for index, frame in enumerate(coords):
        try:
            change = connectivity_change(list(symbols), reference, frame)
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if change.changed:
            changed.append(index)
    return changed


def parse_scan_profile(job_dir: Path) -> dict | None:
    """解析 ORCA 松弛扫描：每个点的坐标值、能量，以及重原子键长。

    没有扫描数据就返回 ``None``（调用方据此判断"这不是扫描任务"）。
    """
    energy_file = _scan_energy_file(job_dir)
    if energy_file is None:
        return None
    rows = _read_scan_energies(energy_file)
    if not rows:
        return None

    lo = min(e for _, e in rows)
    points = [
        {"value": a, "energy": e, "rel_kcal": (e - lo) * HARTREE_TO_KCAL}
        for a, e in rows
    ]
    barrier = max(p["rel_kcal"] for p in points)
    lowest = min(points, key=lambda p: p["rel_kcal"])
    highest = max(points, key=lambda p: p["rel_kcal"])

    # 几何是可选的：只有 .out 还在才解析得出来
    kind = "dihedral"
    series: list[dict] = []
    symbols: list[str] = []
    geometries: list[list[list[float]]] = []
    outs = sorted(job_dir.glob("*.out"))
    if outs:
        try:
            kind, frames = _read_scan_frames(outs[0])
            series = _scan_bond_series(frames)
            # 逐点的几何也带上：界面上拖动势能图要看构象怎么变。
            # 元素符号只在顶层给一份（各点都一样），省掉大量重复。
            symbols = [a[0] for a in frames[0][1]] if frames else []
            geometries = [
                {"value": value, "coords": [[a[1], a[2], a[3]] for a in atoms]}
                for value, atoms in frames
            ]
        except (OSError, ValueError, IndexError):
            series = []

    name, unit = _SCAN_COORDINATES.get(kind, ("扫描坐标", ""))
    warnings: list[str] = []
    if "act" in energy_file.name:
        warnings.append(
            "只找到 relaxscanact.dat（没有 relaxscanscf.dat），其能量与界面报告的"
            "单点能不一致，势垒可能有约 1 kcal/mol 的偏差。"
        )

    # 扫描的**每一帧**也要查成键 —— 曲线画得再漂亮，如果分子早就变了，
    # 那条曲线讲的就不是你要的反应。实测踩过：一个二面角扫描在第 0 个点就把
    # 醇消成了丁二烯 + 水，19 个点全是消除产物，而外部看到的是"一条像样的曲线"。
    changed_frames = _scan_changed_frames(job_dir, symbols, geometries)
    if changed_frames:
        preview = "、".join(str(i + 1) for i in changed_frames[:6])
        warnings.append(
            f"扫描过程中分子结构发生了变化：{len(changed_frames)}/{len(geometries)} 个扫描点"
            f"的成键与起始结构不同（例如第 {preview} 个点）。"
            f"**除非你就是在扫键的断裂或形成**，否则这些点算的不是你想要的那个分子，"
            f"曲线和势垒都不能直接用。"
        )

    return {
        "energy_file": energy_file.name,
        "coordinate": name,
        "unit": unit,
        "points": points,
        "barrier_kcal": barrier,
        "barrier_kj": barrier * 4.184,
        "min_value": lowest["value"],
        "max_value": highest["value"],
        "series": series,
        "symbols": symbols,
        "geometries": geometries,
        "changed_frames": changed_frames,
        "warnings": warnings,
    }


#: 供类型检查与外部引用
__all__ = ["OrcaEngine", "ENV_ORCA_EXE", "parse_scan_profile"]
