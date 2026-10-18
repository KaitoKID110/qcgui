# -*- coding: utf-8 -*-
r"""NMR 化学位移 δ 的回归测试。

δ = σ(参考物) − σ(样品)。这里钉住三条**正确性**要求：

1. 有参考值就给出 δ，且算法是 σ(参考) − σ(样品)（不是反过来）。
2. **没有参考值的元素不许给 δ** —— 拿别的核的参考值顶上去会得到
   看似合理、实则完全错误的数字，比"没有"更糟。
3. 参考值必须能随 JobSpec 持久化（否则读回历史任务时 δ 会消失）。

真实依据（HF/STO-3G 实测）：TMS 的 σ 为 H 33.713 / C 248.638 / Si 577.438；
优化过的水为 O 347.344 / H 32.819，于是 δ(H) = 33.713 − 32.819 ≈ 0.894 ppm。
"""
from qcgui.engines.orca import OrcaEngine
from qcgui.model import JobSpec, JobType, Molecule
from qcgui.model.nmr_reference import (
    REFERENCE_MOLECULES,
    average_shieldings,
    compute_deltas,
)

SNIPPET = """
--------------------------------
CHEMICAL SHIELDING SUMMARY (ppm)
--------------------------------


  Nucleus  Element    Isotropic     Anisotropy
  -------  -------  ------------   ------------
      0       O          347.344         13.331
      1       H           32.819         13.201
      2       H           32.819         13.201


NMR shielding tensor and spin rotation calculation done in   0.0 sec
"""


def _water() -> Molecule:
    return Molecule.from_xyz(
        "3\nwater\n"
        "O 0.0 0.0 0.0\n"
        "H 0.0 0.7572 -0.4692\n"
        "H 0.0 -0.7572 -0.4692\n"
    )


def test_tms_reference_molecule_is_shipped():
    """内置参考物必须是真实的 TMS：Si1 C4 H12，共 17 个原子。"""
    info = REFERENCE_MOLECULES["tms"]
    lines = info["xyz"].strip().splitlines()
    assert int(lines[0]) == 17, lines[0]
    symbols = [ln.split()[0] for ln in lines[2:]]
    assert symbols.count("Si") == 1
    assert symbols.count("C") == 4
    assert symbols.count("H") == 12
    assert set(info["elements"]) == {"H", "C", "Si"}
    print("    TMS 参考物几何正确（17 原子：Si1 C4 H12）")


def test_average_shieldings_groups_by_element():
    """等效核按元素取平均（实测 TMS 优化后仍有 0.16/0.77 ppm 的微小差异）。"""
    rows = [
        {"element": "H", "isotropic": 33.0},
        {"element": "H", "isotropic": 34.0},
        {"element": "C", "isotropic": 248.0},
    ]
    avg = average_shieldings(rows)
    assert abs(avg["H"] - 33.5) < 1e-9
    assert abs(avg["C"] - 248.0) < 1e-9
    print(f"    按元素平均正确：{avg}")


def test_delta_is_reference_minus_sample():
    """δ 必须是 σ(参考) − σ(样品)，方向不能反。"""
    rows = [{"index": 1, "element": "H", "isotropic": 32.819}]
    out, missing = compute_deltas(rows, {"H": 33.713})
    assert not missing
    assert abs(out[0]["delta"] - (33.713 - 32.819)) < 1e-9, out[0]
    assert out[0]["delta"] > 0, "水中的 H 相对 TMS 应当略正位移"
    print(f"    δ = σ(TMS) − σ(样品) = {out[0]['delta']:.3f}")


def test_elements_without_reference_get_no_delta():
    """没有参考物的元素（水里的 O）**不许**给 δ，并要报告缺哪些。"""
    rows = [
        {"index": 0, "element": "O", "isotropic": 347.344},
        {"index": 1, "element": "H", "isotropic": 32.819},
    ]
    out, missing = compute_deltas(rows, {"H": 33.713})
    by_el = {r["element"]: r for r in out}
    assert "delta" not in by_el["O"], "O 没有参考物却算出了 δ"
    assert "delta" in by_el["H"]
    assert missing == ["O"], missing
    print("    O 无参考值 → 不给 δ，并列出缺失元素")


def test_parser_writes_delta_when_spec_has_references():
    """整条解析链路：spec 带参考值 → result.nmr 里有 δ、有参考溯源。"""
    eng = OrcaEngine()
    spec = JobSpec(
        molecule=_water(), job_type=JobType.NMR, method="HF", basis="STO-3G",
        nmr_reference=(("H", 33.713), ("C", 248.638), ("Si", 577.438)),
        nmr_reference_scheme="tms",
    )
    from qcgui.model import Result
    result = Result(status="done", engine="orca", job_type="nmr")
    result.geometry = {"symbols": ["O", "H", "H"],
                       "coords": [[0, 0, 0], [1, 0, 0], [-1, 0, 0]],
                       "units": "angstrom"}
    eng._fill_nmr(result, SNIPPET, spec)

    nmr = result.nmr
    assert nmr.get("reference_scheme") == "tms"
    assert abs(nmr["references"]["H"] - 33.713) < 1e-9
    assert nmr.get("elements_without_reference") == ["O"]
    rows = {r["element"]: r for r in nmr["rows"]}
    assert "delta" in rows["H"] and "delta" not in rows["O"]
    assert abs(rows["H"]["delta"] - (33.713 - 32.819)) < 1e-9
    note = nmr["note"]
    assert "**" not in note, f"说明里不该有 markdown 星号：{note}"
    assert "没有对应参考物" not in note, "缺失元素应由字段给界面渲染，不要塞进 note"
    print(f"    解析出 δ(H)={rows['H']['delta']:.3f}，O 只给 σ")


def test_parser_without_references_gives_sigma_only():
    """没给参考值时只给 σ，不出现任何 δ 字段（不能凭空造）。"""
    eng = OrcaEngine()
    spec = JobSpec(molecule=_water(), job_type=JobType.NMR,
                   method="HF", basis="STO-3G")
    from qcgui.model import Result
    result = Result(status="done", engine="orca", job_type="nmr")
    result.geometry = {"symbols": ["O", "H", "H"],
                       "coords": [[0, 0, 0], [1, 0, 0], [-1, 0, 0]],
                       "units": "angstrom"}
    eng._fill_nmr(result, SNIPPET, spec)
    assert all("delta" not in r for r in result.nmr["rows"])
    assert "references" not in result.nmr
    print("    无参考值时不出现 δ")


def test_jobspec_roundtrip_keeps_nmr_reference():
    """参考值要能随 JobSpec 存取 —— 否则重跑/读历史时 δ 会消失。"""
    spec = JobSpec(molecule=_water(), job_type=JobType.NMR,
                   nmr_reference=(("H", 33.713), ("C", 248.638)),
                   nmr_reference_scheme="tms")
    data = spec.to_dict()
    assert data["nmr_reference"] == [["H", 33.713], ["C", 248.638]]
    back = JobSpec.from_dict(data)
    assert back.nmr_reference == (("H", 33.713), ("C", 248.638))
    assert back.nmr_reference_scheme == "tms"
    print("    nmr_reference 能完整往返")
