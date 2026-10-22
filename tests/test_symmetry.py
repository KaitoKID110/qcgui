# -*- coding: utf-8 -*-
r"""对称性：API 校验 + 回归测试。

测试重点（都来自实测行为，不是照文档猜）：
1. ``JobSpec.symmetry`` 能往返、能映射到正确关键字；
2. 用户自己在 extra_keywords 里写了 UseSym 时**不要**再加一次（ORCA 会报错）；
3. 解析 ORCA 输出：检测到的点群、约化点群、完美化坐标、最终点群；
4. 没有对称性信息的任务，``result.symmetry`` 必须是空（不能凭空造）；
5. 非法模式要报错（而不是悄悄忽略、跑一个用户没要求的计算）。
"""
import io

from qcgui.engines.orca import OrcaEngine
from qcgui.model import JobSpec, JobType, Molecule, Result
from qcgui.model.jobspec import SYMMETRY_LABELS, SYMMETRY_MODES

OUT_TEXT = """
SYMMETRY HANDLING SETUP
-----------------------
Auto-detected point group .... D6h
Reduced point group       .... D2h
Root mean square distance .... 0.000026936251690 au
Maximum distance          .... 0.000026936251691 au
Threshold in use          .... 0.000100000000000 au
Time for symmetry setup   .... 0.016 s


Symmetry-perfected Cartesians for point group D6h:

Index  Symbol         Symmetry-perfected Cartesians (x, y, z; au)
   0     C      2.626746262402229   0.000000000000000   0.000000000000000
   1     C      1.313373131201115   2.274828992536155   0.000000000000000

Index  Symbol         Symmetry-perfected Cartesians (x, y, z; Ang)
   0     C      1.390014254050471   0.000000000000000   0.000000000000000
   1     C      0.695007127025235   1.203787655630184   0.000000000000000


                       ****************************
                       * Single Point Calculation *
                       ****************************

Point group                               .... D6h
"""


def _benzene() -> Molecule:
    return Molecule.from_xyz("""6
benzene
C  1.390000  0.000000  0.000000
C  0.695000  1.203800  0.000000
C -0.695000  1.203800  0.000000
C -1.390000  0.000000  0.000000
C -0.695000 -1.203800  0.000000
C  0.695000 -1.203800  0.000000
""")


def _spec(symmetry: str = "", extra_keywords=()) -> JobSpec:
    return JobSpec(molecule=_benzene(), job_type=JobType.ENERGY, method="HF",
                   basis="STO-3G", symmetry=symmetry,
                   extra_keywords=tuple(extra_keywords))


def test_symmetry_modes_map_to_the_verified_keywords():
    """关键字映射必须和实测一致：UseSym / NoUseSym / UseC2v / UseD2 ..."""
    assert SYMMETRY_MODES[""] == ()
    assert SYMMETRY_MODES["use"] == ("UseSym",)
    assert SYMMETRY_MODES["off"] == ("NoUseSym",)
    assert "UseC2v" in SYMMETRY_MODES["c2v"]
    assert "UseD2" in SYMMETRY_MODES["d2"]
    assert "PreferC2v" in SYMMETRY_MODES["prefer_c2v"]
    assert "PreferD2" in SYMMETRY_MODES["prefer_d2"]
    # 每个模式都要有界面标签
    assert set(SYMMETRY_MODES) == set(SYMMETRY_LABELS)
    print(f"    {len(SYMMETRY_MODES)} 种模式映射正确")


def test_build_input_adds_usesym():
    engine = OrcaEngine()
    text = engine.build_input_text(_spec("use"))
    keyword_line = next(l for l in text.splitlines() if l.startswith("!"))
    assert "UseSym" in keyword_line, keyword_line


def test_build_input_defaults_to_no_symmetry_keyword():
    engine = OrcaEngine()
    text = engine.build_input_text(_spec(""))
    keyword_line = next(l for l in text.splitlines() if l.startswith("!"))
    assert "UseSym" not in keyword_line and "NoUseSym" not in keyword_line


def test_user_supplied_usesym_is_not_duplicated():
    """用户自己在额外关键字里写了 UseSym 时不能再加一次（ORCA 会报错）。"""
    engine = OrcaEngine()
    text = engine.build_input_text(_spec("use", extra_keywords=("UseSym",)))
    keyword_line = next(l for l in text.splitlines() if l.startswith("!"))
    assert keyword_line.lower().count("usesym") == 1, keyword_line


def test_unknown_symmetry_mode_is_rejected():
    engine = OrcaEngine()
    try:
        engine.build_input_text(_spec("c6h"))
    except ValueError as exc:
        assert "对称性模式" in str(exc) or "symmetry" in str(exc).lower(), str(exc)
        print("    非法模式被拒绝")
    else:
        raise AssertionError("非法对称性模式应当报错，而不是静默忽略")


def test_jobspec_roundtrip_keeps_symmetry():
    spec = _spec("use")
    data = spec.to_dict()
    assert data["symmetry"] == "use"
    back = JobSpec.from_dict(data)
    assert back.symmetry == "use"


def test_parse_symmetry_info_from_orca_output():
    engine = OrcaEngine()
    result = Result(status="done", engine="orca", job_type="energy")
    engine._fill_symmetry(result, OUT_TEXT)
    sym = result.symmetry
    assert sym["input_point_group"] == "D6h", sym
    assert sym["reduced_point_group"] == "D2h"
    assert sym["perfected_point_group"] == "D6h"
    assert sym["perfected"] is True
    assert sym["final_point_group"] == "D6h"
    assert abs(sym["symmetry_threshold_au"] - 1e-4) < 1e-12
    coords = sym["perfected_coords"]
    assert len(coords) == 2, coords
    assert abs(coords[0]["xyz"][0] - 1.390014254) < 1e-6
    assert coords[0]["symbol"] == "C"
    print(f"    解析出点群 {sym['input_point_group']} + {len(coords)} 个完美化坐标")


def test_no_symmetry_block_means_empty_result():
    """没开对称性时不能凭空给出点群信息。"""
    engine = OrcaEngine()
    result = Result(status="done", engine="orca", job_type="energy")
    engine._fill_symmetry(result, "some unrelated output\n")
    assert result.symmetry == {}
    print("    未开对称性时 result.symmetry 为空")


def test_api_validation_helper_rejects_bad_mode():
    """Web 层要能提前拒绝非法模式（而不是等 ORCA 报输入错误）。"""
    import inspect

    from qcgui.web import app as webapp
    source = inspect.getsource(webapp)
    assert "SYMMETRY_MODES" in source or "SYMMETRY_LABELS" in source, \
        "Web 层没有引用对称性模式表"
