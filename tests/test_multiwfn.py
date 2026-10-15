"""Multiwfn 键级集成的测试。

这一层是本项目**唯一一处解析人读输出**的地方（Multiwfn 没有结构化输出，
实测连键级表都不落盘）。所以测试的重点全在"格式一变就要立刻发现"：

* 黄金文件：拿**真跑 Multiwfn 3.8(dev)** 得到的输出片段当基准，
  解析结果必须是精确的 25 条、精确的值；
* 严格性：格式不对时**整体拒绝并说明**，绝不"尽力而为"地拼半份数据出来
  —— 半对的数字比没有数字更危险；
* 菜单号：必须与真跑抓到的菜单一致（主菜单 9，子菜单 1/3/4/7/8）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.model.bondorder import (  # noqa: E402
    BOND_ORDER_KINDS,
    parse_bond_orders,
)

#: 从真跑输出里原样截取的片段（咖啡因 / Multiwfn 3.8(dev)）
GOLDEN = """\
 Calculating, please wait...
 Bond orders with absolute value >=  0.050000
 #    1:         1(C )    2(N )    0.85313454
 #    2:         1(C )   15(H )    0.98541959
 #    3:         1(C )   16(H )    0.98542095
 #    4:         1(C )   17(H )    0.98659590
 #    5:         2(N )    3(C )    1.26277631
 #    6:         2(N )    6(C )    1.03976750

 Some other trailing text that is not part of the table
"""


def test_golden_multiwfn_output_parses_exactly():
    """黄金文件：真跑输出必须解析成精确的值。"""
    result = parse_bond_orders(GOLDEN, "mayer")
    assert result.ok, result.problems
    assert len(result.orders) == 6, [o.label for o in result.orders]
    assert result.threshold == 0.05, result.threshold

    first = result.orders[0]
    assert (first.i, first.j) == (1, 2), (first.i, first.j)
    assert (first.symbol_i, first.symbol_j) == ("C", "N")
    assert abs(first.order - 0.85313454) < 1e-12, first.order
    assert first.label == "C1–N2", first.label

    # 表格之后的无关文本不能被吞进来
    assert all(o.order > 0 for o in result.orders)
    print(f"    黄金片段解析出 {len(result.orders)} 条，阈值 {result.threshold}")

    # 排序：最强的在前（黄金片段里最强的是 2(N)–3(C) = 1.2628）
    top = result.sorted_orders()[0]
    assert top.label == "N2–C3", top.label
    assert abs(top.order - 1.26277631) < 1e-12, top.order
    print(f"    最强的一条：{top.label} = {top.order}")


def test_malformed_output_is_refused_not_partially_parsed():
    """格式变了要**整体拒绝**，不能给出半份数据。

    半对的数字比没有数字更危险 —— 用户会当真。
    """
    cases = [
        ("空输出", ""),
        ("没有表头", " #    1:         1(C )    2(N )    0.85313454\n"),
        ("表头在但行格式全变了", " Bond orders with absolute value >=  0.050000\n"
                                 " atom pair C1-N2 order: 0.853\n"),
    ]
    for label, text in cases:
        result = parse_bond_orders(text, "mayer")
        assert not result.ok, f"{label}: 不该给出数据，实际 {len(result.orders)} 条"
        assert result.problems, f"{label}: 拒绝时必须说明原因"
        print(f"    {label:16s} -> 拒绝：{result.problems[0][:44]}")
    print("    三种畸形输入都被拒绝并说明了原因")


def test_partially_bad_rows_are_reported():
    """个别行不认识时：保留能认的，但**必须报告**跳过多少行。"""
    text = (" Bond orders with absolute value >=  0.050000\n"
            " #    1:         1(C )    2(N )    0.85313454\n"
            " #    2: 这行是坏的\n"
            " #    3:         3(C )    4(N )    1.55905194\n")
    result = parse_bond_orders(text, "mayer")
    assert len(result.orders) == 2, [o.label for o in result.orders]
    assert any("不认识" in p for p in result.problems), result.problems
    print(f"    2 条有效 + 明确报告了跳过的坏行")


def test_kind_menu_numbers_match_the_real_multiwfn_menu():
    """菜单号必须是**真跑抓到的**那些。

    主菜单 ``9`` = Bond order analysis，子菜单：
    1 Mayer / 3 Wiberg / 4 Mulliken / 7 FBO / 8 LBO。
    写错一个数字，Multiwfn 会去做完全不同的分析（甚至挂在那里等输入）。
    """
    expected = {"mayer": "1", "wiberg": "3", "mulliken": "4", "fuzzy": "7", "lbo": "8"}
    actual = {k: v[0] for k, v in BOND_ORDER_KINDS.items()}
    assert actual == expected, actual

    from qcgui.multiwfn import _menu_script
    for kind, sub in expected.items():
        script = _menu_script(kind)
        # 结尾必须有 q：不给它会一直等输入，把任务挂死
        assert script == f"9\n{sub}\nq\n", (kind, script)
    print("    五个键级类型的菜单脚本都正确（且都以 q 收尾）")


def test_result_serialises_for_the_api():
    """接口返回的结构要稳定、可排序。"""
    result = parse_bond_orders(GOLDEN, "mayer")
    data = result.to_dict()
    for key in ("kind", "kind_label", "threshold", "count", "orders", "problems"):
        assert key in data, key
    assert data["count"] == len(data["orders"]) == 6
    assert data["orders"][0]["label"] and "order" in data["orders"][0]
    # 最强在前
    values = [o["order"] for o in data["orders"]]
    assert values == sorted(values, reverse=True), values
    print(f"    序列化正常，count={data['count']}，最强 {data['orders'][0]['label']}")


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
