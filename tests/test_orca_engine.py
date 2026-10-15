"""OrcaEngine 端到端测试。

既可用 pytest 运行：  pytest tests/test_orca_engine.py -v
也可直接运行：        python tests/test_orca_engine.py

需要本机有 ORCA；没有时相关用例会被跳过。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qcgui.engines import get_engine  # noqa: E402
from qcgui.model import JobSpec, JobType, Molecule  # noqa: E402

# 实测值：HF/def2-SVP 水的单点能（Hartree）
WATER_HF_DEF2SVP = -75.960983978583

WATER_XYZ = """3
water
O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
"""

# ORCA 6.1 官方教程中乙二醛顺反异构的过渡态初猜（r2SCAN-3c 复合方法，不指定基组）
GLYOXAL_TS_XYZ = """6
glyoxal_ts_guess
O     -1.83324      0.62772      -0.77785
C     -1.27709     -0.09000       0.01396
C      0.24881     -0.22901       0.01383
O      0.80483     -0.94688      -0.77793
H     -1.83538     -0.69931       0.76395
H      0.80720      0.38008       0.76393
"""


def _engine():
    engine = get_engine("orca")
    ok, why = engine.available()
    if not ok:
        _skip(f"ORCA 不可用：{why}")
    return engine


class _Skipped(Exception):
    pass


def _skip(reason: str):
    try:
        import pytest

        pytest.skip(reason)
    except ImportError:
        raise _Skipped(reason)


def _water() -> Molecule:
    return Molecule.from_xyz(WATER_XYZ)


def _jobdir(name: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"qcgui_test_{name}_"))


# ---------------------------------------------------------------- 纯逻辑

def test_molecule_roundtrip():
    mol = _water()
    assert mol.n_atoms == 3
    assert mol.formula == "H2O"
    # 名称取自 XYZ 注释行
    assert mol.name == "water", mol.name
    again = Molecule.from_xyz(mol.to_xyz())
    assert again.symbols == mol.symbols
    assert again.coords == mol.coords
    # 显式指定名称时优先
    assert Molecule.from_xyz(WATER_XYZ, name="foo").name == "foo"


def test_jobspec_fingerprint_is_stable():
    spec = JobSpec(molecule=_water(), job_type=JobType.OPT_FREQ)
    assert spec.fingerprint() == JobSpec(molecule=_water(), job_type=JobType.OPT_FREQ).fingerprint()
    other = JobSpec(molecule=_water(), job_type=JobType.ENERGY)
    assert spec.fingerprint() != other.fingerprint()


def test_input_is_ascii_without_bom():
    engine = _engine()
    spec = JobSpec(molecule=_water(), job_type=JobType.ENERGY, method="HF", basis="def2-SVP")
    job_dir = _jobdir("input")
    inp = engine.generate_input(spec, job_dir)
    raw = inp.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "输入文件带了 BOM，ORCA 会拒绝"
    raw.decode("ascii")  # 非 ASCII 会抛异常
    text = inp.read_text(encoding="ascii")
    assert "! HF def2-SVP" in text
    assert "* xyz 0 1" in text


def test_non_ascii_title_is_sanitised_not_rejected():
    """中文分子名不该让任务跑不起来。

    标题只是注释行，但 ORCA 遇到任何非 ASCII 字符会拒绝整个输入
    （``non-ASCII character(s) found``，退出码 50）。这里剔掉非 ASCII 即可，
    界面上显示的名字仍是原文。
    """
    engine = _engine()
    spec = JobSpec(
        molecule=_water(), job_type=JobType.ENERGY, method="HF", basis="def2-SVP",
        title="水分子 α-β 测试",
    )
    text = engine.build_input_text(spec)
    text.encode("ascii")  # 不抛异常

    header = text.splitlines()[0]
    assert header.startswith("# "), header
    assert header.isascii(), header
    assert "!" in text.splitlines()[1], text.splitlines()[1]

    # 真写文件也不能失败
    inp = engine.generate_input(spec, _jobdir("nonascii_title"))
    assert inp.read_text(encoding="ascii").splitlines()[0].isascii()


def test_available_reports_path():
    engine = _engine()
    ok, why = engine.available()
    assert ok
    assert Path(why).is_file(), f"available() 应返回可执行文件路径，得到 {why!r}"


# ---------------------------------------------------------------- 静默跳过

def test_silently_skipped_analysis_is_reported():
    """请求了某项分析、ORCA 却悄悄跳过时，必须报出来。

    实测：没有 ``NBOEXE`` 时 ORCA 打一行 ``Skipping NBO-Analysis`` 就继续跑完，
    正常终止、退出码 0 —— 任务状态是"成功"，但一个 NBO 数都没有。
    这种"绿点但没数据"比直接报错危险得多。
    """
    from qcgui.engines.orca import _collect_issues

    nbo_spec = JobSpec(
        molecule=_water(), job_type=JobType.ENERGY, method="HF", basis="STO-3G",
        extra_keywords=("NBO",),
    )
    marker = "Environment variable NBOEXE for nbo6.exe or nbo5.exe not set! Skipping NBO-Analysis"
    issues = _collect_issues(marker, nbo_spec)
    assert issues, "请求了 NBO 却没报警"
    # 断言**结构化字段**而不是消息文本 —— 改文案不该弄坏测试，
    # 而"跳过附带分析"必须是 minor（主结果仍然可用）
    assert issues[0].code == "analysis_skipped", issues[0]
    assert issues[0].severity == "minor", issues[0]

    # %nbo 块形式也一样要认出来
    block_spec = JobSpec(
        molecule=_water(), job_type=JobType.ENERGY, method="HF", basis="STO-3G",
        extra_blocks=(("nbo", 'NBOKEYLIST = "$NBO $END"'),),
    )
    assert _collect_issues("Skipping NBO-Analysis", block_spec)

    # 没请求 NBO 就不该报 —— 否则就是噪声
    plain = JobSpec(molecule=_water(), job_type=JobType.ENERGY, method="HF", basis="STO-3G")
    assert not _collect_issues(marker, plain)

    # SCF 收敛策略的 "skipping pre-diagonalization" 是正常输出，绝不能当成"跳过分析"
    assert not _collect_issues(
        "Warning: op=0 Small HOMO/LUMO gap (   0.037) - skipping pre-diagonalization", plain
    )


def test_nbo_request_end_to_end_marks_the_skip():
    """真跑一次 !NBO：任务会正常结束，但结果必须带上警告。"""
    engine = _engine()
    spec = JobSpec(
        molecule=_water(), job_type=JobType.ENERGY, method="HF", basis="STO-3G",
        extra_keywords=("NBO",), nprocs=1, maxcore_mb=300,
    )
    job_dir = _jobdir("nbo_skipped")
    result = engine.execute(spec, job_dir)

    # 能量是有效的 —— 不能因为 NBO 被跳过就把它判成失败
    assert result.status == "done", f"{result.status}: {result.message}"
    assert result.final_energy is not None
    # 但必须明确告诉用户 NBO 没跑
    assert result.warnings, "NBO 被跳过却没产生警告"
    assert any("NBO" in w for w in result.warnings)
    # 警告要能进 JSON（界面和任务列表都靠它）
    assert result.to_dict()["warnings"] == result.warnings

    # 确认输入里真的写了 NBO（没有标题行时关键字就是第一行，不能写死下标）
    inp = _find_inp(job_dir)
    keyword_line = next(
        line for line in inp.read_text(encoding="ascii").splitlines() if line.startswith("!")
    )
    assert "NBO" in keyword_line, keyword_line


def _find_inp(job_dir: Path) -> Path:
    files = sorted(job_dir.glob("*.inp"))
    assert files, f"{job_dir} 里没有 .inp"
    return files[0]


def test_duplicate_keywords_are_deduped_before_writing_the_input():
    """重复关键字必须在写 .inp 前去重。

    ORCA 对重复关键字是**直接报错**而不是忽略：

        INPUT ERROR
        UNRECOGNIZED OR DUPLICATED KEYWORD(S) IN SIMPLE INPUT LINE
                        FREQ

    实测踩过：内置的 OPT+FREQ / FREQ / TS 三个配方都带了重复的 ``Freq``，
    用户提交任务时就撞上了这个。事后修了引擎和配方数据，但**一直没补测试** ——
    直到变异测试发现"把去重整个删掉，测试依然全绿"。
    """
    from qcgui.engines.orca import _dedupe_keywords

    # 大小写不敏感
    assert _dedupe_keywords(["Opt", "Freq", "FREQ", "freq"]) == ["Opt", "Freq"]
    assert _dedupe_keywords(["B3LYP", "b3lyp", "def2-TZVPP"]) == ["B3LYP", "def2-TZVPP"]
    # 空白项丢掉
    assert _dedupe_keywords(["", "   ", "Opt"]) == ["Opt"]
    # 保留首次出现的顺序
    assert _dedupe_keywords(["Freq", "Opt"]) == ["Freq", "Opt"]
    assert _dedupe_keywords([]) == []

    # 端到端：生成的输入行里 FREQ 只出现一次
    engine = _engine()
    spec = JobSpec(molecule=_water(), job_type=JobType.OPT_FREQ, method="B3LYP",
                   basis="def2-SVP", extra_keywords=("Freq", "freq", "TightOpt"))
    keyword_line = next(
        line for line in engine.build_input_text(spec).splitlines() if line.startswith("!")
    )
    assert keyword_line.upper().count("FREQ") == 1, f"关键字重复了: {keyword_line}"
    assert keyword_line.count("TightOpt") == 1, keyword_line


def test_failed_run_is_reported_as_failed():
    """ORCA 没正常终止时必须报 ``failed``，并把有用的错误行带出来。

    这条以前是**空档**：``test_orca_engine.py`` 里所有 status 断言都是
    ``== "done"``，``_first_error()`` 从没被测过。后果是——**万一失败判定坏了
    （比如永远返回 done），失败的任务会显示绿点，而测试全绿**。这正是本项目
    最想避免的那类"静默失败"。

    这里不跑 ORCA，直接造一份"报错了、也没写终止标记"的输出喂给 ``parse()``。
    """
    engine = _engine()
    spec = JobSpec(molecule=_water(), job_type=JobType.ENERGY, method="HF", basis="STO-3G")
    job_dir = _jobdir("failed_run")
    (job_dir / "w.inp").write_text(
        "! HF STO-3G\n* xyz 0 1\nO 0 0 0\nH 0 0 1\nH 0 1 0\n*\n", encoding="ascii")
    (job_dir / "w.out").write_text(
        "some banner line\n"
        "\n"
        "INPUT ERROR\n"
        "UNRECOGNIZED OR DUPLICATED KEYWORD(S) IN SIMPLE INPUT LINE\n"
        "                FREQ\n"
        "\n"
        "ORCA finished by error termination in Startup\n",
        encoding="ascii",
    )

    result = engine.parse(spec, job_dir)
    assert result.status == "failed", f"没正常终止却报成了 {result.status}"
    assert result.ok is False
    assert result.message, "失败时必须给出一条消息"
    assert "ERROR" in result.message.upper(), \
        f"消息应当带上真正的错误行，得到 {result.message!r}"
    # 文件仍然登记着，用户能去翻原始输出
    assert result.files.get("output"), result.files


def test_missing_output_is_reported_as_failed():
    """连 .out 都没有（worker 中途死了）也不能报成成功。"""
    engine = _engine()
    spec = JobSpec(molecule=_water(), job_type=JobType.ENERGY, method="HF", basis="STO-3G")
    job_dir = _jobdir("no_output")
    (job_dir / "w.inp").write_text(
        "! HF STO-3G\n* xyz 0 1\nO 0 0 0\nH 0 0 1\nH 0 1 0\n*\n", encoding="ascii")

    result = engine.parse(spec, job_dir)
    assert result.status == "failed", f"没有输出却报成了 {result.status}"
    assert result.message, "应当说明为什么算失败"


# ---------------------------------------------------------------- 扫描曲线

#: 人造的松弛扫描数据：不跑 ORCA 也能精确断言解析结果
_FAKE_SCAN_ENERGIES = """\
    0.00000000  -117.80000000
   60.00000000  -117.79000000
  120.00000000  -117.80000000
"""


def _fake_scan_out(geoms) -> str:
    """geoms: [(角度, C1–C2 键长, C2–C3 键长), ...]"""
    out = []
    for angle, d12, d23 in geoms:
        out.append(
            f"             *          RELAXED SURFACE SCAN STEP   1          *\n"
            f"             *   Dihedral (  2,   1,   0,   3)  : {angle:12.8f}  *\n"
            "             ***************************************************\n"
            "CARTESIAN COORDINATES (ANGSTROEM)\n"
            "---------------------------------\n"
            "  C      0.000000    0.000000    0.000000\n"
            f"  C      {d12:.6f}    0.000000    0.000000\n"
            f"  C      {d12 + d23:.6f}    0.000000    0.000000\n"
            "  H     -0.500000    0.500000    0.000000\n"
        )
    return "\n".join(out)


def test_scan_profile_parser():
    """扫描曲线解析：各点坐标值、相对能量、势垒、重原子键长。"""
    from qcgui.engines.orca import parse_scan_profile

    job_dir = _jobdir("scan_parse")
    (job_dir / "fake.relaxscanscf.dat").write_text(_FAKE_SCAN_ENERGIES, encoding="ascii")
    (job_dir / "fake.out").write_text(
        _fake_scan_out([(0.0, 1.500, 1.300), (60.0, 1.520, 1.300), (120.0, 1.500, 1.300)]),
        encoding="ascii",
    )

    profile = parse_scan_profile(job_dir)
    assert profile, "应当解析出扫描数据"
    assert profile["coordinate"] == "二面角"
    assert profile["energy_file"].endswith("relaxscanscf.dat")
    assert len(profile["points"]) == 3
    assert profile["points"][0]["rel_kcal"] == 0.0

    # -117.790 比 -117.800 高 0.01 Ha = 6.275 kcal/mol
    assert abs(profile["barrier_kcal"] - 0.01 * 627.5094740631) < 1e-6
    assert profile["min_value"] == 0.0 and profile["max_value"] == 60.0

    labels = {s["label"] for s in profile["series"]}
    assert labels == {"C1–C2", "C2–C3"}, f"重原子成键识别不对: {labels}"
    cc = next(s for s in profile["series"] if s["label"] == "C1–C2")
    assert abs(min(cc["values"]) - 1.500) < 1e-6
    assert abs(max(cc["values"]) - 1.520) < 1e-6

    # 逐点几何：界面靠它做"拖动势能图看构象变化"
    assert profile["symbols"] == ["C", "C", "C", "H"], profile["symbols"]
    geoms = profile["geometries"]
    assert len(geoms) == 3, geoms
    assert [g["value"] for g in geoms] == [0.0, 60.0, 120.0], geoms
    # 第 3 个原子在三个点分别是 2.800 / 2.820 / 2.800（d12 + d23）
    third = [g["coords"][2][0] for g in geoms]
    assert abs(third[0] - 2.800) < 1e-6 and abs(third[1] - 2.820) < 1e-6, third
    # 元素符号只在顶层给一份，不该逐点重复
    assert all("symbols" not in g for g in geoms), "symbols 不该逐点重复"


def test_scan_profile_is_none_without_scan_data():
    """不是扫描任务就返回 None —— 接口据此回 404，而不是报错。"""
    from qcgui.engines.orca import parse_scan_profile

    assert parse_scan_profile(_jobdir("scan_none")) is None


def test_scan_profile_warns_about_the_act_energy_file():
    """只剩 relaxscanact.dat 时要提示：它的能量和界面报告的对不上。"""
    from qcgui.engines.orca import parse_scan_profile

    job_dir = _jobdir("scan_act")
    (job_dir / "fake.relaxscanact.dat").write_text(_FAKE_SCAN_ENERGIES, encoding="ascii")
    profile = parse_scan_profile(job_dir)
    assert profile, "act 文件也应当能解析"
    assert profile["warnings"], "用 act 文件时必须给出警告"
    assert "relaxscanact" in profile["warnings"][0]


# ---------------------------------------------------------------- 真跑 ORCA

def test_energy_end_to_end():
    engine = _engine()
    spec = JobSpec(molecule=_water(), job_type=JobType.ENERGY, method="HF", basis="def2-SVP", nprocs=2)
    job_dir = _jobdir("energy")
    result = engine.execute(spec, job_dir)

    assert result.status == "done", f"{result.status}: {result.message}"
    assert result.final_energy is not None
    assert abs(result.final_energy - WATER_HF_DEF2SVP) < 1e-6, result.final_energy

    # 结构化字段
    assert result.n_atoms == 3
    assert result.n_electrons == 10
    assert result.n_basis_functions and result.n_basis_functions > 0
    assert result.geometry and len(result.geometry["symbols"]) == 3
    assert result.geometry["units"] == "angstrom"
    # 坐标必须是 Å（若误当 Bohr 会得到 ~0.62 的 O-H 键长）
    o = result.geometry["coords"][0]
    h = result.geometry["coords"][1]
    bond = sum((a - b) ** 2 for a, b in zip(o, h)) ** 0.5
    assert 0.90 < bond < 1.00, f"O-H 键长 {bond:.3f} Å 不合理，坐标单位可能没换算"
    assert len(result.loewdin_charges) == 3
    assert result.dipole and result.dipole["magnitude"] > 0
    print(f"    E = {result.final_energy:.10f} Ha, 偶极 = {result.dipole['magnitude']:.4f} D")


def test_smd_solvent_runs():
    engine = _engine()
    spec = JobSpec(
        molecule=_water(), job_type=JobType.ENERGY,
        method="HF", basis="def2-SVP", solvent="water", nprocs=2,
    )
    job_dir = _jobdir("smd")
    result = engine.execute(spec, job_dir)
    assert result.status == "done", f"{result.status}: {result.message}"
    assert result.final_energy is not None
    # 隐式溶剂会改变能量，但不应该变成另一个数量级
    assert abs(result.final_energy - WATER_HF_DEF2SVP) < 0.05
    print(f"    SMD(water) E = {result.final_energy:.10f} Ha")


def test_frequency_runs():
    engine = _engine()
    spec = JobSpec(molecule=_water(), job_type=JobType.FREQ, method="HF", basis="def2-SVP", nprocs=2)
    job_dir = _jobdir("freq")
    result = engine.execute(spec, job_dir)

    assert result.status == "done", f"{result.status}: {result.message}"
    assert result.frequencies, "没有解析到频率"
    assert result.n_imaginary_frequencies == 0, result.frequencies

    real = [f for f in result.frequencies if f > 1.0]
    assert len(real) == 3, f"水应有 3 个真实振动频率，得到 {real}"
    assert all(1000 < f < 5000 for f in real), real

    # 热化学必须来自结构化输出
    thermo = result.thermo
    assert thermo.get("temperature_K") == 298.15
    assert thermo.get("pressure_atm") == 1.0
    for key in ("zpe_hartree", "enthalpy_H_hartree", "gibbs_free_energy_G_hartree",
                "entropy_S_hartree_per_K", "inner_energy_U_hartree"):
        assert key in thermo, f"缺少热化学字段 {key}"

    # G = H − T·S 必须自洽
    ts = thermo["enthalpy_H_hartree"] - thermo["gibbs_free_energy_G_hartree"]
    assert abs(ts - thermo["entropy_TS_hartree"]) < 1e-9, ts

    # 熵换算成 J/(mol·K) 应接近水的标准熵 188.8
    s_jmol = thermo["entropy_S_hartree_per_K"] * 4.3597447222071e-18 * 6.02214076e23
    assert 180 < s_jmol < 200, f"S = {s_jmol:.1f} J/(mol·K) 不合理"

    print(f"    频率 = {[round(f, 2) for f in real]}")
    print(f"    ZPE = {thermo['zpe_hartree']:.6f} Ha, G = {thermo['gibbs_free_energy_G_hartree']:.6f} Ha, "
          f"S = {s_jmol:.1f} J/(mol·K)")


def test_capabilities_are_honest():
    engine = _engine()
    caps = engine.capabilities()
    assert JobType.ENERGY in caps
    assert JobType.OPT_FREQ in caps
    assert JobType.TS_FREQ in caps
    assert JobType.IRC in caps            # 已用真实 IRC 任务验证
    # 尚未实现/未验证的任务类型不应出现在能力里
    assert JobType.NEB_TS not in caps
    assert JobType.SCAN not in caps


def test_irc_frame_parser_reads_energy_from_comment():
    """IRC 轨迹是结构化产物：每帧注释行带该帧能量。"""
    from qcgui.model.trajectory import read_xyz_frames

    path = _jobdir("frame_parser") / "traj.xyz"
    path.write_text(
        "3\n"
        "Coordinates from ORCA-job demo E -75.100000000000\n"
        "O  0.0 0.0 0.0\nH  0.0 0.0 1.0\nH  0.0 1.0 0.0\n"
        "3\n"
        "Coordinates from ORCA-job demo E -75.200000000000\n"
        "O  0.0 0.0 0.0\nH  0.0 0.0 1.1\nH  0.0 1.1 0.0\n",
        encoding="ascii",
    )
    frames = read_xyz_frames(path)
    assert len(frames) == 2
    assert frames[0]["energy"] == -75.1
    assert frames[1]["energy"] == -75.2
    assert frames[1]["symbols"] == ["O", "H", "H"]
    assert frames[1]["coords"][1] == [0.0, 0.0, 1.1]

    # 注释里没有能量时不应假装有
    path.write_text("1\nno energy here\nH 0 0 0\n", encoding="ascii")
    assert read_xyz_frames(path)[0]["energy"] is None


def test_irc_end_to_end():
    """完整 IRC：输入生成 → 运行 → 轨迹解析。较慢，用 QCGUI_SLOW_TESTS=1 开启。"""
    if not os.environ.get("QCGUI_SLOW_TESTS"):
        _skip("慢速用例；设 QCGUI_SLOW_TESTS=1 开启")
    engine = _engine()
    spec = JobSpec(
        molecule=Molecule.from_xyz(GLYOXAL_TS_XYZ),
        job_type=JobType.IRC,
        method="HF",
        basis="def2-SVP",
        nprocs=4,
        extra_blocks=(("irc", "MAXITER 10"),),
    )
    job_dir = _jobdir("irc")
    result = engine.execute(spec, job_dir)

    assert result.status == "done", f"{result.status}: {result.message}"
    irc = result.irc
    assert irc.get("n_frames", 0) >= 3, irc
    assert len(irc["frame_energies"]) == irc["n_frames"]

    # 过渡态必须是全路径最高点
    energies = irc["frame_energies"]
    assert energies[irc["ts_frame_index"]] == max(energies)

    barriers = irc["barriers_kcal"]
    assert all(b > 0 for b in barriers), barriers
    assert irc["forward_endpoint"]["symbols"] == ["O", "C", "C", "O", "H", "H"]
    assert irc["backward_endpoint"]["symbols"] == ["O", "C", "C", "O", "H", "H"]
    print(f"    {irc['n_frames']} 帧，TS 在第 {irc['ts_frame_index']} 帧，"
          f"能垒 {barriers[0]:.2f} / {barriers[1]:.2f} kcal/mol")


def test_transition_state_has_exactly_one_imaginary_frequency():
    """过渡态搜索：必须收敛到一阶鞍点（恰好 1 个虚频）。"""
    engine = _engine()
    spec = JobSpec(
        molecule=Molecule.from_xyz(GLYOXAL_TS_XYZ),
        job_type=JobType.TS_FREQ,
        method="r2SCAN-3c",   # 复合方法自带基组，故 basis 留空
        basis="",
        nprocs=4,
        calc_hess=True,
    )
    job_dir = _jobdir("ts")
    result = engine.execute(spec, job_dir)

    assert result.status == "done", f"{result.status}: {result.message}"
    assert result.final_energy is not None

    imaginary = [f for f in result.frequencies if f < 0]
    assert len(imaginary) == 1, f"过渡态应有且仅有 1 个虚频，得到 {imaginary}"
    assert -300 < imaginary[0] < -50, f"虚频 {imaginary[0]:.1f} cm⁻¹ 不合理"

    # 6 原子体系：3N-6 = 12 个振动自由度
    vibrational = [f for f in result.frequencies if abs(f) > 1e-6]
    assert len(vibrational) == 12, len(vibrational)
    print(f"    虚频 = {imaginary[0]:.1f} cm⁻¹，振动模式 {len(vibrational)} 个（3N-6）")


# ---------------------------------------------------------------- 脚本模式

def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = []
    skipped = []
    for name, func in tests:
        print(f"[ RUN  ] {name}")
        try:
            func()
            print(f"[  OK  ] {name}")
        except _Skipped as exc:
            skipped.append(name)
            print(f"[ SKIP ] {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, exc))
            print(f"[ FAIL ] {name}: {type(exc).__name__}: {exc}")
    print("-" * 60)
    print(f"通过 {len(tests) - len(failed) - len(skipped)} / 失败 {len(failed)} / 跳过 {len(skipped)}")
    for name, exc in failed:
        print(f"  失败: {name} -> {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
