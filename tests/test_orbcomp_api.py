"""轨道成分接口的契约测试（`GET /api/jobs/<id>/orbital-composition`）。

为什么单独一个文件：`tests/test_orbcomp.py` 测的是**解析层**（黄金文件），
这里测的是**接口层契约** —— 参数校验、状态码、错误文案是否可操作。
界面（`app.js` 的轨道成分区）完全依赖这份契约：

* 参数非法 → 400 + `supported` 列表（界面据此提示可选值，不出现"未知错误"）；
* 任务不存在 → 404；
* 没有 `.gbw` → 400，且文案里必须出现 `.gbw`（用户得知道缺什么才能补）；
* Multiwfn 拿不到结果 → 400（不是 200 带空表），这样界面才会走错误分支。

这些用例**不触发 Multiwfn 计算**（都在参数校验或文件检查阶段返回），
所以在没有 Multiwfn / 没有写权限的机器上也能跑。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.jobs import JobManager  # noqa: E402
from qcgui.model import JobSpec, JobType, Molecule  # noqa: E402
from qcgui.web.app import create_app  # noqa: E402

WATER_XYZ = """3
water
O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
"""


@pytest.fixture()
def client():
    root = Path(tempfile.mkdtemp(prefix="qcgui_oc_api_"))
    app = create_app(JobManager(root / "jobs"))
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


def _make_job(root: Path, **overrides) -> str:
    """建一个真实任务记录（不跑引擎），返回 job_id。"""
    from qcgui.jobs import JobStore

    store = JobStore(root / "jobs")
    spec = JobSpec(molecule=Molecule.from_xyz(WATER_XYZ),
                   job_type=overrides.pop("job_type", JobType.ENERGY),
                   method="HF", basis="STO-3G", **overrides)
    record = store.create(spec)
    return record.job_id


def test_unknown_partition_and_orbital_are_rejected():
    """非法参数必须被拒，并给出可选值。

    注意顺序：接口**先查任务是否存在**（404），再做参数校验（400）——
    所以这条要用一个真实存在的任务，否则拿到的是 404 而不是 400。
    """
    root = Path(tempfile.mkdtemp(prefix="qcgui_oc_bad_"))
    app = create_app(JobManager(root / "jobs"))
    app.config.update(TESTING=True)
    job_id = _make_job(root)
    with app.test_client() as c:
        for query, field in (("partition=foo&orbital=homo", "布居"),
                             ("partition=mulliken&orbital=bar", "轨道")):
            res = c.get(f"/api/jobs/{job_id}/orbital-composition?{query}")
            assert res.status_code == 400, (res.status_code, res.get_json())
            body = res.get_json()
            assert "supported" in body, body
            assert isinstance(body["supported"], list) and body["supported"], body
            assert field in body["error"], body


@pytest.mark.parametrize("partition", ["mulliken", "stout", "scpa"])
def test_legal_partitions_pass_validation(client, partition):
    """合法布居方式不该被参数校验拦下（走到"任务不存在"说明校验已通过）。"""
    res = client.get(f"/api/jobs/nonexistent-job/orbital-composition?partition={partition}")
    assert res.status_code == 404, res.get_json()
    assert "任务不存在" in res.get_json()["error"]


@pytest.mark.parametrize("orbital", ["homo", "lumo", "homo-1", "lumo+1"])
def test_legal_orbitals_pass_validation(client, orbital):
    res = client.get(f"/api/jobs/nonexistent-job/orbital-composition?orbital={orbital}")
    assert res.status_code == 404, res.get_json()


def test_missing_job_returns_404(client):
    res = client.get("/api/jobs/20200101-000000-doesnotexist/orbital-composition")
    assert res.status_code == 404
    assert res.get_json()["error"] == "任务不存在"


def test_missing_gbw_returns_actionable_400():
    """没有 .gbw 时文案必须点到 `.gbw` —— 用户得知道缺什么。"""
    root = Path(tempfile.mkdtemp(prefix="qcgui_oc_gbw_"))
    app = create_app(JobManager(root / "jobs"))
    app.config.update(TESTING=True)
    with app.test_client() as c:
        job_id = _make_job(root)
        res = c.get(f"/api/jobs/{job_id}/orbital-composition")
        assert res.status_code == 400, res.get_json()
        assert ".gbw" in res.get_json()["error"], res.get_json()


def test_response_shape_matches_what_the_ui_renders():
    """把界面依赖的字段名钉住（`app.js::renderOrbComp` 读的就是这些）。

    用的是**真实 Multiwfn 输出**（咖啡因 LUMO，mulliken 布居，本次实测捕获）
    作为夹具，不是手写的样本 —— 手写样本会和真实格式漂移。
    这条守的是**字段名与语义**；数值本身由 tests/test_orbcomp.py 的黄金文件守。
    """
    from qcgui.model.orbcomp import OrbitalComposition, parse_orbital_composition

    golden = (Path(__file__).resolve().parent / "fixtures"
              / "orbcomp_mulliken_lumo.txt").read_text(encoding="utf-8", errors="replace")
    parsed = parse_orbital_composition(golden, "mulliken", "lumo")
    assert isinstance(parsed, OrbitalComposition)
    data = parsed.to_dict()
    for field in ("orbital_index", "energy_au", "energy_ev", "occupancy",
                  "partition", "partition_label", "request", "request_label",
                  "threshold", "count", "atoms", "top_label", "problems"):
        assert field in data, f"界面依赖的字段 {field} 缺失"
    assert not data["problems"], data["problems"]
    assert data["count"] == len(data["atoms"]) >= 1
    assert data["threshold"] == 0.5
    # atoms 必须按贡献降序 —— 界面的"相对最强"比例条直接依赖这个顺序
    pcts = [a["percent"] for a in data["atoms"]]
    assert pcts == sorted(pcts, reverse=True), pcts
    assert data["top_label"].startswith(data["atoms"][0]["label"]), data["top_label"]
    # 每个原子都要有 label 与 shells（界面两列都渲染）
    for a in data["atoms"]:
        assert a["label"], a
        assert isinstance(a["shells"], list), a
    # 界面据此做编号翻译：Multiwfn 1 起（这里报 52），ORCA 口径是 51（LUMO）
    assert data["orbital_index"] == 52, data["orbital_index"]
    json.dumps(data)  # 必须可 JSON 序列化（接口就是 jsonify 它）
