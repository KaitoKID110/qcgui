"""任务队列、持久化与批量编排。

**本包不得 import qcgui.engines。** 引擎只在 worker 子进程里被导入，
这样界面进程的地址空间里永远不存在量化库。
"""

from .batch import BatchItem, BatchResult, BatchRunner, collect_structures
from .manager import JobManager
from .store import JobRecord, JobStore

__all__ = [
    "BatchItem",
    "BatchResult",
    "BatchRunner",
    "JobManager",
    "JobRecord",
    "JobStore",
    "collect_structures",
]
