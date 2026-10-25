# -*- coding: utf-8 -*-
r"""复合方法（r2SCAN-3c 等）不能带基组 —— 这条 bug 差点让结果全错。

实测事故
--------
工作流提交 ``method=r2SCAN-3c`` 且没给 basis 时，``JobSpec`` 的默认基组
``def2-SVP`` 生效，生成的输入是：

    ! r2SCAN-3c def2-SVP Opt

ORCA 会用 def2-SVP 覆盖复合方法自带的基组 —— 实际算的是 "r2SCAN-3c 泛函 +
def2-SVP 基组"，而**界面上方法名仍显示 r2SCAN-3c**。这类"名字对、内容错"
正是本项目最需要避免的。

这些测试钉住：
1. ``effective_basis`` 对复合方法返回空；
2. 引擎生成的 ``!`` 行里不出现基组；
3. 工作流构造 JobSpec 时自动清空（用户/默认值漏进来也清）；
4. 普通方法（B3LYP/def2-TZVP）照常保留基组；
5. 刚性基团覆盖全分子时自动跳过（B12H12 硼簇那种情况）。
"""
import time

from qcgui.engines.orca import OrcaEngine
from qcgui.jobs.workflow import WorkflowRunner
from qcgui.model import JobSpec, JobType, Molecule, Result
from qcgui.model.methods import (
    COMPOSITE_METHODS,
    effective_basis,
    is_composite,
)

WATER = """3
water
O  0.000000  0.000000  0.117300
H  0.000000  0.757200 -0.469200
H  0.000000 -0.757200 -0.469200
"""


def _water() -> Molecule:
    return Molecule.from_xyz(WATER)


# ---------------------------------------------------------------- 方法表

def test_composite_methods_recognised():
    assert is_composite("r2SCAN-3c")
    assert is_composite("r2scan-3c")          # 大小写不敏感
    assert not is_composite("B3LYP")
    assert effective_basis("r2SCAN-3c", "def2-SVP") == ""
    assert effective_basis("B3LYP", "def2-TZVP") == "def2-TZVP"
    assert effective_basis("B3LYP", "") == ""
    assert "r2scan-3c" in COMPOSITE_METHODS


# ---------------------------------------------------------------- 引擎

def test_engine_never_puts_basis_on_composite_method():
    """即使 spec 里误带了基组，输入行也不能出现它。"""
    engine = OrcaEngine()
    spec = JobSpec(molecule=_water(), job_type=JobType.OPT,
                   method="r2SCAN-3c", basis="def2-SVP")
    text = engine.build_input_text(spec)
    keyword_line = next(ln for ln in text.splitlines() if ln.startswith("!"))
    assert "def2-SVP" not in keyword_line, keyword_line
    assert "r2SCAN-3c" in keyword_line
    print(f"    输入行: {keyword_line.strip()}")


def test_engine_keeps_basis_for_regular_method():
    engine = OrcaEngine()
    spec = JobSpec(molecule=_water(), job_type=JobType.OPT,
                   method="B3LYP", basis="def2-TZVP")
    text = engine.build_input_text(spec)
    keyword_line = next(ln for ln in text.splitlines() if ln.startswith("!"))
    assert "B3LYP" in keyword_line and "def2-TZVP" in keyword_line


# ---------------------------------------------------------------- 工作流构造

class _Record:
    def __init__(self, jid, molecule="water", job_type="opt"):
        self.job_id = jid
        self.status = "done"
        self.message = ""
        self.running = False
        self.molecule = molecule
        self.job_type = job_type


class _Store:
    def __init__(self, root):
        self.root = root
        self.records = {}
        self.results = {}

    def get(self, jid):
        return self.records.get(jid)

    def load_result(self, jid):
        return self.results.get(jid)


class _StubManager:
    def __init__(self, root):
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

    def cancel(self, jid):
        return True


def _wait(runner, wf_id, timeout=30.0):
    deadline = time.time() + timeout
    info = None
    while time.time() < deadline:
        info = runner.get(wf_id)
        if info is not None and info.status != "running":
            return info
        time.sleep(0.02)
    return info


def test_workflow_clears_default_basis_for_composite_method(tmp_path):
    """工作流步骤只给 method、不给 basis 时，不能落到默认 def2-SVP。"""
    mgr = _StubManager(tmp_path)
    runner = WorkflowRunner(mgr)
    rec = runner.submit(_water(), [
        {"job_type": "opt", "method": "r2SCAN-3c"},
        {"job_type": "energy", "method": "B3LYP", "basis": "def2-TZVP"},
    ])
    info = _wait(runner, rec.workflow_id)
    assert info.status == "done", info.message
    assert mgr.calls[0].method == "r2SCAN-3c"
    assert mgr.calls[0].basis == "", (
        f"复合方法基组应为空，实际 {mgr.calls[0].basis!r} —— "
        "否则 ORCA 会用基组覆盖复合方法自带的基组")
    # 普通方法照常保留
    assert mgr.calls[1].basis == "def2-TZVP"
    print("    工作流：r2SCAN-3c 基组为空，B3LYP 保留 def2-TZVP")


def test_workflow_ignores_explicit_basis_on_composite_method(tmp_path):
    """即使步骤里显式写了 basis，复合方法也要清空。"""
    mgr = _StubManager(tmp_path)
    runner = WorkflowRunner(mgr)
    rec = runner.submit(_water(), [
        {"job_type": "opt", "method": "r2SCAN-3c", "basis": "def2-SVP"},
    ])
    info = _wait(runner, rec.workflow_id)
    assert info.status == "done", info.message
    assert mgr.calls[0].basis == ""


# ---------------------------------------------------------------- 全刚性体系跳过

def _b12h12() -> Molecule:
    phi = (1 + 5 ** 0.5) / 2
    base = [(0, 1, phi), (0, 1, -phi), (0, -1, phi), (0, -1, -phi),
            (1, phi, 0), (1, -phi, 0), (-1, phi, 0), (-1, -phi, 0),
            (phi, 0, 1), (phi, 0, -1), (-phi, 0, 1), (-phi, 0, -1)]
    scale = 1.78 / 2.0
    coords = [(x * scale, y * scale, z * scale) for x, y, z in base]
    hs = []
    for x, y, z in coords:
        norm = (x * x + y * y + z * z) ** 0.5
        hs.append((x + 1.12 * x / norm, y + 1.12 * y / norm, z + 1.12 * z / norm))
    symbols = ["B"] * 12 + ["H"] * 12
    text = f"{len(symbols)}\nB12H12\n" + "\n".join(
        f"{s} {c[0]:.6f} {c[1]:.6f} {c[2]:.6f}" for s, c in zip(symbols, coords + hs))
    return Molecule.from_xyz(text + "\n")


def test_freeze_skipped_when_whole_molecule_is_rigid(tmp_path):
    """B12H12 这类"全是刚性骨架"的体系：冻结无意义，应自动跳过并说明。"""
    mgr = _StubManager(tmp_path)
    runner = WorkflowRunner(mgr)
    rec = runner.submit(_b12h12(), [
        {"job_type": "opt", "freeze_rings": True},
    ])
    info = _wait(runner, rec.workflow_id)
    assert info.status == "done", info.message
    assert mgr.calls[0].rigid_groups == (), "全刚性体系不该生成刚性片段"
    message = info.stages[0].message or ""
    assert "冻结无意义" in message or "无意义" in message, message
    print(f"    跳过信息: {message[:60]}")
