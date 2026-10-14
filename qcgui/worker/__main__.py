"""worker 进程入口。

用法::

    python -m qcgui.worker <jobspec.json> <job_dir>

与父进程的协议：**stdout 上的 JSON Lines**，每个事件一行。

    {"event": "started",  "job_id": ..., "job_dir": ..., "engine": ...}
    {"event": "log",      "line": "..."}
    {"event": "finished", "status": "done", "result": {...}}
    {"event": "error",    "message": "...", "traceback": "..."}

ORCA 自身的输出被引擎重定向进任务目录里的 ``.out`` 文件，
所以 stdout 是干净的，可以专用于本协议。
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from ..model import JobSpec, Result


def emit(payload: dict) -> None:
    """输出一行事件。"""
    try:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def describe_engines() -> dict:
    """在 worker 里收集引擎能力，供界面进程查询（后者不得导入引擎）。"""
    from ..engines import all_engines

    engines = []
    for engine in all_engines():
        available, reason = engine.available()
        exe = ""
        try:
            exe = engine.resolve_executable() or ""
        except Exception:  # noqa: BLE001 - 探测失败不该让整个报告挂掉
            exe = ""
        # "从哪找到的"对可移植性很关键：换一台机器时用户需要知道
        # 到底是 PATH 里找到的、设置里填的、还是自动探测的。
        source = ""
        if exe:
            try:
                from ..orcatools import orca_source

                source = orca_source()
            except Exception:  # noqa: BLE001
                source = ""
        engines.append(
            {
                "name": engine.name,
                "display_name": engine.display_name or engine.name,
                "available": available,
                "detail": reason,
                "exe": exe,
                "source": source,
                "job_types": sorted(t.value for t in engine.capabilities()),
            }
        )
    return {"engines": engines}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)

    # 保证协议通道是 UTF-8，与父进程的解码方式一致
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if len(argv) > 1 and argv[1] == "--capabilities":
        try:
            sys.stdout.write(json.dumps(describe_engines(), ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception as exc:  # noqa: BLE001
            emit({"event": "error", "message": f"无法收集引擎能力: {exc}"})
            return 1
        return 0

    # 扫描曲线解析也要走子进程：界面进程不得导入引擎。
    # 用法: python -m qcgui.worker --scan <job_dir> [engine]
    if len(argv) > 2 and argv[1] == "--scan":
        from ..engines import get_engine

        try:
            engine = get_engine(argv[3] if len(argv) > 3 else "orca")
            profile = engine.parse_scan_profile(Path(argv[2]))
        except Exception as exc:  # noqa: BLE001
            sys.stdout.write(json.dumps(
                {"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
            sys.stdout.flush()
            return 1
        sys.stdout.write(json.dumps({"profile": profile}, ensure_ascii=False))
        sys.stdout.flush()
        return 0 if profile else 3

    if len(argv) < 3:
        emit({"event": "error", "message": "用法: python -m qcgui.worker <jobspec.json> <job_dir>"})
        return 2

    spec_path = Path(argv[1])
    job_dir = Path(argv[2])

    # 引擎只在这里被导入
    from ..engines import get_engine

    try:
        spec = JobSpec.from_dict(json.loads(spec_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError) as exc:
        emit({"event": "error", "message": f"无法读取任务定义: {exc}"})
        return 2

    emit(
        {
            "event": "started",
            "job_id": spec.fingerprint(),
            "job_dir": str(job_dir),
            "engine": spec.engine,
            "job_type": spec.job_type.value,
            "method": spec.level_of_theory,
            "summary": spec.summary(),
        }
    )

    try:
        engine = get_engine(spec.engine)
        result: Result = engine.execute(
            spec,
            job_dir,
            on_line=lambda line: emit({"event": "log", "line": line}),
        )
    except Exception as exc:  # noqa: BLE001 - worker 必须把任何异常转成事件
        emit(
            {
                "event": "error",
                "message": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        )
        return 1

    # 先落盘再汇报，父进程即使漏掉事件也能从磁盘恢复
    try:
        result.save(job_dir / "result.json")
    except OSError as exc:
        emit({"event": "error", "message": f"结果写入失败: {exc}"})

    emit(
        {
            "event": "finished",
            "status": result.status,
            "result": result.to_dict(),
        }
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
