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


def test_overlay_two_structures_in_real_browser():
    """结构叠加 RMSD 必须在真页面里点得动、看得见。

    本项目已经三次栽在"DOM 里有但屏幕上没有"，所以这里除了读数，还要证明
    **两个结构真的同时画在画布上**：

    * 数画布实际发出的 ``arc`` 调用（每个原子一个球）—— 叠加后必须等于
      "主结构原子数 + 被叠加结构原子数"
    * 比较开启叠加前后的画布像素 —— 画面必须真的变了
    * 两个结构的投影点都要落在画布可见区内
    * 两个 RMSD（对齐前 / 对齐后）都要出现在面板和画布图例里

    用料：同一个分子绕 z 轴 90° + 平移 —— 已知变换，所以对齐后必须 ≈ 0。
    """
    from qcgui.model import JobSpec, JobType, Molecule, Result

    symbols = ["C", "O", "H", "N", "H", "C"]
    coords = [[0.0, 0.0, 0.0], [1.21, 0.02, -0.31], [-0.55, 0.94, 0.12],
              [0.35, -1.15, 0.63], [1.72, 0.51, 0.44], [-1.30, -0.42, 0.10]]
    # 绕 z 轴 90°（x' = -y, y' = x）再平移 (1, -2, 3.5)
    moved = [[-c[1] + 1.0, c[0] - 2.0, c[2] + 3.5] for c in coords]

    server = Server()
    try:
        ids = []
        for tag, xyz in (("ovref", coords), ("ovmov", moved)):
            job_id = f"20200101-000000-{tag}"
            body = "\n".join(f"{s} {c[0]:.6f} {c[1]:.6f} {c[2]:.6f}"
                             for s, c in zip(symbols, xyz))
            molecule = Molecule.from_xyz(f"{len(symbols)}\n{tag}\n{body}\n", name=tag)
            spec = JobSpec(molecule=molecule, job_type=JobType.OPT,
                           method="HF", basis="STO-3G")
            server.manager.store.create(spec, job_id=job_id)
            job_dir = server.manager.store.job_dir(job_id)
            (job_dir / "jobspec.json").write_text(spec.canonical_json(), encoding="utf-8")
            Result(status="done", job_type="opt", method="HF",
                   geometry={"symbols": symbols, "coords": xyz, "units": "angstrom"},
                   job_dir=str(job_dir)).save(job_dir / "result.json")
            server.manager.store.update(job_id, status="done", final_energy=-1.0)
            ids.append(job_id)

        chrome = _browser()
        profile = Path(tempfile.mkdtemp(prefix="qcgui_chrome_"))
        url = f"{server.base}/?nosse=1&job={ids[0]}&overtest={ids[1]}"
        proc = subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
             "--window-size=1500,1000", "--virtual-time-budget=30000",
             f"--user-data-dir={profile}", "--dump-dom", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        dom = proc.stdout or ""
    finally:
        server.close()

    match = re.search(r"__OVERTEST__(.*?)__END__", dom, re.S)
    assert match, f"没拿到叠加界面测试输出\nDOM 片段:\n{dom[:800]}"
    text = match.group(1)
    print("    " + "\n    ".join(p.strip() for p in text.split(" | ")))

    assert "ERROR" not in text, text
    for key in ("section=PASS", "visible=PASS", "option_present=PASS", "readout=PASS",
                "legend=PASS", "both_structures_drawn=PASS", "canvas_changed=PASS",
                "on_screen=PASS", "clear=PASS"):
        assert key in text, f"{key} 没通过：{text}"

    # 对齐前必须是个明显不为 0 的数（两个文件本来就摆在不同位置）
    before = re.search(r"rmsd_before=([0-9.eE+-]+)", text)
    after = re.search(r"rmsd_after=([0-9.eE+-]+)", text)
    assert before and after, text
    assert float(before.group(1)) > 1.0, f"对齐前应当明显不为 0：{text}"
    assert float(after.group(1)) < 1e-6, f"已知旋转 + 平移，对齐后应当 ≈ 0：{text}"
    # 被叠加的结构也要有自己的成键（虚线键不是空的）
    bonds = re.search(r"viewer_overlay_bonds=(\d+)", text)
    assert bonds and int(bonds.group(1)) > 0, f"叠加结构没推断出成键：{text}"
    # 顺序：叠加区必须排在结论横幅之后（结论横幅永远最上面）
    assert text.index("res-verdict") < text.index("overlay-section"), \
        f"叠加区不该排在结论横幅前面：{text}"
    print("    两个结构同时在画布上，对齐前/后两个 RMSD 都在页面里显示")


def test_overlay_mismatch_shows_a_red_error_not_a_green_number():
    """两个结构不兼容时，用户必须看到**红色错误**，而不是一个假数字。

    这是项目规矩「最坏的 bug 不是算错，是算错了还显示绿点」的直接检验。
    后端的 400 早就有测试了，但"那时候用户看到的是什么"只有真页面能回答：
    所以这里在真 Chrome 里再点一次「与当前结构叠加」（选中一个原子数不同的
    任务），并逐条检查

    * ``#ov-body`` 里出现 ``.bo-error`` 且文字点明了原因（原子数/元素顺序）
    * 不能留下叠加数据、不能出现画布图例
    * 画布上不能多出任何球（只画主结构）
    * **成功路径同一个页面里也要照常工作**（防止"为了报错把正常路径搞坏"）
    """
    from qcgui.model import JobSpec, JobType, Molecule, Result

    symbols = ["C", "O", "H", "N", "H", "C"]
    coords = [[0.0, 0.0, 0.0], [1.21, 0.02, -0.31], [-0.55, 0.94, 0.12],
              [0.35, -1.15, 0.63], [1.72, 0.51, 0.44], [-1.30, -0.42, 0.10]]
    moved = [[-c[1] + 1.0, c[0] - 2.0, c[2] + 3.5] for c in coords]
    # 原子数不同（3 vs 6）—— 后端必须 400，界面上必须变成红色错误
    bad_symbols = ["O", "H", "H"]
    bad_coords = [[0.0, 0.0, 0.117], [0.0, 0.757, -0.469], [0.0, -0.757, -0.469]]

    server = Server()
    try:
        ids = []
        for tag, sym, xyz in (("errref", symbols, coords),
                              ("errmov", symbols, moved),
                              ("errbad", bad_symbols, bad_coords)):
            job_id = f"20200101-000000-{tag}"
            body = "\n".join(f"{s} {c[0]:.6f} {c[1]:.6f} {c[2]:.6f}"
                             for s, c in zip(sym, xyz))
            molecule = Molecule.from_xyz(f"{len(sym)}\n{tag}\n{body}\n", name=tag)
            spec = JobSpec(molecule=molecule, job_type=JobType.OPT,
                           method="HF", basis="STO-3G")
            server.manager.store.create(spec, job_id=job_id)
            job_dir = server.manager.store.job_dir(job_id)
            (job_dir / "jobspec.json").write_text(spec.canonical_json(), encoding="utf-8")
            Result(status="done", job_type="opt", method="HF",
                   geometry={"symbols": sym, "coords": xyz, "units": "angstrom"},
                   job_dir=str(job_dir)).save(job_dir / "result.json")
            server.manager.store.update(job_id, status="done", final_energy=-1.0)
            ids.append(job_id)

        chrome = _browser()
        profile = Path(tempfile.mkdtemp(prefix="qcgui_chrome_"))
        url = (f"{server.base}/?nosse=1&job={ids[0]}&overtest={ids[1]}"
               f"&overbad={ids[2]}")
        proc = subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
             "--window-size=1500,1000", "--virtual-time-budget=30000",
             f"--user-data-dir={profile}", "--dump-dom", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        dom = proc.stdout or ""
    finally:
        server.close()

    match = re.search(r"__OVERTEST__(.*?)__END__", dom, re.S)
    assert match, f"没拿到叠加界面测试输出\nDOM 片段:\n{dom[:800]}"
    text = match.group(1)
    print("    " + "\n    ".join(p.strip() for p in text.split(" | ")))

    assert "ERROR" not in text, text
    # 失败路径
    for key in ("bad_option_present=PASS", "error_shown=PASS",
                "error_mentions_cause=PASS", "error_no_overlay_data=PASS",
                "error_no_legend=PASS", "error_only_primary_drawn=PASS"):
        assert key in text, f"{key} 没通过：{text}"
    # 成功路径在同一个页面里仍然完好
    for key in ("readout=PASS", "legend=PASS", "both_structures_drawn=PASS", "clear=PASS"):
        assert key in text, f"成功路径被搞坏了：{key}：{text}"
    # 错误文字要说人话（点明原子的对应关系问题），不能是内部异常
    err = re.search(r'error_text="([^"]*)"', text)
    assert err and "原子数" in err.group(1), f"错误提示没有说明原因：{text}"
    assert "Traceback" not in err.group(1) and "500" not in err.group(1), text
    print("    不兼容结构 → 页面出现红色错误、无叠加数据/图例/多余原子；成功路径不受影响")


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
