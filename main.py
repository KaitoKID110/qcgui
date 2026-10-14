#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""qcgui 入口。

默认启动**网页界面**（本地服务 + 浏览器）；加 ``--tk`` 使用原来的桌面界面。

.. important::
   本文件与 ``qcgui.ui`` / ``qcgui.web`` 都**不 import 任何量化引擎**。
   计算全部通过 ``JobManager`` 转给 worker 子进程执行。
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
from pathlib import Path

if platform.system() == "Windows":
    os.environ.setdefault("PYTHONUTF8", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _fatal(message: str) -> None:
    """启动失败时给出可见提示。

    快捷方式是用 ``pythonw.exe`` 启动的，没有控制台；如果模块导入失败，
    进程会静默退出，用户只看到"点了没反应"。所以这里必须弹窗。
    """
    print(message, file=sys.stderr)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("qcgui 启动失败", message)
        root.destroy()
    except Exception:  # 连 tkinter 都不可用时只能靠 stderr
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qcgui", description="量化计算工作台")
    parser.add_argument("--tk", action="store_true",
                        help="使用原来的桌面界面（默认是网页界面）")
    parser.add_argument("--port", type=int, default=None, help="网页服务端口")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--structure", help="桌面界面启动时载入的结构文件")
    parser.add_argument("--selftest", action="store_true",
                        help="桌面界面自检：载入示例 → 跑一个任务 → 打印结果后退出")
    parser.add_argument("--doctor", action="store_true",
                        help="环境诊断：检查 Python / 依赖 / ORCA / 外部工具 / 数据目录")
    parser.add_argument("--job", default="单点能", help="自检时使用的任务类型（中文名）")
    return parser


# ------------------------------------------------------------------ 网页界面

def run_web(args: argparse.Namespace) -> int:
    try:
        from qcgui.web import run_server
    except Exception as exc:  # noqa: BLE001
        # 不要在提示里写死某个 conda 环境名 —— 换一台机器那句话就是误导。
        # 缺什么让 --doctor 去说，它能把具体包名和装法都列出来。
        _fatal(
            f"无法加载网页界面：{type(exc).__name__}: {exc}\n\n"
            "多半是缺少 Python 依赖。在终端运行：\n"
            f"    \"{sys.executable}\" main.py --doctor\n"
            "它会逐项列出缺什么、怎么装。"
        )
        return 2
    try:
        run_server(port=args.port, open_browser=not args.no_browser)
    except KeyboardInterrupt:
        print("\n服务已停止。")
    except OSError as exc:
        _fatal(f"启动服务失败：{exc}")
        return 3
    return 0


# ------------------------------------------------------------------ 桌面界面

def run_tk(args: argparse.Namespace) -> int:
    from qcgui import configure_stdio

    configure_stdio()

    try:
        from qcgui.ui.app import App
    except Exception as exc:  # noqa: BLE001 - 必须让用户看到原因
        _fatal(
            f"无法加载界面模块：{type(exc).__name__}: {exc}\n\n"
            "在终端运行下面这条能看到完整回溯，并查清缺什么：\n"
            f"    \"{sys.executable}\" main.py --doctor"
        )
        return 2

    exit_code = {"value": 0}
    try:
        app = App()
        app.protocol("WM_DELETE_WINDOW", app.on_close)

        if args.selftest:
            _schedule_selftest(app, args, exit_code)
        elif args.structure:
            app.after(100, lambda: app.load_structure(args.structure))

        app.mainloop()
    except Exception as exc:  # noqa: BLE001
        _fatal(f"运行时错误：{type(exc).__name__}: {exc}")
        return 3

    return exit_code["value"]


def main(argv: list[str] | None = None) -> int:
    from qcgui import configure_stdio

    configure_stdio()
    args = build_parser().parse_args(argv)

    if args.doctor:
        from qcgui.doctor import run_doctor

        return run_doctor()
    if args.tk or args.selftest:
        return run_tk(args)
    return run_web(args)


def _schedule_selftest(app, args: argparse.Namespace, exit_code: dict) -> None:
    """把一次完整流程跑一遍，并对界面真正显示出来的内容做断言。"""
    root = Path(__file__).resolve().parent
    structure = Path(args.structure) if args.structure else root / "examples" / "water.xyz"

    def start() -> None:
        if not app.load_structure(structure):
            exit_code["value"] = 1
            app.destroy()
            return
        app.var_job.set(args.job)
        app.var_method.set("HF")
        app.var_basis.set("def2-SVP")
        job_id = app.start_job()
        if job_id is None:
            exit_code["value"] = 1
            app.destroy()
            return
        app.after(400, poll)

    def poll() -> None:
        if app.manager.running_jobs():
            app.after(400, poll)
            return
        app.after(300, finish)

    def finish() -> None:
        # 无论打印是否出问题，都必须销毁窗口 —— 否则主循环不退出，表现为卡死
        try:
            displayed = app.result_box.get("1.0", "end")
            print("=" * 68)
            print("界面显示的结果：")
            print(displayed.rstrip())
            print("=" * 68)

            engines_imported = "qcgui.engines" in sys.modules
            print(f"界面进程是否导入量化引擎: {engines_imported}")
            ok = ("状态      : done" in displayed) and not engines_imported
            print("SELFTEST " + ("OK" if ok else "FAIL"))
            exit_code["value"] = 0 if ok else 1
        except Exception as exc:  # noqa: BLE001
            print(f"SELFTEST FAIL: {type(exc).__name__}: {exc}")
            exit_code["value"] = 1
        finally:
            app.destroy()

    app.after(300, start)


if __name__ == "__main__":
    raise SystemExit(main())
