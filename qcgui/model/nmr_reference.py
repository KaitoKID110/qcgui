"""NMR 参考物与化学位移（δ）换算。

为什么需要
----------
ORCA 的输出是**绝对屏蔽常数 σ(ppm)**，而文献/实验报的是**化学位移 δ(ppm)**：

    δ = σ(参考物) − σ(样品)

参考物必须是**同一方法、同一基组、同一溶剂现算**的 —— 不同方法的 σ 绝对标度
可以差几十 ppm，直接套文献常数会得到"看起来对其实错"的 δ。

实测（HF/STO-3G，TMS）：
  * 用随手摆的甲基构象，等效核 σ 极差可达 C 10.6 ppm / H 1.2 ppm；
  * **先优化再算**，极差降到 C 0.77 ppm / H 0.17 ppm —— 所以参考值必须先优化。
  * TMS 现算结果：H σ=33.713、C σ=248.638、Si σ=577.438 ppm。

因此这里的做法是：内置参考物几何 → 用调用方的方法/基组**先优化、再算 NMR**
→ 按元素对所有等效核的 σ 取平均作为参考值。
"""
from __future__ import annotations

#: TMS（四甲基硅烷）初始几何：Si–C 1.87 Å、C–H 1.09 Å，四个甲基呈四面体。
#: 只是**起点**，实际使用前会先用同样的方法/基组优化，见 workflow.py。
TMS_XYZ = """17
TMS
Si 0.000000 0.000000 0.000000
C 1.079645 1.079645 1.079645
H 0.869874 1.596541 0.143208
H 0.143208 0.869874 1.596541
H 1.596541 0.143208 0.869874
C 1.079645 -1.079645 -1.079645
H 0.467597 -1.708723 -0.433303
H 0.433303 -0.467597 -1.708723
H 1.708723 -0.433303 -0.467597
C -1.079645 1.079645 -1.079645
H -1.575938 0.124223 -0.909462
H -0.909462 1.575938 -0.124223
H -0.124223 0.909462 -1.575938
C -1.079645 -1.079645 1.079645
H -1.706855 -0.399982 0.502787
H -0.502787 -1.706855 0.399982
H -0.399982 -0.502787 1.706855
"""

#: 内置参考物表。键是给界面/接口用的标识，值是几何与它覆盖的元素。
#: 目前只收录最常用的 TMS（¹H / ¹³C / ²⁹Si）。
#: 别的核（¹⁵N 用硝基甲烷、¹⁹F 用 CFCl₃、³¹P 用 H₃PO₄）需要各自的参考物几何，
#: 没有收录时宁可**不给 δ**，也不拿错参考物凑数。
REFERENCE_MOLECULES: dict[str, dict] = {
    "tms": {
        "label": "TMS（四甲基硅烷）",
        "xyz": TMS_XYZ,
        "elements": ("H", "C", "Si"),
        "covers": "¹H / ¹³C / ²⁹Si",
    },
}


def reference_scheme_label(scheme: str) -> str:
    """参考方案的人类可读名称。"""
    info = REFERENCE_MOLECULES.get((scheme or "").strip().lower())
    return info["label"] if info else (scheme or "无")


def average_shieldings(nmr_rows: list[dict]) -> dict[str, float]:
    """把逐核 σ 按元素求平均，作为参考值。

    为什么要平均：TMS 里 12 个 H、4 个 C 化学等价，但实际几何有微小偏差，
    逐核 σ 会差一点点（实测优化后仍差 0.16 / 0.77 ppm）。取平均比随便挑一个稳。
    """
    buckets: dict[str, list[float]] = {}
    for row in nmr_rows or []:
        element = str(row.get("element") or "")
        value = row.get("isotropic")
        if not element or value is None:
            continue
        try:
            buckets.setdefault(element, []).append(float(value))
        except (TypeError, ValueError):
            continue
    return {el: sum(vals) / len(vals) for el, vals in buckets.items() if vals}


def compute_deltas(rows: list[dict],
                   references: dict[str, float]) -> tuple[list[dict], list[str]]:
    """给每个核算 δ = σ(参考) − σ(样品)。

    返回 ``(新 rows, 缺参考的元素列表)``。

    **没有参考值的元素一律不给 δ** —— 拿别的核的参考值顶上会得到完全错误的数字。
    """
    refs = {str(k): float(v) for k, v in (references or {}).items() if v is not None}
    missing: list[str] = []
    out: list[dict] = []
    for row in rows or []:
        item = dict(row)
        element = str(item.get("element") or "")
        ref = refs.get(element)
        if ref is None:
            if element and element not in missing:
                missing.append(element)
        else:
            try:
                item["delta"] = ref - float(item["isotropic"])
                item["reference_shielding"] = ref
            except (KeyError, TypeError, ValueError):
                if element and element not in missing:
                    missing.append(element)
        out.append(item)
    return out, missing
