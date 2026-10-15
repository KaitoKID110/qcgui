"""键级分析（Multiwfn）的结果模型与解析。

.. important::
   **这是本项目对"只用结构化输出"这条架构约束的唯一一次例外**，理由如下：

   * ORCA 有 ``orca_2json``，所以我们从不解析 ORCA 的人读日志；
   * 但 **Multiwfn 根本没有结构化输出** —— 实测它连键级表都不落盘
     （跑 ``9 → 1`` 后任务目录里一个新文件都没有），只把表格打到 stdout。
     想用它就只能读 stdout。

   所以这里把风险压到最低：

   1. 只解析它的**定宽表格转储**（``#    1:    1(C )    2(N )   0.85313454``），
      不是模糊的正则捞数字。格式不对就**整体拒绝**（返回空并说明），
      绝不猜、绝不"尽力而为"地拼半份结果出来；
   2. 原始输出**原样落盘**（``bondorder_mayer.txt``），解析结果可追溯到它；
   3. 带**黄金文件测试**，Multiwfn 升级改了格式会立刻红，而不是悄悄给出错数据。

   这一层是纯模型，不导入任何量子引擎（架构第 1 条）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 支持的分析类型 → （Multiwfn 子菜单号，中文名，落盘文件名）。
#: 菜单号是通过真跑 Multiwfn 抓主菜单确认的（``9 Bond order analysis``）。
BOND_ORDER_KINDS: dict[str, tuple[str, str, str]] = {
    "mayer": ("1", "Mayer 键级", "bondorder_mayer.txt"),
    "wiberg": ("3", "Wiberg 键级（Löwdin 正交化基）", "bondorder_wiberg.txt"),
    "mulliken": ("4", "Mulliken 键级", "bondorder_mulliken.txt"),
    "fuzzy": ("7", "模糊键级 FBO", "bondorder_fuzzy.txt"),
    "lbo": ("8", "Laplacian 键级 LBO", "bondorder_lbo.txt"),
}

#: 一行键级记录：``#    1:         1(C )    2(N )    0.85313454``
#: 原子序号是 1 起的；元素符号可能带宽度填充；键级可正可负、可用科学计数。
_ROW = re.compile(
    r"^\s*#\s*(\d+)\s*:\s*"          # 序号
    r"(\d+)\s*\(\s*([A-Za-z]{1,3})\s*\)\s*"   # 原子 1：(序) 元素
    r"(\d+)\s*\(\s*([A-Za-z]{1,3})\s*\)\s*"   # 原子 2
    r"(-?\d+\.\d+(?:[eEdD][-+]?\d+)?)\s*$"     # 键级
)

#: 表格起始行。必须见到它才认为"这段输出里有键级表"。
_TABLE_HEADER = re.compile(r"Bond orders with absolute value\s*>=?\s*([\d.]+)")


@dataclass
class BondOrder:
    """一对原子之间的键级（原子序号是**1 起**的，与 Multiwfn/ORCA 输入一致）。"""

    i: int
    j: int
    symbol_i: str
    symbol_j: str
    order: float

    @property
    def label(self) -> str:
        return f"{self.symbol_i}{self.i}–{self.symbol_j}{self.j}"

    def to_dict(self) -> dict:
        return {
            "i": self.i, "j": self.j,
            "symbol_i": self.symbol_i, "symbol_j": self.symbol_j,
            "order": self.order, "label": self.label,
        }


@dataclass
class BondOrderResult:
    """一次键级分析的完整结果。"""

    kind: str = "mayer"
    kind_label: str = ""
    threshold: float | None = None
    orders: list[BondOrder] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.orders)

    def sorted_orders(self, strongest_first: bool = True) -> list[BondOrder]:
        return sorted(self.orders, key=lambda o: o.order, reverse=strongest_first)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "kind_label": self.kind_label,
            "threshold": self.threshold,
            "count": len(self.orders),
            "orders": [o.to_dict() for o in self.sorted_orders()],
            "problems": list(self.problems),
        }


def parse_bond_orders(text: str, kind: str = "mayer") -> BondOrderResult:
    """从 Multiwfn 的 stdout 里解析键级表。

    **严格**：必须找到表头 ``Bond orders with absolute value >= x``，
    且至少解析出一行；否则返回空结果并在 ``problems`` 里说明原因。
    宁可交白卷，也不给出半对的数据 —— 半对的数字比没有数字更危险。
    """
    label = BOND_ORDER_KINDS.get(kind, (None, kind, None))[1]
    result = BondOrderResult(kind=kind, kind_label=label)
    if not text:
        result.problems.append("Multiwfn 没有输出任何内容")
        return result

    lines = text.splitlines()
    header_at = None
    for index, line in enumerate(lines):
        match = _TABLE_HEADER.search(line)
        if match:
            header_at = index
            try:
                result.threshold = float(match.group(1))
            except ValueError:
                result.threshold = None
            break
    if header_at is None:
        result.problems.append(
            "输出里没有找到键级表（缺 'Bond orders with absolute value >=' 表头）—— "
            "可能是 Multiwfn 版本不同，或这次分析没有跑到表格那一步")
        return result

    bad: list[str] = []
    for line in lines[header_at + 1:]:
        if not line.strip():
            # 表格结束：后面通常还有别的内容
            if result.orders:
                break
            continue
        if line.lstrip().startswith("#"):
            match = _ROW.match(line)
            if match is None:
                bad.append(line.strip()[:60])
                continue
            seq, i, si, j, sj, value = match.groups()
            try:
                order = float(value.replace("D", "E").replace("d", "e"))
            except ValueError:
                bad.append(line.strip()[:60])
                continue
            result.orders.append(BondOrder(int(i), int(j), si, sj, order))
        elif result.orders:
            break

    if bad:
        result.problems.append(f"{len(bad)} 行的格式不认识，已跳过（例如：{bad[0]}）")
    if not result.orders:
        result.problems.append("表格里一行都没解析出来 —— 格式可能变了")
    return result


def bond_order_summary(orders: list[BondOrder]) -> dict:
    """给界面用的小结：最强/最弱的键、平均键级。"""
    if not orders:
        return {}
    strongest = max(orders, key=lambda o: o.order)
    weakest = min(orders, key=lambda o: o.order)
    return {
        "count": len(orders),
        "strongest": strongest.to_dict(),
        "weakest": weakest.to_dict(),
        "mean": sum(o.order for o in orders) / len(orders),
    }
