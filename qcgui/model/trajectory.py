"""多帧 XYZ 轨迹解析（引擎无关）。

ORCA 的 IRC 等任务会把轨迹写成多帧 XYZ，**每帧注释行带该帧能量**：

    Coordinates from ORCA-job irc_IRC_Full E -227.777590200722

这是引擎记录的**结构化产物**，比解析人读的输出表格可靠。放在 model 层，
使界面/服务层也能读轨迹，而不必去 import 引擎模块。
"""

from __future__ import annotations

import re
from pathlib import Path

#: 从注释行里取能量，形如 ``... E -227.777590200722``
ENERGY_IN_COMMENT = re.compile(r"\bE\s+(-?\d+\.\d+)")


def parse_frame_energy(comment: str) -> float | None:
    match = ENERGY_IN_COMMENT.search(comment or "")
    return float(match.group(1)) if match else None


def read_xyz_frames(path: str | Path) -> list[dict]:
    """读取多帧 XYZ。

    返回 ``[{"comment", "energy", "symbols", "coords"}, ...]``（坐标单位 Å）。
    格式异常时返回已成功解析的帧，不抛异常 —— 轨迹文件可能被中途截断。
    """
    frames: list[dict] = []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return frames

    i = 0
    total = len(lines)
    while i < total:
        head = lines[i].strip()
        if not head:
            i += 1
            continue
        try:
            n_atoms = int(head.split()[0])
        except (ValueError, IndexError):
            break
        if n_atoms <= 0 or i + 2 + n_atoms > total:
            break

        comment = lines[i + 1]
        symbols: list[str] = []
        coords: list[list[float]] = []
        valid = True
        for row in lines[i + 2 : i + 2 + n_atoms]:
            parts = row.split()
            if len(parts) < 4:
                valid = False
                break
            try:
                coords.append([float(parts[1]), float(parts[2]), float(parts[3])])
            except ValueError:
                valid = False
                break
            symbols.append(parts[0])
        if not valid:
            break

        frames.append(
            {
                "comment": comment,
                "energy": parse_frame_energy(comment),
                "symbols": symbols,
                "coords": coords,
            }
        )
        i += 2 + n_atoms

    return frames


def first_frame(path: str | Path) -> dict | None:
    frames = read_xyz_frames(path)
    return frames[0] if frames else None


def last_frame(path: str | Path) -> dict | None:
    frames = read_xyz_frames(path)
    return frames[-1] if frames else None
