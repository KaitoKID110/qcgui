"""网页界面 API 测试。

用 werkzeug 的 ``make_server`` 在随机端口起一个真实服务，然后用 urllib 打接口 ——
测的是真 HTTP，不是直接调函数。

.. important::
   本模块不 import qcgui.engines（与界面进程同样的约束）。

    pytest tests/test_web_api.py -v
    python tests/test_web_api.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.jobs import JobManager  # noqa: E402
from qcgui.model import JobSpec, JobType, Molecule  # noqa: E402
from qcgui.web.app import create_app  # noqa: E402

WATER_HF_DEF2SVP = -75.960983978583

WATER_XYZ = """3
water
O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
"""


def _spec_for_store() -> JobSpec:
    """给 JobStore.create() 用的最小任务定义（不真跑）。

    列表接口的结论判定只读 result.json，不依赖 spec —— 用它是为了造出
    一条**真实的任务记录**（否则列表里查不到这个 job_id）。
    """
    return JobSpec(molecule=Molecule.from_xyz(WATER_XYZ),
                   job_type=JobType.ENERGY, method="HF", basis="STO-3G")


class _Skipped(Exception):
    pass


def _skip(reason: str):
    try:
        import pytest

        pytest.skip(reason)
    except ImportError:
        raise _Skipped(reason)


def _require_orca():
    from shutil import which

    if not (which("orca") or os.environ.get("QCGUI_ORCA_EXE")):
        _skip("本机没有 ORCA")


class Server:
    """测试用的真实 HTTP 服务。"""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="qcgui_web_"))
        self.manager = JobManager(self.root / "jobs")
        self.app = create_app(self.manager)
        from werkzeug.serving import make_server

        self.srv = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.port = self.srv.server_port
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.srv.shutdown()

    def get(self, path: str, raw: bool = False):
        with urllib.request.urlopen(self.base + path, timeout=30) as res:
            body = res.read()
            if raw:
                return res.status, body, dict(res.headers)
            return res.status, json.loads(body.decode("utf-8"))

    def post(self, path: str, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                return res.status, json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


# ------------------------------------------------------------------ 静态与状态

def test_index_and_static_are_served():
    server = Server()
    try:
        status, body, headers = server.get("/", raw=True)
        assert status == 200
        text = body.decode("utf-8")
        assert "qcgui" in text and "MoleculeViewer" in text or "viewer.js" in text
        assert "/static/app.css" in text

        for path, needle in (
            ("/static/app.css", "--accent"),
            ("/static/app.js", "MoleculeViewer"),
            ("/static/viewer.js", "class MoleculeViewer"),
        ):
            code, raw, _ = server.get(path, raw=True)
            assert code == 200, path
            assert needle.encode() in raw, path
    finally:
        server.close()
    print("    首页与三个静态资源均可访问")


def test_status_reports_engines_and_tools():
    server = Server()
    try:
        code, data = server.get("/api/status")
        assert code == 200
        assert data["engines"], "没有报告任何引擎"
        engine = data["engines"][0]
        assert engine["name"] == "orca"
        assert "energy" in engine["job_types"]
        # 外部工具三个都要有状态（哪怕不可用）
        assert set(data["tools"]) >= {"multiwfn", "avogadro", "obabel"}
        assert data["job_types"] and data["solvents"]
        assert data["units"]["hartree_to_kjmol"] > 2000
    finally:
        server.close()
    print("    引擎能力经 worker 子进程查询得到")


# ------------------------------------------------------------------ 结构解析

def test_structure_text_parses_xyz():
    """回归测试：charge 缺省时不能把 None 透传下去（曾经 500）。"""
    server = Server()
    try:
        code, data = server.post("/api/structure/text", {"text": WATER_XYZ})
        assert code == 200, data
        mol = data["molecule"]
        assert mol["symbols"] == ["O", "H", "H"]
        assert mol["n_atoms"] == 3
        assert mol["formula"] == "H2O"
        assert mol["charge"] == 0 and mol["multiplicity"] == 1
        assert abs(mol["coords"][0][2] - 0.1173) < 1e-9
    finally:
        server.close()
    print("    XYZ 文本解析正常（含 charge 缺省）")


def test_structure_text_rejects_garbage():
    server = Server()
    try:
        code, data = server.post("/api/structure/text", {"text": "这不是结构文件"})
        assert code == 400, (code, data)
        assert "error" in data
        code, data = server.post("/api/structure/text", {"text": "   "})
        assert code == 400
    finally:
        server.close()
    print("    非法输入返回 400 而非 500")


def test_structure_endpoints_and_examples():
    server = Server()
    try:
        code, data = server.get("/api/examples")
        assert code == 200
        names = [e["name"] for e in data["examples"]]
        assert "water.xyz" in names, names

        path = next(e["path"] for e in data["examples"] if e["name"] == "water.xyz")
        code, data = server.post("/api/structure/load", {"path": path})
        assert code == 200, data
        assert data["molecule"]["n_atoms"] == 3

        code, data = server.post("/api/structure/load", {"path": "Z:/nope.xyz"})
        assert code == 400
    finally:
        server.close()
    print("    示例列表与按路径载入正常")


def test_geometry_parameters_endpoint():
    """几何参数表的数据接口：前端把哪套几何发过来，这边就返回哪套的参数。

    界面上的「几何参数」区（键长/键角/二面角）就是打这个接口 ——
    它必须只做纯几何计算（不碰引擎、不读任务目录）。
    """
    server = Server()
    try:
        code, data = server.post("/api/geometry/parameters", {
            "symbols": ["O", "H", "H"],
            "coords": [[0.0, 0.0, 0.1173], [0.0, 0.7572, -0.4692], [0.0, -0.7572, -0.4692]],
        })
        assert code == 200, data
        assert len(data["bonds"]) == 2, data
        assert len(data["angles"]) == 1, data
        assert data["dihedrals"] == [], data
        assert abs(data["angles"][0]["value"] - 104.5) < 1.0, data["angles"]
        assert data["bonds"][0]["unit"] == "Å" and data["angles"][0]["unit"] == "°"
        assert data["bonds"][0]["atoms"] == [0, 1], data["bonds"]

        # 原子数对不上 / 字段类型不对 / 坐标不是三个数：一律 400，绝不 500
        code, data = server.post("/api/geometry/parameters",
                                 {"symbols": ["O", "H"], "coords": [[0, 0, 0]]})
        assert code == 400, (code, data)
        assert "不一致" in data["error"], data
        code, data = server.post("/api/geometry/parameters", {"symbols": "O", "coords": []})
        assert code == 400, (code, data)
        code, data = server.post("/api/geometry/parameters",
                                 {"symbols": ["C"], "coords": [["x", "y", "z"]]})
        assert code == 400, (code, data)

        # 空输入不算错误：界面"还没载入结构"时不该报错
        code, data = server.post("/api/geometry/parameters", {"symbols": [], "coords": []})
        assert code == 200 and data["bonds"] == [], (code, data)
    finally:
        server.close()
    print("    几何参数接口正常（水 2 键长 + 1 键角；坏输入 400）")


# ------------------------------------------------------------------ 任务流程

def test_submit_job_and_fetch_result():
    _require_orca()
    server = Server()
    try:
        code, data = server.post("/api/structure/text", {"text": WATER_XYZ})
        molecule = data["molecule"]

        code, data = server.post("/api/jobs", {
            "molecule": molecule,
            "job_type": "energy",
            "method": "HF",
            "basis": "def2-SVP",
            "nprocs": 2,
        })
        assert code == 201, data
        job_id = data["job"]["job_id"]
        assert data["job"]["status"] in ("queued", "running")

        # 等它跑完
        deadline = time.time() + 180
        result = None
        while time.time() < deadline:
            code, data = server.get(f"/api/jobs/{job_id}")
            assert code == 200
            if data["job"]["status"] not in ("queued", "running"):
                result = data["result"]
                break
            time.sleep(0.5)

        assert result is not None, "任务超时未结束"
        assert data["job"]["status"] == "done", data["job"]["message"]
        assert abs(result["final_energy"] - WATER_HF_DEF2SVP) < 1e-6
        assert result["geometry"]["units"] == "angstrom"
        assert len(result["loewdin_charges"]) == 3
        assert result["n_electrons"] == 10

        # 任务列表里能看到
        code, data = server.get("/api/jobs")
        assert code == 200
        assert any(j["job_id"] == job_id for j in data["jobs"])

        # 产出文件可按角色取回
        code, raw, headers = server.get(f"/api/jobs/{job_id}/file/input", raw=True)
        assert code == 200
        assert b"! HF def2-SVP" in raw
        assert not raw.startswith(b"\xef\xbb\xbf"), "输入文件不能带 BOM"

        code, raw, _ = server.get(f"/api/jobs/{job_id}/file/output", raw=True)
        assert code == 200 and b"ORCA TERMINATED NORMALLY" in raw

        # 取不存在的角色 → 404
        try:
            server.get(f"/api/jobs/{job_id}/file/nonexistent")
            raise AssertionError("应当 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.close()
    print(f"    经 API 跑通 HF/def2-SVP 水单点，能量 {WATER_HF_DEF2SVP:.10f} Ha")


def test_cancel_endpoint():
    server = Server()
    try:
        code, data = server.post("/api/jobs/does-not-exist/cancel", {})
        assert code == 200
        assert data["ok"] is False, "不存在的任务不应报告取消成功"
    finally:
        server.close()
    print("    取消接口对未知任务返回 ok=False")


# ------------------------------------------------------------------ 其它接口

def _read_sse_event(res, max_lines: int = 500):
    """从 SSE 流里读出一个 data 事件（跳过空行与注释心跳）。

    读超时返回 None，让调用方自己决定要不要继续等。
    """
    for _ in range(max_lines):
        try:
            raw = res.readline()
        except (TimeoutError, OSError):
            return None
        if not raw:
            return None
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except ValueError:
                return None
    return None


def test_events_stream_headers():
    server = Server()
    try:
        req = urllib.request.Request(server.base + "/api/events")
        with urllib.request.urlopen(req, timeout=15) as res:
            assert res.status == 200
            ctype = res.headers.get("Content-Type", "")
            assert "text/event-stream" in ctype, ctype
            first = _read_sse_event(res)
            assert first and first.get("event") == "hello", first
    finally:
        server.close()
    print("    SSE 通道返回 text/event-stream，并立即推送 hello")


def test_events_broadcast_job_log():
    """任务事件必须经 SSE 广播出来（前端日志就靠它）。"""
    _require_orca()
    server = Server()
    try:
        req = urllib.request.Request(server.base + "/api/events")
        with urllib.request.urlopen(req, timeout=20) as res:
            assert _read_sse_event(res) is not None  # hello

            code, data = server.post("/api/jobs", {
                "molecule": {"symbols": ["O", "H", "H"],
                             "coords": [[0, 0, 0.1173], [0, 0.7572, -0.4692], [0, -0.7572, -0.4692]],
                             "charge": 0, "multiplicity": 1, "name": "water"},
                "job_type": "energy", "method": "HF", "basis": "def2-SVP", "nprocs": 2,
            })
            assert code == 201, data
            job_id = data["job"]["job_id"]

            saw_running = saw_log = saw_finished = False
            deadline = time.time() + 90
            while time.time() < deadline and not saw_finished:
                event = _read_sse_event(res)
                if event is None:
                    break
                if event.get("job_id") != job_id:
                    continue
                kind = event.get("event")
                if kind == "running":
                    saw_running = True
                elif kind == "log":
                    saw_log = True
                elif kind == "finished":
                    saw_finished = True

            assert saw_running, "没有收到 running 事件"
            assert saw_log, "没有收到 log 事件"
            assert saw_finished, "没有收到 finished 事件"
    finally:
        server.close()
    print("    running / log / finished 三类事件均经 SSE 送达")


def _water_at(r: float, name: str) -> dict:
    """按指定 O–H 键长构造水分子（供批量测试）。"""
    import math

    half = math.radians(104.5 / 2)
    dx, dy = r * math.sin(half), r * math.cos(half)
    return {
        "name": name, "charge": 0, "multiplicity": 1,
        "symbols": ["O", "H", "H"],
        "coords": [[0.0, 0.0, 0.0], [dx, dy, 0.0], [-dx, dy, 0.0]],
    }


def test_batch_relative_energies_over_api():
    """网页版批量中间体：三个键长的水，相对能量应与 CLI 路径一致。"""
    _require_orca()
    server = Server()
    try:
        code, data = server.post("/api/batch", {
            "molecules": [
                _water_at(0.90, "w090"),
                _water_at(0.96, "w096"),
                _water_at(1.10, "w110"),
            ],
            "job_type": "energy", "method": "HF", "basis": "def2-SVP",
            "nprocs": 2, "quantity": "energy",
        })
        assert code == 202, data
        batch_id = data["batch_id"]

        deadline = time.time() + 300
        status = None
        while time.time() < deadline:
            code, status = server.get(f"/api/batch/{batch_id}")
            assert code == 200
            if status["status"] != "running":
                break
            time.sleep(1.0)

        assert status and status["status"] == "done", status
        result = status["result"]
        assert result["reference_name"] == "w096", [
            (i["name"], i["relative_kj"]) for i in result["items"]
        ]
        by_name = {i["name"]: i for i in result["items"]}
        assert abs(by_name["w096"]["relative_kj"]) < 1e-9
        assert 11.0 < by_name["w090"]["relative_kj"] < 13.5
        assert 95.0 < by_name["w110"]["relative_kj"] < 105.0
        assert result["saved"]["csv"]
        print("    " + ", ".join(
            f"{i['name']}={i['relative_kj']:.2f}" for i in sorted(
                result["items"], key=lambda x: x["relative_kj"])
        ))
    finally:
        server.close()


def test_external_rejects_unknown_tool():
    server = Server()
    try:
        code, data = server.post("/api/external", {"tool": "nope", "path": "x"})
        assert code == 400
        assert "未知工具" in data["message"]

        code, data = server.post("/api/external", {"tool": "multiwfn"})
        assert code == 400
        assert "缺少文件路径" in data["message"]
    finally:
        server.close()
    print("    外部工具接口对非法请求返回 400")


def test_config_roundtrip():
    server = Server()
    try:
        code, data = server.get("/api/config")
        assert code == 200
        assert "web_port" in data

        original = data.get("web_port", 8760)
        code, data = server.post("/api/config", {"web_port": 8899})
        assert code == 200 and data["config"]["web_port"] == 8899

        code, data = server.get("/api/config")
        assert data["web_port"] == 8899
        # 还原，避免影响真实配置
        server.post("/api/config", {"web_port": original})
    finally:
        server.close()
    print("    配置读写往返正常")


# ------------------------------------------------------------------ 配方

def test_recipes_endpoint_and_favorite():
    server = Server()
    try:
        code, data = server.get("/api/recipes")
        assert code == 200
        recipes = data["recipes"]
        assert len(recipes) >= 15
        by_id = {r["id"]: r for r in recipes}
        assert "optfreq_b3lyp" in by_id
        recipe = by_id["optfreq_b3lyp"]
        assert recipe["job_type"] == "opt_freq"
        assert recipe["orca_keyword_line"].startswith("! ")
        assert recipe["keywords"], "配方应带关键词"

        # 收藏切换 + 排序
        code, res = server.post("/api/recipes/debug_sp_hf_sto3g/favorite",
                                {"favorite": False})
        assert code == 200 and res["favorite"] is False
        code, res = server.post("/api/recipes/debug_sp_hf_sto3g/favorite",
                                {"favorite": True})
        assert code == 200 and res["favorite"] is True

        code, data = server.get("/api/recipes?q=B3LYP")
        assert code == 200
        assert all("b3lyp" in r["name"].lower() or "b3lyp" in r["method"].lower()
                   for r in data["recipes"])

        code, res = server.post("/api/recipes/does_not_exist/favorite", {})
        assert code == 404
    finally:
        server.close()
    print("    配方列表 / 搜索 / 收藏切换正常")


def test_job_from_recipe_end_to_end():
    """用配方提交任务：配方负责 job_type 与关键词，界面只覆盖算力参数。"""
    _require_orca()
    server = Server()
    try:
        code, data = server.post("/api/structure/text", {"text": WATER_XYZ})
        molecule = data["molecule"]

        code, data = server.post("/api/jobs", {
            "recipe_id": "debug_sp_hf_sto3g",
            "molecule": molecule,
            "nprocs": 2,
        })
        assert code == 201, data
        assert data["recipe_id"] == "debug_sp_hf_sto3g"
        job_id = data["job"]["job_id"]

        deadline = time.time() + 180
        result = None
        while time.time() < deadline:
            code, data = server.get(f"/api/jobs/{job_id}")
            if data["job"]["status"] not in ("queued", "running"):
                result = data["result"]
                break
            time.sleep(0.5)
        assert result is not None and data["job"]["status"] == "done", data["job"]

        # 输入文件里必须带出配方的方法与基组
        code, raw, _ = server.get(f"/api/jobs/{job_id}/file/input", raw=True)
        text = raw.decode("utf-8")
        assert "! HF STO-3G" in text, text

        # 派生量必须出现在 JSON 里（曾经漏掉，界面永远显示 0 虚频）
        assert "n_imaginary_frequencies" in result, result.keys()
        assert result["n_imaginary_frequencies"] == 0

        code, data = server.post("/api/jobs", {"recipe_id": "不存在", "molecule": molecule})
        assert code == 400
    finally:
        server.close()
    print("    配方提交 → 运行 → 结果全通，派生字段已序列化")


def test_geometry_lineage_from_parent_job():
    """几何血缘：不传分子，直接用上游任务的结果几何起算。"""
    _require_orca()
    server = Server()
    try:
        code, data = server.post("/api/jobs", {
            "molecule": {"symbols": ["O", "H", "H"],
                         "coords": [[0, 0, 0.1173], [0, 0.7572, -0.4692], [0, -0.7572, -0.4692]],
                         "charge": 0, "multiplicity": 1, "name": "water"},
            "recipe_id": "debug_sp_hf_sto3g", "nprocs": 2,
        })
        parent = data["job"]["job_id"]

        deadline = time.time() + 180
        while time.time() < deadline:
            code, data = server.get(f"/api/jobs/{parent}")
            if data["job"]["status"] not in ("queued", "running"):
                break
            time.sleep(0.5)
        assert data["job"]["status"] == "done", data["job"]

        # 只给上游任务 id，不给 molecule
        code, data = server.post("/api/jobs", {
            "recipe_id": "debug_freq_hf_sto3g",
            "geometry_from_job": parent,
            "nprocs": 2,
        })
        assert code == 201, data
        assert data["parent_job"] == parent
        child = data["job"]["job_id"]
        # 派生任务的名字应当带出上游痕迹
        assert "energy" in data["job"]["molecule"] or "water" in data["job"]["molecule"]

        deadline = time.time() + 240
        result = None
        while time.time() < deadline:
            code, data = server.get(f"/api/jobs/{child}")
            if data["job"]["status"] not in ("queued", "running"):
                result = data["result"]
                break
            time.sleep(0.5)
        assert result is not None and data["job"]["status"] == "done", data["job"]
        assert result["frequencies"], "频率任务应当有频率"
        assert len(result["geometry"]["symbols"]) == 3

        # 不存在的上游任务要报错，而不是静默用别的几何
        code, data = server.post("/api/jobs", {
            "recipe_id": "debug_sp_hf_sto3g", "geometry_from_job": "nope",
        })
        assert code == 400 and "上游任务" in data["error"]
    finally:
        server.close()
    print("    几何血缘：子任务直接沿用父任务的几何，非法父任务被拒")


def test_report_csv_export():
    server = Server()
    try:
        code, raw, headers = server.get("/api/report.csv", raw=True)
        assert code == 200
        assert "text/csv" in headers.get("Content-Type", "")
        assert "attachment" in headers.get("Content-Disposition", "")
        text = raw.decode("utf-8-sig")
        header = text.splitlines()[0]
        for column in ("job_id", "分子", "任务", "方法", "状态", "能量(Ha)",
                       "自由能(Ha)", "上游任务", "配方"):
            assert column in header, f"CSV 缺少列 {column}"
    finally:
        server.close()
    print("    报告 CSV 含热力学与血缘列")


# ------------------------------------------------------------------ 畸形输入

def test_malformed_job_id_returns_400_not_500():
    """job_id 传成对象时应当得到 400，而不是一路穿到 Path / dict 抛 500。

    实测踩过：PowerShell 的 ``Get-Content`` 结果带 PSPath/PSDrive 等
    NoteProperty，打印时看着是普通字符串，但 ``ConvertTo-Json`` 会把它
    序列化成对象，于是服务端收到 dict。
    """
    server = Server()
    try:
        code, data = server.post("/api/external", {
            "tool": "multiwfn",
            "job_id": {"value": "20260101-000000-abc", "PSPath": "x"},
            "role": "molden",
        })
        assert code == 400, (code, data)
        assert "字符串" in data["message"], data
        print("    畸形 job_id → 400:", data["message"])

        code, data = server.post("/api/jobs", {
            "molecule": {"symbols": ["O", "H", "H"],
                         "coords": [[0, 0, 0], [0, 0, 1], [0, 1, 0]]},
            "geometry_from_job": {"value": "x"},
        })
        assert code == 400 and "字符串" in data["error"], (code, data)

        code, data = server.post("/api/jobs", {
            "molecule": {"symbols": ["O", "H", "H"],
                         "coords": [[0, 0, 0], [0, 0, 1], [0, 1, 0]]},
            "recipe_id": {"value": "x"},
        })
        assert code == 400 and "字符串" in data["error"], (code, data)
        print("    geometry_from_job / recipe_id 同样按 400 处理")
    finally:
        server.close()


# ------------------------------------------------------------------ 外部工具选文件

def test_external_rejects_files_the_tool_cannot_read():
    """Multiwfn 读不了 ORCA 的 .gbw、Avogadro 读不了 .inp。

    这正是"点了按钮没反应"的根因：给错类型时程序不报错，只是打不开。
    所以即使请求里点名要这些文件，服务端也必须换成读得了的。
    """
    from qcgui.web.app import _readable_by

    assert _readable_by("multiwfn", "job/x.molden.input") is True
    assert _readable_by("multiwfn", "job/x.molden") is True
    assert _readable_by("multiwfn", "job/x.gbw") is False
    assert _readable_by("multiwfn", "job/x.inp") is False

    assert _readable_by("avogadro", "job/x.xyz") is True
    assert _readable_by("avogadro", "job/x.cube") is True
    assert _readable_by("avogadro", "job/x.molden.input") is True
    assert _readable_by("avogadro", "job/x.inp") is False
    assert _readable_by("avogadro", "job/x.gbw") is False

    # 没登记的工具不限制
    assert _readable_by("explorer", "job/x.gbw") is True
    print("    按工具能读的后缀正确拦截（.gbw / .inp 被挡）")


def test_external_swaps_unreadable_role_for_a_readable_one():
    """端到端：请求里给 role=wavefunction（.gbw）时应改给 Molden。"""
    server = Server()
    try:
        # 造一个带各类产出的任务目录
        job_id = "20260101-000000-test0001"
        job_dir = server.manager.store.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        for name in ("x.gbw", "x.inp", "x.molden.input"):
            (job_dir / name).write_text("placeholder", encoding="ascii")

        from qcgui.model import Result

        result = Result(status="done")
        result.files = {
            "wavefunction": str(job_dir / "x.gbw"),
            "input": str(job_dir / "x.inp"),
            "molden": str(job_dir / "x.molden.input"),
        }
        server.manager.store.save_result(job_id, result)

        from qcgui.web.app import _external_file

        loaded = server.manager.store.load_result(job_id)
        picked = _external_file("multiwfn", job_id, loaded, server.manager)
        assert picked.endswith(".molden.input"), picked

        picked_av = _external_file("avogadro", job_id, loaded, server.manager)
        assert picked_av.endswith(".molden.input"), picked_av
        print("    Multiwfn/Avogadro 都拿到了自己能读的 Molden")
    finally:
        server.close()


# ---------------------------------------------------- 任务列表的实时结论（P0）

def test_job_list_recomputes_verdict_instead_of_trusting_the_stored_one():
    """列表里的结论必须**实时重算**，不能照着写库那一刻的 message 显示。

    背景：``store.save_result()`` 只在**保存结果时**把 ``⛔`` / ``⚠`` 前缀写进
    message，而几何/电荷守卫是后来才加的 —— 所以历史任务卡片永远停在旧结论上。
    实测真实任务库：库里 95 个任务标着 ok，实时判定其实是 10 个 limited +
    2 个 unusable；那两个 unusable 就是原子相距 0.300 / 0.150 Å 的坏几何。

    这条钉住的是：**保存结果时不带前缀（模拟历史任务），列表接口也要给出正确结论**。
    """
    from qcgui.model import Result

    server = Server()
    try:
        # 造一个坏几何结果，但**故意**把它存成"没有前缀"的样子 ——
        # 直接写 result.json，绕过 save_result 的前缀逻辑，等价于历史任务
        job_id = "20260101-000000-badgegeombad"
        job_dir = server.manager.store.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)

        result = Result(status="done", job_type="energy", method="HF/STO-3G")
        result.geometry = {"symbols": ["C", "C"],
                           "coords": [[0.0, 0.0, 0.0], [0.300, 0.0, 0.0]],
                           "units": "angstrom"}
        result.charge = 0
        (job_dir / "result.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False), encoding="utf-8")
        server.manager.store.create(_spec_for_store(), job_id=job_id)
        server.manager.store.update(job_id, status="done", message="")  # 无前缀

        status, body = server.get("/api/jobs?limit=50")
        assert status == 200, body
        assert "total" in body, "列表应回报总数，界面才能如实说明截断"
        row = next((j for j in body["jobs"] if j["job_id"] == job_id), None)
        assert row is not None, "新建的任务没出现在列表里"
        assert row["verdict"] == "unusable", row
        assert row["message"].startswith("⛔"), row["message"]
        assert "0.3" in row["message"], row["message"]
        print("    bad-geometry 任务在列表里被判为 unusable，并带上 ⛔ 前缀")

        # 正常几何不得误报
        ok_id = "20260101-000001-badgegeomok"
        ok_dir = server.manager.store.job_dir(ok_id)
        ok_dir.mkdir(parents=True, exist_ok=True)
        ok = Result(status="done", job_type="energy", method="HF/STO-3G")
        ok.geometry = {"symbols": ["O", "H", "H"],
                       "coords": [[0.0, 0.0, 0.1173], [0.0, 0.7572, -0.4692],
                                  [0.0, -0.7572, -0.4692]],
                       "units": "angstrom"}
        ok.charge = 0
        (ok_dir / "result.json").write_text(
            json.dumps(ok.to_dict(), ensure_ascii=False), encoding="utf-8")
        server.manager.store.create(_spec_for_store(), job_id=ok_id)
        server.manager.store.update(ok_id, status="done", message="")

        status, body = server.get("/api/jobs?limit=50")
        row = next(j for j in body["jobs"] if j["job_id"] == ok_id)
        assert row["verdict"] == "ok", row
        assert not row["message"].startswith(("⛔", "⚠")), row["message"]
        print("    正常几何仍是 ok、无徽章（零误报）")
    finally:
        server.close()


def test_job_list_verdict_is_cached_but_invalidates_on_change():
    """判定要缓存（列表是轮询的），但结果变了必须失效。

    缓存用 result.json 的 mtime+size 当键，所以重写结果后必须重算 ——
    否则用户重跑任务修好了几何，列表里那个红徽章会一直挂着。
    """
    from qcgui.model import Result

    server = Server()
    try:
        job_id = "20260101-000002-badgecache"
        job_dir = server.manager.store.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)

        def write_result(gap):
            r = Result(status="done", job_type="energy", method="HF/STO-3G")
            r.geometry = {"symbols": ["C", "C"],
                          "coords": [[0.0, 0.0, 0.0], [gap, 0.0, 0.0]],
                          "units": "angstrom"}
            r.charge = 0
            (job_dir / "result.json").write_text(
                json.dumps(r.to_dict(), ensure_ascii=False), encoding="utf-8")

        server.manager.store.create(_spec_for_store(), job_id=job_id)
        server.manager.store.update(job_id, status="done", message="")

        write_result(0.300)          # 坏几何
        _, body = server.get("/api/jobs?limit=50")
        assert next(j for j in body["jobs"] if j["job_id"] == job_id)["verdict"] == "unusable"

        time.sleep(0.01)             # 保证 mtime 变（有些文件系统精度只有 10ms）
        write_result(1.500)          # 修好了
        _, body = server.get("/api/jobs?limit=50")
        row = next(j for j in body["jobs"] if j["job_id"] == job_id)
        assert row["verdict"] == "ok", f"结果已改好，列表还挂着旧结论：{row}"
        print("    结果改写后缓存正确失效（坏几何 → ok）")
    finally:
        server.close()


# ------------------------------------------------------------------ 单实例

def test_pick_port_does_not_steal_an_occupied_port():
    """端口探测不能用 SO_REUSEADDR。

    Windows 上 SO_REUSEADDR 的语义是"允许抢占已被占用的端口"，用它做探测
    会在端口已被监听时依然 bind 成功 —— 曾因此出现两个实例同时监听 8760。
    """
    import socket

    from qcgui.web.app import _pick_port

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    occupied = holder.getsockname()[1]
    try:
        chosen = _pick_port(occupied, "127.0.0.1", attempts=5)
        assert chosen != occupied, f"抢占了已占用的端口 {occupied}"
        assert chosen == occupied + 1, chosen
        print(f"    端口 {occupied} 已占用 → 正确避开，选了 {chosen}")
    finally:
        holder.close()


def test_second_instance_is_detected_and_refused():
    """已有 qcgui 在跑时必须能识别出来，从而不再起第二个实例。"""
    from qcgui.web.app import _probe_existing

    server = Server()
    port = server.port
    try:
        assert _probe_existing("127.0.0.1", port) is True, "没认出已在运行的实例"
        print(f"    正确识别出 {port} 上已有实例")
    finally:
        server.close()

    # 关掉之后就不该再认出来了
    assert _probe_existing("127.0.0.1", port, timeout=0.5) is False
    print("    实例关闭后不再误判")


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
