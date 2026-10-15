"""P1 修复的回归测试。

本轮修的三项都属于"界面让你以为改了，其实没改"或者"界面骗你"：

* **F3** 「几何来源」模式下电荷/多重度被静默忽略 —— 走的是一条完全不同的
  代码路径（分子从上游结果几何重建），用户填的值只写在顶层字段里。
  对带电体系来说，这会让整个结果对应错误的自旋/电荷态。
* **F4** `/api/status` 失败时界面停在半初始化（下拉全空、提交必然失败）——
  这部分靠真浏览器用例覆盖，见 ``test_web_browser.py``。
* **F5** 「键长响应」模式的游标画在错误的轴上 —— 同上。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


class _Server:
    def __init__(self):
        from qcgui.jobs import JobManager
        from qcgui.web.app import create_app
        from werkzeug.serving import make_server

        self.root = Path(tempfile.mkdtemp(prefix="qcgui_p1_"))
        self.manager = JobManager(self.root / "jobs")
        self.app = create_app(self.manager)
        self.srv = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()

    def call(self, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method="POST" if data else "GET",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                return res.status, json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace") or "{}")


def _parent_with_geometry(server):
    """造一个"已完成、且有结果几何"的上游任务（模拟界面里的几何来源）。"""
    from qcgui.model import JobSpec, JobType, Molecule, Result

    molecule = Molecule.from_xyz("3\nwater\nO 0 0 0.1173\nH 0 0.7572 -0.4692\n"
                                 "H 0 -0.7572 -0.4692\n", name="water")
    spec = JobSpec(molecule=molecule, job_type=JobType.OPT, method="HF", basis="STO-3G")
    job_id = "20200101-000000-upparent"
    server.manager.store.create(spec, job_id=job_id)
    job_dir = server.manager.store.job_dir(job_id)
    (job_dir / "jobspec.json").write_text(spec.canonical_json(), encoding="utf-8")
    Result(status="done", job_type="opt", method="HF", charge=0, multiplicity=1,
           geometry={"symbols": list(molecule.symbols), "coords": [list(c) for c in molecule.coords],
                     "units": "angstrom"},
           job_dir=str(job_dir)).save(job_dir / "result.json")
    server.manager.store.update(job_id, status="done")
    return job_id, molecule


def _jobspec_of(server, job_id):
    path = server.manager.store.job_dir(job_id) / "jobspec.json"
    for _ in range(50):
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        time.sleep(0.1)
    return None


def test_geometry_lineage_honours_top_level_charge_and_multiplicity():
    """**核心用例（审计 F3）**：走「几何来源」时，顶层电荷/多重度必须生效。

    上游若是不带电的单重态，而用户这次要算阴离子二重态，
    以前会**静默沿用** 0/1 —— 算出来的是另一个电子态，还不报错。
    """
    server = _Server()
    try:
        parent_id, _ = _parent_with_geometry(server)

        cases = [
            # (说明, 顶层字段, 期望的 charge, 期望的 multiplicity)
            ("只改多重度", {"multiplicity": 3}, 0, 3),
            ("只改电荷", {"charge": -1}, -1, 1),
            ("两个都改", {"charge": -1, "multiplicity": 2}, -1, 2),
            ("都不给（沿用上游）", {}, 0, 1),
        ]
        for label, extra, want_charge, want_mult in cases:
            body = {"geometry_from_job": parent_id, "job_type": "energy",
                    "method": "HF", "basis": "STO-3G", "nprocs": 1, "maxcore_mb": 200}
            body.update(extra)
            code, payload = server.call("/api/jobs", body)
            assert code == 201, (label, code, payload)
            spec = _jobspec_of(server, payload["job"]["job_id"])
            assert spec is not None, f"{label}: jobspec 没落盘"
            molecule = spec["molecule"]
            assert molecule["charge"] == want_charge, \
                f"{label}: 电荷应当是 {want_charge}，实际 {molecule['charge']}"
            assert molecule["multiplicity"] == want_mult, \
                f"{label}: 多重度应当是 {want_mult}，实际 {molecule['multiplicity']}"
            print(f"    {label:18s} -> charge={molecule['charge']} mult={molecule['multiplicity']}")
    finally:
        server.close()


def test_charge_multiplicity_input_is_validated():
    """畸形输入要得到 400，而不是静默接受或 500。"""
    server = _Server()
    try:
        parent_id, _ = _parent_with_geometry(server)

        def submit(extra):
            body = {"geometry_from_job": parent_id, "job_type": "energy",
                    "method": "HF", "basis": "STO-3G", "nprocs": 1, "maxcore_mb": 200}
            body.update(extra)
            return server.call("/api/jobs", body)

        for label, extra in (("多重度 0", {"multiplicity": 0}),
                             ("多重度负数", {"multiplicity": -1}),
                             ("电荷是字符串", {"charge": "零"}),
                             ("电荷是对象", {"charge": {"a": 1}}),
                             ("电荷超范围", {"charge": 999})):
            code, payload = submit(extra)
            assert code == 400, f"{label}: 应当 400，实际 {code} {payload}"
            assert "电荷" in payload.get("error", "") or "多重度" in payload.get("error", ""), payload
            print(f"    {label:14s} -> 400 {payload['error'][:34]}")
    finally:
        server.close()


# ---------------------------------------------------------------- 脚本模式

def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, func in tests:
        try:
            print(f"[ RUN  ] {name}")
            func()
            print(f"[  OK  ] {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[ FAIL ] {name}: {exc}")
            failed += 1
    print(f"\n通过 {passed} / 失败 {failed} / 跳过 0")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_main())
