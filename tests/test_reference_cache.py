# -*- coding: utf-8 -*-
r"""NMR 参考值磁盘缓存的回归测试。

最重要的一条：**缓存键必须覆盖所有影响 σ 的输入**。
不同基组、方法、溶剂、额外关键字（! RI / ! TightSCF / DFT 网格）都会改变
屏蔽常数的绝对值。如果键不全，就会拿 A 设置的参考值去算 B 设置的 δ ——
δ 系统性偏移，而界面上完全看不出异常。这是最难发现的那类错误。

实测对照（真实 ORCA，TMS 的 H 参考值）：
  HF/STO-3G 33.713、HF/6-31G* 32.896、B3LYP/STO-3G 32.714 ppm
—— 三者互不相同，所以必须各自一条缓存。
"""
from qcgui.jobs.reference_cache import (
    REFERENCE_VERSION,
    NmrReferenceCache,
    _normalise_extra_keywords,
)

REFS = {"H": 33.713, "C": 248.638, "Si": 577.438}


def _cache(tmp_path) -> NmrReferenceCache:
    return NmrReferenceCache(tmp_path)


def test_put_then_get_hits(tmp_path):
    cache = _cache(tmp_path)
    cache.put("tms", "HF", "STO-3G", REFS, source_jobs=["job-a", "job-b"])
    hit = cache.get("tms", "HF", "STO-3G")
    assert hit is not None
    assert abs(hit["references"]["H"] - 33.713) < 1e-9
    assert hit["source_jobs"] == ["job-a", "job-b"], "来源任务没保存，无法追溯"
    print("    同一条件第二次命中，且能追溯来源任务")


def test_different_basis_does_not_hit(tmp_path):
    """不同基组必须**不命中** —— σ 的绝对标度依赖基组。"""
    cache = _cache(tmp_path)
    cache.put("tms", "HF", "STO-3G", REFS)
    assert cache.get("tms", "HF", "6-31G*") is None, (
        "6-31G* 命中了 STO-3G 的参考值 —— δ 会系统性偏移且看不出来"
    )
    cache.put("tms", "HF", "6-31G*", {"H": 32.896, "C": 201.681, "Si": 451.639})
    assert len(cache.list_entries()) == 2
    assert cache.get("tms", "HF", "STO-3G")["references"]["H"] == REFS["H"]
    print("    不同基组各自独立缓存（STO-3G 33.713 vs 6-31G* 32.896）")


def test_different_method_does_not_hit(tmp_path):
    cache = _cache(tmp_path)
    cache.put("tms", "HF", "STO-3G", REFS)
    assert cache.get("tms", "B3LYP", "STO-3G") is None
    cache.put("tms", "B3LYP", "STO-3G", {"H": 32.714})
    assert len(cache.list_entries()) == 2
    print("    不同方法各自独立缓存（HF 33.713 vs B3LYP 32.714）")


def test_different_solvent_does_not_hit(tmp_path):
    cache = _cache(tmp_path)
    cache.put("tms", "HF", "STO-3G", REFS, solvent=None)
    assert cache.get("tms", "HF", "STO-3G", solvent="water") is None
    cache.put("tms", "HF", "STO-3G", REFS, solvent="water")
    assert len(cache.list_entries()) == 2
    print("    气相与 SMD(water) 各自独立缓存")


def test_different_extra_keywords_does_not_hit(tmp_path):
    """``! RI`` / ``! TightSCF`` 这类关键字同样会改变 σ，必须分开缓存。"""
    cache = _cache(tmp_path)
    cache.put("tms", "HF", "STO-3G", REFS)
    assert cache.get("tms", "HF", "STO-3G", extra_keywords=("RI",)) is None, (
        "加了 ! RI 却命中无 RI 的参考值 —— 两套设置混用"
    )
    cache.put("tms", "HF", "STO-3G", {"H": 33.6}, extra_keywords=["RI"])
    assert len(cache.list_entries()) == 2
    print("    额外关键字不同 → 独立缓存")


def test_extra_keywords_normalisation_is_case_insensitive(tmp_path):
    """ORCA 关键字大小写不敏感：``ri`` 与 ``RI`` 不能算两套缓存。"""
    assert _normalise_extra_keywords(["ri", "TightSCF"]) == ("RI", "TIGHTSCF")
    assert _normalise_extra_keywords("RI tightscf") == ("RI", "TIGHTSCF")
    cache = _cache(tmp_path)
    cache.put("tms", "HF", "STO-3G", REFS, extra_keywords=["ri"])
    assert cache.get("tms", "HF", "STO-3G", extra_keywords=["RI"]) is not None
    assert len(cache.list_entries()) == 1
    print("    ! ri 与 ! RI 视为同一条件（不重复缓存）")


def test_whitespace_is_normalised(tmp_path):
    """方法/基组带空格不该产生第二份缓存。"""
    cache = _cache(tmp_path)
    cache.put("tms", "HF", "STO-3G", REFS)
    assert cache.get("tms", " HF ", " STO-3G ") is not None
    assert len(cache.list_entries()) == 1
    print("    方法/基组前后空格已归一")


def test_reference_version_bump_invalidates_old_entries(tmp_path):
    """版本号变了，旧缓存必须失效（参考物几何可能已改）。"""
    cache = _cache(tmp_path)
    cache.put("tms", "HF", "STO-3G", REFS)
    # 手工把磁盘上的 version 改旧，模拟"升级后残留的旧缓存"
    path = next(tmp_path.glob("*.json"))
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    data["reference_version"] = REFERENCE_VERSION - 1
    data["key"][-1] = REFERENCE_VERSION - 1
    path.write_text(json.dumps(data), encoding="utf-8")
    assert cache.get("tms", "HF", "STO-3G") is None, "旧版本缓存没有被失效"
    print("    版本号变更会让旧缓存失效")


def test_corrupt_cache_file_is_ignored(tmp_path):
    """坏文件当成没缓存：宁可重算，也不要用半截数据算 δ。"""
    cache = _cache(tmp_path)
    cache.put("tms", "HF", "STO-3G", REFS)
    path = next(tmp_path.glob("*.json"))
    path.write_text("{ 这不是合法 JSON", encoding="utf-8")
    assert cache.get("tms", "HF", "STO-3G") is None
    # 重算并写回，仍然能用
    cache.put("tms", "HF", "STO-3G", REFS)
    assert cache.get("tms", "HF", "STO-3G") is not None
    print("    坏缓存被忽略并可被覆盖修复")


def test_clear_removes_everything(tmp_path):
    cache = _cache(tmp_path)
    cache.put("tms", "HF", "STO-3G", REFS)
    cache.put("tms", "HF", "6-31G*", {"H": 32.896})
    assert len(cache.list_entries()) == 2
    assert cache.clear() == 2
    assert cache.list_entries() == []
    print("    清空缓存")

def test_composite_method_ignores_basis_in_cache_key(tmp_path):
    """复合方法（3c 系列）自带基组，键里不该带基组 —— 否则同一方法多条缓存。"""
    cache = _cache(tmp_path)
    cache.put("tms", "r2SCAN-3c", "", REFS)
    # 界面上基组字段可能还留着别的值，不该产生第二条缓存
    assert cache.get("tms", "r2SCAN-3c", "def2-SVP") is not None, (
        "复合方法因基组字段不同而没命中 —— 会重复计算参考值"
    )
    cache.put("tms", "r2SCAN-3c", "def2-SVP", REFS)
    assert len(cache.list_entries()) == 1, "复合方法产生了重复缓存条目"
    # 普通方法仍然按基组区分
    cache.put("tms", "HF", "STO-3G", REFS)
    cache.put("tms", "HF", "6-31G*", {"H": 32.896})
    assert len(cache.list_entries()) == 3
    print("    复合方法忽略基组；普通方法仍按基组区分")
