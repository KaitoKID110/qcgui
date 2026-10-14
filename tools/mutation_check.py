"""变异测试：验证测试套件**真的有牙齿**。

做法：把项目复制到临时目录，对某个关键行为做一处故意的破坏（mutation），
再跑声称覆盖它的那个测试文件。

* 测试**失败** → 合格，这个回归测试是真能抓住问题的
* 测试**仍然通过** → 不合格：这个用例没有覆盖它声称覆盖的行为，
  以后真的改坏了也不会有人知道

**在副本上做**，不碰原始源码 —— 否则正在并行读代码的 teammate 会看到
被人为改坏的文件，报出假发现。

用法::

    python tools/mutation_check.py
"""

from __future__ import annotations

import os
import shutil
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: 用**当前解释器**跑测试 —— 写死某个 conda 环境路径的话，换台机器就废了
PYTHON = os.environ.get("QCGUI_PYTHON") or sys.executable
#: Node 用于 viewer_harness 之类的 JS 测试；没有就跳过相关变异
NODE = os.environ.get("QCGUI_NODE") or shutil.which("node") or ""

#: (说明, 相对路径, 原文, 改成, 该抓住它的测试文件)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "原子编号标注去掉描边（回到读不出来的版本）",
        "qcgui/web/static/viewer.js",
        "        ctx.strokeText(p.label, x, y);",
        "        /* 去掉描边 */;",
        "tests/test_viewer_js.py",
    ),
    (
        "启动时不再清算孤儿任务",
        "qcgui/jobs/manager.py",
        '            if record.status not in ("running", "queued"):\n                continue',
        "            if True:\n                continue",
        "tests/test_job_manager.py",
    ),
    (
        "读 jobspec 时不再容忍坏数据（恢复成会抛异常）",
        "qcgui/jobs/store.py",
        "        except (OSError, ValueError, KeyError, TypeError):\n            return None\n\n    def load_result",
        "        except (OSError,):\n            return None\n\n    def load_result",
        "tests/test_job_manager.py",
    ),
    (
        "扫描曲线不再返回逐点几何（拖动就没得看了）",
        "qcgui/engines/orca.py",
        '        "geometries": geometries,',
        '        "geometries": [],',
        "tests/test_orca_engine.py",
    ),
    (
        "查看器返回的原子顺序被打乱",
        "qcgui/web/app.py",
        '        "symbols": list(molecule.symbols),\n        "coords": [list(c) for c in molecule.coords],',
        '        "symbols": list(reversed(molecule.symbols)),\n'
        '        "coords": list(reversed([list(c) for c in molecule.coords])),',
        "tests/test_edge_cases.py",
    ),
    (
        "二面角扫描块用错坐标字母（D 写成 A）",
        "qcgui/web/static/scanblock.js",
        "  dihedral: { letter: 'D', atoms: 4, label: '二面角', how: 'D i j k l' },",
        "  dihedral: { letter: 'A', atoms: 4, label: '二面角', how: 'D i j k l' },",
        "tests/test_viewer_js.py",
    ),
    (
        "CSV 遇到坏记录时不再兜住（恢复成会炸）",
        "qcgui/jobs/store.py",
        "        try:\n            return JobSpec.from_dict(json.loads(path.read_text(encoding=\"utf-8\")))",
        "        try:\n            raise ValueError('故意破坏')\n"
        "            return JobSpec.from_dict(json.loads(path.read_text(encoding=\"utf-8\")))",
        "tests/test_edge_cases.py",
    ),
    (
        "失败判定失效：不管 ORCA 有没有正常终止都报成功",
        "qcgui/engines/orca.py",
        '        normal = _TERMINATION_MARKER in text',
        '        normal = True',
        "tests/test_orca_engine.py",
    ),
    (
        "重复关键字不再去重（用户又会撞上 INPUT ERROR）",
        "qcgui/engines/orca.py",
        "    seen: set[str] = set()\n    unique: list[str] = []",
        "    return [str(w).strip() for w in words]\n    seen: set[str] = set()\n    unique: list[str] = []",
        "tests/test_orca_engine.py",
    ),
]


def run_suite(root: Path, test_file: str) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["QCGUI_HOME"] = str(Path(tempfile.mkdtemp(prefix="qcgui_mut_home_")))
    if NODE:
        env["QCGUI_NODE"] = NODE
    proc = subprocess.run(
        [PYTHON, test_file],
        cwd=str(root), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900,
    )
    tail = "\n".join((proc.stdout or "").splitlines()[-4:])
    return proc.returncode, tail


def main() -> int:
    print(f"  原始项目: {REPO}")
    print(f"  共 {len(MUTATIONS)} 处变异，每处在**副本**上验证\n")

    toothed = toothless = broken = 0
    toothless_list: list[str] = []

    for index, (desc, rel, old, new, test_file) in enumerate(MUTATIONS, 1):
        src = REPO / rel
        if not src.is_file():
            print(f"  [{index}] ⚠ 跳过（找不到 {rel}）")
            broken += 1
            continue
        content = src.read_text(encoding="utf-8")
        if old not in content:
            print(f"  [{index}] ⚠ 跳过（{rel} 里找不到要替换的片段）")
            broken += 1
            continue

        work = Path(tempfile.mkdtemp(prefix=f"qcgui_mut{index}_"))
        root = work / "qcgui"
        shutil.copytree(REPO, root, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "figures", "audit", ".pytest_cache"))

        target = root / rel
        target.write_text(content.replace(old, new, 1), encoding="utf-8")

        code, tail = run_suite(root, test_file)
        caught = code != 0
        if caught:
            toothed += 1
            print(f"  [{index}] ✅ 抓到了 — {desc}")
        else:
            toothless += 1
            toothless_list.append(f"{desc}（{test_file} 仍然全绿）")
            print(f"  [{index}] ❌ **没抓到** — {desc}")
            print(f"        {test_file} 仍然通过；这个用例没有覆盖它声称覆盖的行为")
        shutil.rmtree(work, ignore_errors=True)

    print()
    print(f"  有牙齿 {toothed} / 没牙齿 {toothless} / 跳过 {broken}")
    if toothless_list:
        print("\n  需要补测试的地方：")
        for item in toothless_list:
            print(f"    - {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
