"""命令行入口 —— 脱离界面即可验证「结构 → 引擎 → 结构化结果」整条链路。

示例::

    python -m qcgui.cli engines
    python -m qcgui.cli run --xyz water.xyz --job opt_freq --method HF --basis def2-SVP
    python -m qcgui.cli run --xyz water.xyz --job energy --solvent water
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from .engines import all_engines, engine_names, get_engine
from .engines.base import EngineError
from .jobs import BatchRunner, JobManager
from .jobs.batch import QUANTITIES
from .model import JobSpec, JobType, Molecule


def cmd_engines(args: argparse.Namespace) -> int:
    print(f"已注册引擎: {', '.join(engine_names()) or '（无）'}\n")
    for engine in all_engines():
        ok, why = engine.available()
        mark = "可用" if ok else "不可用"
        print(f"  {engine.name:<8} {engine.display_name or '':<12} [{mark}] {why}")
        caps = ", ".join(sorted(t.value for t in engine.capabilities())) or "（无）"
        print(f"           支持任务: {caps}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    engine = get_engine(args.engine)
    ok, why = engine.available()
    if not ok:
        print(f"错误：引擎 {args.engine} 不可用 —— {why}", file=sys.stderr)
        return 2

    molecule = Molecule.from_file(
        args.structure,
        charge=args.charge,
        multiplicity=args.mult,
    )
    if args.name:
        molecule = replace(molecule, name=args.name)

    spec = JobSpec(
        molecule=molecule,
        job_type=JobType(args.job),
        method=args.method,
        basis=args.basis,
        engine=args.engine,
        nprocs=args.nprocs,
        maxcore_mb=args.maxcore,
        solvent=args.solvent,
        extra_keywords=tuple(args.keyword or ()),
        calc_hess=args.calc_hess,
        title=args.title or "",
    )

    outdir = Path(args.outdir) if args.outdir else Path(tempfile.mkdtemp(prefix="qcgui_"))
    job_dir = outdir / spec.fingerprint()
    print(f"分子    : {molecule}")
    print(f"任务    : {spec.summary()}")
    print(f"引擎    : {engine.name}")
    print(f"任务目录: {job_dir}")
    print(f"指纹    : {spec.fingerprint()}")
    print("-" * 72)

    def on_line(line: str) -> None:
        if args.verbose or line.startswith(("FINAL SINGLE POINT", "***", "ERROR")):
            print(line)

    try:
        result = engine.execute(spec, job_dir, on_line=on_line)
    except EngineError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print("-" * 72)
    for line in result.summary_lines():
        print(line)
    saved = result.save(job_dir / "result.json")
    print(f"\n结构化结果: {saved}")
    return 0 if result.ok else 1


def cmd_batch(args: argparse.Namespace) -> int:
    """批量结构相对能量。每个结构仍是独立任务，走 worker 子进程。"""
    try:
        molecules = [
            Molecule.from_file(p, charge=args.charge, multiplicity=args.mult)
            for p in args.structures
        ]
    except (OSError, ValueError, KeyError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    outdir = Path(args.outdir) if args.outdir else Path(tempfile.mkdtemp(prefix="qcgui_batch_"))
    manager = JobManager(outdir / "jobs")
    runner = BatchRunner(manager)

    print(f"结构数  : {len(molecules)}")
    print(f"任务    : {JobType(args.job).label}  {args.method}/{args.basis}"
          + (f"  SMD({args.solvent})" if args.solvent else ""))
    print(f"比较量  : {QUANTITIES.get(args.quantity, args.quantity)}")
    print(f"输出目录: {outdir}")
    print("-" * 72)

    def on_item(item) -> None:
        print(f"  [{item.status:>9}] {item.name}", flush=True)

    try:
        result = runner.run(
            molecules,
            job_type=JobType(args.job),
            method=args.method,
            basis=args.basis,
            solvent=args.solvent,
            nprocs=args.nprocs,
            maxcore_mb=args.maxcore,
            quantity=args.quantity,
            on_item=on_item,
            on_event=(lambda jid, ev: print(ev.get("line", "")) if args.verbose
                      and ev.get("event") == "log" else None),
        )
    except EngineError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print("-" * 72)
    for line in result.summary_lines():
        print(line)
    saved = result.save(outdir)
    print(f"\n结果: {saved['json']}")
    print(f"表格: {saved['csv']}")
    return 0 if result.finished else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qcgui", description="量化计算工作台（命令行）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_engines = sub.add_parser("engines", help="列出可用引擎与支持的任务")
    p_engines.set_defaults(func=cmd_engines)

    p_run = sub.add_parser("run", help="运行一次计算")
    p_run.add_argument("--structure", "--xyz", dest="structure", required=True,
                       help="结构文件（.xyz / .cjson）")
    p_run.add_argument("--job", default="energy",
                       choices=[t.value for t in JobType], help="任务类型")
    p_run.add_argument("--method", default="B3LYP", help="方法/泛函，默认 B3LYP")
    p_run.add_argument("--basis", default="def2-SVP", help="基组，默认 def2-SVP")
    p_run.add_argument("--engine", default="orca", help="引擎，默认 orca")
    p_run.add_argument("--charge", type=int, default=0)
    p_run.add_argument("--mult", type=int, default=1)
    p_run.add_argument("--solvent", default=None, help="SMD 溶剂名，如 water")
    p_run.add_argument("--nprocs", type=int, default=4)
    p_run.add_argument("--maxcore", type=int, default=2000, help="每进程内存上限 (MB)")
    p_run.add_argument("--keyword", action="append", help="追加关键字，可重复")
    p_run.add_argument("--calc-hess", action="store_true", help="TS 优化前先算精确 Hessian")
    p_run.add_argument("--name", default=None, help="分子名（影响输出文件名）")
    p_run.add_argument("--title", default="", help="输入文件标题")
    p_run.add_argument("--outdir", default=None, help="任务根目录，默认临时目录")
    p_run.add_argument("-v", "--verbose", action="store_true", help="实时打印引擎输出")
    p_run.set_defaults(func=cmd_run)

    p_batch = sub.add_parser("batch", help="批量结构相对能量")
    p_batch.add_argument("structures", nargs="+", help="结构文件（.xyz / .cjson），可多个")
    p_batch.add_argument("--job", default="energy", choices=[t.value for t in JobType])
    p_batch.add_argument("--method", default="B3LYP")
    p_batch.add_argument("--basis", default="def2-SVP")
    p_batch.add_argument("--solvent", default=None, help="SMD 溶剂名")
    p_batch.add_argument("--quantity", default="energy", choices=sorted(QUANTITIES),
                         help="用于比较的能量量")
    p_batch.add_argument("--charge", type=int, default=0, help="应用于全部结构")
    p_batch.add_argument("--mult", type=int, default=1, help="应用于全部结构")
    p_batch.add_argument("--nprocs", type=int, default=4)
    p_batch.add_argument("--maxcore", type=int, default=2000)
    p_batch.add_argument("--outdir", default=None, help="输出根目录，默认临时目录")
    p_batch.add_argument("-v", "--verbose", action="store_true")
    p_batch.set_defaults(func=cmd_batch)

    return parser


def main(argv: list[str] | None = None) -> int:
    from . import configure_stdio

    configure_stdio()
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
