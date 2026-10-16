"""真实浏览器里的前端交互测试。

用无头 Chrome 打开页面，注入 ``?nosse=1&viewertest=1``：
- ``nosse=1`` 关掉 SSE 长连接（否则页面永远处于"加载中"，无头模式等不到）
- ``viewertest=1`` 用**合成鼠标事件**驱动真实的事件处理器，把结果写进 DOM

再由 ``--dump-dom`` 取回 DOM 解析断言。这验证的是**事件绑定**，
是无头逻辑测试（只验证投影数学）覆盖不到的部分。

找不到 Chrome 时整体跳过（可用 ``QCGUI_CHROME`` 指定）。

    pytest tests/test_web_browser.py -v
    python tests/test_web_browser.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.jobs import JobManager  # noqa: E402
from qcgui.web.app import create_app  # noqa: E402

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

_VIEWERTEST_RE = re.compile(
    r'<pre id="viewertest-output"[^>]*>(.*?)</pre>', re.DOTALL
)


class _Skipped(Exception):
    pass


def _skip(reason: str):
    try:
        import pytest

        pytest.skip(reason)
    except ImportError:
        raise _Skipped(reason)


def find_browser() -> str | None:
    explicit = os.environ.get("QCGUI_CHROME")
    if explicit and Path(explicit).is_file():
        return explicit
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def _browser() -> str:
    browser = find_browser()
    if not browser:
        _skip("找不到 Chrome/Edge，跳过浏览器交互测试")
    return browser


# ------------------------------------------------------------------ 服务

class Server:
    def __init__(self, jobs_root: Path | None = None) -> None:
        # jobs_root 给定时直接用那个任务库（键级测试需要**真的 .gbw**，
        # 那玩意只能来自真跑过 ORCA 的任务，临时库里不可能有）
        self.root = Path(tempfile.mkdtemp(prefix="qcgui_browser_"))
        self.manager = JobManager(jobs_root or (self.root / "jobs"))
        self.app = create_app(self.manager)
        from werkzeug.serving import make_server

        self.srv = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.port = self.srv.server_port
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()

    def example_path(self) -> str:
        with urllib.request.urlopen(self.base + "/api/examples", timeout=20) as res:
            data = json.loads(res.read().decode("utf-8"))
        return data["examples"][0]["path"]

    @staticmethod
    def real_jobs_root() -> Path | None:
        """本机真实任务库（机器相关，找不到就跳过 —— CI 上正是这种情形）。"""
        override = os.environ.get("QCGUI_JOBS_ROOT")
        if override:
            root = Path(override)
            return root if root.is_dir() else None
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return None
        root = Path(local) / "qcgui" / "jobs"
        return root if root.is_dir() else None

    def finished_job_with_gbw(self) -> str | None:
        """找一个既有 .gbw 又已完成的任务。找不到返回 None（调用方跳过）。"""
        root = Path(self.manager.store.root)
        if not root.is_dir():
            return None
        for job_dir in sorted(root.iterdir(), reverse=True):
            if not job_dir.is_dir():
                continue
            if not any(job_dir.glob("*.gbw")):
                continue
            record = self.manager.store.get(job_dir.name)
            if record is not None and record.status == "done":
                return job_dir.name
        return None


def run_viewer_test(base: str, load_path: str, timeout: int = 120) -> str:
    """跑无头浏览器，返回 viewertest 那段文本。"""
    chrome = _browser()
    profile = Path(tempfile.mkdtemp(prefix="qcgui_chrome_"))
    url = f"{base}/?nosse=1&viewertest=1&load={load_path}"
    proc = subprocess.run(
        [
            chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", "--window-size=1600,1000",
            "--virtual-time-budget=25000", f"--user-data-dir={profile}",
            "--dump-dom", url,
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    dom = proc.stdout or ""
    match = _VIEWERTEST_RE.search(dom)
    assert match, f"DOM 里没有测试输出（页面可能没跑起来）\nDOM 片段:\n{dom[:1200]}"
    return match.group(1)


# ------------------------------------------------------------------ 测试

def test_viewer_interaction_in_real_browser():
    server = Server()
    try:
        text = run_viewer_test(server.base, server.example_path())
    finally:
        server.close()

    print("    " + "\n    ".join(text.strip().splitlines()))

    assert "VIEWERTEST RESULT: PASS" in text, text
    for line in text.splitlines():
        if line.startswith(("rotate", "zoom", "pan", "reset", "toolbar", "trajectory")):
            assert line.rstrip().endswith("PASS"), line

    assert "rotate" in text and "zoom" in text and "pan" in text
    assert "trajectory display=none" in text, "无轨迹时轨迹栏不该占位"
    assert "atoms=" in text and "atoms=0" not in text, text


def test_scan_curve_drag_changes_the_displayed_geometry():
    """拖动扫描曲线要能换预览构象。

    这是"在势能图上拖着看构象怎么变"的核心行为，只有真浏览器能验：
    合成指针事件 → 图表算最近的点 → 中栏换成那个点的几何。

    人造一份扫描输出（不跑 ORCA），所以是秒级的；用的就是解析器测试里
    那份 fixture 的格式。
    """
    from qcgui.model import JobSpec, JobType, Molecule

    server = Server()
    try:
        job_id = "20200101-000000-scandrag"
        server.manager.store.create(
            JobSpec(molecule=Molecule.from_xyz("3\nwater\nO 0 0 0\nH 0 0.76 0\nH 0 -0.76 0\n"),
                    job_type=JobType.OPT, method="r2SCAN-3c", basis=""),
            job_id=job_id,
        )
        job_dir = server.manager.store.job_dir(job_id)
        # 3 个扫描点，每个点的几何都不同（第 3 个原子逐步拉远）
        energies, outs = [], []
        for k, (angle, d) in enumerate(((0.0, 1.30), (60.0, 1.45), (120.0, 1.60))):
            energies.append(f"{angle:12.8f}  {-117.80 + k * 0.01:.8f}")
            outs.append(
                f"             *          RELAXED SURFACE SCAN STEP   1          *\n"
                f"             *   Dihedral (  2,   1,   0,   3)  : {angle:12.8f}  *\n"
                "             ***************************************************\n"
                "CARTESIAN COORDINATES (ANGSTROEM)\n"
                "---------------------------------\n"
                "  C      0.000000    0.000000    0.000000\n"
                "  C      1.500000    0.000000    0.000000\n"
                f"  C      {d + 1.5:.6f}    0.000000    0.000000\n"
                "  H     -0.500000    0.500000    0.000000\n"
            )
        (job_dir / "fake.relaxscanscf.dat").write_text("\n".join(energies) + "\n", encoding="ascii")
        (job_dir / "fake.out").write_text("\n".join(outs), encoding="ascii")
        # 还得有 result.json：结果面板拿到 null 会直接早退，扫描区就没机会加载。
        # 坐标故意和扫描第 1 点不同 —— 否则拖到第 1 点时"没变化"，
        # 就分不清是"拖动没生效"还是"本来就一样"。
        (job_dir / "result.json").write_text(json.dumps({
            "status": "done", "engine": "orca", "engine_version": "6.1.1",
            "job_type": "opt", "method": "r2SCAN-3c",
            "final_energy": -117.80,
            "geometry": {"symbols": ["C", "C", "C", "H"],
                         "coords": [[0, 0, 0], [1.5, 0, 0], [2.60, 0, 0], [-0.5, 0.5, 0]],
                         "units": "angstrom"},
        }), encoding="utf-8")

        chrome = _browser()
        profile = Path(tempfile.mkdtemp(prefix="qcgui_chrome_"))
        url = f"{server.base}/?nosse=1&job={job_id}&scantest=1"
        proc = subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
             "--window-size=1500,1000", "--virtual-time-budget=25000",
             f"--user-data-dir={profile}", "--dump-dom", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        dom = proc.stdout or ""
    finally:
        server.close()

    match = re.search(r"__SCANTEST__(.*?)__END__", dom, re.S)
    assert match, f"没拿到扫描拖动测试输出\nDOM 片段:\n{dom[:800]}"
    text = match.group(1)
    print("    " + "\n    ".join(p.strip() for p in text.split(" | ")))

    assert "ERROR" not in text, text
    # 每一项都应当出现，且不能有任何一个 FAIL
    for key in ("pointerdown", "pointermove", "readout", "readout_changed",
                "matches_profile", "cursor_visible", "coords_changed=PASS"):
        assert key in text, f"缺少 {key} 的结果：{text}"
    assert "FAIL" not in text, text
    # F5：「键长响应」模式下游标不能画到图外（审计 F5）
    assert "F5_bond_cursor_cy" in text, text
    assert "SKIP" in text or "inside_plot=PASS" in text, text
    # 拖动必须真的换了构象
    assert text.count("coords_changed=PASS") >= 2, text
    # 拖到最右侧应当选中最后一个点
    assert "pointermove  index=2" in text, f"拖到最右侧应选中最后一个点：{text}"


def test_interaction_fixes_in_real_browser():
    """三处修复必须在真页面里生效（审计 F1/F2 + P0 结论横幅）。

    都是"接线"层面的问题 —— 纯逻辑测试证明不了，而本项目已经踩过
    "DOM 里有但用户看不见"（扫描区被插到滚动区下面）这种坑。

    * F1 SSE 断开时「运行」按钮永久禁用 → 看门狗要能恢复
    * F2 快速切换任务时结果面板串台 → 过期响应要被丢弃
    * P0 结论横幅要真的渲染出来，且带上正确的严重度类名
    """
    from qcgui.model import JobSpec, JobType, Molecule, Result

    server = Server()
    try:
        # 造一个"算完但分子变了"的任务：这正是结论横幅最该拦下的一类
        symbols = ["C", "C", "O", "H", "H", "H", "H"]
        before = [[0.0, 0.0, 0.0], [1.5, 0.0, 0.0], [2.9, 0.0, 0.0],
                  [-0.6, 0.9, 0.0], [-0.6, -0.9, 0.0], [1.5, 0.9, 0.0], [3.86, 0.0, 0.0]]
        after = [list(c) for c in before]
        after[2] = [4.2, 2.0, 0.0]
        after[6] = [4.9, 2.0, 0.0]
        after[3] = [4.2, 2.6, 0.0]
        body = "\n".join(f"{s} {c[0]:.6f} {c[1]:.6f} {c[2]:.6f}"
                         for s, c in zip(symbols, before))
        molecule = Molecule.from_xyz(f"{len(symbols)}\nbroken\n{body}\n", name="broken")
        spec = JobSpec(molecule=molecule, job_type=JobType.OPT, method="HF", basis="STO-3G")
        job_id = "20200101-000000-fixtest"
        server.manager.store.create(spec, job_id=job_id)
        job_dir = server.manager.store.job_dir(job_id)
        (job_dir / "jobspec.json").write_text(spec.canonical_json(), encoding="utf-8")
        Result(status="done", job_type="opt", method="HF",
               geometry={"symbols": symbols, "coords": after, "units": "angstrom"},
               job_dir=str(job_dir)).save(job_dir / "result.json")
        # 索引里的状态也要改成 done —— 直接写 result.json 绕过了 save_result，
        # 而"运行"按钮的看门狗**看的是索引状态**（踩过：不改的话它永远等下去，
        # 诊断输出 F1_polls=99 就是这么来的）
        server.manager.store.update(job_id, status="done", final_energy=-1.0)

        chrome = _browser()
        profile = Path(tempfile.mkdtemp(prefix="qcgui_chrome_"))
        url = f"{server.base}/?nosse=1&fixtest={job_id}"
        proc = subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
             "--window-size=1500,1000", "--virtual-time-budget=30000",
             f"--user-data-dir={profile}", "--dump-dom", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        dom = proc.stdout or ""
    finally:
        server.close()

    match = re.search(r"__FIXTEST__(.*?)__END__", dom, re.S)
    assert match, f"没拿到交互测试输出\nDOM 片段:\n{dom[:600]}"
    text = match.group(1)
    print("    " + "\n    ".join(p.strip() for p in text.split(" | ")))

    assert "ERROR" not in text, text
    for key in ("F1_button_restored", "F1_cancel_disabled", "F2_stale_discarded",
                "verdict_banner"):
        assert f"{key}=PASS" in text, f"{key} 没通过：{text}"
    # 结论横幅的严重度类名要对 —— 这一条是 blocking，必须落成 unusable
    assert "class=unusable" in text, f"结论横幅的类名不对：{text}"
    assert "结果不可用" in text, text
    print("    三处修复在真页面里都生效")


def test_bond_order_ui_in_real_browser():
    """键级界面（Multiwfn 集成）必须在真页面里能用、能看见。

    后端早有测试，但**界面是新写的** —— 本项目已经因为"DOM 里有但用户看不见"
    吃过一次亏（结论横幅被扫描区挤到滚动区下面）。所以这里要点一遍真按钮，
    并检查它排在结论横幅**之后**、确实在可见区内。

    需要一个**有 .gbw 且装了 Multiwfn** 的任务；本机没有就跳过（CI 上正是这种情形）。
    """
    # 用**和应用同一套**探测逻辑，而不是 shutil.which ——
    # 本机的 Multiwfn 装在 D:\multiwfn\... 靠自动探测找到，不在 PATH 上。
    # 用 which 判断会让这个测试在本机也静默跳过，等于没有牙齿。
    from qcgui.external import resolve_tool

    exe, _ = resolve_tool("multiwfn")
    if not exe:
        print("    跳过：本机没有 Multiwfn")
        return

    server = Server(Server.real_jobs_root())
    try:
        job_id = server.finished_job_with_gbw()
        if job_id is None:
            print("    跳过：真实任务库里没有带 .gbw 的已完成任务")
            return
        chrome = _browser()
        profile = Path(tempfile.mkdtemp(prefix="qcgui_chrome_"))
        url = f"{server.base}/?nosse=1&job={job_id}&botest={job_id}&bokind=mayer"
        proc = subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
             "--window-size=1500,1100", "--virtual-time-budget=120000",
             f"--user-data-dir={profile}", "--dump-dom", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
        )
        dom = proc.stdout or ""
    finally:
        server.close()

    match = re.search(r"__BOTEST__(.*?)__END__", dom, re.S)
    assert match, f"没拿到键级界面测试输出\nDOM 片段:\n{dom[:600]}"
    text = match.group(1)
    print("    " + "\n    ".join(p.strip() for p in text.split(" | ")))

    assert "ERROR" not in text, text
    for key in ("section=PASS", "visible=PASS", "table=PASS", "bar=PASS"):
        assert key in text, f"{key} 没通过：{text}"
    # 排在结论横幅之后 —— 结论横幅永远是最上面那条
    assert "res-verdict" in text, text
    assert text.index("res-verdict") < text.index("bondorder-section"), \
        f"键级区不该排在结论横幅前面：{text}"
    print("    键级界面在真页面里可用、可见、顺序正确")


def test_page_loads_without_javascript_errors():
    """页面首屏不得抛 JS 异常：用一个会渲染失败的探针检测。"""
    server = Server()
    try:
        chrome = _browser()
        profile = Path(tempfile.mkdtemp(prefix="qcgui_chrome_"))
        url = f"{server.base}/?nosse=1"
        proc = subprocess.run(
            [
                chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                "--window-size=1400,900", "--virtual-time-budget=12000",
                f"--user-data-dir={profile}", "--dump-dom", url,
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
    finally:
        server.close()

    dom = proc.stdout or ""
    # 关键元素都渲染出来了 → JS 正常执行到底
    for marker in ('id="viewer"', 'id="jobs-list"', 'id="log"', 'id="select-jobtype"'):
        assert marker in dom, f"DOM 缺少 {marker}"
    # 任务类型下拉应当被 JS 填上了选项（空的话说明 init 没跑完）
    assert "<option value=\"energy\"" in dom, "任务类型下拉没有被填充，init 可能中断"
    print("    首屏渲染完整，下拉已由 JS 填充")


#: 过氧化氢构型：3 根键、2 个键角、**1 个二面角**（H–O–O–H = 90°）。
#: 特意选它，因为二面角最容易写反（差 180°），而 90° 一眼能看出对错。
GEOM_FIXTURE = {
    "symbols": ["O", "O", "H", "H"],
    "coords": [[0.0, 0.0, 0.0], [0.0, 0.0, 1.45], [0.97, 0.0, 0.0], [0.0, 0.97, 1.45]],
}


def create_geometry_fixture_job(server: Server, job_id: str = "20200101-000000-geomtest") -> str:
    """造一个"已完成、带结果几何"的任务（不跑 ORCA），供几何参数表测试用。

    结果几何是 :data:`GEOM_FIXTURE`。返回任务 id。
    """
    from qcgui.model import JobSpec, JobType, Molecule, Result

    body = "\n".join(f"{s} {c[0]:.6f} {c[1]:.6f} {c[2]:.6f}"
                     for s, c in zip(GEOM_FIXTURE["symbols"], GEOM_FIXTURE["coords"]))
    molecule = Molecule.from_xyz(
        f"{len(GEOM_FIXTURE['symbols'])}\nh2o2\n{body}\n", name="h2o2")
    spec = JobSpec(molecule=molecule, job_type=JobType.OPT, method="HF", basis="STO-3G")
    server.manager.store.create(spec, job_id=job_id)
    job_dir = server.manager.store.job_dir(job_id)
    (job_dir / "jobspec.json").write_text(spec.canonical_json(), encoding="utf-8")
    Result(status="done", job_type="opt", method="HF",
           geometry=dict(GEOM_FIXTURE, units="angstrom"),
           job_dir=str(job_dir)).save(job_dir / "result.json")
    # 索引状态也要改成 done（结果面板与"运行"按钮看的是索引状态）
    server.manager.store.update(job_id, status="done", final_energy=-1.0)
    return job_id


def _dump_geomtest(url: str, timeout: int = 180) -> str:
    """跑无头浏览器，取回 __GEOMTEST__ 那段文本。"""
    chrome = _browser()
    profile = Path(tempfile.mkdtemp(prefix="qcgui_chrome_"))
    proc = subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         "--window-size=1600,1000", "--virtual-time-budget=30000",
         f"--user-data-dir={profile}", "--dump-dom", url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    dom = proc.stdout or ""
    match = re.search(r"__GEOMTEST__(.*?)__END__", dom, re.S)
    assert match, f"没拿到几何参数表测试输出\nDOM 片段:\n{dom[:800]}"
    return match.group(1)


def _print_diagnostics(text: str) -> None:
    """打印几何参数测试的诊断行（``a | b | c`` 转成多行）。

    为什么不能直接 ``print``：这些行里有 ``Å``/``°``，而 Windows 控制台默认是
    GBK —— 直接打印会抛 ``UnicodeEncodeError``。实测：那会让**一条本该通过的
    测试在断言之前就 ERROR 掉**（``pytest -s`` 时必然发生），看起来像功能坏了。
    诊断输出降级成替代字符就行，**断言才是测试的正文**。
    """
    block = "    " + "\n    ".join(p.strip() for p in text.split(" | "))
    try:
        print(block)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(block.encode(encoding, "replace").decode(encoding, "replace"))


def test_geometry_table_in_real_browser():
    """「几何参数」表必须在真页面里渲染出来、能点、能高亮 —— 而且看得见。

    本项目已经三次栽在"DOM 里有但屏幕上没有"（结论横幅被挤到滚动区下面、
    3D 数值没画出来）。所以这里同时断言：
    * 行数与值真的对（3 键长 / 2 键角 / 1 个 90° 二面角，按值升序）
    * 区块在**可见区**内（getBoundingClientRect），且排在结论横幅之后
    * 点一行之后查看器里高亮的原子数与行里的原子一致，
      并且 ``highlightDrawCount`` 证明高亮环**真的画出来了**
    * 切标签会清掉旧高亮；再点同一行能取消
    """
    from qcgui.model.geometry import geometry_parameters

    server = Server()
    try:
        job_id = create_geometry_fixture_job(server)
        text = _dump_geomtest(f"{server.base}/?nosse=1&job={job_id}&geomtest=1")
    finally:
        server.close()

    _print_diagnostics(text)

    # 期望值直接来自纯几何层 —— 浏览器里看到的必须和后端算的一致
    expected = geometry_parameters(GEOM_FIXTURE["symbols"], GEOM_FIXTURE["coords"])
    assert len(expected["bonds"]) == 3 and len(expected["angles"]) == 2
    assert len(expected["dihedrals"]) == 1

    assert "ERROR" not in text, text
    assert "FAIL" not in text, text
    assert "GEOMTEST RESULT: PASS" in text, text
    assert "source=result" in text, "用了任务结果的几何（优先来源）"
    assert "counts=bonds:3,angles:2,dihedrals:1" in text, text
    # 可见 + 位置：结论横幅永远在最上面，几何参数紧跟其后（不能被推到滚动区下面）
    assert "visible=PASS" in text, text
    assert "after_verdict=PASS" in text, text
    # 面板里的顺序：结论横幅第一、几何参数紧跟其后（不能被推到滚动区下面）
    order_line = next(p.strip() for p in text.split(" | ") if p.strip().startswith("order="))
    order = order_line.split("=", 1)[1].split(",")
    assert order[0].startswith("res-verdict"), order
    assert order[1] == "geometry-section", order
    # 三张表：行数与标题上的计数都要对
    assert "bonds_rows=3 sorted=PASS" in text, text
    assert "angles_rows=2 sorted=PASS cleared=PASS" in text, text
    assert "dihedrals_rows=1" in text, text
    assert "tab_counts=bonds:3 angles:2 dihedrals:1" in text, text
    # 按值排序：第一行必须是最小的键长、最后一行是最大的
    assert "bonds_first=O0–H2=0.970Å" in text, text
    assert "bonds_last=O0–O1=1.450Å" in text, text
    # 点一行 → 高亮涉及的原子（2 个），并且真的画了 2 个高亮环
    assert "pick=O0–H2 highlight=0,2 n=2" in text, text
    assert "draws_forced=2" in text, text
    # 键角 3 个原子、二面角 4 个原子：数值也必须是算出来的那个
    assert "angle_pick=" in text and "n=3 drawn=3" in text, text
    assert "dihedral_pick=H2–O0–O1–H3 value=90.00 n=4 drawn=4" in text, text
    assert "toggle_off=PASS" in text, text
    # 表与 3D 错位时（拖过扫描曲线 / 停在轨迹某帧）点一行：必须先把查看器切到
    # 表的那套几何，高亮才落在正确的原子上 —— 否则就是"高亮指错原子"那类错误
    mismatch = next(p.strip() for p in text.split(" | ")
                    if p.strip().startswith("mismatch_switch="))
    assert "mismatch_switch=PASS" in mismatch and "ok=PASS" in mismatch, mismatch
    print("    几何参数表在真页面里可用、可见、点击高亮正确")


def test_geometry_table_falls_back_to_the_loaded_structure():
    """没有任务结果时，几何参数表用**当前载入的结构**，并标明来源。

    这条覆盖"数据来源"的第二档。行数/条数不写死 —— 直接和纯几何层
    对同一个文件算出来的结果比：浏览器 → 接口 → geometry_parameters
    整条链必须一致。
    """
    import urllib.parse

    from qcgui.model.geometry import geometry_parameters

    server = Server()
    try:
        path = server.example_path()
        text = _dump_geomtest(
            f"{server.base}/?nosse=1&geomtest=1&load={urllib.parse.quote(path, safe='')}")
    finally:
        server.close()

    _print_diagnostics(text)

    symbols, coords = [], []
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    count = int(lines[0].split()[0])
    for line in lines[2:2 + count]:
        parts = line.split()
        symbols.append(parts[0])
        coords.append([float(x) for x in parts[1:4]])
    expected = geometry_parameters(symbols, coords)

    assert "ERROR" not in text, text
    assert "FAIL" not in text, text
    assert "GEOMTEST RESULT: PASS" in text, text
    assert "source=structure" in text, "没有任务结果时应当退回当前载入的结构"
    assert f"atoms={len(symbols)}" in text, text
    assert (f"counts=bonds:{len(expected['bonds'])}"
            f",angles:{len(expected['angles'])}"
            f",dihedrals:{len(expected['dihedrals'])}") in text, text
    order_line = next(p.strip() for p in text.split(" | ") if p.strip().startswith("order="))
    assert "geometry-section" in order_line, order_line
    assert "visible=PASS" in text, text
    assert f"bonds_rows={len(expected['bonds'])} sorted=PASS" in text, text
    # 点一行同样要高亮并画出来
    assert "draws_forced=" in text and "draws_forced=0" not in text, text
    mismatch = next(p.strip() for p in text.split(" | ")
                    if p.strip().startswith("mismatch_switch="))
    assert "ok=PASS" in mismatch, mismatch
    print(f"    {Path(path).name}: 表由当前载入的结构生成，条数与后端一致")


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
