"""pytest 全局配置：把临时目录钉死在可写位置，并保证测试结束就清掉。

为什么需要这个文件
------------------
这些测试大量用 ``tempfile.mkdtemp()`` 造任务库、SQLite 索引和 Chrome 用户档。
``tempfile`` 会**尊重** ``TEMP`` / ``TMP`` 环境变量 —— 本机（2026-10-16 那次
删除事故之后）``%LOCALAPPDATA%\\Temp`` 对进程不可写，于是 ``tempfile`` 悄悄
**退回当前工作目录**，把 ``qcgui_*`` 目录撒在仓库根里。

后果不只是脏：
* ``test_all_source_files_are_tracked_by_git`` 会把这几百个文件当成
  "没被 git 跟踪"，于是仓库看起来"克隆出来会缺东西"；
* 仓库根会越跑越脏，且**只在临时目录不可写的机器上出现** ——
  正是 ``test_portability`` 一直在防的那类"只在本机绿"的隐患。

所以这里显式把临时目录指到仓库内一个**已被忽略**的子目录，并在每次测试后
清理该测试创建的条目。不使用系统临时目录，是为了让"能不能写"这件事
**可控且可复现**。

放在 ``.pytest_cache/`` 下面是有意的：它已经在 .gitignore 里，而且
``test_all_source_files_are_tracked_by_git`` 也把它排除在外 ——
换一个新目录名（例如 ``.qcwork``）反而会被那个测试判成"未跟踪文件"。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TMP_ROOT = REPO / ".pytest_cache" / "tmp"


@pytest.fixture(scope="session", autouse=True)
def _pin_temp_dir():
    """整个测试会话期间把临时目录钉在仓库内可写处。"""
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    saved = {name: os.environ.get(name) for name in ("TEMP", "TMP", "TMPDIR")}
    os.environ["TEMP"] = str(TMP_ROOT)
    os.environ["TMP"] = str(TMP_ROOT)
    os.environ["TMPDIR"] = str(TMP_ROOT)
    tempfile.tempdir = str(TMP_ROOT)
    try:
        yield TMP_ROOT
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        tempfile.tempdir = None
        shutil.rmtree(TMP_ROOT, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _pin_writable_home():
    """把 qcgui 的数据目录钉在**可写**位置。

    为什么：``qcgui/paths.py`` 的数据目录顺序是 ``QCGUI_HOME`` →
    ``%LOCALAPPDATA%\\qcgui``。本机后者**不可写**（沙箱限制），于是

    * ``/api/config`` 写 ``config.json`` 直接 PermissionError → 500；
      ``test_audited_500_paths_are_now_4xx`` 就是被这个绊倒的（实测：不设
      ``QCGUI_HOME`` 时它是唯一剩下的失败）；
    * 其它要落盘的功能（Multiwfn 原始输出等）同样会失败。

    实测对照（同一份代码、262 个用例）：

    | ``QCGUI_HOME`` | 结果 |
    |---|---|
    | 设为可写的新库 | 259 passed / 3 skipped |
    | 不设（落到不可写的旧库） | **38 failed** / 221 passed / 3 skipped |

    **结果取决于一个环境变量设没设**，这正是 ``test_portability`` 一直在防的
    "只在本机绿"的隐患。所以在会话开始时显式钉一个可写目录：
    优先用 ``QCGUI_HOME``（若它可写），否则用仓库内被忽略的
    ``.pytest_cache/qcgui-home``。
    """
    def writable(path: Path) -> bool:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / "_write_probe.tmp"
            probe.write_text("x", encoding="ascii")
            probe.unlink()
            return True
        except OSError:
            return False

    saved = os.environ.get("QCGUI_HOME")
    candidate = Path(saved) if saved else None
    if candidate is None or not writable(candidate):
        candidate = REPO / ".pytest_cache" / "qcgui-home"
        if not writable(candidate):
            candidate = None
    if candidate is not None:
        os.environ["QCGUI_HOME"] = str(candidate)
        # `jobs/` 也要建出来：`_pin_real_jobs_root` 是按 `.is_dir()` 挑候选的，
        # 少了这一层它就会跳到下一个（不可写的）候选上去。
        (candidate / "jobs").mkdir(parents=True, exist_ok=True)
    try:
        yield candidate
    finally:
        if saved is None:
            os.environ.pop("QCGUI_HOME", None)
        else:
            os.environ["QCGUI_HOME"] = saved
        # 这个会话专用的数据目录用完就清 —— 除了别人传进来的那个（那是用户的）
        session_home = REPO / ".pytest_cache" / "qcgui-home"
        if (candidate is not None and candidate == session_home
                and saved != str(session_home)):
            shutil.rmtree(session_home, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _pin_real_jobs_root(_pin_writable_home):
    """把"真实任务库"钉在一个**可写**位置，避免测试依赖外部环境变量。

    为什么需要：几条浏览器用例会往真实任务目录里落 Multiwfn 的原始输出
    （项目刻意留的痕）。任务库的选择顺序是
    ``QCGUI_JOBS_ROOT`` → ``QCGUI_HOME/jobs`` → ``%LOCALAPPDATA%\\qcgui\\jobs``，
    其中最后一个是历史默认位置。本机它**不可写**（沙箱限制），

    * 设了 ``QCGUI_HOME`` 时指向新库 → 用例能真跑；
    * 忘了设时落到旧库 → 一堆用例因 PermissionError 失败，而失败原因与
      被测代码毫无关系。

    也就是说：**同一份代码，跑测试的结果取决于一个环境变量设没设**。
    这正是 ``test_portability`` 一直在防的那类"只在本机绿"的隐患。

    所以这里显式挑一个**可写**的候选并设进 ``QCGUI_JOBS_ROOT``（对所有进程
    生效，包括浏览器里跑的服务）。一个可写的都没有时，需要真实任务库的用例
    会自己 skip 并说明原因。
    """
    home = _pin_writable_home          # 依赖它，保证 QCGUI_HOME 已经钉好
    session_lib = Path(home) / "jobs" if home is not None else None

    def count_jobs(lib: Path) -> int:
        if not lib.is_dir():
            return 0
        return sum(1 for d in lib.iterdir() if d.is_dir())

    def has_gbw(lib: Path) -> bool:
        if not lib.is_dir():
            return False
        return any(d.is_dir() and any(d.glob("*.gbw")) for d in lib.iterdir())

    def writable(lib: Path) -> bool:
        probe = lib / "_write_probe.tmp"
        try:
            probe.write_text("x", encoding="ascii")
            probe.unlink()
            return True
        except OSError:
            return False

    # 只在**真的有任务数据**时才用外部库。空库会让"需要真实任务"的用例全部跳过，
    # 等于把测试削弱成空跑 —— 那样"绿"没有意义。所以：
    #   优先「可写 + 有任务」的外部候选（开发者机器上的真实任务库），
    #   都不可用时退回会话专属的空库（那些用例会带原因 skip，不静默通过）。
    candidates = []
    override = os.environ.get("QCGUI_JOBS_ROOT")
    if override:
        candidates.append(Path(override))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "qcgui" / "jobs")
    candidates.append(REPO / "jobs")

    saved = os.environ.get("QCGUI_JOBS_ROOT")
    picked = None
    picked_kind = "会话专属空库（真实任务用例会带原因跳过）"
    for cand in candidates:
        if count_jobs(cand) == 0 or not writable(cand):
            continue
        picked = cand
        picked_kind = f"真实任务库（{count_jobs(cand)} 个任务，有 .gbw: {has_gbw(cand)}）"
        break
    if picked is None:
        picked = session_lib

    if picked is not None:
        os.environ["QCGUI_JOBS_ROOT"] = str(picked)
    print(f"\n[tests] 任务库 = {picked}  —— {picked_kind}")
    try:
        yield picked
    finally:
        if saved is None:
            os.environ.pop("QCGUI_JOBS_ROOT", None)
        else:
            os.environ["QCGUI_JOBS_ROOT"] = saved


@pytest.fixture(autouse=True)
def _clean_temp_after_each_test():
    """每个测试结束后，只清掉**它自己新建**的临时条目。

    两条经验教训：
    * 不能无差别清空整个临时目录 —— 取消类用例（``test_cancel_kills_worker_and_children``）
      会留下还在跑的 worker 进程，它手上的 SQLite 被删掉就会报
      ``no such table: jobs``；
    * 也不能完全不清 —— 同一次会话里跑上百个用例会积下几千个目录，
      既拖慢后续用例，也会把"未跟踪文件"这类判断带偏。

    所以按名字取差集：测试前记下已有的条目，测试后只删新增的。
    """
    before = {p.name for p in TMP_ROOT.iterdir()} if TMP_ROOT.is_dir() else set()
    yield
    if not TMP_ROOT.is_dir():
        return
    for entry in TMP_ROOT.iterdir():
        if entry.name in before:
            continue
        # pytest 自己的 tmp_path 基目录（pytest-of-<user> / pytest-N）**跨用例复用**，
        # 删掉它会让后续所有用 tmp_path 的用例报 FileNotFoundError。
        # 这是实测踩过的坑，不是理论风险。
        if entry.name.startswith(("pytest-of-", "pytest-")):
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            pass
