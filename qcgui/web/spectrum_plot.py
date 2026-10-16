"""红外谱图的 Pillow 渲染（3 倍超采样 + LANCZOS 缩小）。

**为什么不用 matplotlib**：本机 p4env 里的 matplotlib 3.10.9 在 ``canvas.draw()``
阶段原生崩溃（0xC0000409，Agg 渲染器问题），连空图都存不出来。Pillow 正常，
项目里 ``tools/plot_scan.py`` 就是因此改成自己画的 —— 这里沿用同一套做法：
先按 3 倍尺寸画，最后 LANCZOS 缩回来，等于免费得到抗锯齿。

**为什么放在 web 层**：模型层只管纯数学（可被单测 / 导出 / CLI 复用），
绘图是展示层的事。PIL 在 requirements 里是**可选依赖**（见 doctor.py），
所以这个模块由路由**惰性导入**：没装 Pillow 时界面其余部分照常可用，
只有谱图区显示一句看得懂的报错。

坐标约定：横轴按红外惯例**反向**（高波数在左，GaussView / 文献都是这么画的）。
"""

from __future__ import annotations

import io
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..model.spectrum import (
    CM_UNIT,
    KIND_LABELS,
    NO_INTENSITY_NOTE,
    broaden,
    normalize_peaks,
    strongest_index,
)

#: 超采样倍数：按 N 倍画再缩回来 == 免费抗锯齿
SS = 3

FIG_W, FIG_H = 880, 450
#: 上边距特意留得宽（92px）：标题 + "无强度谱"警告 + 虚频说明，最多三行，
#: 再加一行纵轴单位 —— 挤在一起会被裁掉，而这三行都是"别误读结果"的关键信息。
L, R, T, B = 78, 16, 92, 56

C_CURVE = (47, 111, 208)
C_FILL = (47, 111, 208, 46)
C_STICK = (152, 164, 182)
C_IMAG = (208, 58, 58)
C_GRID = (228, 232, 238)
C_AXIS = (138, 146, 156)
C_TXT = (34, 38, 44)
C_DIM = (108, 116, 126)
C_WARN = (191, 106, 18)

#: 图上写波数单位时用 ASCII 的 "cm-1"（原因见 model/spectrum.py 的 CM_UNIT）。
CM = CM_UNIT

#: 中文字体候选。找不到就退到 PIL 自带位图字体 —— 退化成方框也总比报错好，
#: 但本机是 Windows，msyh 一定在。
FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    order = FONT_CANDIDATES[1:] + FONT_CANDIDATES[:1] if bold else FONT_CANDIDATES
    for path in order:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size * SS)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size * SS)
    except TypeError:                        # 老 Pillow 没有 size 参数
        return ImageFont.load_default()


def _ticks(lo: float, hi: float, target: int = 6) -> list[float]:
    """挑一列"整"的刻度值（1/2/5 × 10ⁿ 那一套），个数接近 target。

    步长要能取到小数：没有强度数据时纵轴满量程就是 1.0，刻度得是
    0 / 0.5 / 1.0；只用整数步长会退化成一格（实测踩过）。
    """
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / max(1, target)
    steps = [m * 10.0 ** e for e in range(-4, 7) for m in (1, 2, 5)]
    step = next((s for s in sorted(steps) if s >= raw - 1e-12), steps[-1])
    out, v = [], math.ceil(lo / step) * step
    while v <= hi + 1e-9:
        out.append(v)
        v += step
    return out


def _fmt_tick(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 10 or v == 0 else f"{v:.1f}"


def _dashed_v(draw: ImageDraw.ImageDraw, x: float, y0: float, y1: float,
              color, width: int = 1, on: int = 5, off: int = 4) -> None:
    """竖虚线。PIL 没有虚线，自己按长度切（和 tools/plot_scan.py 同一招）。"""
    t = y0
    while t < y1:
        seg = min(on * SS, y1 - t)
        draw.line([(x, t), (x, t + seg)], fill=color, width=width * SS)
        t += (on + off) * SS


def render_spectrum_png(
    peaks,
    *,
    x_min: float,
    x_max: float,
    step: float,
    fwhm: float,
    kind: str = "gaussian",
    has_intensity: bool = False,
    intensity_unit: str = "相对强度",
    title: str = "",
    width: int = FIG_W,
    height: int = FIG_H,
) -> bytes:
    """把峰列表画成一张 PNG（返回字节；路由直接返回给浏览器）。

    画面上必须能看出来的三件事（都是"任务对不对"的关键信息）：
    1. **虚频**用红色竖线标出 —— 过渡态应该正好 1 个虚频，极小点 0 个；
    2. **最强峰**用点线 + 文字标出（只在真有强度时才标，补的 1.0 不算）；
    3. 没有强度数据时，图上直接写"无强度数据"，不让人误以为峰高有意义。

    强度全为 0 或没有峰时不抛异常：照常画坐标框，中间写一行说明 ——
    界面上一个空框配文字，比一个 500 错误有用得多。
    """
    normalized = normalize_peaks(peaks)
    xs, ys = broaden(normalized, x_min, x_max, step, fwhm, kind)

    img = Image.new("RGB", (width * SS, height * SS), "white")
    x0, y0 = L * SS, T * SS
    x1, y1 = (width - R) * SS, (height - B) * SS

    y_max = max(ys) if ys else 0.0
    empty = (not ys) or y_max <= 0.0
    y_top = y_max * 1.14 if not empty else 1.0

    def px(x: float) -> float:
        # 反向：高波数在左（红外谱图的惯例）
        return x0 + (x_max - x) / (x_max - x_min) * (x1 - x0)

    def py(y: float) -> float:
        return y1 - (y / y_top) * (y1 - y0)

    # ---- 网格与刻度 ----
    if not empty:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        poly = [(px(xs[0]), y1)] + [(px(x), py(y)) for x, y in zip(xs, ys)] \
            + [(px(xs[-1]), y1)]
        od.polygon(poly, fill=C_FILL)
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    d = ImageDraw.Draw(img)
    f_tick, f_lab, f_title, f_note = _font(12), _font(13), _font(17, True), _font(12)

    for t in _ticks(0.0, y_top, 4):
        if t <= 0:
            continue
        yy = py(t)
        _dashed_v(d, x0, yy, x1, C_GRID, 1, 3, 4)
        d.text((x0 - 7 * SS, yy), _fmt_tick(t), font=f_tick, fill=C_DIM, anchor="rm")

    for t in _ticks(x_min, x_max, 7):
        xx = px(t)
        d.line([(xx, y0), (xx, y1)], fill=C_GRID, width=SS)
        d.line([(xx, y1), (xx, y1 + 4 * SS)], fill=C_AXIS, width=SS)
        d.text((xx, y1 + 9 * SS), _fmt_tick(t), font=f_tick, fill=C_DIM, anchor="ma")

    # ---- 谱线 ----
    if not empty:
        d.line([(px(x), py(y)) for x, y in zip(xs, ys)],
               fill=C_CURVE, width=2 * SS, joint="curve")

    # ---- 峰位竖线（棒谱）+ 虚频高亮 ----
    for pos, intensity in normalized:
        xx = px(pos)
        if xx < x0 - 1 or xx > x1 + 1:
            continue
        imag = pos < 0.0
        top = py(min(intensity, y_top)) if not empty else y1
        if intensity > 0 and not empty:
            d.line([(xx, y1), (xx, top)], fill=C_IMAG if imag else C_STICK,
                   width=(2 if imag else 1) * SS)
        if imag:
            # 红色三角 + 文字：虚频是判断过渡态对不对的头号指标
            half = 5 * SS
            d.polygon([(xx, top - 2 * SS), (xx - half, top - 12 * SS),
                       (xx + half, top - 12 * SS)], fill=C_IMAG)
            d.text((xx, top - 15 * SS), f"虚频 {pos:.1f} {CM}", font=f_note,
                   fill=C_IMAG, anchor="mb")

    # ---- 最强峰（只在真有强度时标）----
    best = strongest_index(normalized) if has_intensity else None
    if best is not None and normalized and not empty:
        pos, intensity = normalized[best]
        xx = px(pos)
        _dashed_v(d, xx, y0, y1, C_WARN, 1, 4, 5)
        label = f"最强峰 {pos:.1f} {CM}"
        box = d.textbbox((0, 0), label, font=f_note)
        w = box[2] - box[0]
        tx = min(max(xx, x0 + w / 2 + 4 * SS), x1 - w / 2 - 4 * SS)
        d.text((tx, y0 + 4 * SS), label, font=f_note, fill=C_WARN, anchor="ma")

    # ---- 画框 ----
    d.line([(x0, y0), (x0, y1)], fill=C_AXIS, width=2 * SS)
    d.line([(x0, y1), (x1, y1)], fill=C_AXIS, width=2 * SS)
    d.text(((x0 + x1) / 2, height * SS - 14 * SS), f"波数 ({CM})",
           font=f_lab, fill=C_TXT, anchor="mm")
    d.text((x0, y0 - 22 * SS), intensity_unit, font=f_lab, fill=C_DIM, anchor="la")

    # ---- 顶部说明（"无强度谱"必须写在图上）----
    head: list[tuple[str, tuple, ImageFont.ImageFont]] = []
    if title:
        head.append((title, C_TXT, f_title))
    if empty:
        head.append(("强度全为 0：没有可画的吸收峰", C_WARN, f_note))
    elif not has_intensity:
        head.append((f"注意：{NO_INTENSITY_NOTE}", C_WARN, f_note))
    imags = [p for p, _ in normalized if p < 0.0]
    if imags:
        shown = ", ".join(f"{p:.1f}" for p in imags[:3]) + ("…" if len(imags) > 3 else "")
        head.append((f"虚频 {len(imags)} 个（红色）：{shown} {CM}", C_IMAG, f_note))
    for k, (text, color, font) in enumerate(head[:3]):
        d.text((L * SS, (8 + 22 * k) * SS), text, font=font, fill=color, anchor="la")

    buf = io.BytesIO()
    img.resize((width, height), Image.LANCZOS).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def plot_kind_label(kind: str) -> str:
    """线型的中文标签，给标题用。"""
    return KIND_LABELS.get(kind, kind)
