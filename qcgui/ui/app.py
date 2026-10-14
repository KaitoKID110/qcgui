"""主界面。

设计要点
--------
* **不 import 任何量化引擎** —— 计算一律通过 ``JobManager`` 交给 worker 子进程。
* 引擎日志来自工作线程，一律经 ``queue`` 转到主线程再渲染（Tk 不是线程安全的）。
* 界面展示的数据全部来自 ``Result``，即引擎的结构化输出，不解析日志文本。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from ..jobs import BatchRunner, JobManager
from ..jobs.store import JobRecord
from ..model import JobSpec, JobType, Molecule, Result
from ..paths import jobs_root
from ..units import HARTREE_PER_K_TO_JMOLK, HARTREE_TO_KCAL, HARTREE_TO_KJMOL
from .logpanel import LogPanel

JOB_TYPE_CHOICES: list[tuple[str, JobType]] = [
    ("单点能", JobType.ENERGY),
    ("几何优化", JobType.OPT),
    ("频率 + 热力学", JobType.FREQ),
    ("优化 + 频率", JobType.OPT_FREQ),
    ("过渡态优化", JobType.TS),
    ("过渡态 + 频率", JobType.TS_FREQ),
    ("内禀反应坐标 IRC", JobType.IRC),
]

METHOD_CHOICES = [
    "HF", "B3LYP", "B3LYP-D4", "PBE0", "PBE", "M06-2X", "wB97X-D4",
    "TPSS", "r2SCAN", "MP2", "DLPNO-CCSD(T)", "CCSD(T)",
]

BASIS_CHOICES = [
    "def2-SVP", "def2-TZVP", "def2-TZVPP", "def2-QZVPP",
    "6-31G*", "6-311G**", "cc-pVDZ", "cc-pVTZ", "ma-def2-TZVP",
]

SOLVENT_CHOICES = [
    "气相", "water", "methanol", "ethanol", "acetonitrile", "dmso", "dmf",
    "dichloromethane", "chloroform", "thf", "toluene", "hexane",
]


class App(ctk.CTk):
    """主窗口。"""

    def __init__(self, manager: JobManager | None = None):
        super().__init__()
        self.title("qcgui — 量化计算工作台（ORCA）")

        ctk.set_appearance_mode("dark")
        self._setup_style()

        # 按屏幕可用空间决定初始尺寸并居中，避免窗口超出屏幕
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        width = min(1240, max(900, screen_w - 120))
        height = min(820, max(640, screen_h - 140))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2 - 20)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(900, 620)

        self.manager = manager or JobManager(jobs_root())
        self.molecule: Molecule | None = None
        self.current_job: str | None = None
        self._batch_runner: BatchRunner | None = None
        self._batch_result = None

        self._events: queue.Queue[tuple[str, dict]] = queue.Queue()

        self._build()
        self._refresh_jobs()
        self.log.put(f"任务存储目录: {self.manager.store.root}")
        ok, why = self._engine_hint()
        self.log.put(f"引擎探测（在 worker 侧执行）: {why}" if not ok else f"引擎: {why}")
        self.after(80, self._tick)

    # ================================================================ 构建

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_toolbar()

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 6))
        body.grid_columnconfigure(0, weight=0, minsize=330)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self._build_left(body)
        self._build_right(body)

        self.log = LogPanel(self, corner_radius=10)
        self.log.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.grid_rowconfigure(2, weight=0, minsize=200)

    def _setup_style(self) -> None:
        """让 ttk 的表格与深色主题协调。"""
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:  # pragma: no cover
            pass
        style.configure(
            "Treeview",
            background="#2B2B2B", fieldbackground="#2B2B2B", foreground="#DCE4EE",
            bordercolor="#3A3A3A", borderwidth=0, rowheight=22,
        )
        style.configure(
            "Treeview.Heading",
            background="#3A3A3A", foreground="#DCE4EE", relief="flat",
        )
        style.map(
            "Treeview",
            background=[("selected", "#1F6AA5")],
            foreground=[("selected", "#FFFFFF")],
        )
        style.map("Treeview.Heading", background=[("active", "#454545")])
        style.configure("Vertical.TScrollbar", background="#3A3A3A", troughcolor="#2B2B2B",
                        bordercolor="#2B2B2B", arrowcolor="#DCE4EE")

    def _build_toolbar(self) -> None:
        """两行工具栏：第一行文件操作与运行控制，第二行计算参数。"""
        bar = ctk.CTkFrame(self, corner_radius=10)
        bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))

        # ---- 第一行 ----
        row1 = ctk.CTkFrame(bar, fg_color="transparent")
        row1.pack(fill="x", padx=10, pady=(10, 4))

        ctk.CTkButton(row1, text="打开结构…", width=100, command=self._open_structure).pack(
            side="left", padx=(0, 6)
        )
        ctk.CTkButton(
            row1, text="从 SMILES…", width=104, fg_color="transparent", border_width=1,
            command=self._from_smiles,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            row1, text="示例：水", width=84, fg_color="transparent", border_width=1,
            command=self._load_example,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            row1, text="批量中间体…", width=118, fg_color="transparent", border_width=1,
            command=self.start_batch,
        ).pack(side="left", padx=6)

        self.btn_run = ctk.CTkButton(
            row1, text="▶  运行", width=100, height=32,
            font=ctk.CTkFont(size=13, weight="bold"), command=self.start_job,
        )
        self.btn_run.pack(side="right", padx=(6, 0))

        self.btn_cancel = ctk.CTkButton(
            row1, text="■ 取消", width=88, height=32, fg_color="#8B2E2E",
            hover_color="#A33A3A", state="disabled", command=self.cancel_job,
        )
        self.btn_cancel.pack(side="right", padx=6)

        # ---- 第二行 ----
        row2 = ctk.CTkFrame(bar, fg_color="transparent")
        row2.pack(fill="x", padx=10, pady=(2, 10))

        self.var_job = tk.StringVar(value=JOB_TYPE_CHOICES[0][0])
        self._labeled_combo(row2, "任务", self.var_job, [c[0] for c in JOB_TYPE_CHOICES], 118)

        self.var_method = tk.StringVar(value="B3LYP")
        self._labeled_combo(row2, "方法", self.var_method, METHOD_CHOICES, 128, editable=True)

        self.var_basis = tk.StringVar(value="def2-SVP")
        self._labeled_combo(row2, "基组", self.var_basis, BASIS_CHOICES, 118, editable=True)

        self.var_solvent = tk.StringVar(value="气相")
        self._labeled_combo(row2, "溶剂", self.var_solvent, SOLVENT_CHOICES, 108)

        ctk.CTkLabel(row2, text="核数").pack(side="left", padx=(12, 2))
        self.var_nprocs = tk.StringVar(value="4")
        ctk.CTkEntry(row2, textvariable=self.var_nprocs, width=48).pack(side="left")

    def _labeled_combo(self, parent, label, var, values, width, editable=False):
        ctk.CTkLabel(parent, text=label).pack(side="left", padx=(10, 2))
        combo = ctk.CTkComboBox(
            parent, variable=var, values=values, width=width,
            state="normal" if editable else "readonly",
        )
        combo.pack(side="left")
        return combo

    def _build_left(self, parent) -> None:
        frame = ctk.CTkFrame(parent, corner_radius=10)
        frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        frame.grid_rowconfigure(2, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(frame, text="结构", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 2)
        )
        self.lbl_structure = ctk.CTkLabel(
            frame, text="尚未载入结构", justify="left", anchor="w",
            font=ctk.CTkFont(size=12),
        )
        self.lbl_structure.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 6))

        holders = ctk.CTkFrame(frame, fg_color="transparent")
        holders.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        holders.grid_rowconfigure(0, weight=1)
        holders.grid_columnconfigure(0, weight=1)

        style = ttk.Style()
        style.configure("Atom.Treeview", rowheight=20, font=("Consolas", 10))

        self.atom_tree = ttk.Treeview(
            holders, columns=("sym", "x", "y", "z"), show="headings",
            style="Atom.Treeview", height=10,
        )
        for col, text, width in (
            ("sym", "元素", 50), ("x", "x", 80), ("y", "y", 80), ("z", "z", 80)
        ):
            self.atom_tree.heading(col, text=text)
            self.atom_tree.column(col, width=width, anchor="e" if col != "sym" else "center")
        self.atom_tree.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(holders, orient="vertical", command=self.atom_tree.yview)
        self.atom_tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")

    def _build_right(self, parent) -> None:
        frame = ctk.CTkFrame(parent, corner_radius=10)
        frame.grid(row=0, column=1, sticky="nsew")
        frame.grid_rowconfigure(1, weight=3)
        frame.grid_rowconfigure(3, weight=2)
        frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(frame, text="任务列表", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 2)
        )

        columns = ("created", "molecule", "job", "method", "status", "energy", "time")
        self.job_tree = ttk.Treeview(frame, columns=columns, show="headings", height=8)
        headers = {
            "created": ("时间", 130), "molecule": ("分子", 110), "job": ("任务", 110),
            "method": ("方法", 130), "status": ("状态", 80), "energy": ("能量 (Ha)", 130),
            "time": ("耗时 (s)", 70),
        }
        for col, (text, width) in headers.items():
            self.job_tree.heading(col, text=text)
            self.job_tree.column(col, width=width, anchor="w" if col in ("created", "molecule") else "center")
        self.job_tree.grid(row=1, column=0, sticky="nsew", padx=(12, 0))
        self.job_tree.bind("<<TreeviewSelect>>", self._on_job_selected)

        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.job_tree.yview)
        self.job_tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=1, column=1, sticky="ns")

        ctk.CTkLabel(frame, text="结果", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=2, column=0, sticky="w", padx=12, pady=(10, 2)
        )
        self.result_box = ctk.CTkTextbox(
            frame, wrap="none", font=ctk.CTkFont(family="Consolas", size=11)
        )
        self.result_box.grid(row=3, column=0, columnspan=2, sticky="nsew", padx=12, pady=(0, 12))
        self.result_box.configure(state="disabled")

    # ================================================================ 结构

    def _open_structure(self) -> None:
        path = filedialog.askopenfilename(
            title="选择结构文件",
            filetypes=[
                ("结构文件", "*.xyz *.cjson *.json"),
                ("XYZ", "*.xyz"), ("cjson", "*.cjson *.json"), ("全部", "*.*"),
            ],
        )
        if path:
            self.load_structure(path)

    def load_structure(self, path: str | Path) -> bool:
        try:
            molecule = Molecule.from_file(path)
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showerror("载入失败", str(exc))
            self.log.put(f"✖ 载入失败: {exc}")
            return False
        self.set_molecule(molecule)
        return True

    def _load_example(self) -> None:
        example = Path(__file__).resolve().parents[2] / "examples" / "water.xyz"
        if example.is_file():
            self.load_structure(example)
        else:
            messagebox.showwarning("缺少示例", f"未找到 {example}")

    def _from_smiles(self) -> None:
        dialog = ctk.CTkInputDialog(text="输入 SMILES：", title="从 SMILES 生成结构")
        smiles = dialog.get_input()
        if not smiles:
            return
        try:
            from ..cheminfo import molecule_from_smiles

            molecule = molecule_from_smiles(smiles)
        except (RuntimeError, ValueError) as exc:
            messagebox.showerror("生成失败", str(exc))
            self.log.put(f"✖ {exc}")
            return
        self.set_molecule(molecule)

    def set_molecule(self, molecule: Molecule) -> None:
        self.molecule = molecule
        self.lbl_structure.configure(
            text=(
                f"名称: {molecule.name}\n"
                f"式量: {molecule.formula}    原子数: {molecule.n_atoms}\n"
                f"电荷: {molecule.charge}    多重度: {molecule.multiplicity}"
            )
        )
        self.atom_tree.delete(*self.atom_tree.get_children())
        for atom in molecule.atoms:
            self.atom_tree.insert(
                "", "end",
                values=(atom.symbol, f"{atom.x:.6f}", f"{atom.y:.6f}", f"{atom.z:.6f}"),
            )
        self.log.put(f"载入结构: {molecule}")

    # ================================================================ 运行

    def _selected_job_type(self) -> JobType:
        label = self.var_job.get()
        for text, job_type in JOB_TYPE_CHOICES:
            if text == label:
                return job_type
        return JobType.ENERGY

    def build_spec(self) -> JobSpec:
        if self.molecule is None:
            raise ValueError("请先载入结构")
        solvent = self.var_solvent.get().strip()
        if solvent in ("", "气相", "none", "None"):
            solvent = None
        try:
            nprocs = max(1, int(self.var_nprocs.get().strip() or "1"))
        except ValueError as exc:
            raise ValueError(f"核数必须是整数: {self.var_nprocs.get()!r}") from exc
        return JobSpec(
            molecule=self.molecule,
            job_type=self._selected_job_type(),
            method=self.var_method.get().strip() or "HF",
            basis=self.var_basis.get().strip() or "def2-SVP",
            nprocs=nprocs,
            solvent=solvent,
            title=f"{self.molecule.name}",
        )

    def start_job(self) -> str | None:
        try:
            spec = self.build_spec()
        except ValueError as exc:
            messagebox.showwarning("无法运行", str(exc))
            return None

        record = self.manager.submit(spec, on_event=self._on_event)
        self.current_job = record.job_id
        self._set_running(True)
        self.log.put("")
        self.log.put(f"▶ 提交任务 {record.job_id}  ·  {spec.summary()}")
        self._refresh_jobs()
        return record.job_id

    def cancel_job(self) -> None:
        runner = getattr(self, "_batch_runner", None)
        if runner is not None:
            runner.cancel()
            self.log.put("■ 正在取消整批 …")
            return
        if self.current_job and self.manager.cancel(self.current_job):
            self.log.put(f"■ 正在取消 {self.current_job} …")

    # ================================================================ 批量

    def start_batch(self) -> None:
        """批量中间体相对能量：多选结构 → 同级别跑完 → 相对能量表。"""
        paths = filedialog.askopenfilenames(
            title="选择要比较的结构（可多选）",
            filetypes=[("结构文件", "*.xyz *.cjson *.json"), ("全部", "*.*")],
        )
        if not paths:
            return
        try:
            molecules = [Molecule.from_file(p) for p in paths]
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showerror("载入失败", str(exc))
            return

        self._batch_result = None
        self._batch_runner = BatchRunner(self.manager)
        self._set_running(True)
        self.log.put("")
        self.log.put(f"▶ 批量计算 {len(molecules)} 个结构（每个结构一个独立任务）")
        threading.Thread(
            target=self._run_batch, args=(molecules,), name="batch", daemon=True
        ).start()

    def _run_batch(self, molecules: list) -> None:
        """在工作线程里跑整批；界面更新一律经队列回主线程。"""
        try:
            spec = self.build_spec()
        except ValueError as exc:
            self._events.put(("", {"event": "error", "message": str(exc)}))
            self._events.put(("", {"event": "batch_finished", "batch": None}))
            return

        try:
            batch = self._batch_runner.run(
                molecules,
                job_type=spec.job_type,
                method=spec.method,
                basis=spec.basis,
                solvent=spec.solvent,
                nprocs=spec.nprocs,
                maxcore_mb=spec.maxcore_mb,
                quantity="energy",
                on_event=self._on_event,
                on_item=lambda item: self.log.put(
                    f"    [{item.status:>9}] {item.name}"
                    + (f"  {item.value:.8f} Ha" if item.value is not None else "")
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 任何异常都要回到界面
            self._events.put(("", {"event": "error", "message": f"批量计算异常: {exc}"}))
            batch = None
        self._events.put(("", {"event": "batch_finished", "batch": batch}))

    def show_batch(self, batch) -> None:
        self._batch_result = batch
        self._batch_runner = None
        lines = batch.summary_lines()
        try:
            saved = batch.save(self.manager.store.root.parent / "batches")
            lines += ["", f"结果已保存: {saved['json']}", f"表格已保存: {saved['csv']}"]
            self.log.put(f"✔ 批量完成，表格已保存到 {saved['csv']}")
        except OSError as exc:
            lines.append(f"（保存失败: {exc}）")
        self.result_box.configure(state="normal")
        self.result_box.delete("1.0", "end")
        self.result_box.insert("1.0", "\n".join(lines))
        self.result_box.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        self.btn_run.configure(state="disabled" if running else "normal")
        self.btn_cancel.configure(state="normal" if running else "disabled")

    # ================================================================ 事件

    def _on_event(self, job_id: str, event: dict) -> None:
        """由 worker 读取线程调用 —— 只入队，绝不碰控件。"""
        self._events.put((job_id, event))

    def _tick(self) -> None:
        try:
            self._drain_events()
            self.log.drain()
        finally:
            self.after(80, self._tick)

    def _drain_events(self) -> None:
        while True:
            try:
                job_id, event = self._events.get_nowait()
            except queue.Empty:
                return
            kind = event.get("event")
            if kind == "log":
                self.log.put(event.get("line", ""))
            elif kind == "running":
                self.log.put(f"   worker 已启动，任务目录 {event.get('job_dir', '')}")
                self._refresh_jobs()
            elif kind == "finished":
                status = event.get("status", "?")
                mark = "✔" if status == "done" else "✖"
                self.log.put(f"{mark} 任务结束: {status}")
                self._set_running(False)
                self._refresh_jobs()
                self.show_result(job_id)
            elif kind == "failed":
                self.log.put(f"✖ {event.get('message', '')}")
                self._set_running(False)
                self._refresh_jobs()
            elif kind == "error":
                self.log.put(f"✖ worker 错误: {event.get('message', '')}")
            elif kind == "raw":
                self.log.put(event.get("line", ""))
            elif kind == "batch_finished":
                self._set_running(False)
                self._refresh_jobs()
                batch = event.get("batch")
                if batch is not None:
                    self.show_batch(batch)

    def _engine_hint(self) -> tuple[bool, str]:
        """只做展示用的粗探测，不导入引擎模块。"""
        from shutil import which

        import os

        explicit = os.environ.get("QCGUI_ORCA_EXE")
        if explicit and Path(explicit).is_file():
            return True, explicit
        found = which("orca") or which("orca.exe")
        if found:
            return True, found
        return False, "PATH 中未找到 orca；worker 运行时会报错"

    # ================================================================ 结果

    def _refresh_jobs(self) -> None:
        selected = self.job_tree.selection()
        self.job_tree.delete(*self.job_tree.get_children())
        for record in self.manager.store.list(limit=100):
            self.job_tree.insert("", "end", iid=record.job_id, values=_record_row(record))
        if selected and self.job_tree.exists(selected[0]):
            self.job_tree.selection_set(selected[0])

    def _on_job_selected(self, _event=None) -> None:
        selection = self.job_tree.selection()
        if selection:
            self.show_result(selection[0])

    def show_result(self, job_id: str) -> Result | None:
        result = self.manager.store.load_result(job_id)
        record = self.manager.store.get(job_id)
        text = _format_result(result, record)
        self.result_box.configure(state="normal")
        self.result_box.delete("1.0", "end")
        self.result_box.insert("1.0", text)
        self.result_box.configure(state="disabled")
        return result

    # ================================================================ 收尾

    def on_close(self) -> None:
        running = self.manager.running_jobs()
        if running:
            if not messagebox.askyesno(
                "仍有任务在运行",
                f"有 {len(running)} 个任务正在运行，退出会终止它们。确定退出吗？",
            ):
                return
            for job_id in running:
                self.manager.cancel(job_id)
        self.destroy()


# ==================================================================== 格式化


def _record_row(record: JobRecord) -> tuple:
    created = ""
    if record.created_at:
        import time as _time

        created = _time.strftime("%m-%d %H:%M:%S", _time.localtime(record.created_at))
    energy = f"{record.final_energy:.8f}" if record.final_energy is not None else ""
    duration = f"{record.duration:.1f}" if record.duration else ""
    return (
        created, record.molecule, record.job_type, record.method,
        record.status, energy, duration,
    )


def _format_result(result: Result | None, record: JobRecord | None) -> str:
    if result is None:
        if record is None:
            return "（选中一个任务查看结果）"
        lines = [f"任务 {record.job_id}", f"状态: {record.status}"]
        if record.message:
            lines.append(f"说明: {record.message}")
        lines.append("")
        lines.append("（尚无结构化结果 —— 任务可能仍在运行或已失败）")
        return "\n".join(lines)

    lines = list(result.summary_lines())
    lines.append("")

    if result.thermo:
        t = result.thermo
        lines.append("── 热化学 ──")
        if "temperature_K" in t:
            lines.append(f"温度/压力   : {t['temperature_K']} K, {t.get('pressure_atm', '?')} atm")
        for key, label, factor, unit in (
            ("zpe_hartree", "零点能 ZPE", HARTREE_TO_KJMOL, "kJ/mol"),
            ("inner_energy_U_hartree", "内能 U", HARTREE_TO_KJMOL, "kJ/mol"),
            ("enthalpy_H_hartree", "焓 H", HARTREE_TO_KJMOL, "kJ/mol"),
            ("gibbs_free_energy_G_hartree", "自由能 G", HARTREE_TO_KJMOL, "kJ/mol"),
        ):
            if key in t:
                lines.append(f"{label:<12}: {t[key]:.8f} Ha  =  {t[key] * factor:.2f} {unit}")
        if "entropy_S_hartree_per_K" in t:
            lines.append(
                f"熵 S        : {t['entropy_S_hartree_per_K']:.8e} Ha/K"
                f"  =  {t['entropy_S_hartree_per_K'] * HARTREE_PER_K_TO_JMOLK:.2f} J/(mol·K)"
            )
        lines.append("")

    if result.frequencies:
        real = [f for f in result.frequencies if abs(f) > 1e-6]
        lines.append("── 振动频率 (cm⁻¹) ──")
        lines.append("  " + "  ".join(f"{f:.1f}" for f in real))
        lines.append(f"  虚频个数: {result.n_imaginary_frequencies}")
        lines.append("")

    if result.irc:
        irc = result.irc
        kcal = HARTREE_TO_KJMOL / 4.184
        lines.append("── IRC 反应路径 ──")
        lines.append(f"轨迹帧数: {irc.get('n_frames', '?')}")
        if irc.get("ts_frame_index") is not None and irc.get("ts_energy") is not None:
            lines.append(
                f"过渡态  : 第 {irc['ts_frame_index']} 帧, E = {irc['ts_energy']:.8f} Ha"
            )
        barriers = irc.get("barriers_kcal") or []
        if len(barriers) == 2:
            lines.append(f"两端能垒: {barriers[0]:.2f} / {barriers[1]:.2f} kcal/mol")
        energies = [e for e in (irc.get("frame_energies") or []) if e is not None]
        if energies:
            base = min(energies)
            profile = "  ".join(f"{(e - base) * kcal:.1f}" for e in energies)
            lines.append("相对能量剖面 (kcal/mol，以最低点为 0)：")
            lines.append("  " + profile)
        for role, path in (irc.get("files") or {}).items():
            lines.append(f"  {role:<20}: {path}")
        lines.append("")

    charges = result.charges()
    if charges and result.geometry:
        lines.append("── 原子电荷 (Löwdin) ──")
        for sym, q in zip(result.geometry["symbols"], charges):
            lines.append(f"  {sym:<3s} {q:>8.4f}")
        lines.append("")

    if result.mayer_bond_orders.get("values"):
        lines.append("── Mayer 键级 ──")
        lines.append("  " + "  ".join(f"{v:.3f}" for v in result.mayer_bond_orders["values"]))
        lines.append("")

    if result.dipole and result.dipole.get("magnitude") is not None:
        lines.append(f"偶极矩: {result.dipole['magnitude']:.4f} D")
        lines.append("")

    if result.files:
        lines.append("── 产出文件 ──")
        for role, path in result.files.items():
            lines.append(f"  {role:<12}: {path}")

    return "\n".join(lines)
