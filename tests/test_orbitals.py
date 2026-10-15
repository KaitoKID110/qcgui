"""分子轨道：解析、cube 导出、Molden 转换、以及 Web 接口。

涉及真实 ORCA 运行与 orca_plot 调用，缺 ORCA 时自动跳过。

    pytest tests/test_orbitals.py -v
    python tests/test_orbitals.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui import orcatools  # noqa: E402
from qcgui.engines.orca import OrcaEngine  # noqa: E402
from qcgui.model import JobSpec, JobType, Molecule  # noqa: E402
from qcgui.units import HARTREE_TO_EV  # noqa: E402

WATER_XYZ = """3
water
O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
"""


class _Skipped(Exception):
    pass


def _skip(reason: str):
    try:
        import pytest

        pytest.skip(reason)
    except ImportError:
        raise _Skipped(reason)


def _require_orca() -> str:
    exe = orcatools.orca_executable()
    if not exe:
        _skip("找不到 ORCA，跳过")
    return exe


def _run_water_job() -> tuple[object, object, Path]:
    """跑一个便宜的 HF/STO-3G 水单点，返回 (引擎, 结果, 任务目录)。"""
    from qcgui.engines import get_engine

    _require_orca()
    engine = get_engine("orca")
    ok, why = engine.available()
    if not ok:
        _skip(f"ORCA 不可用: {why}")

    work = Path(tempfile.mkdtemp(prefix="qcgui_orbitals_"))
    spec = JobSpec(
        molecule=Molecule.from_xyz(WATER_XYZ),
        job_type=JobType.ENERGY,
        method="HF",
        basis="STO-3G",
        nprocs=2,
        maxcore_mb=1000,
        title="water",
    )
    result = engine.execute(spec, work)
    assert result.status == "done", result.message
    return engine, result, work


# ------------------------------------------------------------------ orcatools

def test_probe_finds_orca_and_tools():
    _require_orca()
    info = orcatools.probe()
    assert info["orca"] and Path(info["orca"]).is_file()
    for name in ("orca_plot", "orca_2mkl", "orca_2json"):
        assert info["tools"][name], f"{name} 没找到"
    assert info["ok"] is True
    print(f"    orca={Path(info['orca']).name}  三个配套工具均就位")


def test_mo_cube_name_matches_orca_plot_convention():
    gbw = Path("water_energy.gbw")
    assert orcatools.mo_cube_name(gbw, 5) == "water_energy.mo5a.cube"
    assert orcatools.mo_cube_name(gbw, 5, operator=1) == "water_energy.mo5b.cube"
    print("    cube 命名与 orca_plot 实际产出一致")


def test_missing_tool_and_gbw_are_reported_not_raised():
    """缺文件 / 缺工具都要返回失败对象并给出消息，而不是抛异常。

    .. note::
       两种情形都必须能过。**CI 上没有 orca_plot**，此时代码正确地先报
       "工具缺失"而不是"文件不存在" —— 以前这里写死了文件那条消息，
       于是只有 CI 红（本机装了 ORCA，永远走文件那条分支）。
    """
    run = orcatools.export_mo_cube(Path(tempfile.gettempdir()), "不存在.gbw", 0)
    assert run.ok is False, f"应当返回失败对象，实际 ok={run.ok}"
    assert run.message, "失败时必须给出说明"

    run2 = orcatools.make_molden(Path(tempfile.gettempdir()), "不存在.gbw")
    assert run2.ok is False, f"应当返回失败对象，实际 ok={run2.ok}"
    assert run2.message, "失败时必须给出说明"

    # 有工具时走"文件不存在"分支；没有工具时走"工具缺失"分支 —— 两者都对
    if orcatools.find_tool("orca_plot"):
        assert "找不到波函数文件" in run.message, run.message
    else:
        assert ("orca_plot" in run.message or "找不到" in run.message
                or "未找到" in run.message), run.message
    print(f"    缺文件时返回失败对象：{run.message[:60]}")


# ------------------------------------------------------------------ 引擎解析

def test_engine_parses_orbitals_and_gap():
    engine, result, _ = _run_water_job()
    orb = result.orbitals
    assert orb, "结果里应当有分子轨道数据"

    # 水的 STO-3G：5 个占据 + 2 个空 = 7 个轨道，10 个电子
    assert orb["count"] == 7, orb["count"]
    assert orb["n_electrons"] == 10
    assert orb["homo"] == 4, f"HOMO 应当是 #4，实际 {orb['homo']}"
    assert orb["lumo"] == 5, f"LUMO 应当是 #5，实际 {orb['lumo']}"

    orbitals = orb["orbitals"]
    assert len(orbitals) == 7
    assert orbitals[4]["occupancy"] == 2.0
    assert orbitals[5]["occupancy"] == 0.0
    assert orbitals[0]["energy"] < orbitals[4]["energy"], "轨道应按能量递增"

    # HOMO-LUMO 间隙自洽
    gap = orb["lumo_energy"] - orb["homo_energy"]
    assert abs(orb["gap_hartree"] - gap) < 1e-9
    assert abs(orb["gap_ev"] - gap * HARTREE_TO_EV) < 1e-6
    # STO-3G 是极小基，会**严重高估**间隙：水实测约 10.6 eV，这里给到 ~27 eV。
    # 所以这里只做量级校验，真正的物理锚点是 HOMO 能量。
    assert 20.0 < orb["gap_ev"] < 40.0, f"水的间隙 {orb['gap_ev']} eV 超出 STO-3G 的合理范围"
    assert -13.0 < orb["homo_ev"] < -9.0, f"HOMO {orb['homo_ev']} eV 偏离水的真实值(-12.6 eV)太远"

    # 与原始 JSON 逐值对齐，防止解析层算错
    assert abs(orbitals[4]["energy"] - (-0.391237)) < 0.01
    assert abs(orbitals[5]["energy"] - 0.605171) < 0.01

    print(f"    7 个轨道，HOMO=#{orb['homo']} ({orb['homo_ev']:.3f} eV)，"
          f"LUMO=#{orb['lumo']} ({orb['lumo_ev']:.3f} eV)，间隙 {orb['gap_ev']:.3f} eV")


def test_orbitals_survive_result_roundtrip():
    engine, result, _ = _run_water_job()
    from qcgui.model import Result

    again = Result.from_dict(json.loads(json.dumps(result.to_dict())))
    assert again.orbitals["homo"] == result.orbitals["homo"]
    assert len(again.orbitals["orbitals"]) == len(result.orbitals["orbitals"])
    print("    轨道数据经 JSON 往返后一致")


# ------------------------------------------------------------------ cube / molden

def test_export_homo_cube():
    _require_orca()
    if not orcatools.find_tool("orca_plot"):
        _skip("找不到 orca_plot")
    engine, result, job_dir = _run_water_job()
    gbw = result.files["wavefunction"]

    homo = result.orbitals["homo"]
    run = orcatools.export_mo_cube(job_dir, gbw, homo, grid=30)
    assert run.ok, run.message
    assert run.artifacts, run.message

    cube = Path(run.artifacts[0])
    assert cube.is_file() and cube.stat().st_size > 0
    assert cube.name == orcatools.mo_cube_name(Path(gbw), homo)

    head = cube.read_text(encoding="utf-8", errors="replace").splitlines()[:2]
    assert "Molecular orbital" in head[1], head
    assert f"orbital {homo} " in head[1], f"文件头写的轨道号不对: {head[1]}"

    print(f"    HOMO #{homo} → {cube.name}（{cube.stat().st_size / 1024:.0f} KB）")


def test_export_rejects_out_of_range_orbital():
    """编号越界时必须报失败 —— orca_plot 会打印 Invalid MO 但仍可能留下文件。"""
    _require_orca()
    engine, result, job_dir = _run_water_job()
    run = orcatools.export_mo_cube(job_dir, result.files["wavefunction"], 99, grid=30)
    assert run.ok is False, "越界轨道不该报告成功"
    print("    越界轨道被正确拒绝")


def test_make_molden_for_multiwfn():
    _require_orca()
    if not orcatools.find_tool("orca_2mkl"):
        _skip("找不到 orca_2mkl")
    engine, result, job_dir = _run_water_job()
    run = orcatools.make_molden(job_dir, result.files["wavefunction"])
    assert run.ok, run.message
    molden = Path(run.artifacts[0])
    assert molden.is_file() and molden.stat().st_size > 0
    text = molden.read_text(encoding="utf-8", errors="replace")[:400]
    assert "[Molden" in text or "[Atoms]" in text, text[:200]
    print(f"    Molden: {molden.name}（{molden.stat().st_size / 1024:.0f} KB）")


# ------------------------------------------------------------------ Web 接口

class Server:
    def __init__(self) -> None:
        from qcgui.jobs import JobManager
        from qcgui.web.app import create_app
        from werkzeug.serving import make_server

        self.root = Path(tempfile.mkdtemp(prefix="qcgui_orb_api_"))
        self.manager = JobManager(self.root / "jobs")
        self.app = create_app(self.manager)
        self.srv = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.port = self.srv.server_port
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()

    def request(self, path: str, method: str = "GET", payload=None, timeout: float = 600):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read().decode("utf-8"))


def test_web_orbital_endpoints():
    _require_orca()
    server = Server()
    try:
        code, data = server.request("/api/jobs", "POST", {
            "molecule": {
                "symbols": ["O", "H", "H"],
                "coords": [[0, 0, 0.1173], [0, 0.7572, -0.4692], [0, -0.7572, -0.4692]],
                "charge": 0, "multiplicity": 1, "name": "water",
            },
            "recipe_id": "debug_sp_hf_sto3g", "nprocs": 2,
        })
        assert code == 201, data
        job_id = data["job"]["job_id"]

        deadline = time.time() + 180
        while time.time() < deadline:
            code, data = server.request(f"/api/jobs/{job_id}")
            if data["job"]["status"] not in ("queued", "running"):
                break
            time.sleep(0.5)
        assert data["job"]["status"] == "done", data["job"]

        # 轨道列表
        code, orb = server.request(f"/api/jobs/{job_id}/orbitals")
        assert code == 200
        assert orb["count"] == 7 and orb["homo"] == 4 and orb["lumo"] == 5
        assert orb["gap_ev"] > 0
        assert orb["wavefunction"], "应当有波函数文件名"
        assert orb["tools"]["ok"] is True
        assert orb["cubes"] == {}, "还没导出时不该有 cube"

        # 导出 HOMO 的 cube
        code, run = server.request(f"/api/jobs/{job_id}/orbitals/4/cube", "POST", {"grid": 30})
        assert code == 200 and run["ok"], run
        assert run["role"] == "mo4"

        # 导出后再查，应当出现这个 cube，且能下载
        code, orb2 = server.request(f"/api/jobs/{job_id}/orbitals")
        assert "mo4" in orb2["cubes"], orb2["cubes"]
        with urllib.request.urlopen(server.base + run["url"], timeout=60) as res:
            body = res.read()
        assert b"Molecular orbital 4" in body[:200], body[:200]

        # 越界轨道
        try:
            server.request(f"/api/jobs/{job_id}/orbitals/99/cube", "POST", {"grid": 30})
            raise AssertionError("越界轨道应当返回 500")
        except urllib.error.HTTPError as exc:
            assert exc.code == 500

        # Molden
        code, molden = server.request(f"/api/jobs/{job_id}/molden", "POST")
        assert code == 200 and molden["ok"], molden
        code, orb3 = server.request(f"/api/jobs/{job_id}/orbitals")
        assert orb3["molden"], "Molden 应当被记录进产出文件"

        print(f"    轨道列表 / cube 导出下载 / 越界拒绝 / Molden 全部正常"
              f"（间隙 {orb['gap_ev']:.2f} eV）")
    finally:
        server.close()


# ------------------------------------------------------------------ 脚本模式

def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed, skipped = [], []
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
