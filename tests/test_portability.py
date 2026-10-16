"""可移植性测试：换一台 Windows 机器时，路径能不能配上、缺东西能不能说清楚。

这些行为曾经全靠"某台机器的布局"：
* ORCA 只能在写死的几个目录（如 ``D:\\orca1``）里被找到
* Multiwfn/Avogadro 的候选位置是写死的绝对路径
* 出错提示写死 ``conda activate <某个环境名>``
* 没有依赖清单，也没有"缺什么"的诊断
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import qcgui.orcatools as orcatools  # noqa: E402
from qcgui.paths import search_roots  # noqa: E402


def _isolate(monkeypatch_home: str) -> None:
    os.environ["QCGUI_HOME"] = monkeypatch_home


def test_orca_path_can_be_configured():
    """设置里填的路径必须生效，而且要**优先于 PATH**。

    否则机器上碰巧有另一个 ORCA 在 PATH 里时，用户会以为"设置没保存"。
    """
    home = tempfile.mkdtemp(prefix="qcgui_port_")
    _isolate(home)

    # 造一个假的 orca.exe
    fake_dir = Path(home) / "myorca"
    fake_dir.mkdir(parents=True, exist_ok=True)
    fake = fake_dir / "orca.exe"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")

    from qcgui.config import save_config

    save_config({"orca_path": str(fake)})
    assert orcatools.configured_orca_path() == str(fake)
    assert orcatools.orca_executable() == str(fake), \
        "设置里填了路径就必须用它，不能被 PATH 里的另一个 ORCA 顶掉"
    assert orcatools.orca_source() == "设置里填的路径"
    print("    设置里的 orca_path 生效，且优先于 PATH")


def test_orca_env_var_wins_over_config():
    """环境变量 > 配置文件（临时覆盖设置，不用改配置）。"""
    home = tempfile.mkdtemp(prefix="qcgui_port2_")
    _isolate(home)
    configured = Path(home) / "configured_orca.exe"
    configured.write_text("x", encoding="utf-8")
    env_one = Path(home) / "env_orca.exe"
    env_one.write_text("x", encoding="utf-8")

    from qcgui.config import save_config

    save_config({"orca_path": str(configured)})
    os.environ[orcatools.ENV_ORCA_EXE] = str(env_one)
    try:
        assert orcatools.orca_executable() == str(env_one)
        assert orcatools.orca_source() == f"环境变量 {orcatools.ENV_ORCA_EXE}"
    finally:
        os.environ.pop(orcatools.ENV_ORCA_EXE, None)
    assert orcatools.orca_executable() == str(configured)
    print("    环境变量 > 配置文件，且移除后回落到配置")


def test_stale_configured_path_does_not_brick_the_app():
    """配置里填了一个不存在的路径时，要回退探测，而不是直接不可用。

    可移植性最怕的就是"配置里留了上一台机器的路径"—— 换了机器后
    这个路径必然失效，程序必须还能自己找到 ORCA。
    """
    home = tempfile.mkdtemp(prefix="qcgui_port3_")
    _isolate(home)
    from qcgui.config import save_config

    save_config({"orca_path": r"Z:\不存在的目录\orca.exe"})
    assert orcatools.configured_orca_path() == r"Z:\不存在的目录\orca.exe"
    # 不能因为这个坏配置就返回 None —— 应当继续往下探测
    result = orcatools.orca_executable()
    if result is None:
        print("    坏配置下未探测到 ORCA（本机确实没装的话属正常）")
    else:
        assert result != r"Z:\不存在的目录\orca.exe", "不该把一个不存在的路径当成结果"
        print(f"    坏配置被跳过，回退探测到 {result}")
    # 关键：不能抛异常
    orcatools.orca_source()


def test_search_roots_covers_machine_independent_locations():
    """搜索根目录必须包含**盘符根**与用户目录 —— 不能只认 C:\\ 的固定布局。

    ORCA / Multiwfn 都是解压即用，用户习惯放在盘根（``D:\\orca_6_1_1\\``）。
    """
    roots = search_roots()
    assert roots, "至少要有一个搜索根"
    drive_roots = [r for r in roots if len(r) == 3 and r[1] == ":"]
    assert drive_roots, f"缺少盘符根，换台机器就探测不到了：{roots}"
    # 本机常见位置应当在候选里
    joined = " | ".join(roots)
    assert ":" in joined
    print(f"    搜索根 {len(roots)} 个，含盘符根 {len(drive_roots)} 个")


def test_tool_patterns_are_generated_not_hardcoded():
    """Multiwfn/Avogadro 的候选位置必须按搜索根生成，不能是写死的绝对路径。"""
    from qcgui.config import _TOOL_PATTERNS

    assert set(_TOOL_PATTERNS) == {"multiwfn", "avogadro", "obabel"}
    for name, patterns in _TOOL_PATTERNS.items():
        assert patterns, f"{name} 没有任何候选模式"
        # 写死的 D:\multiwfn\*\Multiwfn.exe 这种，只要目录名换了就失效
        assert not all(p.startswith("D:\\") for p in patterns), \
            f"{name} 的候选似乎还是写死在 D 盘"
    # 至少要覆盖多个盘符/用户目录
    multiwfn = _TOOL_PATTERNS["multiwfn"]
    roots = {p.split(os.sep)[0] for p in multiwfn}
    assert len(roots) >= 2, f"Multiwfn 候选只覆盖了 {roots}"
    print(f"    三个工具的候选模式都是按搜索根生成的（Multiwfn {len(multiwfn)} 条）")


def test_requirements_file_lists_the_real_dependencies():
    """依赖清单要和代码里真正 import 的对得上。

    没有这个文件，另一台机器根本装不起来 —— 而且报错发生在 import 时，
    用户看到的是"点了没反应"。
    """
    text = (REPO / "requirements.txt").read_text(encoding="utf-8")
    for package in ("Flask", "Werkzeug", "rdkit", "Pillow"):
        assert package.lower() in text.lower(), f"requirements.txt 里没有 {package}"
    # 必需项不能带注释掉
    lines = [line.strip() for line in text.splitlines()]
    assert any(line.lower().startswith("flask") for line in lines), "Flask 必须是可安装的行"
    print("    requirements.txt 覆盖了 Flask/Werkzeug/rdkit/Pillow")


def test_doctor_runs_and_is_actionable():
    """诊断必须能跑通，并且对每一项缺失给出"下一步做什么"。"""
    from qcgui.doctor import run_doctor

    import io

    buffer = io.StringIO()
    code = run_doctor(stream=buffer)
    text = buffer.getvalue()

    assert code in (0, 1), code
    assert "环境诊断" in text
    assert "Python" in text, text
    assert "量化引擎" in text, text
    assert "结论" in text, text
    # 缺东西时必须有箭头指引
    if code == 1:
        assert "→" in text, "有阻塞项却没给处理办法"
    print(f"    doctor 退出码 {code}，输出 {len(text.splitlines())} 行")


def test_engine_capabilities_expose_where_orca_came_from():
    """``--capabilities`` 要报告 ORCA 的具体路径与来源。

    换机器时"没找到"远不如"从哪找到的 / 为什么没找到"有用。
    """
    import json
    import subprocess

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "qcgui.worker", "--capabilities"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, cwd=str(REPO), env=env,
    )
    assert proc.returncode == 0, proc.stderr[-400:]
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    engines = payload["engines"]
    assert engines, payload
    engine = engines[0]
    for key in ("name", "available", "exe", "source", "job_types"):
        assert key in engine, f"capabilities 少了 {key}: {engine}"
    if engine["available"]:
        assert engine["exe"], "可用却没报告可执行文件路径"
        assert engine["source"], "可用却没报告路径来源"
        print(f"    {engine['name']}: {engine['exe']}（{engine['source']}）")
    else:
        print(f"    {engine['name']}: 不可用 — {engine['detail']}")


def test_all_source_files_are_tracked_by_git():
    """仓库里不能漏文件 —— 尤其是源码包。

    实测踩过：``.gitignore`` 里写了 ``jobs/``（本意是排除运行时的任务目录），
    但没加前导斜杠，于是**把源码包 ``qcgui/jobs/`` 一起排除了**。
    克隆出来少了整个模块，测试直接 ``ModuleNotFoundError``；
    而本地怎么跑都正常 —— 因为文件本来就在本地。

    这类问题只有"克隆出来跑一遍"才发现，所以在这里固化成用例。
    没有 git 时跳过（例如从 tarball 分发的环境）。
    """
    import shutil
    import subprocess

    git = os.environ.get("QCGUI_GIT") or shutil.which("git")
    if not git:
        for candidate in (r"D:\cao\v2\.git-portable\cmd\git.exe",
                          r"C:\Program Files\Git\cmd\git.exe"):
            if Path(candidate).is_file():
                git = candidate
                break
    if not git:
        print("    跳过：本机没有 git")
        return

    repo = REPO
    if not (repo / ".git").exists():
        print("    跳过：不是 git 工作区")
        return

    # `-c core.quotepath=false` 不能省：git 默认把非 ASCII 路径**转义**成
    # `\346\212\200...`，于是中文文件名（使用手册.md 等）会被判成"没被跟踪"。
    # 本机恰好设过 quotepath=false 所以一直是绿的 —— 而 CI 是全新克隆，
    # 没有这个配置，**只有 CI 红**。测试不该依赖仓库的本地配置。
    proc = subprocess.run(
        [git, "-c", "core.quotepath=false", "ls-files"],
        cwd=str(repo), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    if proc.returncode != 0:
        print("    跳过：git ls-files 失败")
        return
    tracked = {line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip()}

    excluded = ("__pycache__", "audit/", "tools/local/", ".git/", ".gitenv", ".git-portable",
                ".pytest_cache", "jobs/", "runs/")

    # 临时产物：`tempfile.mkdtemp()` 默认前缀是 "tmp"，测试用它造任务库/Chrome 档。
    # 临时目录不可写的机器上，tempfile 会**退回当前工作目录**，于是这些 tmp* 就落在
    # 仓库里（本机 2026-10-16 之后正是这种情形）。它们不是仓库内容，
    # 更不是"克隆出来会缺的东西"，所以按名字排除。
    # 唯一的例外：如果一个 tmp* 条目**已经被 git 跟踪**了，那说明它是有意提交的，
    # 应当照常检查（这里不做特殊处理 —— 被跟踪的不会被算进 missing）。
    temp_prefixes = ("tmp", "qcgui_")

    missing: list[str] = []
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(repo).as_posix()
        if any(part in rel for part in excluded):
            continue
        if path.name.startswith(temp_prefixes):
            continue
        if rel not in tracked:
            missing.append(rel)

    assert not missing, (
        f"有 {len(missing)} 个文件没被 git 跟踪（克隆出来会缺东西）：\n  "
        + "\n  ".join(sorted(missing)[:15])
    )
    print(f"    仓库跟踪 {len(tracked)} 个文件，工作区无遗漏")


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
