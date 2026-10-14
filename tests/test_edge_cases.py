"""边界情况与异常路径排查。

这里不测"功能对不对"，而是专门撞每个接口的**坏输入**：缺字段、类型不对、
不存在的东西、极端数值。判定标准是：

* 绝不出现 **500**（未捕获异常）
* 拒绝时给出可读的 **4xx**，而不是静默成功或崩掉

    pytest tests/test_edge_cases.py -v
    python tests/test_edge_cases.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.jobs import JobManager  # noqa: E402
from qcgui.web.app import create_app  # noqa: E402

WATER_XYZ = """3
water
O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
"""

GHOST = "20200101-000000-doesnotexist"


class _Skipped(Exception):
    pass


def _skip(reason: str):
    try:
        import pytest

        pytest.skip(reason)
    except ImportError:
        raise _Skipped(reason)


class Server:
    """真实 HTTP 服务。"""

    def __init__(self) -> None:
        from werkzeug.serving import make_server

        self.root = Path(tempfile.mkdtemp(prefix="qcgui_edge_"))
        self.manager = JobManager(self.root / "jobs")
        self.app = create_app(self.manager)
        self.srv = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.port = self.srv.server_port
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()

    def call(self, path: str, method: str = "GET", payload=None,
             raw_body: bytes | None = None):
        if raw_body is None and payload is not None:
            raw_body = json.dumps(payload).encode("utf-8")
        # 路径里可能有中文（如配方 id），urllib 要求 URL 是 ASCII
        safe = urllib.parse.quote(path, safe="/?&=:%")
        req = urllib.request.Request(
            self.base + safe, data=raw_body, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                body = res.read().decode("utf-8", "replace")
                try:
                    return res.status, json.loads(body)
                except ValueError:
                    return res.status, body
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(body)
            except ValueError:
                return exc.code, body


def _no_500(results) -> None:
    bad = [(n, c, d) for n, c, d in results if c >= 500]
    assert not bad, "出现 500：\n" + "\n".join(
        f"  {n} -> {c} {str(d)[:200]}" for n, c, d in bad
    )


def _all_client_errors(results, what: str) -> None:
    _no_500(results)
    for name, code, data in results:
        assert 400 <= code < 500, f"{what} / {name}: 应为 4xx，实际 {code} {data}"


# ------------------------------------------------------------------ 结构接口

def test_structure_endpoints_reject_bad_input():
    server = Server()
    try:
        cases = [
            ("load 缺 path", "/api/structure/load", {"x": 1}),
            ("load 不存在的文件", "/api/structure/load", {"path": "Z:/nope/none.xyz"}),
            ("load 指向目录", "/api/structure/load", {"path": str(server.root)}),
            ("load path 是数字", "/api/structure/load", {"path": 123}),
            ("load path 是对象", "/api/structure/load", {"path": {"a": 1}}),
            ("text 是空的", "/api/structure/text", {"text": ""}),
            ("text 全是空白", "/api/structure/text", {"text": "   \n\t "}),
            ("text 是垃圾", "/api/structure/text", {"text": "这不是结构文件\n随便写点什么"}),
            ("text 是数字", "/api/structure/text", {"text": 12345}),
            ("smiles 是空的", "/api/structure/smiles", {"smiles": ""}),
            ("smiles 是垃圾", "/api/structure/smiles", {"smiles": "!!!not-a-smiles!!!"}),
            ("smiles 是数字", "/api/structure/smiles", {"smiles": 42}),
        ]
        results = [(n, *server.call(p, "POST", q)) for n, p, q in cases]
        _all_client_errors(results, "坏结构输入")
        print(f"    {len(cases)} 种坏结构输入都得到 4xx，无 500")
    finally:
        server.close()


def test_unicode_molecule_name_is_handled():
    """分子名含非 ASCII 时，接口不能崩。

    ORCA 输入必须是纯 ASCII（否则报 ``non-ASCII character(s) found`` 退出码 50），
    所以这里只要求"有明确回应"，不接受 500。
    """
    server = Server()
    try:
        code, data = server.call(
            "/api/structure/text", "POST", {"text": WATER_XYZ, "name": "水分子 α-β"}
        )
        assert code == 200, (code, data)
        assert data["molecule"]["n_atoms"] == 3, data

        code2, data2 = server.call("/api/jobs", "POST", {
            "molecule": data["molecule"], "method": "HF", "basis": "STO-3G",
        })
        assert code2 < 500, (code2, data2)
        print(f"    非 ASCII 分子名：载入 {code}，提交 {code2}")
    finally:
        server.close()


# ------------------------------------------------------------------ 任务接口

def test_job_endpoints_reject_bad_input():
    server = Server()
    try:
        mol = {"symbols": ["O", "H", "H"],
               "coords": [[0, 0, 0], [0, 0, 1], [0, 1, 0]],
               "charge": 0, "multiplicity": 1, "name": "w"}
        cases = [
            ("提交缺 molecule", {}),
            ("molecule 是空对象", {"molecule": {}}),
            ("molecule 是字符串", {"molecule": "水"}),
            ("job_type 未知", {"molecule": mol, "job_type": "不存在的任务"}),
            ("job_type 是数字", {"molecule": mol, "job_type": 7}),
            ("nprocs 为负", {"molecule": mol, "nprocs": -4}),
            ("nprocs 为 0", {"molecule": mol, "nprocs": 0}),
            ("nprocs 大到离谱", {"molecule": mol, "nprocs": 99999}),
            ("nprocs 是字符串", {"molecule": mol, "nprocs": "四"}),
            ("maxcore 为负", {"molecule": mol, "maxcore_mb": -100}),
            ("extra_blocks 是字符串", {"molecule": mol, "extra_blocks": "x"}),
            ("extra_blocks 元素不是二元组", {"molecule": mol, "extra_blocks": [[1, 2, 3]]}),
            ("配方不存在", {"molecule": mol, "recipe_id": "无此配方"}),
            ("几何血缘指向不存在的任务", {"molecule": mol, "geometry_from_job": GHOST}),
            ("电荷是字符串", {"molecule": mol, "charge": "零"}),
            ("坐标含非数字", {"molecule": {"symbols": ["O"], "coords": [["a", "b", "c"]]}}),
            ("symbols 与 coords 长度不符", {"molecule": {"symbols": ["O", "H"], "coords": [[0, 0, 0]]}}),
        ]
        results = [(n, *server.call("/api/jobs", "POST", q)) for n, q in cases]
        _no_500(results)
        # 前 15 条应当被明确拒绝；最后两条由解析层自行处理
        for name, code, data in results[:15]:
            assert 400 <= code < 500, f"{name}: 应为 4xx，实际 {code} {data}"
        print(f"    {len(cases)} 种坏提交参数无 500，其中 15 种被明确拒绝")
    finally:
        server.close()


def test_missing_job_ids_return_4xx_not_500():
    server = Server()
    try:
        cases = [
            ("查任务", f"/api/jobs/{GHOST}", "GET"),
            ("取文件", f"/api/jobs/{GHOST}/file/input", "GET"),
            ("IRC 数据", f"/api/jobs/{GHOST}/irc", "GET"),
            ("轨道列表", f"/api/jobs/{GHOST}/orbitals", "GET"),
            ("导 cube", f"/api/jobs/{GHOST}/orbitals/0/cube", "POST"),
            ("转 Molden", f"/api/jobs/{GHOST}/molden", "POST"),
            ("取批次", f"/api/batch/{GHOST}", "GET"),
            ("取消批次", f"/api/batch/{GHOST}/cancel", "POST"),
        ]
        results = [(n, *server.call(p, m, {} if m == "POST" else None)) for n, p, m in cases]
        _all_client_errors(results, "不存在的任务")

        # 取消接口是幂等的：对未知任务返回 200 {"ok": false}，这是有意设计
        code, data = server.call(f"/api/jobs/{GHOST}/cancel", "POST", {})
        assert code == 200 and data.get("ok") is False, (code, data)
        print(f"    {len(cases)} 个针对不存在任务的接口都返回 4xx；取消接口按设计返回 ok=false")
    finally:
        server.close()


def test_file_role_cannot_escape_the_job_directory():
    """``role`` 只是 result.files 的键，不该能拿来读任务目录之外的文件。"""
    server = Server()
    try:
        from qcgui.model import JobSpec, JobType, Molecule, Result

        job_id = "20200101-000000-travtest"
        spec = JobSpec(
            molecule=Molecule.from_xyz(WATER_XYZ),
            job_type=JobType.ENERGY, method="HF", basis="STO-3G",
        )
        server.manager.store.create(spec, job_id=job_id)
        job_dir = server.manager.store.job_dir(job_id)
        (job_dir / "x.inp").write_text("placeholder", encoding="ascii")

        result = Result(status="done")
        result.files = {"input": str(job_dir / "x.inp")}
        server.manager.store.save_result(job_id, result)

        # 正常 role 应当能拿到
        code, data = server.call(f"/api/jobs/{job_id}/file/input")
        assert code == 200, f"正常 role 取不到文件: {code} {data}"

        # 这些都不该成功
        for role in ("jobspec", "..", "jobspec.json", "x", "%2e%2e%2fjobspec.json"):
            code, data = server.call(f"/api/jobs/{job_id}/file/{role}")
            assert code < 500, (role, code, data)
            assert code != 200, f"role={role} 竟然读到了文件: {data}"
        print("    正常 role 可读；5 种穿越尝试全部被拒")
    finally:
        server.close()


# ------------------------------------------------------------------ 其它接口

def test_misc_endpoints_reject_bad_input():
    server = Server()
    try:
        cases = [
            ("外部工具未知", "POST", "/api/external", {"tool": "not_a_tool"}),
            ("外部工具缺路径", "POST", "/api/external", {"tool": "multiwfn"}),
            ("外部工具路径不存在", "POST", "/api/external", {"tool": "multiwfn", "path": "Z:/nope/x.xyz"}),
            ("explorer 缺路径", "POST", "/api/external", {"tool": "explorer"}),
            ("obabel 缺 target", "POST", "/api/external", {"tool": "obabel", "path": "Z:/nope"}),
            ("收藏不存在的配方", "POST", "/api/recipes/无此配方/favorite", {}),
            ("删除内置配方", "DELETE", "/api/recipes/opt_b3lyp", None),
            ("批量分子表为空", "POST", "/api/batch", {"molecules": []}),
            ("批量 molecules 不是列表", "POST", "/api/batch", {"molecules": "x"}),
        ]
        results = [(n, *server.call(p, m, q)) for n, m, p, q in cases]
        _no_500(results)
        print(f"    {len(cases)} 种坏请求：{sum(1 for _, c, _ in results if 400 <= c < 500)} 个 4xx，无 500")
    finally:
        server.close()


def test_malformed_json_body_does_not_500():
    server = Server()
    try:
        bodies = [b"", b"{", b"null", b"[]", b'"just a string"', b"\xff\xfe\x00bad"]
        results = [(repr(b[:12]), *server.call("/api/jobs", "POST", raw_body=b)) for b in bodies]
        _no_500(results)
        print(f"    {len(bodies)} 种畸形请求体都无 500")
    finally:
        server.close()


def test_empty_state_endpoints_work():
    """没有任何任务时，各列表接口也要正常工作。"""
    server = Server()
    try:
        code, jobs = server.call("/api/jobs")
        assert code == 200 and jobs.get("jobs") == [], jobs

        code, raw = server.call("/api/report.csv")
        assert code == 200 and "job_id" in str(raw), (code, raw)

        code, recipes = server.call("/api/recipes")
        assert code == 200 and recipes["recipes"], "配方库不该为空"

        code, cfg = server.call("/api/config")
        assert code == 200 and isinstance(cfg, dict), (code, cfg)

        code, status = server.call("/api/status")
        assert code == 200 and "engines" in status, status
        assert status["job_types"], "任务类型不该为空"
        print(f"    空状态下 jobs / report.csv / recipes / config / status 均正常"
              f"（引擎支持 {len(status['job_types'])} 种任务）")
    finally:
        server.close()


def test_degenerate_molecules_are_rejected():
    """退化输入必须在构造分子时就被拦下，而不是带到 ORCA 才炸。

    坐标里的 NaN / inf / 1e10、不认识的元素符号 —— 这些送进 ORCA 只会得到
    垃圾或崩溃，而且报错位置离用户的操作很远，很难排查。
    """
    server = Server()
    try:
        cases = [
            ("0 个原子", "0\n空分子\n", 400),
            ("元素符号非法", "2\n怪元素\nXx 0 0 0\nQ 0 0 1\n", 400),
            ("坐标是 NaN", "1\nnan\nO nan nan nan\n", 400),
            ("坐标是 inf", "1\ninf\nO inf 0 0\n", 400),
            ("坐标极大", "1\nbig\nO 1e10 0 0\n", 400),
            ("原子数说不符", "5\n谎报\nO 0 0 0\nH 0 0 1\n", 400),
            ("1 个原子", "1\n单原子\nO 0 0 0\n", 200),
            ("2 个原子", "2\n双原子\nO 0 0 0\nH 0 0 0.9\n", 200),
        ]
        results = [(n, want, *server.call("/api/structure/text", "POST", {"text": t}))
                   for n, t, want in cases]
        for name, want, code, data in results:
            assert code < 500, f"{name}: 出现 500 —— {str(data)[:150]}"
            assert code == want, f"{name}: 期望 {want}，实际 {code} {data}"
        print(f"    {len(cases)} 种退化分子：该拒的拒、该收的收")
    finally:
        server.close()


def test_overlapping_atoms_are_rejected():
    """两个原子几乎重合必须被拒。

    ORCA 对这种情况只报 ``Zero distance encountered between atoms 3 and 2``，
    用户根本看不出是哪一步坏了。
    """
    server = Server()
    try:
        cases = [
            ("完全重合", "2\n重合\nO 0.0 0.0 0.0\nH 0.0000001 0.0 0.0\n"),
            ("相距 0.1 Å", "2\n贴住\nO 0.0 0.0 0.0\nH 0.1 0.0 0.0\n"),
            ("正常键长 0.96 Å", "2\n正常\nO 0.0 0.0 0.0\nH 0.96 0.0 0.0\n"),
        ]
        results = [(n, *server.call("/api/structure/text", "POST", {"text": t})) for n, t in cases]
        assert results[0][1] == 400 and "重合" in results[0][2]["error"], results[0]
        assert results[1][1] == 400, results[1]
        assert results[2][1] == 200, results[2]
        print("    重合 / 0.1 Å 被拒，0.96 Å 正常键长收下")
    finally:
        server.close()


def test_smiles_generation_produces_valid_geometry():
    """SMILES → 3D 不能生成原子重合的几何。

    实测踩过：RDKit 的 ETKDG 在**乙烷**上会把两个氢嵌到相距 0.002 Å 的位置，
    随后的 MMFF 预优化也救不回来（重合原子上力未定义，它直接"收敛"），
    最后 ORCA 报一句 ``Zero distance encountered between atoms``。
    """
    try:
        from qcgui.cheminfo import _min_pair_gap, molecule_from_smiles
    except ImportError:
        _skip("未安装 rdkit")

    for smiles in ("CC", "CC=C", "C", "c1ccccc1", "CC(=O)O", "CCO", "C1CC1"):
        molecule = molecule_from_smiles(smiles)
        gap, i, j = _min_pair_gap(molecule.atoms)
        assert gap >= 0.4, (
            f"{smiles}: 原子 {i + 1} 与 {j + 1} 相距仅 {gap:.4f} Å"
        )
    print("    7 个常见 SMILES 都生成了合理几何")


_FAKE_SCAN_ENERGIES = """\
    0.00000000  -117.80000000
   60.00000000  -117.79000000
  120.00000000  -117.80000000
"""

_FAKE_SCAN_OUT = """\
             *          RELAXED SURFACE SCAN STEP   1          *
             *   Dihedral (  2,   1,   0,   3)  :   0.00000000  *
             ***************************************************
CARTESIAN COORDINATES (ANGSTROEM)
---------------------------------
  C      0.000000    0.000000    0.000000
  C      1.500000    0.000000    0.000000
  C      2.800000    0.000000    0.000000
  H     -0.500000    0.500000    0.000000
"""


def test_scan_endpoint_returns_profile_or_404():
    """扫描曲线接口：有数据给 200，不是扫描任务给 404，不能 500。

    解析走 worker 子进程（界面进程不导入引擎），所以这条也顺带验证了
    子进程通道是通的。
    """
    server = Server()
    try:
        from qcgui.model import JobSpec, JobType, Molecule

        # ① 造一个"看起来像扫描任务"的目录
        scan_id = "20200101-000000-scantest"
        spec = JobSpec(molecule=Molecule.from_xyz(WATER_XYZ), job_type=JobType.OPT,
                       method="r2SCAN-3c", basis="")
        server.manager.store.create(spec, job_id=scan_id)
        job_dir = server.manager.store.job_dir(scan_id)
        (job_dir / "fake.relaxscanscf.dat").write_text(_FAKE_SCAN_ENERGIES, encoding="ascii")
        (job_dir / "fake.out").write_text(_FAKE_SCAN_OUT, encoding="ascii")

        code, data = server.call(f"/api/jobs/{scan_id}/scan")
        assert code == 200, (code, data)
        assert len(data["points"]) == 3, data
        assert abs(data["barrier_kcal"] - 0.01 * 627.5094740631) < 1e-6, data["barrier_kcal"]
        assert data["coordinate"] == "二面角", data
        cc = next(s for s in data["series"] if s["label"] == "C1–C2")
        assert abs(cc["values"][0] - 1.5) < 1e-6, cc

        # ② 普通任务（没有扫描数据）→ 404，不是 500
        plain_id = "20200101-000000-plaintest"
        server.manager.store.create(
            JobSpec(molecule=Molecule.from_xyz(WATER_XYZ), job_type=JobType.ENERGY,
                    method="HF", basis="STO-3G"),
            job_id=plain_id,
        )
        code, data = server.call(f"/api/jobs/{plain_id}/scan")
        assert code == 404, (code, data)

        # ③ 不存在的任务 → 404
        code, data = server.call(f"/api/jobs/{GHOST}/scan")
        assert code == 404, (code, data)
        print("    扫描任务 200（3 点、势垒 6.275）；普通任务与不存在的任务都 404")
    finally:
        server.close()


def test_repo_text_files_are_clean_utf8():
    """仓库里的 markdown 不能带 BOM、不能有替换字符。

    这两个都是**文本被搞坏**的标志。实测踩过：用 PowerShell 的
    ``Get-Content -Raw`` / ``Set-Content -Encoding UTF8`` 做替换时，PS 5.1 会按系统
    ANSI（本机 cp936）解码 UTF-8 文件，写回时变成双重编码 —— 手册里约 600 个汉字
    被换成 U+FFFD，还丢了一个 ASCII 示意图的换行。

    所以这里守住底线：**乱码和 BOM 一旦出现就测试失败**，而不是等到有人打开文档
    才发现。要改这些文件请用编辑工具，不要用 shell 的文本替换。
    """
    repo = Path(__file__).resolve().parents[1]
    checked = 0
    for name in ("使用手册.md", "README.md", "MIGRATION.md"):
        path = repo / name
        if not path.is_file():
            continue
        checked += 1
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{name} 带了 UTF-8 BOM"
        text = raw.decode("utf-8")  # 不是合法 UTF-8 会直接抛
        assert "\ufffd" not in text, f"{name} 里有替换字符 U+FFFD，文本被破坏过"
        for trace in ("锛", "鐨", "涓枃"):
            assert trace not in text, f"{name} 里出现 GBK 双重编码痕迹「{trace}」"
        # 代码围栏必须成对，否则整个文档结构会错乱
        fences = sum(1 for line in text.splitlines() if line.strip().startswith("```"))
        assert fences % 2 == 0, f"{name} 的代码围栏有 {fences} 个，不成对"
    assert checked, "一个文档都没找到，路径不对"
    print(f"    {checked} 个 markdown：无 BOM、无替换字符、围栏成对")


def test_viewer_atom_order_matches_orca_input_order():
    """界面显示的原子编号必须和写进 ORCA 的原子顺序一致。

    不一致的话，照着界面上的编号填扫描坐标就会作用到**错误的原子**上，
    而且**不会有任何报错** —— 只是结果悄悄错了。所以这条要守住。

    （另一半验证在 ``tools/verify_index_consistency.py``：ORCA 的 ``%geom Scan``
    确实按输入顺序解读下标 —— 用甲醇扫 H–C–O–H 二面角实测过，每个扫描点
    按输入顺序算回来的值都和扫描值相等。）
    """
    server = Server()
    try:
        from qcgui.model import Molecule
        from qcgui.web.app import molecule_to_dict

        # 两个 H 的坐标故意取得不同，这样顺序被换掉能被查出来
        molecule = Molecule.from_text(
            "3\nwater\n"
            "O  0.000000  0.000000  0.000000\n"
            "H  0.000000  0.757200 -0.469200\n"
            "H  0.000000 -0.957200 -0.469200\n"
        )
        payload_molecule = molecule_to_dict(molecule)      # 前端拿到的就是这个

        code, data = server.call("/api/jobs", "POST", {
            "molecule": payload_molecule, "job_type": "energy",
            "method": "HF", "basis": "STO-3G", "nprocs": 1, "maxcore_mb": 200,
        })
        assert code == 201, (code, data)
        job_dir = server.manager.store.job_dir(data["job"]["job_id"])

        # .inp 是 worker 进程异步写的，等它落盘（而不是假设立刻就在）
        inp = None
        for _ in range(100):
            inp = next(iter(sorted(job_dir.glob("*.inp"))), None)
            if inp is not None:
                break
            time.sleep(0.1)
        assert inp is not None, f"{job_dir} 里一直没出现 .inp"
        lines = inp.read_text(encoding="ascii").splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith("* xyz"))
        written = []
        for line in lines[start + 1:]:
            if line.startswith("*"):
                break
            parts = line.split()
            if len(parts) == 4:
                written.append((parts[0], float(parts[1]), float(parts[2]), float(parts[3])))

        assert len(written) == 3, written
        for i in range(3):
            assert written[i][0] == payload_molecule["symbols"][i], (i, written[i])
            for k in range(3):
                assert abs(written[i][1 + k] - payload_molecule["coords"][i][k]) < 1e-9, \
                    f"第 {i} 个原子的坐标对不上：{written[i]} vs {payload_molecule['coords'][i]}"
        print("    界面编号顺序与 .inp 原子顺序逐位一致（3/3）")
    finally:
        server.close()


def test_report_csv_survives_a_broken_jobspec():
    """一条坏记录不能把整张 CSV 打挂。

    实测踩过：``/api/report.csv`` 直接 500、响应体还是空的。根因是历史任务的
    jobspec 不满足**后加的**原子重叠校验，``load_spec`` 抛异常一路冒到接口。
    一张表被两条坏记录打挂，其余一百条一条都导不出来。
    """
    server = Server()
    try:
        from qcgui.model import JobSpec, JobType, Molecule

        good = "20200101-000000-csvgood"
        server.manager.store.create(
            JobSpec(molecule=Molecule.from_xyz(WATER_XYZ), job_type=JobType.ENERGY,
                    method="HF", basis="STO-3G"),
            job_id=good,
        )
        bad = "20200101-000001-csvbad"
        server.manager.store.create(
            JobSpec(molecule=Molecule.from_xyz(WATER_XYZ), job_type=JobType.ENERGY,
                    method="HF", basis="STO-3G"),
            job_id=bad,
        )
        # 把它变成一个"读不出来"的 jobspec
        (server.manager.store.job_dir(bad) / "jobspec.json").write_text(
            "{ 这不是合法 JSON", encoding="utf-8")

        code, raw = server.call("/api/report.csv")
        assert code == 200, f"CSV 应当 200，实际 {code}：{str(raw)[:200]}"
        text = str(raw)
        assert "job_id" in text, text[:200]
        assert good in text, "正常任务必须出现在导出里"
        assert bad in text, "坏记录也应当成行出现（只是上游/配方列为空）"
        rows = [l for l in text.splitlines() if l.strip()]
        assert len(rows) >= 3, rows          # 表头 + 两条
        print(f"    CSV 200，{len(rows) - 1} 条任务（含 1 条读不出 spec 的）")
    finally:
        server.close()


def test_audited_500_paths_are_now_4xx():
    """独立审计发现并已修复的 500 路径，一条都不许回来。

    这批问题的共同点：**接口层对字段不做类型/形状校验**，坏值一路穿到
    ``.strip()`` / ``isascii()`` / ``items()`` 才炸成 500 HTML。
    参考实现是同一套 ``_string_field`` / ``_bounded_int`` / ``_blocks_field``。
    """
    server = Server()
    mol = {"symbols": ["O", "H", "H"], "coords": [[0, 0, 0], [0, 0, 1], [0, 1, 0]],
           "charge": 0, "multiplicity": 1, "name": "w"}
    try:
        # ① method / basis / solvent / title 传对象或数组
        for field, value in (("method", {"a": 1}), ("basis", {"a": 1}),
                             ("solvent", ["thf"]), ("title", {"a": 1})):
            # 注意别用字面量再补一个同名字段 —— 后面的键会把要测的坏值覆盖掉
            payload = {"molecule": mol, "method": "HF", "basis": "STO-3G"}
            payload[field] = value
            code, data = server.call("/api/jobs", "POST", payload)
            assert code == 400, f"{field} 传 {type(value).__name__} 应当 400，实际 {code}"
            assert field in data.get("error", ""), data
        print("    method/basis/solvent/title 传非字符串 → 400")

        # ①' 批量同样
        code, _ = server.call("/api/batch", "POST",
                              {"molecules": [mol, mol], "method": {"a": 1}})
        assert code == 400, f"批量 method 传对象应当 400，实际 {code}"

        # ⑤ extra_keywords 给字符串会被按字符拆开
        code, data = server.call("/api/jobs", "POST",
                                 {"molecule": mol, "extra_keywords": "TightSCF"})
        assert code == 400, f"extra_keywords 给字符串应当 400，实际 {code}"
        assert "extra_keywords" in data.get("error", ""), data
        print("    批量 method、extra_keywords 字符串 → 400")

        # ② /api/config 写坏值：应当被丢弃，且不能把 /api/status 弄挂
        code, _ = server.call("/api/config", "POST", {"multiwfn_path": {"a": 1},
                                                      "web_port": None})
        assert code == 200, code
        code, status = server.call("/api/status")
        assert code == 200, f"坏配置把 /api/status 弄挂了：{code}"
        assert status["config"]["multiwfn_path"] == "", status["config"]
        assert isinstance(status["config"]["web_port"], int), status["config"]
        print("    坏配置被丢弃，/api/status 保持 200")

        # ③ 用户配方 blocks 形状畸形：不能拖垮所有配方接口
        from qcgui.paths import data_root
        rec_dir = Path(data_root()) / "recipes"
        rec_dir.mkdir(parents=True, exist_ok=True)
        bad = rec_dir / "_edge_bad_recipe.json"
        bad.write_text(json.dumps({"id": "_edge_bad", "name": "坏", "blocks": [["onlyname"]]}),
                       encoding="utf-8")
        try:
            code, data = server.call("/api/recipes")
            assert code == 200, f"坏配方文件拖垮了配方接口：{code}"
            assert data.get("recipes"), data
        finally:
            bad.unlink(missing_ok=True)
        print("    畸形配方文件被跳过，配方接口保持 200")

        # ④ result.json 是合法 JSON 但不是对象
        from qcgui.model import JobSpec, JobType, Molecule
        jid = "20200101-000000-listjson"
        server.manager.store.create(
            JobSpec(molecule=Molecule.from_xyz(WATER_XYZ), job_type=JobType.ENERGY,
                    method="HF", basis="STO-3G"),
            job_id=jid,
        )
        (server.manager.store.job_dir(jid) / "result.json").write_text("[]", encoding="utf-8")
        code, _ = server.call(f"/api/jobs/{jid}")
        assert code == 200, f"非对象 result.json 让任务详情 500 了：{code}"
        code, raw = server.call("/api/report.csv")
        assert code == 200, f"非对象 result.json 让整张 CSV 500 了：{code}"
        print("    非对象 result.json 不再打挂任务详情与 CSV")
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
