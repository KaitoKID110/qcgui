"""任务结论判定（P0 第 1 项）的测试。

背景：本项目已有三次教训都属于同一类 —— **算错了却显示绿色成功**：
静默跳过的 NBO、消除成丁二烯却报 done 的扫描、跑满 50 圈没收敛的过渡态。
用户不会去翻日志，只会看那个绿点。

所以这里测两件事：
1. 结论本身的判定逻辑（纯函数）
2. **读取旧结果时的补算** —— 检查是后加的，之前存下的 result.json 没有结论，
   实测那个消除任务在历史库里仍然显示"结果可用"，必须能补上
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.model import (  # noqa: E402
    SEVERITY_BLOCKING,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    Issue,
    JobSpec,
    JobType,
    Molecule,
    Result,
)
from qcgui.model.result import describe_convergence_problem  # noqa: E402

WATER_XYZ = "3\nwater\nO 0 0 0\nH 0 0 1\nH 0 1 0\n"


def test_verdict_covers_every_severity():
    """四类情形各自的结论必须是它该有的那个。"""
    ok = Result(status="done")
    assert ok.verdict == "ok", ok.verdict
    assert "全部检查通过" in ok.verdict_reason

    minor = Result(status="done", issues=[Issue("analysis_skipped", SEVERITY_MINOR, "NBO 被跳过")])
    assert minor.verdict == "limited", minor.verdict

    major = Result(status="done", issues=[Issue("not_converged", SEVERITY_MAJOR, "没收敛")])
    assert major.verdict == "limited", major.verdict

    blocking = Result(status="done", issues=[Issue("connectivity_changed", SEVERITY_BLOCKING, "成键变了")])
    assert blocking.verdict == "unusable", blocking.verdict

    # 严重度高的必须压过低的一起决定结论
    mixed = Result(status="done", issues=[
        Issue("analysis_skipped", SEVERITY_MINOR, "NBO 被跳过"),
        Issue("connectivity_changed", SEVERITY_BLOCKING, "成键变了"),
    ])
    assert mixed.verdict == "unusable", mixed.verdict
    assert "成键变了" in mixed.verdict_reason, mixed.verdict_reason

    # 没跑完的一律不可用
    for status in ("failed", "cancelled", "unknown"):
        r = Result(status=status, message="进程退出码 2")
        assert r.verdict == "unusable", (status, r.verdict)

    print("    ok / limited / unusable 四类判定都正确")


def test_verdict_is_conservative_for_legacy_results():
    """老结果只有 warnings、没有结构化 issues 时，保守判成"有限制"而不是"可用"。"""
    legacy = Result(status="done", warnings=["某项分析被跳过"])
    assert legacy.verdict == "limited", legacy.verdict
    assert legacy.verdict_reason == "某项分析被跳过"
    print("    老结果（只有 warnings）保守判为 limited")


def test_verdict_serialises_with_the_result():
    """结论要跟着 result.to_dict() 一起出去 —— 界面不该自己再推一套判据。"""
    result = Result(status="done", issues=[Issue("not_converged", SEVERITY_MAJOR, "没收敛")])
    data = result.to_dict()
    for key in ("verdict", "verdict_label", "verdict_reason", "issues"):
        assert key in data, f"to_dict 少了 {key}"
    assert data["verdict"] == "limited"
    assert data["issues"][0]["code"] == "not_converged"
    assert data["issues"][0]["severity"] == SEVERITY_MAJOR
    # 能往返
    back = Result.from_dict(data)
    assert back.verdict == "limited", back.verdict
    print("    结论与结构化问题都能序列化并往返")


def test_convergence_markers_do_not_false_positive():
    """收敛判据必须是证据式的 —— 中间圈那句话绝不能当判据。"""
    converged = ("***********HURRAY***********\n"
                 "*** THE OPTIMIZATION HAS CONVERGED ***\n****ORCA TERMINATED NORMALLY****")
    assert describe_convergence_problem(converged) is None

    maxed = ("The optimization did not converge but reached the maximum\n"
             "number of optimization cycles.")
    assert "没有收敛" in describe_convergence_problem(maxed)

    # 每个中间圈都会打印这句（真实语料里 265 次），它不代表最终没收敛
    intermediate = ("The optimization has not yet converged - more geometry cycles are needed\n"
                    + converged)
    assert describe_convergence_problem(intermediate) is None

    assert describe_convergence_problem("") is None
    assert describe_convergence_problem("完全无关的输出") is not None   # 没有公告 → 报
    print("    收敛/未收敛/中间圈三种情形判定都正确")


# ------------------------------------------------------------------ 读取时补算

def _mol(symbols, coords, name="t"):
    body = "\n".join(f"{s} {c[0]:.6f} {c[1]:.6f} {c[2]:.6f}" for s, c in zip(symbols, coords))
    return Molecule.from_xyz(f"{len(symbols)}\n{name}\n{body}\n", name=name)


class _Server:
    """起一个隔离的任务库 + Flask 服务。"""

    def __init__(self):
        from qcgui.jobs import JobManager
        from qcgui.web.app import create_app
        from werkzeug.serving import make_server

        self.root = Path(tempfile.mkdtemp(prefix="qcgui_verdict_"))
        self.manager = JobManager(self.root / "jobs")
        self.app = create_app(self.manager)
        self.srv = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=60) as res:
                return res.status, json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, None


def test_reading_a_legacy_result_backfills_the_verdict():
    """**核心用例**：检查是后加的，旧 result.json 里没有结论 —— 读取时必须补上。

    实测：那个把 (Z)-2-丁烯-1-醇消成 1,3-丁二烯 + 水的任务，
    其 result.json 存于成键检查上线之前，在历史库里一直显示"结果可用"。
    """
    symbols = ["C", "C", "O", "H", "H", "H", "H"]
    before = [
        [0.0, 0.0, 0.0], [1.5, 0.0, 0.0], [2.9, 0.0, 0.0],
        [-0.6, 0.9, 0.0], [-0.6, -0.9, 0.0], [1.5, 0.9, 0.0], [3.86, 0.0, 0.0],
    ]
    # 消除后：C-O 断、氢转移
    after = [list(c) for c in before]
    after[2] = [4.2, 2.0, 0.0]
    after[6] = [4.9, 2.0, 0.0]
    after[3] = [4.2, 2.6, 0.0]

    server = _Server()
    try:
        spec = JobSpec(molecule=_mol(symbols, before), job_type=JobType.OPT,
                       method="HF", basis="STO-3G")
        job_id = "20200101-000000-legacyconn"
        server.manager.store.create(spec, job_id=job_id)
        # jobspec.json 是**引擎**生成输入时写的；读取时补算要靠它拿到原始几何，
        # 所以测试里必须自己写一份（store.create 只建目录和索引行）
        (server.manager.store.job_dir(job_id) / "jobspec.json").write_text(
            spec.canonical_json(), encoding="utf-8")

        # 造一个"旧格式"的结果：有几何、**没有 issues**（正是历史数据的形态）
        result = Result(status="done", job_type="opt", method="HF",
                        geometry={"symbols": symbols, "coords": after,
                                  "units": "angstrom"},
                        job_dir=str(server.manager.store.job_dir(job_id)))
        assert result.issues == [], "前提：旧结果没有结构化问题"
        result.save(server.manager.store.job_dir(job_id) / "result.json")

        code, payload = server.get(f"/api/jobs/{job_id}")
        assert code == 200, code
        res = payload["result"]
        assert res["verdict"] == "unusable", f"旧结果必须被补算成不可用：{res['verdict']}"
        assert any(i["code"] == "connectivity_changed" for i in res["issues"]), res["issues"]
        assert "成键" in res["verdict_reason"], res["verdict_reason"]
        print(f"    旧结果被补算：{res['verdict']} — {res['verdict_reason'][:40]}…")

        # 没变形的任务不能被误判
        good_id = "20200101-000001-legacygood"
        server.manager.store.create(spec, job_id=good_id)
        same = Result(status="done", job_type="opt", method="HF",
                      geometry={"symbols": symbols, "coords": before, "units": "angstrom"},
                      job_dir=str(server.manager.store.job_dir(good_id)))
        same.save(server.manager.store.job_dir(good_id) / "result.json")
        code, payload = server.get(f"/api/jobs/{good_id}")
        assert payload["result"]["verdict"] == "ok", payload["result"]["verdict"]
        print("    未变形的旧结果仍判为可用（无误报）")

        # 单点能任务不做这个检查（输出几何就是输入几何）
        energy_spec = JobSpec(molecule=_mol(symbols, before), job_type=JobType.ENERGY,
                              method="HF", basis="STO-3G")
        eid = "20200101-000002-legacyenergy"
        server.manager.store.create(energy_spec, job_id=eid)
        (server.manager.store.job_dir(eid) / "jobspec.json").write_text(
            energy_spec.canonical_json(), encoding="utf-8")
        e = Result(status="done", job_type="energy", method="HF",
                   geometry={"symbols": symbols, "coords": after, "units": "angstrom"},
                   job_dir=str(server.manager.store.job_dir(eid)))
        e.save(server.manager.store.job_dir(eid) / "result.json")
        code, payload = server.get(f"/api/jobs/{eid}")
        assert payload["result"]["verdict"] == "ok", payload["result"]["verdict"]
        print("    单点能任务不误报")
    finally:
        server.close()


def test_reading_a_legacy_result_backfills_non_convergence():
    """收敛检查同样要在读取时补 —— 它只读输出尾部，很便宜。"""
    from qcgui.model import JobSpec, JobType

    server = _Server()
    try:
        spec = JobSpec(molecule=_mol(["O", "H", "H"], [[0, 0, 0], [0, 0, 1], [0, 1, 0]]),
                       job_type=JobType.OPT, method="HF", basis="STO-3G")
        job_id = "20200101-000003-legacyconv"
        dir_path = server.manager.store.job_dir(job_id)
        server.manager.store.create(spec, job_id=job_id)
        (dir_path / "jobspec.json").write_text(spec.canonical_json(), encoding="utf-8")
        # 旧结果：done、没有 issues；输出里明确说了没收敛
        (dir_path / "w.inp").write_text("! HF STO-3G\n", encoding="ascii")
        (dir_path / "w.out").write_text(
            "The optimization did not converge but reached the maximum\n"
            "number of optimization cycles.\n"
            "****ORCA TERMINATED NORMALLY****\n", encoding="ascii")
        r = Result(status="done", job_type="opt", method="HF", job_dir=str(dir_path))
        r.save(dir_path / "result.json")

        code, payload = server.get(f"/api/jobs/{job_id}")
        assert code == 200, code
        res = payload["result"]
        assert res["verdict"] == "limited", res["verdict"]
        assert any(i["code"] == "not_converged" for i in res["issues"]), res["issues"]
        print(f"    旧结果补算出未收敛：{res['verdict']}")

        # 已收敛的不能误报
        good = "20200101-000004-legacyconvok"
        gdir = server.manager.store.job_dir(good)
        server.manager.store.create(spec, job_id=good)
        (gdir / "jobspec.json").write_text(spec.canonical_json(), encoding="utf-8")
        (gdir / "w.inp").write_text("! HF STO-3G\n", encoding="ascii")
        (gdir / "w.out").write_text(
            "***********HURRAY***********\n"
            "*** THE OPTIMIZATION HAS CONVERGED ***\n"
            "****ORCA TERMINATED NORMALLY****\n", encoding="ascii")
        rg = Result(status="done", job_type="opt", method="HF", job_dir=str(gdir))
        rg.save(gdir / "result.json")
        code, payload = server.get(f"/api/jobs/{good}")
        assert payload["result"]["verdict"] == "ok", payload["result"]["verdict"]
        print("    已收敛的旧结果仍判为可用（无误报）")
    finally:
        server.close()


# ------------------------------------------------------------------ 脚本模式

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
