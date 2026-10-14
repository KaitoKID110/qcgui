"""网页界面（Flask）。

**本包不得 import qcgui.engines。** 引擎能力通过 ``python -m qcgui.worker --capabilities``
子进程查询，计算通过 ``JobManager`` 交给 worker 执行。
"""

from .app import create_app, run_server

__all__ = ["create_app", "run_server"]
