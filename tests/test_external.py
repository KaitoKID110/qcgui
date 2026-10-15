"""外部工具集成测试（Multiwfn / Avogadro2 / obabel）。

重点验证**真的能调用起来**，而不只是拼对了命令行：

- Multiwfn 走非交互批量接口，检查它确实回显 ``Loaded ... successfully!``
- Avogadro2 真的启动进程，确认后立刻关掉
- 路径探测与"配置写错时回退"的行为

    pytest tests/test_external.py -v
    python tests/test_external.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui import external  # noqa: E402
from qcgui.config import detect_tool, load_config, resolve_tool, tool_status  # noqa: E402

WATER_XYZ = """3
water
O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
"""


class _Skipped(Exception):
    pass


def _skip(reason: str):
    try:
        import pytest

        pytest.skip(reason)
    except ImportError:
        raise _Skipped(reason)


def _water_file() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="qcgui_ext_"))
    path = directory / "water.xyz"
    path.write_text(WATER_XYZ, encoding="ascii")
    return path


# ------------------------------------------------------------------ 路径探测

def test_tool_status_has_all_three():
    status = tool_status()
    assert set(status) == {"multiwfn", "avogadro", "obabel"}
    for name, entry in status.items():
        assert set(entry) >= {"path", "available", "source"}
        print(f"    {name:<10} {entry['available']}  {entry['path'] or '-'}  ({entry['source']})")


def test_resolve_falls_back_when_configured_path_is_wrong():
    """配置里写了不存在的路径时，应回退到自动探测并在 source 里说明。"""
    bogus = {"multiwfn_path": "Z:/definitely/not/here.exe"}
    path, source = resolve_tool("multiwfn", bogus)
    if detect_tool("multiwfn"):
        assert path and path != "Z:/definitely/not/here.exe"
        assert "回退" in source, source
        print(f"    已回退: {source} -> {path}")
    else:
        assert path is None
        assert "不存在" in source
        print(f"    正确报告缺失: {source}")


def test_missing_tool_gives_actionable_message():
    """工具缺失时，报错要说明去哪里改，而不是抛异常。

    .. note::
       本机装了 Avogadro 时，这里其实测的是"**文件**不存在"；
       CI 上没装，才真的走到"**工具**缺失"分支。两种都必须是失败对象 +
       可操作的消息，所以断言放宽到"说了问题在哪"，并且**带上消息** ——
       否则 CI 只显示一句空的 AssertionError，没法定位。
    """
    result = external.view_with_avogadro("Z:/nope.xyz")
    assert result.ok is False, f"应当失败，实际 ok={result.ok}"
    # 不要写死工具 id：装了 Avogadro 时是 "avogadro"，**没装时返回的是显示名
    # "Avogadro2"**（实测 CI 上报出来的就是这个）。这里只要求它能指出是哪个工具。
    assert "avogadro" in str(result.tool).lower(), f"tool 字段不对：{result.tool!r}"
    assert result.message, "失败时必须给出说明"
    low = result.message.lower()
    assert ("找不到" in result.message or "不存在" in result.message
            or "avogadro" in low), f"消息没有指出问题在哪：{result.message!r}"
    print(f"    {result.message}")


# ------------------------------------------------------------------ Multiwfn

def test_multiwfn_batch_actually_loads_file():
    """真跑一次 Multiwfn（非交互），确认它回显载入成功。"""
    exe, _ = resolve_tool("multiwfn")
    if not exe:
        _skip("本机没有 Multiwfn")

    path = _water_file()
    result, output = external.run_multiwfn_batch(path, commands="q\n", timeout=90)

    assert "Loaded" in output, f"Multiwfn 输出里没有载入信息：\n{output[-800:]}"
    assert path.name in output
    assert result.tool == "multiwfn"
    print("    Multiwfn 非交互载入成功：" +
          next((l.strip() for l in output.splitlines() if "Loaded" in l), ""))


def test_multiwfn_reports_missing_file():
    """文件不存在时必须给出可操作的说明。

    .. note::
       本机装了 Multiwfn 时走"文件不存在"分支；**CI 上没装 Multiwfn**，
       代码正确地先报"工具缺失"。两种都是合理的失败，断言必须都能过 ——
       以前写死了"不存在"，于是只有 CI 红。
    """
    result = external.run_multiwfn(Path(tempfile.gettempdir()) / "not-here.xyz")
    assert result.ok is False, f"应当失败，实际 ok={result.ok}"
    assert result.message, "失败时必须给出说明"
    assert ("不存在" in result.message or "找不到" in result.message
            or "Multiwfn" in result.message), f"消息没有指出问题在哪：{result.message!r}"
    print(f"    {result.message}")


# ------------------------------------------------------------------ Avogadro2

def test_avogadro_actually_launches():
    """真的把 Avogadro2 拉起来，确认进程存在后立刻关掉。"""
    exe, _ = resolve_tool("avogadro")
    if not exe:
        _skip("本机没有 Avogadro2")

    path = _water_file()
    result = external.view_with_avogadro(path)
    assert result.ok, result.message
    assert result.pid, "没有拿到进程 ID"

    try:
        time.sleep(2.0)
        alive = subprocess.run(
            ["tasklist", "/FI", f"PID eq {result.pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace",
        ).stdout
        assert str(result.pid) in alive, f"进程未存活:\n{alive}"
        print(f"    Avogadro2 已启动 (PID {result.pid})")
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(result.pid)],
                       capture_output=True)


# ------------------------------------------------------------------ 其它

def test_obabel_missing_source():
    result = external.convert_with_obabel("Z:/nope.cif", "Z:/out.xyz")
    assert result.ok is False
    assert "不存在" in result.message or "找不到" in result.message
    print(f"    {result.message}")


def test_open_folder_rejects_missing_path():
    result = external.open_in_file_manager("Z:/no/such/dir")
    assert result.ok is False
    print(f"    {result.message}")


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
