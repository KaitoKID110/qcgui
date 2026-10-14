"""把 ORCA 松弛扫描的结果画成图。

扫描任务在界面里只能看到原始 ``.relaxscanscf.dat``，不直观。这个脚本把
"能量 + 几何"一起画出来 —— 对扭转扫描来说，键长随角度的变化往往和能量
曲线同样有信息量（例如超共轭会让某个键在最优构象处变短）。

    python tools/plot_scan.py --job <任务目录> --out scan.png
    python tools/plot_scan.py --job <丙烯目录> --compare <乙烷目录> --out cmp.png

比对着画时会把第二个扫描的极小点对齐到第一个的极小点（两个转子周期
可能不同，例如丙烯 0–120°，乙烷 −60–60°）。

**为什么用 Pillow 而不是 matplotlib**：这台机器的 p4env 里 matplotlib 3.10.9
在 ``canvas.draw()`` 阶段就原生崩溃（0xC0000409，Agg 渲染器问题），连空图都
存不出来。Pillow 正常，所以这里自己画。绘图按 3 倍超采样再缩回来，保证
线条和文字有抗锯齿。

两个坑：
* 能量取 ``relaxscanscf.dat``，**不要**取 ``relaxscanact.dat`` —— 后者和
  界面/优化报告的单点能对不上（实测差约 1.2 kcal/mol）。
* 二面角扫描的**起点必须与几何实际值一致**，否则 ORCA 报
  ``GSTEP: could not impose initial constraints``（详见使用手册避坑清单）。
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HARTREE_TO_KCAL = 627.5094740631

#: 超采样倍数：先按 N 倍画，最后缩回来 == 免费抗锯齿
SS = 3
FIG_W, FIG_H = 1380, 646

FONT_REG = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"

C_BLUE = (47, 111, 208)
C_ORANGE = (208, 122, 47)
C_PURPLE = (142, 68, 173)
C_RED = (192, 57, 43)
C_GREY = (128, 128, 128)
C_GRID = (223, 227, 232)
C_TXT = (34, 38, 44)
C_DIM = (110, 118, 128)


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.truetype(FONT_REG, size)


class Panel:
    """一块带坐标系的画布，坐标以数据单位给，内部换算成像素。"""

    def __init__(self, draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]):
        self.d = draw
        # 框坐标按**最终像素**给，这里统一乘超采样倍数 ——
        # 否则会把 1 倍的坐标画到 3 倍的画布上，整张图缩到左上角一角
        self.x0, self.y0, self.x1, self.y1 = (v * SS for v in box)
        self.xlim = (0.0, 1.0)
        self.ylim = (0.0, 1.0)
        self.f_label = _font(FONT_REG, 17 * SS)
        self.f_tick = _font(FONT_REG, 14 * SS)
        self.f_title = _font(FONT_BOLD, 20 * SS)
        self.f_note = _font(FONT_REG, 15 * SS)
        self.f_bold = _font(FONT_BOLD, 16 * SS)

    # --- 坐标换算 ---
    def px(self, x: float) -> float:
        lo, hi = self.xlim
        return self.x0 + (x - lo) / (hi - lo) * (self.x1 - self.x0)

    def py(self, y: float) -> float:
        lo, hi = self.ylim
        return self.y1 - (y - lo) / (hi - lo) * (self.y1 - self.y0)

    # --- 基本图元 ---
    def line(self, pts, color, width=2, dash=None):
        scaled = [(self.px(x), self.py(y)) for x, y in pts]
        if dash is None:
            self.d.line(scaled, fill=color, width=width * SS, joint="curve")
            return
        # PIL 没有虚线，自己按弧长切
        on, off = dash
        for (ax, ay), (bx, by) in zip(scaled, scaled[1:]):
            length = math.hypot(bx - ax, by - ay)
            if length <= 0:
                continue
            ux, uy = (bx - ax) / length, (by - ay) / length
            t = 0.0
            while t < length:
                seg = min(on * SS, length - t)
                self.d.line([(ax + ux * t, ay + uy * t),
                             (ax + ux * (t + seg), ay + uy * (t + seg))],
                            fill=color, width=width * SS)
                t += (on + off) * SS

    def marker(self, x, y, color, r=4, shape="o"):
        cx, cy = self.px(x), self.py(y)
        rr = r * SS
        box = [cx - rr, cy - rr, cx + rr, cy + rr]
        if shape == "o":
            self.d.ellipse(box, fill=color)
        else:
            self.d.rectangle(box, fill=color)

    def text(self, x, y, s, font=None, color=C_TXT, anchor="la"):
        self.d.text((x * SS, y * SS), s, font=font or self.f_note, fill=color, anchor=anchor)

    def data_text(self, x, y, s, font=None, color=C_TXT, anchor="la"):
        self.d.text((self.px(x), self.py(y)), s, font=font or self.f_note,
                    fill=color, anchor=anchor)

    def ticks_x(self, values, fmt="{:.0f}", y_off=8):
        for v in values:
            px = self.px(v)
            self.d.line([(px, self.y1), (px, self.y1 + 4 * SS)], fill=C_GREY, width=SS)
            self.d.text((px, self.y1 + y_off * SS), fmt.format(v), font=self.f_tick,
                        fill=C_DIM, anchor="ma")

    def ticks_y(self, values, fmt="{:.1f}", x_off=8):
        for v in values:
            py = self.py(v)
            self.d.line([(self.x0 - 4 * SS, py), (self.x0, py)], fill=C_GREY, width=SS)
            self.d.text((self.x0 - x_off * SS, py), fmt.format(v), font=self.f_tick,
                        fill=C_DIM, anchor="rm")

    def grid_y(self, values):
        for v in values:
            py = self.py(v)
            self.line([(self.xlim[0], v), (self.xlim[1], v)], C_GRID, 1, dash=(3, 4))

    def grid_x(self, values):
        for v in values:
            px = self.px(v)
            self.d.line([(px, self.y0), (px, self.y1)], fill=C_GRID, width=SS)

    def frame(self):
        self.d.line([(self.x0, self.y0), (self.x0, self.y1)], fill=C_GREY, width=2 * SS)
        self.d.line([(self.x0, self.y1), (self.x1, self.y1)], fill=C_GREY, width=2 * SS)


# ------------------------------------------------------------------ 解析

#: 扫描数据的解析只在引擎里实现一次，这里直接复用 ——
#: 两边各写一套的话，早晚会在"用哪个能量文件""怎么判收敛点"上分叉。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qcgui.engines.orca import parse_scan_profile  # noqa: E402


def load_profile(job_dir: Path) -> dict:
    """读一个扫描任务；没有扫描数据就报错退出。"""
    profile = parse_scan_profile(job_dir)
    if not profile:
        raise SystemExit(f"{job_dir} 里没有扫描数据（*.relaxscanscf.dat）")
    return profile


# ------------------------------------------------------------------ 画

def _relative(rows):
    lo = min(e for _, e in rows)
    return [(a, (e - lo) * HARTREE_TO_KCAL) for a, e in rows]


def _shift_to_min(rows):
    """平移曲线，使极小点落在 0°，便于叠加不同周期的转子。

    3 重转子在一个周期内两端都是极小点，两者的能量只差一点点数值噪声
    （实测乙烷两端差约 0.001 kcal/mol）—— 直接 ``index(min())`` 会取到最后
    一个，整条曲线就被平移到视野外。

    所以容差按**曲线自身的高度**取相对值：相距不到峰高 0.5% 的都算同一个
    极小点，取角度最小的那个。
    """
    energies = [e for _, e in rows]
    lo, hi = min(energies), max(energies)
    eps = max(1e-6, (hi - lo) * 0.005)
    idx = next(i for i, e in enumerate(energies) if e - lo < eps)
    offset = rows[idx][0]
    return [(a - offset, e) for a, e in rows]


def render(primary: Path, compare: Path | None, out: Path,
           label: str, label2: str) -> dict:
    profile = load_profile(primary)
    rows = [(p["value"], p["rel_kcal"]) for p in profile["points"]]
    # 键长曲线：引擎已经挑好了重原子之间的键
    series = [(s["label"],
               [p["value"] for p in profile["points"]],
               s["values"]) for s in profile["series"]]
    x1 = [a for a, _ in rows]
    y1 = [e for _, e in rows]
    barrier = max(y1) - min(y1)
    a_min = min(rows, key=lambda r: r[1])[0]
    a_max = max(rows, key=lambda r: r[1])[0]

    if compare:
        other = load_profile(compare)
        rows2 = _shift_to_min([(p["value"], p["rel_kcal"]) for p in other["points"]])
        b2 = max(e for _, e in rows2) - min(e for _, e in rows2)
    else:
        rows2, b2 = None, None

    img = Image.new("RGB", (FIG_W * SS, FIG_H * SS), "white")
    d = ImageDraw.Draw(img)

    # 大标题
    d.text((60 * SS, 24 * SS), "甲基内旋转 —— 势能曲线与几何响应",
           font=_font(FONT_BOLD, 26 * SS), fill=C_TXT, anchor="la")
    d.text((60 * SS, 60 * SS),
           "ORCA 6.1.1 · r2SCAN-3c · 气相 · 每个点做完整几何优化",
           font=_font(FONT_REG, 15 * SS), fill=C_DIM, anchor="la")

    # ---------------- 左：势能曲线 ----------------
    ax1 = Panel(d, (100, 120, 660, 540))
    y_top = max([max(y1)] + ([max(e for _, e in rows2)] if rows2 else []))
    pad_y = max(0.35, y_top * 0.18)
    ax1.xlim = (min(x1) - 4, max(x1) + 34)
    ax1.ylim = (min(y1) - pad_y, y_top + pad_y * 1.45)
    ticks = [k * 0.5 for k in range(int((y_top + pad_y * 1.4) / 0.5) + 1)]
    ax1.grid_y(ticks)
    ax1.ticks_y(ticks)
    ax1.ticks_x([0, 20, 40, 60, 80, 100, 120])
    ax1.frame()

    if rows2:
        ax1.line(rows2, C_ORANGE, 2, dash=(6, 4))
        for a, e in rows2:
            ax1.marker(a, e, C_ORANGE, 3, "s")

    ax1.line(rows, C_BLUE, 3)
    for a, e in rows:
        ax1.marker(a, e, C_BLUE, 4, "o")

    # 势垒双箭头
    xa = ax1.px(a_max)
    ax1.d.line([(xa, ax1.py(min(y1))), (xa, ax1.py(max(y1)))], fill=C_RED, width=2 * SS)
    for yy, sgn in ((ax1.py(min(y1)), -1), (ax1.py(max(y1)), 1)):
        ax1.d.polygon([(xa, yy), (xa - 6 * SS, yy - sgn * 9 * SS),
                       (xa + 6 * SS, yy - sgn * 9 * SS)], fill=C_RED)
    d.text((xa + 12 * SS, ax1.py((min(y1) + max(y1)) / 2)),
           f"势垒 {barrier:.2f} kcal/mol", font=_font(FONT_BOLD, 16 * SS),
           fill=C_RED, anchor="lm")
    d.text((xa + 12 * SS, ax1.py((min(y1) + max(y1)) / 2) + 22 * SS),
           f"({barrier * 4.184:.1f} kJ/mol)", font=_font(FONT_REG, 14 * SS),
           fill=C_RED, anchor="lm")

    # 构象标注
    ax1.data_text(a_min, min(y1) - pad_y * 0.55, "重叠式（最小）", ax1.f_note, C_BLUE, "ma")
    ax1.data_text(a_max, max(y1) + pad_y * 0.42, "交叉式（最大）", ax1.f_note, C_DIM, "ma")

    # 图例（放右下角空白处，别压住构象标注）
    lx, ly = 380, 512
    d.line([(lx * SS, ly * SS), ((lx + 34) * SS, ly * SS)], fill=C_BLUE, width=3 * SS)
    d.text(((lx + 42) * SS, ly * SS), label, font=_font(FONT_REG, 15 * SS),
           fill=C_TXT, anchor="lm")
    if rows2:
        d.line([(lx * SS, (ly + 24) * SS), ((lx + 34) * SS, (ly + 24) * SS)],
               fill=C_ORANGE, width=2 * SS)
        d.text(((lx + 42) * SS, (ly + 24) * SS), f"{label2}（势垒 {b2:.2f}）",
               font=_font(FONT_REG, 15 * SS), fill=C_TXT, anchor="lm")

    d.text((100 * SS, 566 * SS), "二面角 H–C(CH3)–C=C  (°)", font=_font(FONT_REG, 17 * SS),
           fill=C_TXT, anchor="ma")
    # 纵轴标签竖排嫌麻烦，放左上角当说明
    d.text((100 * SS, 98 * SS), "相对能量 (kcal/mol)", font=_font(FONT_REG, 16 * SS),
           fill=C_DIM, anchor="la")

    # ---------------- 右：键长 ----------------
    ax2 = Panel(d, (820, 120, 1320, 540))
    span = None
    if series:
        allv = [v for _, _, ys in series for v in ys if v is not None]
        lo, hi = min(allv), max(allv)
        margin = max(0.004, (hi - lo) * 0.45)
        ax2.xlim = (min(x1) - 4, max(x1) + 4)
        ax2.ylim = (lo - margin, hi + margin)

        ticks = [lo + (hi - lo) * k / 4 for k in range(5)]
        ax2.grid_y(ticks)
        ax2.ticks_y(ticks, fmt="{:.4f}")
        ax2.ticks_x([0, 20, 40, 60, 80, 100, 120])
        ax2.frame()

        colours = [C_BLUE, C_PURPLE, (46, 160, 120)]
        for k, (name, xs, ys) in enumerate(series):
            pts = [(a, v) for a, v in zip(xs, ys) if v is not None]
            if not pts:
                continue
            col = colours[k % len(colours)]
            ax2.line(pts, col, 3 if k == 0 else 2, dash=None if k == 0 else (6, 4))
            for a, v in pts:
                ax2.marker(a, v, col, 4 if k == 0 else 3, "o" if k == 0 else "s")
            d.text((ax2.px(pts[-1][0]) + 8 * SS, ax2.py(pts[-1][1])),
                   name, font=_font(FONT_REG, 14 * SS), fill=col, anchor="lm")

        main = [v for v in series[0][2] if v is not None]
        d.text((820 * SS, 98 * SS), "键长随扭转角的变化 (Å)", font=_font(FONT_REG, 16 * SS),
               fill=C_DIM, anchor="la")
        span = max(main) - min(main)
        # 标注放面板中央，避免贴边被裁掉
        ax2.data_text(60, max(main) + (hi - lo) * 0.30,
                      f"最稳定构象处短 {span * 1000:.1f} mÅ", _font(FONT_BOLD, 16 * SS),
                      C_RED, "mm")
    else:
        ax2.text(440, 330, "没找到几何数据（任务目录里需要 .out）",
                 ax2.f_note, C_DIM, "mm")

    d.text((1070 * SS, 566 * SS), "二面角 H–C(CH3)–C=C  (°)",
           font=_font(FONT_REG, 17 * SS), fill=C_TXT, anchor="ma")

    # 页脚结论
    d.text((60 * SS, 606 * SS),
           f"极小点 {a_min:g}°：甲基上的一个 C–H 与 C=C 共面。C–C 单键在此处缩短 "
           f"{span * 1000:.0f} mÅ —— 超共轭 σ(C–H)→π*(C=C) 的几何指纹。"
           if span else f"极小点 {a_min:g}°。",
           font=_font(FONT_REG, 15 * SS), fill=C_DIM, anchor="la")

    out.parent.mkdir(parents=True, exist_ok=True)
    img.resize((FIG_W, FIG_H), Image.LANCZOS).save(out, optimize=True)
    return {"barrier": barrier, "compare_barrier": b2, "span": span,
            "angle_min": a_min, "angle_max": a_max, "points": len(rows),
            "bonds": [s[0] for s in series], "out": str(out)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把 ORCA 松弛扫描画成图（Pillow 渲染）")
    ap.add_argument("--job", required=True, help="扫描任务目录")
    ap.add_argument("--compare", help="对照扫描任务目录（可选）")
    ap.add_argument("--out", default="scan.png", help="输出 PNG")
    ap.add_argument("--label", default="丙烯 CH3–CH=CH2")
    ap.add_argument("--label2", default="乙烷 CH3–CH3")
    args = ap.parse_args(argv)

    info = render(Path(args.job), Path(args.compare) if args.compare else None,
                  Path(args.out), args.label, args.label2)
    print(f"  扫描点 {info['points']} 个   极小 {info['angle_min']:g}°   极大 {info['angle_max']:g}°")
    print(f"  势垒 {info['barrier']:.3f} kcal/mol", end="")
    print(f"   对照 {info['compare_barrier']:.3f} kcal/mol" if info["compare_barrier"] else "")
    if info["span"]:
        print(f"  键长变化 {info['span'] * 1000:.1f} mÅ   曲线: {', '.join(info['bonds'])}")
    print(f"  已保存 {info['out']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
