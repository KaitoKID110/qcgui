"""生成应用图标 ``docs/qcgui.ico``。

可重复运行：``python tools/make_icon.py``。
图形是一分子的水（本项目的测试分子）：中心氧 + 两个氢，键角沿用 104.5°，
配色与界面主色一致。
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "qcgui.ico"

BG = (31, 106, 165, 255)        # 与界面按钮主色一致
O_COLOR = (243, 246, 250, 255)
H_COLOR = (176, 205, 232, 255)
BOND = (255, 255, 255, 230)

#: 水的 H–O–H 键角，与真实分子一致
BOND_ANGLE = 104.5

ICO_SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]


def render(size: int = 256, supersample: int = 4) -> Image.Image:
    """先按 4 倍超采样绘制再缩小，得到平滑边缘。"""
    s = size * supersample
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = s * 0.035
    draw.rounded_rectangle(
        [pad, pad, s - pad, s - pad], radius=s * 0.22, fill=BG
    )

    cx, cy = s * 0.5, s * 0.575          # 氧略微下移，给两个氢留位置
    r_o, r_h = s * 0.165, s * 0.100
    reach = s * 0.30
    half = math.radians(BOND_ANGLE / 2.0)
    dx, dy = reach * math.sin(half), reach * math.cos(half)
    h1 = (cx + dx, cy - dy)
    h2 = (cx - dx, cy - dy)
    o = (cx, cy)

    width = max(1, int(s * 0.038))
    draw.line([o, h1], fill=BOND, width=width)
    draw.line([o, h2], fill=BOND, width=width)

    for (px, py), radius, color in (
        (h1, r_h, H_COLOR),
        (h2, r_h, H_COLOR),
        (o, r_o, O_COLOR),
    ):
        draw.ellipse([px - radius, py - radius, px + radius, py + radius], fill=color)

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    base = render(256)
    base.save(OUT, format="ICO", sizes=ICO_SIZES)
    print(f"已生成 {OUT}  ({OUT.stat().st_size / 1024:.1f} KB)")
    print("尺寸: " + ", ".join(f"{w}x{h}" for w, h in ICO_SIZES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
