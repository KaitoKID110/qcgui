# -*- coding: utf-8 -*-
r"""NMR 任务类型与屏蔽常数解析的回归测试。

真实依据（实测 ORCA 6.1.1，水 / HF / STO-3G）：
  * ``! ... NMR`` 能正常终止；
  * ``orca_2json`` 的 property.json 里**没有** NMR 数据，
    屏蔽常数只在 .out 的 ``CHEMICAL SHIELDING SUMMARY`` 定宽表里；
  * 优化前后数值不同（几何变了），说明几何血缘确实在起作用。

另一个必须钉住的点：ORCA 给的是**绝对屏蔽常数 σ**，不是化学位移 δ。
把 σ 当 δ 显示会让人误以为能和文献直接比 —— 这是"看起来能用其实错"。
"""
from qcgui.engines.orca import OrcaEngine, _JOB_KEYWORDS
from qcgui.model import JobType, Result

#: 从真实 ORCA 输出里截出来的那一段（原样保留定宽格式）
REAL_SNIPPET = """
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


def _result(**kw) -> Result:
    r = Result(status="done", engine="orca", job_type="nmr")
    r.geometry = {"symbols": ["O", "H", "H"],
                  "coords": [[0, 0, 0], [1, 0, 0], [-1, 0, 0]],
                  "units": "angstrom"}
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_nmr_is_a_supported_job_type():
    """NMR 必须是已声明支持的任务类型，且输入里带 NMR 关键字。"""
    assert JobType.NMR.value == "nmr"
    assert JobType.NMR.label == "NMR 屏蔽常数"
    assert _JOB_KEYWORDS.get(JobType.NMR) == ("NMR",), "NMR 关键字没登记"
    print("    NMR 任务类型与 ! NMR 关键字都在")


def test_shielding_summary_is_parsed():
    """真实输出版式必须能解析出每个核的 σ 与各向异性。"""
    eng = OrcaEngine()
    result = _result()
    eng._fill_nmr(result, REAL_SNIPPET)

    nmr = result.nmr
    assert nmr, f"没解析出 NMR：{[i.message for i in result.issues]}"
    assert nmr["quantity"] == "isotropic_shielding_ppm"
    assert nmr["n_nuclei"] == 3
    rows = nmr["rows"]
    assert [r["index"] for r in rows] == [0, 1, 2]
    assert [r["element"] for r in rows] == ["O", "H", "H"]
    assert abs(rows[0]["isotropic"] - 347.344) < 1e-6
    assert abs(rows[0]["anisotropy"] - 13.331) < 1e-6
    # 说明里必须写明"σ 不是 δ"
    assert "σ" in nmr["note"] and "化学位移" in nmr["note"]
    print(f"    解析出 {len(rows)} 个核：O σ={rows[0]['isotropic']} ppm")


def test_missing_summary_records_an_issue_instead_of_faking():
    """输出里没有那张表时必须留空 + 记 major issue，不能编数据。"""
    eng = OrcaEngine()
    result = _result()
    eng._fill_nmr(result, "some output without any nmr table\n")
    assert result.nmr == {}, "没有数据却写了 nmr"
    codes = [i.code for i in result.issues]
    assert "nmr_summary_missing" in codes, codes
    print("    缺表时留空并记 issue（不编数据）")


def test_element_order_mismatch_is_rejected():
    """表里的元素顺序与结构对不上时，宁可报错也不能张冠李戴。"""
    eng = OrcaEngine()
    result = _result()
    result.geometry = {"symbols": ["H", "H", "O"],   # 故意与表里 O,H,H 不同
                       "coords": [[0, 0, 0], [1, 0, 0], [-1, 0, 0]],
                       "units": "angstrom"}
    eng._fill_nmr(result, REAL_SNIPPET)
    assert result.nmr == {}, "顺序对不上却仍然写了结果"
    codes = [i.code for i in result.issues]
    assert "nmr_element_mismatch" in codes, codes
    print("    元素顺序不一致时拒绝出数据")


def test_nmr_survives_result_roundtrip():
    """nmr 必须能持久化到 result.json 再读回来（不是只活在内存里）。"""
    eng = OrcaEngine()
    result = _result()
    eng._fill_nmr(result, REAL_SNIPPET)
    data = result.to_dict()
    assert "nmr" in data and data["nmr"]["rows"], "to_dict 里没有 nmr"
    back = Result.from_dict(data)
    assert back.nmr["rows"][0]["isotropic"] == result.nmr["rows"][0]["isotropic"]
    print("    nmr 字段能完整存取")
