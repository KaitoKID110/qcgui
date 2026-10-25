"""方法别名表：把"化学上常用的名字"翻译成 ORCA 真正接受的写法。

为什么需要
----------
有些方法（尤其是专门为 NMR 拟合的泛函）不是 ORCA 的简单关键字，而是通过
LibXC 提供：

    !  <basis> <其它关键字>
    %method
      Functional hyb_gga_xc_wp04
    end

如果直接把 ``! WP04`` 写进输入行，ORCA 会报 ``INPUT ERROR``（实测）。
所以这里统一做一层翻译：别名 → (LibXC 名, 说明)。

实测确认（ORCA 6.1.1，内置 LibXC 7.0.0）
-----------------------------------------
* ``hyb_gga_xc_wp04``：ORCA 输出 "Using LibXC functional ID 612 : hybrid fitted
  to proton NMR shifts" —— 即 Cramer 等为 **¹H NMR 化学位移**拟合的 WP04；
* ``hyb_gga_xc_wc04``：同一系列里为 **¹³C NMR** 拟合的 WC04。

用法
----
``JobSpec.method`` 直接写别名（如 ``"WP04"``）即可，引擎会自动改成
``%method`` 块并把方法名从 ``!`` 行移除。大小写不敏感。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LibxcMethod:
    """一个通过 LibXC 提供的方法。"""

    alias: str
    libxc_name: str
    description: str


#: 别名（大写）→ LibXC 方法。键都用大写，查表时统一 upper()。
LIBXC_METHODS: dict[str, LibxcMethod] = {
    "WP04": LibxcMethod(
        alias="WP04",
        libxc_name="hyb_gga_xc_wp04",
        description="WP04（LibXC 612）：为 ¹H NMR 化学位移拟合的混合泛函",
    ),
    "WC04": LibxcMethod(
        alias="WC04",
        libxc_name="hyb_gga_xc_wc04",
        description="WC04：为 ¹³C NMR 化学位移拟合的混合泛函",
    ),
}


def resolve_method(method: str) -> LibxcMethod | None:
    """方法名是否是一个 LibXC 别名（大小写不敏感）。"""
    return LIBXC_METHODS.get((method or "").strip().upper())


def method_descriptions() -> list[dict]:
    """给界面用的方法说明列表。"""
    return [
        {"alias": m.alias, "libxc_name": m.libxc_name,
         "description": m.description}
        for m in LIBXC_METHODS.values()
    ]


def suggested_basis(method: str) -> str:
    """该方法推荐的基组（NMR 屏蔽常数对基组敏感，用专门基组）。

    pcSseg-n 是 Jensen 专门为 NMR 屏蔽常数优化的基组族（实测本机 ORCA 支持
    ``pcSseg-2``）。没有专门要求时给出它作为建议，用户仍可自行改。
    """
    alias = (method or "").strip().upper()
    if alias in ("WP04", "WC04"):
        return "pcSseg-2"
    return ""


#: 自带基组的复合方法（3c 系列等）。对这些方法**不能**再指定基组：
#: ORCA 里 ``! r2SCAN-3c def2-SVP`` 会用 def2-SVP 覆盖复合方法自带的基组，
#: 于是实际算的就不是 r2SCAN-3c 了 —— 而界面上方法名还显示 r2SCAN-3c。
COMPOSITE_METHODS: frozenset[str] = frozenset({
    "r2scan-3c", "b97-3c", "pbeh-3c", "hf-3c", "b3lyp-3c", "pbe-3c",
    "rpbeh-3c", "r2scan-3c-v", "b97-3c-v",
})


def is_composite(method: str) -> bool:
    """该方法是否自带基组（复合方法）。"""
    return (method or "").strip().lower() in COMPOSITE_METHODS


def effective_basis(method: str, basis: str) -> str:
    """实际应该写进 ORCA 输入的基组：复合方法一律为空。"""
    if is_composite(method):
        return ""
    return (basis or "").strip()
