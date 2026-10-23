# -*- coding: utf-8 -*-
r"""工作流增强的回归测试：每步不同方法/基组、opt→单点能、WP04(LibXC) 方法。

背景（实测）
------------
1. ORCA 6.1.1 **没有** ``! WP04`` 这种关键字（实测 INPUT ERROR），
   但内置 LibXC 7.0 里有 ``hyb_gga_xc_wp04``；正确写法是
   ``%method`` + ``Functional hyb_gga_xc_wp04``。写错的话任务直接起不来。
2. "优化完算更高精度单点能"是最常见的组合：低级别优化、高级别能量。
   必须保证第二步用的是**第一步的结果几何**，且两步的级别各自生效。
3. 界面模板（WF_PRESETS）与后端任务类型必须对得上 —— 模板里写了一个
   后端不存在的任务类型，用户点了就会失败。
"""
import inspect
import json
from pathlib import Path

from qcgui.engines.orca import OrcaEngine
from qcgui.jobs.workflow import WorkflowRunner
from qcgui.model import JobSpec, JobType, Molecule, Result
from qcgui.model.methods import LIBXC_METHODS, resolve_method, suggested_basis

WATER = """3
water
O  0.000000  0.000000  0.117300
H  0.000000  0.757200 -0.469200
H  0.000000 -0.757200 -0.469200
"""


def _water() -> Molecule:
    return Molecule.from_xyz(WATER)


# ---------------------------------------------------------------- LibXC 方法

def test_wp04_is_a_libxc_method_not_a_plain_keyword():
    method = resolve_method("WP04")
    assert method is not None
    assert method.libxc_name == "hyb_gga_xc_wp04"
    # 大小写不敏感
    assert resolve_method("wp04") is not None
    assert resolve_method("B3LYP") is None


def test_wp04_wc04_and_suggested_basis():
    assert set(LIBXC_METHODS) >= {"WP04", "WC04"}
    assert suggested_basis("WP04") == "pcSseg-2"
    assert suggested_basis("B3LYP") == ""


def test_build_input_translates_wp04_into_method_block():
    """WP04 必须写成 %method 块；**不能**出现在 ! 行（会 INPUT ERROR）。"""
    engine = OrcaEngine()
    spec = JobSpec(molecule=_water(), job_type=JobType.NMR, method="WP04",
                   basis="pcSseg-2")
    text = engine.build_input_text(spec)
    keyword_line = next(l for l in text.splitlines() if l.startswith("!"))
    assert "WP04" not in keyword_line.upper(), keyword_line
    assert "%method" in text, text[:400]
    block = text[text.index("%method"):]
    assert "hyb_gga_xc_wp04" in block.split("end")[0], block[:200]
    print(f"    关键字行: {keyword_line.strip()}")


def test_plain_method_still_goes_on_keyword_line():
    engine = OrcaEngine()
    spec = JobSpec(molecule=_water(), job_type=JobType.NMR, method="B3LYP",
                   basis="pcSseg-2")
    text = engine.build_input_text(spec)
    keyword_line = next(l for l in text.splitlines() if l.startswith("!"))
    assert "B3LYP" in keyword_line
    assert "%method" not in text


def test_user_method_block_is_not_duplicated():
    """用户自己写了 %method 块时不要再加一个（ORCA 不接受重复块）。"""
    engine = OrcaEngine()
    spec = JobSpec(molecule=_water(), job_type=JobType.NMR, method="WP04",
                   basis="pcSseg-2",
                   extra_blocks=(("method", "Functional hyb_gga_xc_wp04"),))
    text = engine.build_input_text(spec)
    assert text.count("%method") == 1, text


# ---------------------------------------------------------------- 每步不同级别

class _Record:
    def __init__(self, job_id, status="done", molecule="water", job_type="energy"):
        self.job_id = job_id
        self.status = status
        self.message = ""
        self.running = False
        # molecule_from_record 会读这两个字段（生成下一步的分子名/类型）
        self.molecule = molecule
        self.job_type = job_type


class _Store:
    def __init__(self, root: Path):
        self.root = root
        self.records = {}
        self.results = {}

    def get(self, job_id):
        return self.records.get(job_id)

    def load_result(self, job_id):
        return self.results.get(job_id)


class _StubManager:
    """记录每次提交的 spec；返回带轻微位移的几何，便于验证血缘。"""

    def __init__(self, root: Path):
        self.store = _Store(root)
        self.calls = []
        self._n = 0

    def submit(self, spec, on_event=None, blocking=False, job_id=None):
        self.calls.append(spec)
        self._n += 1
        jid = f"job-{self._n:03d}"
        self.store.records[jid] = _Record(jid, molecule=spec.molecule.name,
                                          job_type=spec.job_type.value)
        result = Result(status="done", engine="orca")
        result.geometry = {
            "symbols": list(spec.molecule.symbols),
            "coords": [[c[0] + 1.0, c[1], c[2]] for c in spec.molecule.coords],
            "units": "angstrom",
        }
        self.store.results[jid] = result
        return self.store.records[jid]

    def cancel(self, job_id):
        return True


def test_each_stage_can_use_its_own_method_and_basis(tmp_path):
    mgr = _StubManager(tmp_path)
    runner = WorkflowRunner(mgr)
    rec = runner.submit(_water(), [
        {"job_type": "opt", "method": "HF", "basis": "STO-3G"},
        {"job_type": "energy", "method": "B3LYP", "basis": "def2-TZVP"},
    ])
    info = None
    import time
    for _ in range(300):
        info = runner.get(rec.workflow_id)
        if info is not None and info.status != "running":
            break
        time.sleep(0.02)

    assert info.status == "done", info.message
    assert [c.job_type.value for c in mgr.calls] == ["opt", "energy"]
    assert (mgr.calls[0].method, mgr.calls[0].basis) == ("HF", "STO-3G")
    assert (mgr.calls[1].method, mgr.calls[1].basis) == ("B3LYP", "def2-TZVP")
    # 第二步用的是第一步的结果几何（x 累加 1.0）
    assert mgr.calls[1].molecule.coords[0][0] == 1.0
    # 记录里带上了每步级别，界面才能显示
    assert info.stages[0].method == "HF" and info.stages[1].method == "B3LYP"
    assert info.stages[0].basis == "STO-3G" and info.stages[1].basis == "def2-TZVP"
    print("    两步分别用 HF/STO-3G 与 B3LYP/def2-TZVP，且第二步沿用第一步几何")


def test_stage_without_level_inherits_shared_settings(tmp_path):
    """步骤里留空 → 沿用顶层共享设置（界面"留空=沿用"的语义）。"""
    mgr = _StubManager(tmp_path)
    runner = WorkflowRunner(mgr)
    rec = runner.submit(_water(), [
        {"job_type": "opt"},
        {"job_type": "energy"},
    ], )
    # submit 的共享设置来自 stages 合并；这里直接看两次提交都拿到了什么
    import time
    for _ in range(300):
        info = runner.get(rec.workflow_id)
        if info is not None and info.status != "running":
            break
        time.sleep(0.02)
    assert info.status == "done"
    # 没给 method/basis 时用 JobSpec 默认值，两步一致
    assert mgr.calls[0].method == mgr.calls[1].method
    assert mgr.calls[0].basis == mgr.calls[1].basis


# ---------------------------------------------------------------- 界面模板契约

def test_workflow_presets_reference_real_job_types():
    """前端模板里出现的任务类型必须是后端真有的（否则点了就失败）。"""
    js = Path(r"D:\cao\v2\qcgui\qcgui\web\static\app.js").read_text(
        encoding="utf-8", errors="replace")
    assert "WF_PRESETS" in js, "app.js 里找不到工作流模板"
    # 关键模板必须在
    for key in ("opt,energy", "opt,nmr", "opt,nmr-wp04", "opt,freq"):
        assert key in js, f"模板 {key} 缺失"
    # 模板里用到的 job_type 必须是 JobType 的真值
    valid = {t.value for t in JobType}
    import re
    stages = re.findall(r"\{ job_type: '([a-z_]+)'", js)
    assert stages, "没解析到任何模板步骤"
    bad = sorted(set(stages) - valid)
    assert not bad, f"模板里出现后端不支持的任务类型: {bad}"
    print(f"    模板步骤类型均合法: {sorted(set(stages))}")


def test_stage_levels_are_exposed_in_api_shape():
    """/api/workflows 返回的步骤必须带 method/basis/solvent（界面显示用）。"""
    from qcgui.jobs.workflow import StageRecord
    d = StageRecord(job_type="opt", method="HF", basis="STO-3G").to_dict()
    assert d["method"] == "HF" and d["basis"] == "STO-3G"


def test_workflow_save_is_atomic():
    """状态落盘必须是原子写（并发读不能读到半截 JSON）。"""
    from qcgui.jobs import workflow as wf_mod
    src = inspect.getsource(wf_mod.WorkflowRunner._save)
    assert "os.replace" in src and ".tmp" in src, "工作流状态落盘不是原子写"
