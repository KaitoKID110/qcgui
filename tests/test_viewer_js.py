"""3D 分子查看器（viewer.js）的逻辑测试。

用 Node 无头驱动 ``viewer.js``（见 ``viewer_harness.js``），验证**计算逻辑**：
成键推断、轨迹插值、元素表、电荷着色。渲染本身靠浏览器截图人工确认。

没有 Node 时整体跳过。

    pytest tests/test_viewer_js.py -v
    python tests/test_viewer_js.py
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "tests" / "viewer_harness.js"
STATIC = REPO / "qcgui" / "web" / "static"


class _Skipped(Exception):
    pass


def _skip(reason: str):
    try:
        import pytest

        pytest.skip(reason)
    except ImportError:
        raise _Skipped(reason)


def find_node() -> str | None:
    """找 node：环境变量 → PATH → DSH 运行时目录。"""
    explicit = os.environ.get("QCGUI_NODE")
    if explicit and Path(explicit).is_file():
        return explicit
    found = shutil.which("node")
    if found:
        return found
    pattern = str(Path.home() / ".dsh" / "dsh-runtimes" / "*" / "dependencies" / "node" / "bin" / "node.exe")
    matches = sorted(glob.glob(pattern))
    return matches[0] if matches else None


def _node() -> str:
    node = find_node()
    if not node:
        _skip("找不到 node，跳过查看器逻辑测试")
    return node


def run_case(name: str) -> dict:
    proc = subprocess.run(
        [_node(), str(HARNESS), name],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO), timeout=60,
    )
    assert proc.returncode == 0, f"harness 失败:\n{proc.stdout}\n{proc.stderr}"
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "error" not in data, data["error"]
    return data


# ------------------------------------------------------------------ 语法

def test_javascript_syntax_is_valid():
    node = _node()
    for name in ("viewer.js", "app.js"):
        path = STATIC / name
        proc = subprocess.run([node, "--check", str(path)],
                              capture_output=True, text=True, errors="replace")
        assert proc.returncode == 0, f"{name} 语法错误:\n{proc.stderr}"
    print("    viewer.js 与 app.js 语法检查通过")


# ------------------------------------------------------------------ 逻辑

def test_bond_inference_on_water():
    data = run_case("bonds")
    assert data["n_atoms"] == 3
    assert sorted(tuple(b) for b in data["bonds"]) == [(0, 1), (0, 2)], data["bonds"]
    assert data["radius"] > 0
    print(f"    水: {data['n_atoms']} 原子，推断出 {len(data['bonds'])} 根键，半径 {data['radius']:.3f} Å")


def test_bond_cutoff_rejects_far_atoms():
    data = run_case("bond_cutoff")
    assert data["bonded"] == 1, "1.54 Å 的 C–C 应当成键"
    assert data["far"] == 0, "6.0 Å 的两个碳不应成键"
    print("    键长判据正确（1.54 Å 成键 / 6.0 Å 不成键）")


def test_trajectory_interpolation():
    data = run_case("interpolate")
    assert data["n_frames"] == 2
    assert abs(data["y_half"] - 1.1) < 1e-9, data
    print(f"    两帧之间插值正确：0.5 帧 y = {data['y_half']:.3f}")


def test_element_tables():
    data = run_case("elements")
    for symbol, present in data["has"]:
        assert present, f"元素表缺少 {symbol}"
    assert data["carbon_color"] != [0, 0, 0], "碳的颜色不能是纯黑（深色背景下看不见）"
    print("    元素表覆盖常见元素，碳色在深色背景下可见")


def test_empty_input_does_not_throw():
    data = run_case("empty")
    assert data["ok"] and data["atoms"] == 0
    print("    空数据/越界操作不抛异常")


def test_charge_coloring():
    data = run_case("charge_colors")
    neg, pos = data["neg"], data["pos"]
    assert neg[0] > neg[2], f"负电荷应偏红（R>B），得到 {neg}"
    assert pos[2] > pos[0], f"正电荷应偏蓝（B>R），得到 {pos}"
    print(f"    电荷着色正确：负电 {neg} / 正电 {pos}")


def test_rotation_changes_projection():
    """绕 Y、绕 X 旋转都必须真的改变投影坐标。"""
    data = run_case("rotation")
    before, after_y, after_x = data["before"], data["afterY"], data["afterX"]
    dist = lambda a, b: sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5
    assert dist(before, after_y) > 0.1, f"绕 Y 旋转没生效: {before} -> {after_y}"
    assert dist(before, after_x) > 0.1, f"绕 X 旋转没生效: {before} -> {after_x}"
    print(f"    旋转生效（绕 Y 位移 {dist(before, after_y):.2f} Å，绕 X {dist(before, after_x):.2f} Å）")


def test_zoom_and_pan():
    data = run_case("zoom")
    assert abs(data["s2"] / data["s1"] - 2.0) < 1e-9, data
    assert data["s1"] > 0
    # 平移量应当反映到投影中心
    assert data["cx"] > 400, data
    assert data["cy"] < 300, data
    print(f"    缩放线性（×2 → scale ×{data['s2'] / data['s1']:.3f}），平移生效")


def test_display_styles_differ():
    data = run_case("styles")
    assert data["spacefill"] > data["ballstick"] > data["stick"] > data["wireframe"], data
    assert abs(data["spacefill"] - data["vdw_carbon"]) < 1e-9, data
    print("    四种模式半径关系正确：空间填充 > 球棍 > 棍状 > 线框")


def test_render_emits_draw_calls():
    """渲染管线必须真的发出绘制调用，而不是静默什么都不画。"""
    data = run_case("render_draw_calls")
    assert data["arcs"] >= data["n_atoms"], data
    assert data["strokes"] >= data["n_bonds"], data
    assert data["fills"] > 0
    print(f"    渲染发出 {data['arcs']} 个圆、{data['strokes']} 次描边、{data['fills']} 次填充")


def test_render_empty_does_not_throw():
    data = run_case("render_empty")
    assert data["ok"], data
    print("    空结构渲染只画提示文字，不抛异常")


def test_playback_state_machine():
    data = run_case("playback_state")
    assert data["idle"] is False
    assert data["playing"] is True, "play() 之后应处于播放状态"
    assert data["paused"] is False, "pause() 之后应停止"
    print("    播放/暂停状态切换正确")


def test_scan_block_construction():
    """扫描块拼装：正常输入拼对，坏输入给可读报错。

    拼错一个原子编号或坐标字母，ORCA 要么直接报错、要么算出一个不是你要的
    东西 —— 所以这段逻辑单独验。
    """
    data = run_case("scan_block")
    assert data["block_name"] == "geom", data
    lines = data["lines"]
    assert lines[0] == "Scan", lines
    assert lines[1] == "  D 3 0 1 2 = 0, 120, 13", lines
    assert lines[2] == "end", lines
    # 步长判据要放宽、迭代上限要提高，否则扫描容易卡在第 1 步
    assert "MaxIter 250" in lines, lines
    assert any("TolRMSD" in l for l in lines), lines
    # 逗号分隔的输入也要认
    assert data["comma_ok"].startswith("B 0 1 ="), data["comma_ok"]

    bad = data["bad"]
    assert "没有报错" not in bad.values(), f"有坏输入没被拦下: {bad}"
    for key, message in bad.items():
        assert message and len(message) > 4, f"{key} 的报错太含糊: {message!r}"
    print(f"    正常拼装正确；{len(bad)} 种坏输入全部被拦下并给出说明")


def test_labels_are_drawn_with_an_outline():
    """原子编号必须带描边。

    实测踩过：原来用"黑字打底、白字错开半像素"的假阴影，压在跟文字同色系的
    球面上完全读不出来，看着就像"编号功能没做"。
    """
    data = run_case("labels_have_outline")
    assert data["on_stroke"] > 0, f"开了编号却没有描边调用：{data}"
    assert data["on_fill"] > 0, f"开了编号却没有填字调用：{data}"
    assert data["off_stroke"] == 0 and data["off_fill"] == 0, f"关掉编号还在画字：{data}"
    print(f"    编号：{data['on_stroke']} 次描边 + {data['on_fill']} 次填字；关闭时 0 次")


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
