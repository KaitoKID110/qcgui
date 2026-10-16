"""给「几何参数表」拍一张真浏览器截图（无头 Chrome）。

为什么要有这个脚本：本项目的界面改动必须**真浏览器验证 + 截图** ——
"DOM 里有"不等于"用户看得见"（已经栽过三次：结论横幅被挤到滚动区下面、
扫描区被插到末尾、3D 数值没画出来）。截图让下一个人能一眼复核，
而不是只看到一句"我测过了"。

做法：起一个临时任务库 + 一个已完成的任务（结果几何是过氧化氢构型），
用 ``?job=<id>&geomtest=1`` 打开页面。``geomtest=1`` 那个钩子会点一行、
把高亮留在打开状态，所以截到的就是用户点完一行看到的样子。

    python tools/shot_geometry_table.py [输出 png 路径]
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tests.test_web_browser import (  # noqa: E402
    Server,
    create_geometry_fixture_job,
    find_browser,
)

DEFAULT_OUT = REPO / "figures" / "geometry_table.png"


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    # 诊断行里有 Å/°，Windows 控制台默认 GBK 会直接抛 UnicodeEncodeError ——
    # 截图成功了却因为"打印不出来"而报错，最误导人。用项目自己的兜底。
    from qcgui import configure_stdio

    configure_stdio()
    browser = find_browser()
    if not browser:
        print("找不到 Chrome/Edge，无法截图")
        return 1

    server = Server()
    try:
        job_id = create_geometry_fixture_job(server, "20200101-000000-shotgeom")
        url = f"{server.base}/?nosse=1&job={job_id}&geomtest=1"
        print("URL:", url)

        # 一次跑两件事：--dump-dom 取钩子写的断言文本，--screenshot 出图。
        # 分开两次跑（同一 URL）更稳：两个开关一起用时偶发只出其中一个。
        profile = Path(tempfile.mkdtemp(prefix="qcgui_shot_"))
        common = [
            browser, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", "--window-size=1600,1000",
            "--virtual-time-budget=30000", f"--user-data-dir={profile}",
        ]
        shot = subprocess.run(
            common + [f"--screenshot={out}", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        dom = subprocess.run(
            common + ["--dump-dom", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        ).stdout or ""
    finally:
        server.close()

    if not out.is_file():
        print("截图失败:", (shot.stderr or "")[-500:])
        return 1

    marker = "__GEOMTEST__"
    if marker in dom:
        body = dom.split(marker, 1)[1].split("__END__", 1)[0]
        for part in body.split(" | "):
            print("   " + part.strip())
    print(f"\n截图: {out}  ({out.stat().st_size} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
