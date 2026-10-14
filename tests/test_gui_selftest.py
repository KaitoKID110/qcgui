"""端到端验收：整条链路「结构 → ORCA → JSON → 界面」。

跑的是 ``main.py --selftest`` 这个真实入口（会真的建窗口、真的跑 ORCA），
并对**界面实际显示出来的文本**做断言 —— 不是对内部对象做断言。

    pytest tests/test_gui_selftest.py -v
    python tests/test_gui_selftest.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WATER_HF_DEF2SVP = -75.960983978583

EXPECTED_MARKERS = (
    "引擎      : orca",
    "状态      : done",
    "最终能量",
    "偶极矩",
    "Mayer 键级",
    "产出文件",
    "界面进程是否导入量化引擎: False",
    "SELFTEST OK",
)


class _Skipped(Exception):
    pass


def _skip(reason: str):
    try:
        import pytest

        pytest.skip(reason)
    except ImportError:
        raise _Skipped(reason)


def _run_selftest(job: str = "单点能") -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["QCGUI_HOME"] = tempfile.mkdtemp(prefix="qcgui_gui_home_")
    return subprocess.run(
        [sys.executable, "main.py", "--selftest", "--job", job],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )


def test_gui_selftest_full_chain():
    if os.environ.get("QCGUI_SKIP_GUI_TESTS"):
        _skip("已通过 QCGUI_SKIP_GUI_TESTS 关闭界面测试")

    proc = _run_selftest()
    out = proc.stdout or ""

    if "no display" in (proc.stderr or "").lower() or "couldn't connect to display" in out.lower():
        _skip("没有可用的图形环境")

    assert proc.returncode == 0, f"自检失败 (exit {proc.returncode}):\n{out}\n{proc.stderr}"

    for marker in EXPECTED_MARKERS:
        assert marker in out, f"界面输出缺少 «{marker}»:\n{out}"

    # 能量必须是真实数值，且与引擎层测得的一致
    assert f"{WATER_HF_DEF2SVP:.10f}" in out, out
    print("    界面显示: 引擎/状态/能量/偶极/键级/产出文件 全部到位")
    print("    界面进程未导入量化引擎")


def test_optimization_chain_via_gui():
    if os.environ.get("QCGUI_SKIP_GUI_TESTS"):
        _skip("已通过 QCGUI_SKIP_GUI_TESTS 关闭界面测试")

    proc = _run_selftest(job="几何优化")
    out = proc.stdout or ""
    assert proc.returncode == 0, f"优化自检失败 (exit {proc.returncode}):\n{out}\n{proc.stderr}"
    assert "任务      : opt" in out, out
    assert "SELFTEST OK" in out, out
    print("    几何优化链路通过界面验收")


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
