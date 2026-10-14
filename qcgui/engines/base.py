"""引擎抽象。

**这是"界面与引擎分离"的关键接口。** 界面层只依赖 ``Engine`` 与 ``Result``，
永远不会 import psi4 / pyscf / 任何量化库。

每个引擎适配器实现四件事：
1. ``available()``     —— 本机能不能用（可执行文件在哪）
2. ``capabilities()``  —— 支持哪些任务类型（界面据此禁用不支持的选项）
3. ``generate_input()``—— 生成引擎输入文件
4. ``run()``           —— 跑起来，可选地流式回报日志、响应取消
5. ``parse()``         —— 把结构化输出翻译成 ``Result``
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from ..model import JobSpec, JobType, Result

#: 日志行回调
LineHandler = Callable[[str], None]


class EngineError(RuntimeError):
    """引擎不可用、不支持该任务、或输入非法。"""


@dataclass(frozen=True)
class RunOutcome:
    """一次外部进程运行的结果。"""

    exit_code: int
    cancelled: bool = False
    out_file: str = ""


class Engine(ABC):
    """量化引擎适配器基类。"""

    #: 稳定标识，用于 JobSpec.engine 与引擎注册表
    name: str = ""
    #: 展示名
    display_name: str = ""

    # ---------- 能力 ----------

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """本机是否可用。返回 ``(可用, 说明)``，说明里给出可执行文件路径或缺失原因。"""

    @abstractmethod
    def capabilities(self) -> frozenset[JobType]:
        """本引擎支持的任务类型。**只声明已验证过的**，不要声明做不到的。"""

    def supports(self, job_type: JobType) -> bool:
        return job_type in self.capabilities()

    def require(self, job_type: JobType) -> None:
        if not self.supports(job_type):
            raise EngineError(
                f"引擎 {self.name} 不支持任务类型 {job_type.value}；"
                f"支持: {sorted(t.value for t in self.capabilities())}"
            )

    # ---------- 三阶段 ----------

    @abstractmethod
    def generate_input(self, spec: JobSpec, job_dir: Path) -> Path:
        """在 ``job_dir`` 生成输入文件，返回其路径。"""

    @abstractmethod
    def run(
        self,
        spec: JobSpec,
        job_dir: Path,
        on_line: LineHandler | None = None,
        cancel: object | None = None,
    ) -> RunOutcome:
        """执行计算。``cancel`` 需提供 ``is_set()``（如 ``threading.Event``）。"""

    @abstractmethod
    def parse(self, spec: JobSpec, job_dir: Path) -> Result:
        """解析结构化的输出，返回 ``Result``。**不得依赖日志文本作为数据来源。**"""

    def parse_scan_profile(self, job_dir: str | Path) -> dict | None:
        """解析势能面扫描的曲线数据（各点的坐标值、能量、键长）。

        不是扫描任务、或引擎不支持时返回 ``None``。曲线数据量不大但解析要读
        整个输出文件，所以按需调用、不塞进 ``Result``。
        """
        return None

    # ---------- 端到端 ----------

    def execute(
        self,
        spec: JobSpec,
        job_dir: str | Path,
        on_line: LineHandler | None = None,
        cancel: object | None = None,
    ) -> Result:
        """生成输入 → 运行 → 解析，返回结果。这是上层唯一需要调用的方法。"""
        job_dir = Path(job_dir)
        job_dir.mkdir(parents=True, exist_ok=True)

        ok, why = self.available()
        if not ok:
            raise EngineError(f"引擎 {self.name} 不可用：{why}")
        self.require(spec.job_type)

        self.generate_input(spec, job_dir)
        outcome = self.run(spec, job_dir, on_line=on_line, cancel=cancel)
        result = self.parse(spec, job_dir)
        result.spec_fingerprint = result.spec_fingerprint or spec.fingerprint()

        if outcome.cancelled:
            result.status = "cancelled"
            result.message = result.message or "已取消"
        elif outcome.exit_code != 0 and result.status == "done":
            result.status = "failed"
            result.message = result.message or f"进程退出码 {outcome.exit_code}"

        return result


# ---------------------------------------------------------------- 注册表

_REGISTRY: dict[str, type[Engine]] = {}


def register(cls: type[Engine]) -> type[Engine]:
    """把引擎适配器登记进注册表（用作装饰器）。"""
    if not cls.name:
        raise ValueError(f"{cls.__name__} 缺少 name")
    _REGISTRY[cls.name] = cls
    return cls


def get_engine(name: str) -> Engine:
    """按名字取引擎实例。"""
    if name not in _REGISTRY:
        raise EngineError(f"未知引擎 {name!r}；已注册: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()


def engine_names() -> list[str]:
    return sorted(_REGISTRY)


def all_engines() -> Iterable[Engine]:
    for name in engine_names():
        yield _REGISTRY[name]()


def which(executable: str) -> str | None:
    """封装 ``shutil.which``，Windows 下同时尝试 .exe。"""
    found = shutil.which(executable)
    if found:
        return found
    if not executable.lower().endswith(".exe"):
        return shutil.which(executable + ".exe")
    return None
