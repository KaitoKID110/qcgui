"""验证：界面里显示的原子编号，和 %geom Scan 里用的编号是不是同一套。

用户的疑问很实在 —— 如果两边不一致，照着界面上的编号填扫描坐标就会扫错原子。

分两半验证：

A. **查看器的编号顺序 == 写进 ORCA 输入的原子顺序**吗？
   把 API 给的 molecule 和实际生成的 .inp 逐原子比对坐标。

B. **ORCA 的 %geom Scan 按输入顺序解读编号**吗？
   这一半才是关键：ORCA 内部会重排原子（输出里打印的 ``Dihedral ( 2, 1, 0, 3)``
   就可能和输入下标不同），所以必须实测。
   做法：拿甲醇扫一个 H–C–O–H 二面角，然后**按输入顺序**从每个扫描点的几何里
   算出那个二面角，看是否等于扫描值。甲醇里 C 和 O 元素不同，不存在歧义。
"""

from __future__ import annotations

import json
import math
import re
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qcgui.paths import jobs_root  # noqa: E402

BASE = os.environ.get("QCGUI_BASE", "http://127.0.0.1:8760")
#: 任务库自动发现（尊重 QCGUI_HOME），不写死某一台机器的路径
JOBS = Path(os.environ.get("QCGUI_JOBS_ROOT") or jobs_root())
#: 用来做顺序比对的示例结构，相对仓库定位
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "benzyne.xyz"


def call(path: str, payload=None, timeout: int = 120):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def dist(a, b):
    return math.dist(a, b)


def dihedral(atoms, i, j, k, l):
    """按给定下标算二面角（度），采用**标准（IUPAC）符号约定**。

    注意：随手写的二面角公式很容易把符号搞反。第一版这里用了
    ``atan2(dot(cross(n1, b2), n2), dot(n1, n2))``，对同一组原子算出来的值
    恰好是 ORCA 的负数 —— 看起来像"编号不一致"，其实是手性约定不同。
    下面用的是通行的 Blondel–Karrer 形式。
    """
    p0, p1, p2, p3 = (a[1:] for a in (atoms[i], atoms[j], atoms[k], atoms[l]))

    def sub(u, v):
        return [u[t] - v[t] for t in range(3)]

    def dot(u, v):
        return sum(u[t] * v[t] for t in range(3))

    def cross(u, v):
        return [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]

    b0 = [-t for t in sub(p1, p0)]
    b1 = sub(p2, p1)
    b2 = sub(p3, p2)
    norm = math.sqrt(dot(b1, b1))
    b1 = [t / norm for t in b1]
    v = [b0[t] - dot(b0, b1) * b1[t] for t in range(3)]
    w = [b2[t] - dot(b2, b1) * b1[t] for t in range(3)]
    return math.degrees(math.atan2(dot(cross(b1, v), w), dot(v, w)))


def read_scan_points(job_dir: Path):
    """从 .out 取每个扫描点的（扫描值, 收敛后的几何）。"""
    out = sorted(job_dir.glob("*.out"))[0]
    lines = out.read_text(encoding="utf-8", errors="replace").splitlines()
    steps = []
    for i, line in enumerate(lines):
        if "RELAXED SURFACE SCAN STEP" not in line:
            continue
        for j in range(i, min(i + 6, len(lines))):
            m = re.search(r"Dihedral\s*\([^)]*\)\s*:\s*(-?[\d.]+)", lines[j])
            if m:
                steps.append([float(m.group(1)), i, None])
                break
    for k, st in enumerate(steps):
        end = steps[k + 1][1] if k + 1 < len(steps) else len(lines)
        last = None
        for i in range(st[1], end):
            if "CARTESIAN COORDINATES (ANGSTROEM)" not in lines[i]:
                continue
            atoms = []
            for line in lines[i + 2:]:
                p = line.split()
                if len(p) == 4 and re.fullmatch(r"[A-Za-z]{1,2}", p[0]):
                    try:
                        atoms.append((p[0], float(p[1]), float(p[2]), float(p[3])))
                    except ValueError:
                        break
                elif atoms:
                    break
            if atoms:
                last = atoms
        st[2] = last
    return [(s[0], s[2]) for s in steps if s[2]]


def main() -> int:
    print("=" * 70)
    print("A. 查看器编号顺序 vs ORCA 输入顺序")
    print("=" * 70)
    loaded = call("/api/structure/load", {"path": str(EXAMPLE)})
    mol = loaded["molecule"]
    job = call("/api/jobs", {"molecule": mol, "job_type": "energy", "method": "HF",
                            "basis": "STO-3G", "nprocs": 1, "maxcore_mb": 200,
                            "title": "order_A"})
    jid = job["job"]["job_id"]
    time.sleep(8)
    inp = sorted((JOBS / jid).glob("*.inp"))[0]
    lines = inp.read_text(encoding="ascii").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("* xyz"))
    written = []
    for line in lines[start + 1:]:
        if line.startswith("*"):
            break
        p = line.split()
        if len(p) == 4:
            written.append((p[0], float(p[1]), float(p[2]), float(p[3])))

    bad = 0
    for i in range(mol["n_atoms"]):
        same = (written[i][0] == mol["symbols"][i]
                and all(abs(written[i][1 + k] - mol["coords"][i][k]) < 1e-9 for k in range(3)))
        if not same:
            bad += 1
            print(f"   第 {i} 个不一致: inp={written[i]}  viewer={mol['symbols'][i]} {mol['coords'][i]}")
    print(f"  {mol['n_atoms']} 个原子逐位比对，不一致 {bad} 个 -> "
          f"{'完全一致 ✅' if bad == 0 else '有问题 ❌'}")

    print()
    print("=" * 70)
    print("B. ORCA 的 %geom Scan 是否按输入顺序解读编号")
    print("=" * 70)
    meth = call("/api/structure/smiles", {"smiles": "CO"})["molecule"]
    syms, xyz = meth["symbols"], meth["coords"]
    print("  甲醇原子顺序: " + " ".join(f"{s}{i}" for i, s in enumerate(syms)))

    ci = next(i for i, s in enumerate(syms) if s == "C")
    oi = next(i for i, s in enumerate(syms) if s == "O")
    hs = [i for i, s in enumerate(syms) if s == "H"]
    h_on_o = min(hs, key=lambda i: dist(xyz[i], xyz[oi]))
    h_on_c = min((i for i in hs if i != h_on_o), key=lambda i: dist(xyz[i], xyz[ci]))
    quad = (h_on_c, ci, oi, h_on_o)
    init = dihedral([(s, *c) for s, c in zip(syms, xyz)], *quad)
    print(f"  扫描坐标（输入顺序）: H{h_on_c} - C{ci} - O{oi} - H{h_on_o}")
    print(f"  初始值 = {init:+.2f}°  ->  从 {init - 60:.0f}° 扫到 {init + 60:.0f}°")

    block = (f"Scan\n  D {' '.join(str(k) for k in quad)} = "
             f"{init - 60:.1f}, {init + 60:.1f}, 5\nend\nMaxIter 250\n"
             f"TolRMSD 0.010\nTolMaxD 0.030")
    scan = call("/api/jobs", {"molecule": meth, "job_type": "opt", "method": "HF",
                              "basis": "STO-3G", "nprocs": 2, "maxcore_mb": 300,
                              "title": "order_B", "extra_blocks": [["geom", block]]})
    sid = scan["job"]["job_id"]
    print(f"  扫描任务 {sid}")
    for _ in range(60):
        st = call(f"/api/jobs/{sid}")
        if st["job"]["status"] not in ("queued", "running"):
            break
        time.sleep(10)
    print(f"  状态: {st['job']['status']}")

    points = read_scan_points(JOBS / sid)
    print()
    print("  扫描值(°.dat)   按输入顺序从几何里算出的二面角   差")
    ok = True
    for value, atoms in points:
        got = dihedral(atoms, *quad)
        # 周期性：可能差 360
        delta = (got - value + 180) % 360 - 180
        flag = "✅" if abs(delta) < 1.0 else "❌"
        if abs(delta) >= 1.0:
            ok = False
        print(f"     {value:8.2f}          {got:8.2f}                    {delta:+6.2f}  {flag}")

    print()
    print("  结论：" + ("编号是同一套 —— 界面显示的下标可以直接填进扫描 ✅"
                      if ok and bad == 0 else "两边不一致，需要修正 ❌"))
    return 0 if (ok and bad == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
