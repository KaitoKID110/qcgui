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
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="qcgui_browser_"))
        self.manager = JobManager(self.root / "jobs")
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
    # 拖动必须真的换了构象
    assert text.count("coords_changed=PASS") >= 2, text
    # 拖到最右侧应当选中最后一个点
    assert "pointermove  index=2" in text, f"拖到最右侧应选中最后一个点：{text}"


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
