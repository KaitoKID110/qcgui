"""引擎注册表。

导入本包即完成所有引擎适配器的登记。
"""

from .base import (
    Engine,
    EngineError,
    RunOutcome,
    all_engines,
    engine_names,
    get_engine,
    register,
    which,
)

# 导入以触发 @register（顺序无关）
from . import orca as _orca  # noqa: E402,F401

__all__ = [
    "Engine",
    "EngineError",
    "RunOutcome",
    "all_engines",
    "engine_names",
    "get_engine",
    "register",
    "which",
]
