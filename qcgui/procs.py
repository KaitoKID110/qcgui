"""进程管理工具。

Windows 上必须连带子进程一起杀：ORCA 用 MPI 时会 fork 出 mpiexec 之类的子进程，
worker 进程下面又挂着 ORCA —— 只杀父进程会留下孤儿继续写任务目录。
"""

from __future__ import annotations

import os
import subprocess


def terminate(proc: subprocess.Popen | None, grace: float = 3.0, tree: bool = True) -> None:
    """终止进程。

    :param grace: 温和终止后等待的秒数，超时才强杀
    :param tree: Windows 下是否连带子进程（默认是）
    """
    if proc is None or proc.poll() is not None:
        return

    if tree and os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
        _wait(proc, grace)
        return

    try:
        proc.terminate()
    except OSError:
        pass
    if _wait(proc, grace):
        return

    try:
        proc.kill()
    except OSError:
        pass
    _wait(proc, grace)


def _wait(proc: subprocess.Popen, timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False
