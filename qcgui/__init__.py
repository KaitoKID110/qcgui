"""qcgui —— 以 ORCA 为主引擎的量化计算工作台。"""

from __future__ import annotations

import sys

__version__ = "0.1.0"


def configure_stdio(encoding: str = "utf-8") -> None:
    """把 stdout/stderr 显式切到 UTF-8，且永不因编码问题抛异常。

    为什么必须这么做：``PYTHONIOENCODING`` 环境变量只在**解释器启动时**被读取，
    进程内再设置它对 ``sys.stdout`` 已经无效。Windows 上被重定向或没有控制台时
    （例如快捷方式用 ``pythonw.exe`` 启动），stdout 会落到 GBK，
    一旦打印含非 ASCII 的内容（如 ``Löwdin``）就抛 ``UnicodeEncodeError``；
    若这发生在 Tk 回调里，异常被吞掉且主循环不退出，表现为"卡死"。

    这里用 ``errors="replace"`` 兜底：宁可打印出替代字符，也不能崩。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding=encoding, errors="replace")
        except (ValueError, OSError):
            pass


__all__ = ["__version__", "configure_stdio"]
