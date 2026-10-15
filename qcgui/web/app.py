"""网页界面后端。

架构位置：``浏览器 → Flask 路由 → JobManager → worker 子进程 → 引擎``。
本模块**不 import 任何量化引擎**；引擎能力经由
``python -m qcgui.worker --capabilities`` 子进程查询（见 ``probe_engines``）。

实时性：计算日志与状态变化通过 **SSE**（Server-Sent Events）推给浏览器，
前端无需轮询日志。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

from .. import __version__
from ..config import load_config, save_config, tool_status
from ..external import (
    convert_with_obabel,
    open_in_file_manager,
    run_multiwfn,
    view_with_avogadro,
)
from ..jobs import BatchRunner, JobManager
from ..model import JobSpec, JobType, Molecule, scan_warnings
from ..model.trajectory import read_xyz_frames
from ..orcatools import export_mo_cube, make_molden, probe as probe_orca_tools
from ..paths import jobs_root
from ..recipes import all_recipes, delete_user_recipe, get_recipe, save_user_recipe
from ..units import HARTREE_TO_KCAL, HARTREE_TO_KJMOL

STATIC_DIR = Path(__file__).resolve().parent / "static"
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"

#: 前端可用的任务类型（含中文标签），与 JobType 一一对应
JOB_TYPE_LABELS: dict[str, str] = {
    "energy": "单点能",
    "opt": "几何优化",
    "freq": "频率 + 热力学",
    "opt_freq": "优化 + 频率",
    "ts": "过渡态优化",
    "ts_freq": "过渡态 + 频率",
    "irc": "内禀反应坐标 IRC",
    "neb_ts": "NEB-TS 自动搜索过渡态",
    "scan": "势能面扫描",
}

SOLVENT_CHOICES = [
    "water", "methanol", "ethanol", "acetonitrile", "dmso", "dmf",
    "dichloromethane", "chloroform", "thf", "toluene", "hexane",
]


# ------------------------------------------------------------------ 事件总线

class EventBus:
    """把 JobManager 的事件广播给所有 SSE 订阅者。"""

    def __init__(self) -> None:
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=4000)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, job_id: str, event: dict) -> None:
        payload = {"job_id": job_id, **event}
        with self._lock:
            subscribers = list(self._subs)
        for q in subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass  # 慢消费者丢帧，不影响任务

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


# ------------------------------------------------------------ 引擎能力探测

_engine_cache: dict = {"data": None, "ts": 0.0}
_engine_lock = threading.Lock()


def probe_engines(force: bool = False, ttl: float = 30.0) -> list[dict]:
    """通过 worker 子进程查询引擎能力（本进程不导入引擎）。"""
    with _engine_lock:
        now = time.time()
        cached = _engine_cache["data"]
        if not force and cached is not None and now - _engine_cache["ts"] < ttl:
            return cached

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    engines: list[dict] = []
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "qcgui.worker", "--capabilities"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(REPO_ROOT),
            env=env,
            timeout=60,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            engines = json.loads(proc.stdout)["engines"]
    except (OSError, ValueError, subprocess.SubprocessError, KeyError):
        engines = []

    with _engine_lock:
        _engine_cache.update(data=engines, ts=time.time())
    return engines


def parse_scan_profile(job_dir: Path, engine: str = "orca") -> dict | None:
    """通过 worker 子进程解析扫描曲线（本进程不导入引擎）。

    不是扫描任务时返回 ``None``；子进程报错时抛 ``ValueError``。
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "qcgui.worker", "--scan", str(job_dir), engine],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(REPO_ROOT),
            env=env,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"扫描解析子进程失败: {exc}") from exc

    payload: dict = {}
    if proc.stdout and proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            payload = {}
    if payload.get("error"):
        raise ValueError(str(payload["error"]))
    return payload.get("profile")


# ------------------------------------------------------------------ 序列化

def _supported_job_types(engines: list[dict]) -> list[dict]:
    """只列出**当前可用引擎真支持**的任务类型。

    以前这里把 ``JobType`` 全列出来了，于是下拉里出现了引擎根本不支持的
    ``neb_ts`` / ``scan``（而且标签没登记，直接显示成原始值），用户一选就失败。
    """
    supported: set[str] = set()
    for engine in engines or []:
        if engine.get("available"):
            supported.update(engine.get("job_types") or [])

    # 一个可用引擎都没有时，把全部列出来让用户至少能看到选项（运行时会报错）
    chosen = [t for t in JobType if not supported or t.value in supported]
    return [
        {"value": t.value, "label": JOB_TYPE_LABELS.get(t.value) or t.label or t.value}
        for t in chosen
    ]


def molecule_to_dict(molecule: Molecule) -> dict:
    return {
        "name": molecule.name,
        "formula": molecule.formula,
        "n_atoms": molecule.n_atoms,
        "charge": molecule.charge,
        "multiplicity": molecule.multiplicity,
        "symbols": list(molecule.symbols),
        "coords": [list(c) for c in molecule.coords],
    }


def molecule_from_payload(data: dict) -> Molecule:
    from ..model import Atom

    if not isinstance(data, dict):
        raise ValueError(f"molecule 必须是对象，收到 {type(data).__name__}")
    symbols = data.get("symbols") or []
    coords = data.get("coords") or []
    if not isinstance(symbols, (list, tuple)) or not isinstance(coords, (list, tuple)):
        raise ValueError("symbols 与 coords 都必须是列表")
    if not symbols or len(symbols) != len(coords):
        raise ValueError("symbols 与 coords 缺失或长度不一致")
    try:
        atoms = tuple(
            Atom(str(s), float(c[0]), float(c[1]), float(c[2]))
            for s, c in zip(symbols, coords)
        )
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"坐标无法解析: {exc}") from exc
    return Molecule(
        atoms,
        _int_field(data, "charge", 0) or 0,
        _int_field(data, "multiplicity", 1) or 1,
        str(data.get("name") or "mol"),
    )


def _read_output_tail(job_dir: str, limit: int = 300_000) -> str:
    """只读 ORCA 输出的**尾部**。

    收敛公告在文件最后，没必要读整个 17 MB。
    """
    try:
        outs = sorted(Path(job_dir).glob("*.out"))
        if not outs:
            return ""
        path = outs[0]
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            return fh.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


def _result_payload(result, spec=None) -> dict | None:
    """结果的可序列化形式，并按需**补算**读取时才能判的检查。

    为什么要在读的时候补：这两项检查都是后来才加的，之前存下的
    ``result.json`` 里没有对应结论 —— 实测那个消除成丁二烯的任务
    在历史库里仍然显示"结果可用"，而这正是最该被拦下的一条。

    两项都便宜（成键只比坐标；收敛只读输出尾部），放在读取路径上没问题。
    """
    if result is None:
        return None
    from ..model import Issue, SEVERITY_BLOCKING, SEVERITY_MAJOR
    from ..model.geometry import MOVING_JOB_TYPES, describe_connectivity_change
    from ..model.result import describe_convergence_problem

    data = result.to_dict()
    issues = list(result.issues or [])
    known = {i.code for i in issues}
    job_type = getattr(getattr(spec, "job_type", None), "value", None) if spec else None
    added = False

    if spec is not None and job_type in MOVING_JOB_TYPES \
            and "connectivity_changed" not in known:
        geometry = result.geometry or {}
        coords_after = geometry.get("coords")
        before = getattr(spec.molecule, "coords", None)
        if coords_after and before and len(geometry.get("symbols") or before) == len(before):
            try:
                described = describe_connectivity_change(
                    list(geometry.get("symbols") or spec.molecule.symbols),
                    before, coords_after,
                )
            except (TypeError, ValueError, KeyError, IndexError):
                described = None
            if described:
                issues.append(Issue(
                    code="connectivity_changed",
                    severity=SEVERITY_BLOCKING,
                    message=(f"算出来的**已经不是原来的分子**了：{described}。"
                             f"这个几何不能当作 {spec.molecule.name or '原分子'} 的结构用。"),
                ))
                added = True

    if spec is not None and job_type in MOVING_JOB_TYPES \
            and not ({"not_converged", "no_convergence_notice"} & known):
        # 只有正常结束的任务才谈得上"收敛公告"；失败的任务由状态判定覆盖
        if result.status == "done":
            text = _read_output_tail(result.job_dir)
            problem = describe_convergence_problem(text) if text else None
            if problem:
                issues.append(Issue(code="not_converged", severity=SEVERITY_MAJOR,
                                    message=problem))
                added = True

    if added:
        result.issues = issues
        result.warnings = [i.message for i in issues]
        data["issues"] = [i.to_dict() for i in issues]
        data["warnings"] = [i.message for i in issues]
        # 结论跟着重算 —— 否则同一次响应里的字段会自相矛盾
        data["verdict"] = result.verdict
        data["verdict_label"] = result.verdict_label
        data["verdict_reason"] = result.verdict_reason
    return data


def _apply_charge_multiplicity(molecule: Molecule, payload: dict) -> Molecule:
    """把**顶层的** ``charge`` / ``multiplicity`` 应用到分子上。

    为什么需要单独处理：走「几何来源」时，分子是从上游任务的结果几何重建的，
    电荷与多重度取的是**上游那次计算**的值。用户在界面里把电荷改成 -1、
    多重度改成 2，那两个数只写在顶层字段里 —— 以前被**静默忽略**，
    于是算的是另一个自旋/电荷态，而且不报任何错（审计 F3）。
    对带电体系（比如 MeO⁻ 进攻）来说这会让整个结果对应错误的状态。

    给了就覆盖，**没给就沿用** —— 不能把上游的 0/1 无脑盖成默认值。
    """
    from dataclasses import replace

    updates: dict = {}
    for key, label in (("charge", "电荷"), ("multiplicity", "多重度")):
        if key not in payload:
            continue
        raw = payload[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"{label}必须是整数，收到 {type(raw).__name__}")
        value = int(raw)
        if key == "charge" and not (-20 <= value <= 20):
            raise ValueError(f"电荷 {value} 超出合理范围（-20 ~ 20）")
        if key == "multiplicity" and value < 1:
            raise ValueError(f"多重度必须 ≥ 1，收到 {value}")
        updates[key] = value
    if not updates:
        return molecule
    return replace(molecule, **updates)


def molecule_from_job(manager: JobManager, job_id: str) -> Molecule:
    """取某个已完成任务的结果几何，作为下一个计算的起点。

    这是 ACH-Orca-Workbench 里 "Geometry: From parent's optimised geometry"
    的等价物 —— 没有它，优化完还得手工把 xyz 重新载进来。
    """
    from ..model import Atom

    record = manager.store.get(job_id)
    if record is None:
        raise ValueError(f"上游任务不存在: {job_id}")
    result = manager.store.load_result(job_id)
    if result is None or not result.geometry:
        raise ValueError(f"上游任务 {job_id} 没有可用的结果几何")
    geometry = result.geometry
    atoms = tuple(
        Atom(str(s), float(c[0]), float(c[1]), float(c[2]))
        for s, c in zip(geometry["symbols"], geometry["coords"])
    )
    name = (record.molecule or "mol").split()[0]
    return Molecule(
        atoms,
        int(result.charge if result.charge is not None else 0),
        int(result.multiplicity if result.multiplicity is not None else 1),
        f"{name}_{record.job_type}",
    )


def _string_field(payload: dict, key: str, default: str = "") -> str:
    """取一个必须是字符串的字段，不是字符串就抛 ``ValueError``。

    调用方传来的 JSON 里出现嵌套对象是很容易犯的失误 —— 例如 PowerShell 的
    ``Get-Content`` 结果带着 PSPath/PSDrive 等 NoteProperty，被
    ``ConvertTo-Json`` 一序列化就成了对象，看着像普通字符串却不是。
    这类畸形输入应当得到 400，而不是一路穿到 ``Path / dict`` 抛 500。
    """
    value = payload.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串，收到 {type(value).__name__}")
    return value


def _json_payload() -> dict:
    """取请求体，并保证拿到的是 dict。

    ``request.get_json(silent=True) or {}`` 并不够 —— 合法 JSON 也可能是
    字符串 / 数字 / 数组，那样 payload 就不是 dict，之后任何 ``.get()``
    都会 AttributeError 变成 500。这类畸形输入应当是 400。
    """
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _int_field(payload: dict, key: str, default: int | None = None) -> int | None:
    """取一个整数参数；不是整数就抛 ``ValueError``（调用方转成 400）。"""
    value = payload.get(key, default)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{key} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} 必须是整数，收到 {value!r}")


def _bounded_int(payload: dict, key: str, default: int, low: int,
                 high: int | None = None, what: str = "") -> int:
    """取一个有范围限制的整数参数；越界就抛 ``ValueError``。

    **不做静默夹取** —— 用户要 4 核却悄悄按 1 核跑，比直接报错糟糕得多。
    """
    value = _int_field(payload, key, default)
    if value is None:
        return default
    if value < low or (high is not None and value > high):
        limit = f"{low}–{high}" if high is not None else f"不小于 {low}"
        raise ValueError(f"{what or key} 必须{limit}，收到 {value}")
    return value


def _keywords_field(payload: dict, key: str = "extra_keywords") -> tuple[str, ...]:
    """解析额外关键字；必须是**字符串列表**。

    以前是 ``tuple(payload.get(key) or ())`` —— 传一个字符串 ``"TightSCF"``
    会被 ``tuple()`` **按字符拆开**，生成

        ! HF STO-3G T i g h t S C F

    用户看着莫名其妙，ORCA 还会因为不认识 ``T`` 而报错。
    独立审计实测确认，这里补上形状校验。
    """
    raw = payload.get(key)
    if not raw:
        return ()
    if isinstance(raw, str):
        raise ValueError(
            f"{key} 必须是字符串列表（例如 [\"TightSCF\"]），"
            f"不能直接给一个字符串 —— 那会被按字符拆开"
        )
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{key} 必须是字符串列表，收到 {type(raw).__name__}")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"{key} 的每一项都必须是字符串，收到 {type(item).__name__}")
        if item.strip():
            out.append(item.strip())
    return tuple(out)


def _blocks_field(payload: dict, key: str = "extra_blocks"):
    """解析 ``[[块名, 内容], ...]``；形状不对就抛 ``ValueError``。"""
    raw = payload.get(key)
    if not raw:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{key} 必须是 [[块名, 内容], ...] 形式")
    parsed: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, str) or not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"{key} 的每一项都必须是 [块名, 内容] 两个元素")
        parsed.append((str(item[0]), str(item[1])))
    return tuple(parsed)


#: 外部工具**能真正读入**的文件后缀。
#:
#: 给错类型时它们不会报错，只是打不开 —— 界面上的表现就是"点了按钮没反应"。
#: 所以必须在服务端拦住：Multiwfn 读不了 ORCA 的 .gbw，Avogadro 读不了 .inp。
_TOOL_SUFFIXES = {
    "multiwfn": (".molden", ".molden.input", ".wfn", ".wfx", ".fch", ".fchk",
                 ".mkl", ".mwfn", ".cube"),
    "avogadro": (".xyz", ".mol", ".sdf", ".pdb", ".cif", ".cube",
                 ".molden", ".molden.input", ".out"),
}


def _readable_by(tool: str, path) -> bool:
    """这个文件是 ``tool`` 读得了的类型吗。"""
    suffixes = _TOOL_SUFFIXES.get(tool)
    if not suffixes:
        return True
    return str(path).lower().endswith(suffixes)


def _external_file(tool: str, job_id: str, result, manager) -> str | None:
    """给外部工具挑一个它**真能读**的文件，必要时现场生成。

    工具栏原本按固定 role 取文件 —— multiwfn 拿 ``.gbw``、avogadro 拿 ``.inp``，
    而这两个程序都读不了 ORCA 的这两种文件，界面表现就是"点了没反应"。
    """
    files = result.files or {}

    if tool == "multiwfn":
        # Multiwfn 读不了 ORCA 的 .gbw，必须给 Molden；没有就现场转一个
        if files.get("molden"):
            return files["molden"]
        gbw = files.get("wavefunction")
        if not gbw:
            return None
        run = make_molden(manager.store.job_dir(job_id), gbw)
        if run.ok:
            result.files["molden"] = run.artifacts[0]
            manager.store.save_result(job_id, result)
            return run.artifacts[0]
        return None

    if tool == "avogadro":
        # 优先给 cube（Avogadro 能画轨道等值面），其次 Molden（含几何与轨道系数）
        cubes = sorted(
            path for role, path in files.items()
            if role.startswith("mo") and str(path).endswith(".cube")
        )
        if cubes:
            return cubes[-1]
        if files.get("molden"):
            return files["molden"]
        # 兜底：把结果几何写成 xyz —— "打开当前结构"最直接的答案
        geometry = getattr(result, "geometry", None) or {}
        symbols = geometry.get("symbols")
        if symbols:
            target = manager.store.job_dir(job_id) / "result_geometry.xyz"
            rows = [str(len(symbols)), "qcgui result geometry"]
            for symbol, coord in zip(symbols, geometry.get("coords") or []):
                rows.append("%-3s %14.8f %14.8f %14.8f" % (symbol, *coord))
            target.write_text("\n".join(rows) + "\n", encoding="ascii")
            result.files["xyz"] = str(target)
            manager.store.save_result(job_id, result)
            return str(target)
        return None

    for role in ("molden", "output", "input"):
        if files.get(role):
            return files[role]
    return None


# ------------------------------------------------------------------ 应用工厂

def _attach_bus(manager: JobManager, bus: EventBus) -> None:
    """把 JobManager 的事件转发到事件总线。

    注意：manager 可能是**外部注入**的（测试、嵌入式用法），此时它的
    ``on_event`` 未必指向总线；必须在这里接上，否则界面收不到任何实时事件。
    """
    previous = getattr(manager, "on_event", None)

    def forward(job_id: str, event: dict) -> None:
        bus.publish(job_id, event)
        if previous is not None:
            try:
                previous(job_id, event)
            except Exception:  # 旧回调异常不应影响广播
                pass

    manager.on_event = forward


def create_app(manager: JobManager | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["JSON_AS_ASCII"] = False

    bus = EventBus()
    mgr = manager or JobManager(jobs_root())
    _attach_bus(mgr, bus)
    batches: dict[str, dict] = {}
    batches_lock = threading.Lock()
    batch_seq = {"n": 0}

    app.config["MANAGER"] = mgr
    app.config["BUS"] = bus
    app.config["BATCHES"] = batches

    # ------------------------------------------------------------ 静态资源

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(STATIC_DIR, filename)

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(REPO_ROOT / "docs", "qcgui.ico")

    # ---------------------------------------------------------------- 状态

    @app.get("/api/status")
    def api_status():
        engines = probe_engines()
        config = load_config()
        return jsonify(
            {
                "version": __version__,
                "jobs_root": str(mgr.store.root),
                "engines": engines,
                "tools": tool_status(config),
                "config": config,
                "job_types": _supported_job_types(engines),
                "solvents": SOLVENT_CHOICES,
                "units": {"hartree_to_kjmol": HARTREE_TO_KJMOL, "hartree_to_kcal": HARTREE_TO_KCAL},
            }
        )

    @app.get("/api/config")
    def api_config_get():
        return jsonify(load_config())

    @app.post("/api/config")
    def api_config_set():
        payload = _json_payload()
        config = load_config()
        config.update({k: v for k, v in payload.items() if k in config})
        path = save_config(config)
        # 设置对话框写着"修改后立即生效"，但引擎探测有 30 秒缓存 ——
        # 刚填完 ORCA 路径却还显示旧的探测结果，看起来就像没保存上。
        # 保存配置时顺手把缓存清掉。
        engines = probe_engines(force=True)
        return jsonify({"ok": True, "path": str(path), "config": config,
                        "engines": engines})

    # ---------------------------------------------------------------- 结构

    @app.get("/api/examples")
    def api_examples():
        items = []
        if EXAMPLES_DIR.is_dir():
            for path in sorted(EXAMPLES_DIR.glob("*")):
                if path.suffix.lower() in (".xyz", ".cjson", ".json"):
                    items.append({"name": path.name, "path": str(path)})
        return jsonify({"examples": items})

    @app.post("/api/structure/load")
    def api_structure_load():
        payload = _json_payload()
        try:
            path = _string_field(payload, "path").strip()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if not path:
            return jsonify({"error": "缺少 path"}), 400
        try:
            molecule = Molecule.from_file(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400
        # 一并把来源路径给前端：工具栏的 Avogadro / Multiwfn 需要它
        return jsonify({"molecule": molecule_to_dict(molecule), "path": str(path)})

    @app.post("/api/structure/text")
    def api_structure_text():
        payload = _json_payload()
        try:
            text = _string_field(payload, "text")
            name = _string_field(payload, "name") or None
            charge = _int_field(payload, "charge")
            multiplicity = _int_field(payload, "multiplicity")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if not text.strip():
            return jsonify({"error": "内容为空"}), 400
        try:
            molecule = Molecule.from_text(
                text, name=name, charge=charge, multiplicity=multiplicity,
            )
        except (ValueError, TypeError, KeyError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"molecule": molecule_to_dict(molecule)})

    @app.post("/api/structure/smiles")
    def api_structure_smiles():
        payload = _json_payload()
        try:
            smiles = _string_field(payload, "smiles").strip()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if not smiles:
            return jsonify({"error": "SMILES 为空"}), 400
        try:
            from ..cheminfo import molecule_from_smiles

            molecule = molecule_from_smiles(smiles)
        except (RuntimeError, ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"molecule": molecule_to_dict(molecule)})

    # ---------------------------------------------------------------- 任务

    @app.get("/api/jobs")
    def api_jobs():
        try:
            limit = _int_field({"limit": request.args.get("limit")}, "limit", 100) or 100
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        limit = max(1, min(limit, 1000))
        return jsonify({"jobs": [r.to_dict() for r in mgr.store.list(limit=limit)]})

    @app.get("/api/jobs/<job_id>")
    def api_job(job_id: str):
        record = mgr.store.get(job_id)
        if record is None:
            return jsonify({"error": "任务不存在"}), 404
        result = mgr.store.load_result(job_id)
        return jsonify({"job": record.to_dict(),
                        "result": _result_payload(result, mgr.store.load_spec(job_id))})

    @app.post("/api/jobs")
    def api_job_create():
        payload = _json_payload()

        # 几何来源：优先用上游任务的结果几何（几何血缘）
        try:
            parent_job = _string_field(payload, "geometry_from_job").strip()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if parent_job:
            try:
                molecule = molecule_from_job(mgr, parent_job)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
        else:
            try:
                molecule = molecule_from_payload(payload.get("molecule") or {})
            except (ValueError, TypeError, KeyError) as exc:
                return jsonify({"error": f"结构无效: {exc}"}), 400

        # 顶层的 charge / multiplicity 覆盖分子自带的那份。
        #
        # 为什么必须放在这里：走「几何来源」时分子是从上游任务的结果几何重建的
        # （:func:`molecule_from_job`），电荷与多重度取的是**上游那次计算**的值；
        # 用户在界面里改了，值只写在顶层字段里。以前这里的条件写成
        # `if payload.get("charge") is not None`，于是**只改多重度会完全没反应**；
        # 而前端当时也根本没发顶层字段 —— 两个缺陷叠起来就是"填了 -1/2 却被
        # 静默忽略"（审计 F3）。对带电体系来说，这会让整个结果对应错误的状态。
        try:
            molecule = _apply_charge_multiplicity(molecule, payload)
        except (ValueError, TypeError) as exc:
            return jsonify({"error": f"电荷/多重度无效: {exc}"}), 400

        recipe = None
        try:
            recipe_id = _string_field(payload, "recipe_id").strip()
            new_nprocs = _bounded_int(payload, "nprocs", 4, 1, 256, "并行核数") \
                if "nprocs" in payload else None
            new_maxcore = _bounded_int(payload, "maxcore_mb", 2000, 0, 1_000_000, "内存") \
                if "maxcore_mb" in payload else None
            new_title = _string_field(payload, "title")
            new_blocks = _blocks_field(payload)
            overrides: dict = {}
            for key in ("method", "basis", "solvent", "extra_keywords"):
                if key not in payload:
                    continue
                # 这几个以前是原样透传的 —— 传对象/数组就会在后面的 .strip() 或
                # jobspec 的 isascii() 上抛 AttributeError，变成 500 HTML。
                # 独立审计实测确认（method/basis/solvent 各是一条 500 路径）。
                if key == "extra_keywords":
                    overrides[key] = _keywords_field(payload, key)
                elif key == "solvent":
                    overrides[key] = _string_field(payload, key).strip() or None
                else:
                    overrides[key] = _string_field(payload, key).strip()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        if recipe_id:
            recipe = get_recipe(recipe_id)
            if recipe is None:
                return jsonify({"error": f"未知配方: {recipe_id}"}), 400

        if new_nprocs is not None:
            overrides["nprocs"] = new_nprocs
        if new_maxcore is not None:
            overrides["maxcore_mb"] = new_maxcore
        if payload.get("calc_hess"):
            overrides["calc_hess"] = True
        # 自定义块设置：约束优化 / 扫描 都要靠它
        if new_blocks:
            overrides["extra_blocks"] = new_blocks
        if new_title:
            overrides["title"] = new_title

        try:
            if recipe is not None:
                spec = recipe.build_spec(molecule, **overrides)
                spec = replace(spec, parent_job=parent_job, recipe_id=recipe.id)
            else:
                # 这几个字段以前直接 .strip()，传对象/数组就 AttributeError → 500。
                # 用户真实会撞上：PowerShell 的 ConvertTo-Json 会把某些值变成对象。
                solvent = _string_field(payload, "solvent").strip() or None
                if solvent in ("气相", "none", "None"):
                    solvent = None
                # 关键：要区分"没给基组"和"显式给空基组"。
                # 复合方法（r2SCAN-3c / PBEh-3c / B97-3c）自带基组，
                # 若被兜底成 def2-SVP，会生成 `! r2SCAN-3c def2-SVP` —— 
                # 显式基组把复合方法的基组顶掉，算出个四不像的方法。
                basis = _string_field(payload, "basis", "def2-SVP") if "basis" in payload \
                    else "def2-SVP"
                spec = JobSpec(
                    molecule=molecule,
                    job_type=JobType(payload.get("job_type", "energy")),
                    method=_string_field(payload, "method", "B3LYP").strip() or "B3LYP",
                    basis=(basis or "").strip(),
                    engine=payload.get("engine", "orca"),
                    nprocs=_bounded_int(payload, "nprocs", 4, 1, 256, "并行核数"),
                    maxcore_mb=_bounded_int(payload, "maxcore_mb", 2000, 0, 1_000_000, "内存"),
                    solvent=solvent,
                    extra_keywords=_keywords_field(payload),
                    extra_blocks=_blocks_field(payload),
                    calc_hess=bool(payload.get("calc_hess", False)),
                    title=_string_field(payload, "title").strip() or molecule.name,
                    parent_job=parent_job,
                )
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

        # 提交前就检查扫描设置。实测过的两种翻车：起点离分子实际值太远
        # （优化第一步硬扳，掉到别的反应通道）、区间跨过 180°
        # （ORCA 内坐标奇点，直接 exit 2）。算力已经花出去就晚了，所以放在这里。
        warnings = scan_warnings(spec.molecule.symbols, spec.molecule.coords,
                                 spec.extra_blocks)

        record = mgr.submit(spec)
        return jsonify(
            {
                "job": record.to_dict(),
                "summary": spec.summary(),
                "recipe_id": spec.recipe_id,
                "parent_job": spec.parent_job,
                "warnings": warnings,
            }
        ), 201

    @app.post("/api/jobs/<job_id>/cancel")
    def api_job_cancel(job_id: str):
        ok = mgr.cancel(job_id)
        return jsonify({"ok": ok})

    @app.get("/api/jobs/<job_id>/irc")
    def api_job_irc(job_id: str):
        """把 IRC 轨迹读成查看器可直接播放的帧序列。"""
        record = mgr.store.get(job_id)
        if record is None:
            return jsonify({"error": "任务不存在"}), 404
        result = mgr.store.load_result(job_id)
        if result is None or not result.irc:
            return jsonify({"error": "该任务没有 IRC 轨迹"}), 404

        path = result.irc.get("files", {}).get("full_trajectory")
        if not path or not Path(path).is_file():
            return jsonify({"error": "轨迹文件缺失"}), 404

        frames = [
            {"symbols": f["symbols"], "coords": f["coords"], "energy": f["energy"]}
            for f in read_xyz_frames(path)
        ]
        return jsonify(
            {
                "frames": frames,
                "barriers_kcal": result.irc.get("barriers_kcal"),
                "ts_frame_index": result.irc.get("ts_frame_index"),
                "frame_energies": result.irc.get("frame_energies"),
            }
        )

    @app.get("/api/jobs/<job_id>/scan")
    def api_job_scan(job_id: str):
        """势能面扫描的曲线数据（各点的坐标值、能量、重原子键长）。

        解析要读整个输出文件，所以按需调用，不塞进 ``result.json``。
        解析走 worker 子进程 —— 界面进程不导入引擎。
        """
        record = mgr.store.get(job_id)
        if record is None:
            return jsonify({"error": "任务不存在"}), 404
        try:
            profile = parse_scan_profile(mgr.store.job_dir(job_id), record.engine or "orca")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if not profile:
            return jsonify({"error": "该任务没有扫描数据（不是势能面扫描）"}), 404
        return jsonify(profile)

    @app.get("/api/jobs/<job_id>/bondorders")
    def api_job_bond_orders(job_id: str):
        """键级分析（Multiwfn）—— 补上"化学解释"这块能力。

        这些分析（Mayer/Wiberg/FBO/LBO 键级、轨道成分、模糊空间）
        **都不需要买 NBO**，用已经装好的 Multiwfn 就能做。

        流程：``.gbw`` --(orca_2mkl)--> ``.molden.input`` --(Multiwfn)--> 键级表。
        按需调用（Multiwfn 要建积分矩阵），结果不塞进 ``result.json``。

        惰性导入引擎/外部工具：界面进程不导入它们（架构第 1 条）。
        """
        from ..model.bondorder import BOND_ORDER_KINDS

        record = mgr.store.get(job_id)
        if record is None:
            return jsonify({"error": "任务不存在"}), 404

        kind = request.args.get("kind", "mayer")
        if kind not in BOND_ORDER_KINDS:
            return jsonify({
                "error": f"不支持的键级类型 {kind!r}",
                "supported": sorted(BOND_ORDER_KINDS),
            }), 400

        job_dir = mgr.store.job_dir(job_id)
        molden = sorted(job_dir.glob("*.molden.input"))
        if not molden:
            # 还没有 molden：用 ORCA 自带的 orca_2mkl 现转一个
            gbw = sorted(job_dir.glob("*.gbw"))
            if not gbw:
                return jsonify({
                    "error": "这个任务没有波函数文件（.gbw）—— "
                             "键级分析需要它，请用带波函数输出的方法重算"}),
            from .. import orcatools
            run = orcatools.make_molden(job_dir, gbw[0].name)
            if not run.ok:
                return jsonify({"error": f"生成 molden 失败：{run.message}"}), 400
            molden = sorted(job_dir.glob("*.molden.input"))
            if not molden:
                return jsonify({"error": "orca_2mkl 没有产出 molden 文件"}), 400

        from ..multiwfn import run_bond_order_analysis
        result = run_bond_order_analysis(molden[0], job_dir, kind=kind)
        if not result.ok:
            return jsonify({
                "error": "；".join(result.problems) or "Multiwfn 没有给出键级数据",
                "kind": kind,
                "supported": sorted(BOND_ORDER_KINDS),
            }), 400
        return jsonify(result.to_dict())

    @app.get("/api/jobs/<job_id>/orbital-composition")
    def api_job_orbital_composition(job_id: str):
        """轨道成分分析（Multiwfn 菜单 8）：**这个轨道主要在哪几个原子上**。

        光有轨道能级图看不出这个，而它对判断反应位点常常比能级本身更有用。

        ``partition`` = mulliken / stout / scpa，``orbital`` = homo / lumo /
        homo-1 / lumo+1 / all。同样按需调用，惰性导入引擎侧。
        """
        from ..model.orbcomp import ORBITAL_CHOICES, ORBITAL_PARTITIONS

        record = mgr.store.get(job_id)
        if record is None:
            return jsonify({"error": "任务不存在"}), 404

        partition = request.args.get("partition", "mulliken")
        orbital = request.args.get("orbital", "homo")
        if partition not in ORBITAL_PARTITIONS:
            return jsonify({"error": f"不支持的布居方式 {partition!r}",
                            "supported": sorted(ORBITAL_PARTITIONS)}), 400
        if orbital not in ORBITAL_CHOICES:
            return jsonify({"error": f"不支持的轨道选择 {orbital!r}",
                            "supported": sorted(ORBITAL_CHOICES)}), 400

        job_dir = mgr.store.job_dir(job_id)
        molden = sorted(job_dir.glob("*.molden.input"))
        if not molden:
            gbw = sorted(job_dir.glob("*.gbw"))
            if not gbw:
                return jsonify({"error": "这个任务没有波函数文件（.gbw）—— "
                                         "轨道成分分析需要它"}), 400
            from .. import orcatools
            run = orcatools.make_molden(job_dir, gbw[0].name)
            if not run.ok:
                return jsonify({"error": f"生成 molden 失败：{run.message}"}), 400
            molden = sorted(job_dir.glob("*.molden.input"))
            if not molden:
                return jsonify({"error": "orca_2mkl 没有产出 molden 文件"}), 400

        from ..multiwfn import run_orbital_composition
        result = run_orbital_composition(molden[0], job_dir,
                                        partition=partition, request=orbital)
        if not result.ok:
            return jsonify({
                "error": "；".join(result.problems) or "Multiwfn 没有给出轨道成分",
                "supported": sorted(ORBITAL_PARTITIONS),
            }), 400
        return jsonify(result.to_dict())

    @app.get("/api/jobs/<job_id>/file/<role>")
    def api_job_file(job_id: str, role: str):
        record = mgr.store.get(job_id)
        if record is None:
            abort(404)
        result = mgr.store.load_result(job_id)
        path = (result.files.get(role) if result else None)
        if not path:
            abort(404)

        target = Path(path).resolve()
        job_dir = Path(record.job_dir).resolve()
        # 只允许读取任务目录内的文件，避免任意路径读取
        if target != job_dir and job_dir not in target.parents:
            abort(403)
        if not target.is_file():
            abort(404)
        return send_file(
            target,
            as_attachment=request.args.get("download") == "1",
            download_name=target.name,
        )

    # ---------------------------------------------------------------- 批量

    @app.post("/api/batch")
    def api_batch_create():
        payload = _json_payload()
        items = payload.get("molecules") or []
        if len(items) < 2:
            return jsonify({"error": "批量至少需要两个结构"}), 400

        try:
            molecules = [molecule_from_payload(m) for m in items]
        except (ValueError, TypeError, KeyError) as exc:
            return jsonify({"error": str(exc)}), 400

        recipe = None
        try:
            recipe_id = _string_field(payload, "recipe_id").strip()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if recipe_id:
            recipe = get_recipe(recipe_id)
            if recipe is None:
                return jsonify({"error": f"未知配方: {recipe_id}"}), 400

        try:
            if recipe is not None:
                job_type = recipe.job_type
                # 与单任务提交同理：这些字段必须走类型校验，否则传对象 →
                # 后面的 .strip() / isascii() 抛 AttributeError → 500
                method = _string_field(payload, "method", recipe.method).strip() or recipe.method
                basis = _string_field(payload, "basis", recipe.basis or "") \
                    if payload.get("basis") is not None else (recipe.basis or "")
                solvent = _string_field(payload, "solvent", recipe.solvent or "").strip() or None
                extra_keywords = recipe.keywords
                extra_blocks = recipe.blocks
            else:
                job_type = JobType(payload.get("job_type", "energy"))
                method = _string_field(payload, "method", "B3LYP").strip() or "B3LYP"
                basis = _string_field(payload, "basis", "def2-SVP").strip()
                solvent = _string_field(payload, "solvent").strip() or None
                extra_keywords = _keywords_field(payload)
                extra_blocks = ()
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

        with batches_lock:
            batch_seq["n"] += 1
            batch_id = f"batch-{int(time.time())}-{batch_seq['n']}"
            batches[batch_id] = {"status": "running", "result": None, "error": None}

        def worker() -> None:
            try:
                runner = BatchRunner(mgr)
                with batches_lock:
                    batches[batch_id]["runner"] = runner
                result = runner.run(
                    molecules,
                    job_type=job_type,
                    method=method,
                    basis=basis,
                    solvent=solvent,
                    nprocs=_bounded_int(payload, "nprocs", 4, 1, 256, "并行核数"),
                    maxcore_mb=_bounded_int(payload, "maxcore_mb", 2000, 0, 1_000_000, "内存"),
                    quantity=payload.get("quantity", "energy"),
                    extra_keywords=extra_keywords,
                    extra_blocks=extra_blocks,
                    on_item=lambda item: bus.publish(
                        batch_id,
                        {"event": "batch_item", "item": item.to_dict()},
                    ),
                    on_event=lambda job_id, event: bus.publish(batch_id, event),
                )
                payload_out = result.to_dict()
                payload_out["summary_lines"] = result.summary_lines()
                saved = result.save(mgr.store.root.parent / "batches", stem=batch_id)
                payload_out["saved"] = {k: str(v) for k, v in saved.items()}
                with batches_lock:
                    batches[batch_id].update(status="done", result=payload_out)
                bus.publish(batch_id, {"event": "batch_done", "batch": payload_out})
            except Exception as exc:  # noqa: BLE001
                with batches_lock:
                    batches[batch_id].update(status="failed", error=str(exc))
                bus.publish(batch_id, {"event": "batch_failed", "message": str(exc)})

        threading.Thread(target=worker, name=batch_id, daemon=True).start()
        return jsonify({"batch_id": batch_id}), 202

    @app.get("/api/batch/<batch_id>")
    def api_batch_get(batch_id: str):
        with batches_lock:
            entry = batches.get(batch_id)
        if entry is None:
            return jsonify({"error": "批次不存在"}), 404
        return jsonify(
            {"status": entry["status"], "result": entry["result"], "error": entry["error"]}
        )

    @app.post("/api/batch/<batch_id>/cancel")
    def api_batch_cancel(batch_id: str):
        with batches_lock:
            entry = batches.get(batch_id)
        if entry is None:
            return jsonify({"error": "批次不存在"}), 404
        runner = entry.get("runner")
        if runner is not None:
            runner.cancel()
        return jsonify({"ok": True})

    # ------------------------------------------------------------ 外部工具

    @app.post("/api/external")
    def api_external():
        payload = _json_payload()
        tool = payload.get("tool")
        try:
            # path 也要校验类型：非字符串一路传到 Path() 会 TypeError（500）
            path = _string_field(payload, "path").strip() or None
            target = _string_field(payload, "target").strip() or None
        except ValueError as exc:
            return jsonify({"ok": False, "tool": tool, "message": str(exc)}), 400

        # 也可以传 job_id + role，由后端解析出实际文件
        if not path and payload.get("job_id"):
            try:
                job_id = _string_field(payload, "job_id")
                role = _string_field(payload, "role", "").strip()
            except ValueError as exc:
                return jsonify({"ok": False, "tool": tool, "message": str(exc)}), 400
            result = mgr.store.load_result(job_id)
            if result is not None:
                # 指定的 role 既存在、又是这个工具读得了的类型，才用它
                requested = (result.files or {}).get(role) if role else None
                if requested and _readable_by(tool, requested):
                    path = requested
                else:
                    path = _external_file(tool, job_id, result, mgr)
                if not path:
                    return jsonify({
                        "ok": False, "tool": tool,
                        "message": f"该任务没有 {tool} 能读的文件"
                                   f"（需要 Molden 或轨道 cube，且波函数要保留下来）",
                    }), 400

        # 路径检查必须在分支之前 —— 否则 explorer 缺 path 会一路穿到 Path(None)
        if not path:
            return jsonify({"ok": False, "tool": tool, "message": "缺少文件路径"}), 400
        if tool == "explorer":
            return jsonify(open_in_file_manager(path).to_dict())
        if tool == "avogadro":
            return jsonify(view_with_avogadro(path).to_dict())
        if tool == "multiwfn":
            return jsonify(run_multiwfn(path).to_dict())
        if tool == "obabel":
            if not target:
                return jsonify({"ok": False, "tool": tool, "message": "缺少 target"}), 400
            return jsonify(convert_with_obabel(path, target).to_dict())
        return jsonify({"ok": False, "tool": tool, "message": f"未知工具: {tool}"}), 400

    @app.post("/api/shutdown")
    def api_shutdown():
        """关闭本地服务。

        快捷方式用 ``pythonw.exe`` 启动（没有控制台），所以必须有一个
        从界面里停掉服务的出口，否则用户只能去任务管理器里杀进程。
        """
        for job_id in mgr.running_jobs():
            mgr.cancel(job_id)
        bus.publish("", {"event": "log", "line": "服务正在关闭…"})

        def _stop() -> None:
            time.sleep(0.6)
            os._exit(0)  # Flask 开发服务器没有优雅关闭接口，直接退出

        threading.Thread(target=_stop, daemon=True).start()
        return jsonify({"ok": True})

    @app.get("/api/recipes")
    def api_recipes():
        query = request.args.get("q", "")
        from ..recipes import find_by_name

        recipes = find_by_name(query) if query else all_recipes()
        return jsonify({"recipes": [r.to_dict() for r in recipes]})

    @app.post("/api/recipes/<recipe_id>/favorite")
    def api_recipe_favorite(recipe_id: str):
        from ..recipes import set_favorite

        recipe = get_recipe(recipe_id)
        if recipe is None:
            return jsonify({"error": "配方不存在"}), 404
        payload = _json_payload()
        favorite = bool(payload.get("favorite", not recipe.favorite))
        favorites = set_favorite(recipe_id, favorite)
        return jsonify({"ok": True, "id": recipe_id, "favorite": favorite,
                        "favorites": sorted(favorites)})

    @app.delete("/api/recipes/<recipe_id>")
    def api_recipe_delete(recipe_id: str):
        recipe = get_recipe(recipe_id)
        if recipe is None:
            return jsonify({"error": "配方不存在"}), 404
        if recipe.builtin:
            return jsonify({"error": "内置配方不可删除"}), 400
        return jsonify({"ok": delete_user_recipe(recipe_id)})

    @app.get("/api/report.csv")
    def api_report_csv():
        """把已完成任务导成一张表（对应 Workbench 的 Report 标签页）。"""
        import csv
        import io

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "job_id", "分子", "任务", "方法", "状态", "能量(Ha)",
            "零点能(Ha)", "焓(Ha)", "自由能(Ha)", "熵(J/mol/K)",
            "虚频数", "耗时(s)", "创建时间", "上游任务", "配方",
        ])

        for record in mgr.store.list(limit=1000):
            result = mgr.store.load_result(record.job_id)
            spec = mgr.store.load_spec(record.job_id)
            thermo = (result.thermo if result else {}) or {}
            writer.writerow([
                record.job_id,
                record.molecule,
                record.job_type,
                record.method,
                record.status,
                f"{result.final_energy:.10f}" if result and result.final_energy is not None else "",
                f"{thermo['zpe_hartree']:.8f}" if "zpe_hartree" in thermo else "",
                f"{thermo['enthalpy_H_hartree']:.8f}" if "enthalpy_H_hartree" in thermo else "",
                f"{thermo['gibbs_free_energy_G_hartree']:.8f}" if "gibbs_free_energy_G_hartree" in thermo else "",
                f"{thermo['entropy_S_hartree_per_K'] * 2625499.6394799:.2f}" if "entropy_S_hartree_per_K" in thermo else "",
                result.n_imaginary_frequencies if result else "",
                f"{record.duration:.1f}" if record.duration else "",
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created_at)) if record.created_at else "",
                getattr(spec, "parent_job", "") if spec else "",
                getattr(spec, "recipe_id", "") if spec else "",
            ])

        data = buffer.getvalue()
        return Response(
            data,
            mimetype="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="qcgui_report.csv"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/jobs/<job_id>/orbitals")
    def api_job_orbitals(job_id: str):
        """分子轨道能级与占据数（来自结构化输出，无需重算）。"""
        record = mgr.store.get(job_id)
        if record is None:
            return jsonify({"error": "任务不存在"}), 404
        result = mgr.store.load_result(job_id)
        if result is None:
            return jsonify({"error": "该任务没有结果"}), 404

        data = dict(result.orbitals or {})
        data["cubes"] = {
            role: Path(path).name
            for role, path in (result.files or {}).items()
            if role.startswith("mo") and str(path).endswith(".cube")
        }
        data["molden"] = Path(result.files["molden"]).name if result.files.get("molden") else None
        data["wavefunction"] = Path(result.files["wavefunction"]).name if result.files.get("wavefunction") else None
        data["tools"] = probe_orca_tools()
        return jsonify(data)

    @app.post("/api/jobs/<job_id>/orbitals/<int:mo>/cube")
    def api_job_orbital_cube(job_id: str, mo: int):
        """把一个分子轨道导成 cube（基于已有 .gbw，不重跑 SCF）。"""
        record = mgr.store.get(job_id)
        if record is None:
            return jsonify({"error": "任务不存在"}), 404
        result = mgr.store.load_result(job_id)
        if result is None:
            return jsonify({"error": "该任务没有结果"}), 404

        gbw = (result.files or {}).get("wavefunction")
        if not gbw:
            return jsonify({"error": "该任务没有保存波函数（.gbw），无法导出轨道"}), 400

        payload = _json_payload()
        try:
            operator = _int_field(payload, "operator", 0) or 0
            grid = _int_field(payload, "grid", 50) or 50
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        run = export_mo_cube(
            record.job_dir,
            gbw,
            int(mo),
            operator=operator,
            grid=grid,
        )
        body = run.to_dict()
        if run.ok:
            role = f"mo{mo}"
            result.files[role] = run.artifacts[0]
            mgr.store.save_result(job_id, result)
            body["role"] = role
            body["url"] = f"/api/jobs/{job_id}/file/{role}"
            bus.publish(job_id, {"event": "log", "line": f"✔ {run.message}"})
        else:
            bus.publish(job_id, {"event": "log", "line": f"✖ {run.message}"})
        return jsonify(body), (200 if run.ok else 500)

    @app.post("/api/jobs/<job_id>/molden")
    def api_job_molden(job_id: str):
        """把 .gbw 转成 Molden —— Multiwfn / Avogadro 都读这个格式。"""
        record = mgr.store.get(job_id)
        if record is None:
            return jsonify({"error": "任务不存在"}), 404
        result = mgr.store.load_result(job_id)
        if result is None:
            return jsonify({"error": "该任务没有结果"}), 404

        gbw = (result.files or {}).get("wavefunction")
        if not gbw:
            return jsonify({"error": "该任务没有保存波函数（.gbw）"}), 400

        run = make_molden(record.job_dir, gbw)
        body = run.to_dict()
        if run.ok:
            result.files["molden"] = run.artifacts[0]
            mgr.store.save_result(job_id, result)
            body["role"] = "molden"
            body["url"] = f"/api/jobs/{job_id}/file/molden"
        return jsonify(body), (200 if run.ok else 500)

    # ---------------------------------------------------------------- SSE
    @app.get("/api/events")
    def api_events():
        q = bus.subscribe()

        def stream():
            try:
                yield "retry: 3000\n\n"
                yield 'data: {"event":"hello"}\n\n'
                while True:
                    try:
                        item = q.get(timeout=15.0)
                    except queue.Empty:
                        yield ": keep-alive\n\n"
                        continue
                    yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
            finally:
                bus.unsubscribe(q)

        return Response(
            stream(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


# ------------------------------------------------------------------ 启动

def _probe_existing(host: str, port: int, timeout: float = 1.5) -> bool:
    """该端口上是否已经有一个 qcgui 在跑。

    桌面快捷方式被双击两次是很正常的事，那时不该起第二个实例 ——
    两个实例会同时监听同一端口、共用同一个任务库，请求落到哪个不确定。
    """
    import json as _json
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/api/status", timeout=timeout
        ) as response:
            data = _json.loads(response.read().decode("utf-8"))
        return isinstance(data, dict) and "engines" in data
    except Exception:
        return False


def _pick_port(preferred: int, host: str, attempts: int = 20) -> int:
    """从 preferred 开始找一个可用端口。"""
    import socket

    for offset in range(attempts):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            # 这里**不能**设 SO_REUSEADDR：在 Windows 上它的语义是"允许抢占
            # 已被占用的端口"，会让探测在端口已被监听时依然成功，
            # 于是返回一个占用中的端口 —— 这正是曾经出现两个实例
            # 同时监听 8760 的原因。探测需要排他语义。
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise OSError(f"{preferred}–{preferred + attempts - 1} 范围内没有可用端口")


def run_server(
    host: str = "127.0.0.1",
    port: int | None = None,
    open_browser: bool | None = None,
    manager: JobManager | None = None,
) -> None:
    """启动本地服务；默认自动打开浏览器。"""
    import webbrowser

    config = load_config()
    preferred = int(port if port is not None else config.get("web_port", 8760))
    if open_browser is None:
        open_browser = bool(config.get("auto_open_browser", True))

    # 单实例守卫：已经有一个在跑就直接用那个，别再起第二个
    if _probe_existing(host, preferred):
        url = f"http://{host}:{preferred}/"
        print(f"已有 qcgui 实例在 {url} 运行，不再启动第二个。")
        if open_browser:
            webbrowser.open(url)
        return

    actual = _pick_port(preferred, host)

    app = create_app(manager)
    url = f"http://{host}:{actual}/"
    print(f"qcgui 网页界面: {url}")
    print(f"任务存储目录  : {app.config['MANAGER'].store.root}")
    engines = probe_engines(force=True)
    for engine in engines:
        mark = "可用" if engine["available"] else "不可用"
        print(f"引擎          : {engine['display_name']} [{mark}] {engine['detail']}")
    print("按 Ctrl+C 停止服务。")

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    app.run(host=host, port=actual, threaded=True, debug=False, use_reloader=False)
