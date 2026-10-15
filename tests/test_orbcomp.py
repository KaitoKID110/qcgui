"""轨道成分分析（Multiwfn 菜单 8）的测试。

同样属于"本项目唯一的解析人读输出"那一类，所以重点在**格式漂移要立刻发现**：
黄金文件用真跑 Multiwfn 3.8(dev) 的输出，加上严格性（格式不对就整体拒绝）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.model.orbcomp import (  # noqa: E402
    ORBITAL_CHOICES,
    ORBITAL_PARTITIONS,
    parse_orbital_composition,
)

#: 从真跑输出里原样截取（咖啡因 HOMO，Multiwfn 3.8(dev)）
GOLDEN = """\
 Orbital composition analysis with Mulliken partition
 Orbital:    51  Energy(a.u.):     -0.319356  Occ:  2.000000  Type: Alpha&Beta
 Printing threshold of absolute value:  >   0.50000 %

 Basis Type    Atom    Shell      Local       Cross term        Total
   20   Z        2(N )   10      3.40768 %      1.20955 %      4.61724 %
   76   Z        6(C )   34     10.48203 %      5.30060 %     15.78264 %

 Composition of each shell
 Shell    10 Type: P    in atom    2(N ) :     4.91595 %
 Shell    11 Type: P    in atom    2(N ) :     2.83546 %
 Shell    34 Type: P    in atom    6(C ) :    16.72766 %
 Shell    35 Type: P    in atom    6(C ) :    13.28508 %
 Shell    46 Type: P    in atom    8(O ) :     3.66564 %
"""


def test_golden_orbital_composition_parses_exactly():
    """黄金文件：真跑输出必须解析成精确的数值。"""
    result = parse_orbital_composition(GOLDEN, "mulliken", "homo")
    assert result.ok, result.problems
    assert result.orbital_index == 51, result.orbital_index
    assert abs(result.energy_au - (-0.319356)) < 1e-9, result.energy_au
    assert abs(result.occupancy - 2.0) < 1e-9, result.occupancy
    assert result.threshold == 0.5, result.threshold

    # 逐 shell 必须**按原子汇总**（这才是"主要在哪几个原子上"）
    assert len(result.atoms) == 3, [a.to_dict() for a in result.atoms]
    top = result.sorted_atoms()[0]
    assert (top.index, top.symbol) == (6, "C"), (top.index, top.symbol)
    # 16.72766 + 13.28508
    assert abs(top.percent - 30.01274) < 1e-6, top.percent
    assert len(top.shells) == 2, top.shells

    n2 = next(a for a in result.atoms if a.index == 2)
    assert abs(n2.percent - (4.91595 + 2.83546)) < 1e-6, n2.percent
    print(f"    3 个原子，最强的 {top.symbol}{top.index} = {top.percent:.2f}%"
          f"（{len(top.shells)} 个 shell 汇总）")

    # 排序：贡献大的在前
    percents = [a.percent for a in result.sorted_atoms()]
    assert percents == sorted(percents, reverse=True), percents
    print(f"    排序正确：{percents}")


def test_malformed_output_is_refused():
    """格式不对要整体拒绝并说明 —— 半份数据比没有数据更危险。"""
    cases = [
        ("空输出", ""),
        ("没有轨道头", " Composition of each shell\n Shell 10 Type: P in atom 2(N ) : 4.9 %\n"),
        ("有轨道头但没有 shell 段",
         " Orbital:    51  Energy(a.u.):     -0.319356  Occ:  2.000000  Type: Alpha&Beta\n"),
    ]
    for label, text in cases:
        result = parse_orbital_composition(text, "mulliken", "homo")
        assert not result.ok, f"{label}: 不该给出数据"
        assert result.problems, f"{label}: 拒绝时必须说明原因"
        print(f"    {label:22s} -> 拒绝：{result.problems[0][:42]}")


def test_menu_numbers_and_orbital_tokens_are_correct():
    """菜单号与轨道代号必须是**真跑确认过**的那些。

    写错一个数字，Multiwfn 会做完全不同的分析，或者停在那里等输入。
    """
    assert {k: v[0] for k, v in ORBITAL_PARTITIONS.items()} == \
        {"mulliken": "1", "stout": "2", "scpa": "3"}
    assert {k: v[0] for k, v in ORBITAL_CHOICES.items()} == \
        {"homo": "h", "lumo": "l", "homo-1": "h-1", "lumo+1": "l+1", "all": "a"}

    from qcgui.multiwfn import run_orbital_composition
    import inspect
    source = inspect.getsource(run_orbital_composition)
    # 菜单 8 + 分区方式 + 轨道 + q 收尾
    assert 'f"8\\n{sub}\\n{orbital}\\nq\\n"' in source, "菜单脚本不对"
    print("    菜单 8 + 子菜单 1/2/3 + 轨道代号 + q 收尾，全部正确")


def test_serialises_for_the_api():
    """接口返回的结构要直接能用（含给 3D 图用的 top_label）。"""
    data = parse_orbital_composition(GOLDEN, "mulliken", "homo").to_dict()
    for key in ("orbital_index", "energy_au", "energy_ev", "occupancy", "partition",
                "request", "count", "atoms", "top_label", "problems"):
        assert key in data, key
    assert data["count"] == 3
    assert abs(data["energy_ev"] - (-0.319356 * 27.211386245988)) < 1e-6
    # 最强的在最前面
    assert data["atoms"][0]["symbol"] == "C"
    assert data["atoms"][0]["percent"] > data["atoms"][1]["percent"]
    assert "C6" in data["top_label"], data["top_label"]
    print(f"    序列化正常：{data['top_label']}")


# ---------------------------------------------------------------- 脚本模式

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
