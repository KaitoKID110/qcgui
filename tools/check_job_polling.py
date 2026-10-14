"""验证任务列表的兜底轮询真的会触发。

背景：界面原来只靠 SSE 推送刷新任务列表。SSE 一断（服务重启/网络抖动），
`finished` 事件就收不到，列表永远停在"运行中"。加了兜底轮询后，这里验证
它确实在发请求。

做法：临时把一条任务标成 running，用无头 Chrome 打开页面，数服务端日志里
`GET /api/jobs?limit=80` 出现了几次 —— 只靠 SSE 的话应当只有首屏那 1 次。

注意必须带 `?nosse=1`：SSE 长连接会让页面永远处于"加载中"，
`--virtual-time-budget` 等不到，Chrome 会一直挂住（实测踩过）。
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qcgui.paths import jobs_root  # noqa: E402

#: 任务库自动发现（尊重 QCGUI_HOME），不写死某一台机器的路径
DB = Path(os.environ.get("QCGUI_JOBS_DB") or (jobs_root() / "index.sqlite"))
LOG = Path(tempfile.gettempdir()) / "qcgui_server.log.err"


def chrome() -> str:
    """找 Chrome：环境变量 → PATH → 常见安装位置。"""
    explicit = os.environ.get("QCGUI_CHROME")
    if explicit and Path(explicit).is_file():
        return explicit
    found = shutil.which("chrome") or shutil.which("chrome.exe")
    if found:
        return found
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")):
        if Path(p).is_file():
            return p
    raise SystemExit("找不到 Chrome（可用环境变量 QCGUI_CHROME 指定）")


def main() -> int:
    conn = sqlite3.connect(DB)
    jid, old = conn.execute(
        "select job_id, status from jobs order by created_at desc limit 1").fetchone()
    conn.execute("update jobs set status='running', finished_at=NULL where job_id=?", (jid,))
    conn.commit()
    conn.close()
    print(f"  临时把 {jid} 标成 running（原状态 {old}）")

    start = LOG.stat().st_size if LOG.is_file() else 0
    out = Path(tempfile.gettempdir()) / "qcgui_poll.png"
    out.unlink(missing_ok=True)
    profile = tempfile.mkdtemp(prefix="qcgui_chrome_")
    try:
        subprocess.run(
            [chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
             "--window-size=1400,900", "--virtual-time-budget=40000",
             "--user-data-dir=" + profile, "--screenshot=" + str(out),
             "http://127.0.0.1:8760/?nosse=1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240)
        print(f"  截图 {'成功' if out.is_file() else '失败'}")
    finally:
        conn = sqlite3.connect(DB)
        conn.execute("update jobs set status=?, finished_at=strftime('%s','now') where job_id=?",
                     (old, jid))
        conn.commit()
        conn.close()
        print(f"  已还原 {jid} 为 {old}")

    time.sleep(1)
    chunk = LOG.read_text(encoding="utf-8", errors="replace")[start:]
    polls = len(re.findall(r"GET /api/jobs\?limit=80", chunk))
    print(f"  页面加载期间服务端收到 {polls} 次 GET /api/jobs?limit=80")
    print("  结论：" + ("兜底轮询在工作 ✅（首屏 1 次 + 轮询若干次）"
                      if polls >= 3 else "轮询没触发 ❌"))
    return 0 if polls >= 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
