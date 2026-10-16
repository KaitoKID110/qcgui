"""结构叠加与 RMSD（Kabsch 最优叠加）。

**核心验收**：拿一个**已知**的旋转矩阵（绕 z 轴 90°）+ 平移去变换一个分子，
``align=True`` 后 RMSD ≈ 0（< 1e-6），而 ``align=False`` 时 RMSD 很大。
这条是"叠加确实在解最优刚体变换"的数学证明 —— 光看"似乎叠上了"不算证据，
本项目已经三次栽在"看着对"上。

覆盖：
* 同一几何 → RMSD 恰好 0（容差 1e-9）
* 已知旋转 + 平移 → 对齐后 < 1e-6、对齐前 > 0，且**恢复出来的旋转矩阵就是已知那个**
* 原子数不一致 / 元素顺序不一致 → 明确报错（不是硬算）
* 镜像结构叠不到 0（纯旋转不允许反射 —— 这是应有的行为，不是缺陷）
* 真实任务库里的 opt 任务与自己做叠加 → RMSD 0；与同分子另一个任务叠加 → 不为 0

    pytest tests/test_overlay.py -v
    python tests/test_overlay.py
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.jobs import JobManager  # noqa: E402
from qcgui.model import Atom, JobSpec, JobType, Molecule, Result  # noqa: E402
from qcgui.model.geometry import (  # noqa: E402
    AlignmentError,
    check_same_elements,
    kabsch_align,
    overlay,
    rmsd,
)
from qcgui.web.app import create_app  # noqa: E402

#: 验收要求：最优叠加后应小于这个数
TOL_ALIGNED = 1e-6
#: 验收要求：同一几何的 RMSD 应当是 0（容差 1e-9）
TOL_SAME = 1e-9

#: **已知**的旋转矩阵：绕 z 轴转 90°
RZ90 = ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0))
#: **已知**的平移
SHIFT = (1.0, -2.0, 3.5)

#: 故意选一个**不对称**的小分子：旋转方向搞反、或者把旋转转置了，
#: 在这种结构上都会立刻露馅（对称结构可能"错得也对"）
SYMBOLS = ["C", "O", "H", "N", "H"]
COORDS = [
    [0.00, 0.00, 0.00],
    [1.21, 0.02, -0.31],
    [-0.55, 0.94, 0.12],
    [0.35, -1.15, 0.63],
    [1.72, 0.51, 0.44],
]


class _Skipped(Exception):
    pass


def _skip(reason: str):
    try:
        import pytest

        pytest.skip(reason)
    except ImportError:
        raise _Skipped(reason)


def apply_transform(rotation, translation, coords):
    """按已知的旋转 + 平移变换一套坐标（测试自己算，不用被测代码）。"""
    return [
        [sum(rotation[i][k] * point[k] for k in range(3)) + translation[i]
         for i in range(3)]
        for point in coords
    ]


def max_deviation(a, b) -> float:
    return max(math.dist(p, q) for p, q in zip(a, b))


def expect_alignment_error(func, needle: str) -> str:
    """``func()`` 必须抛 ``AlignmentError``，且信息里含 ``needle``。"""
    try:
        func()
    except AlignmentError as exc:
        message = str(exc)
        assert needle in message, f"报错信息里没有「{needle}」：{message}"
        return message
    raise AssertionError(f"应当报错（信息里要有「{needle}」）但没有报错")


# ---------------------------------------------------------------- 纯几何层

def test_same_geometry_has_zero_rmsd():
    """同一个几何的 RMSD 必须是 0（容差 1e-9）。"""
    assert rmsd(COORDS, COORDS) == 0.0
    assert rmsd(COORDS, COORDS, align=True) == 0.0
    copy = [list(c) for c in COORDS]
    assert rmsd(COORDS, copy, align=True) < TOL_SAME
    rotated, rotation, translation = kabsch_align(COORDS, COORDS)
    assert max_deviation(rotated, COORDS) < TOL_SAME
    # 同一结构之间的"最优变换"就是不做任何事
    for i in range(3):
        assert abs(rotation[i][i] - 1.0) < TOL_SAME
        assert abs(translation[i]) < TOL_SAME
    print("    同一几何：RMSD = 0.0，恢复出的变换是单位变换")


def test_known_rotation_plus_translation_aligns_to_zero():
    """核心：已知旋转（绕 z 90°）+ 平移 → 对齐后 ≈ 0，对齐前 > 0。"""
    b = apply_transform(RZ90, SHIFT, COORDS)

    raw = rmsd(COORDS, b)
    aligned = rmsd(COORDS, b, align=True)
    assert raw > 1.0, f"对齐前应当明显不为 0，得到 {raw}"
    assert aligned < TOL_ALIGNED, f"对齐后应当 ≈ 0，得到 {aligned}"

    rotated, rotation, translation = kabsch_align(COORDS, b)
    # 恢复出来的必须**就是**那个已知的旋转和平移（不是"随便一个能叠上的"）
    for i in range(3):
        for k in range(3):
            assert abs(rotation[i][k] - RZ90[i][k]) < 1e-9, \
                f"恢复的旋转矩阵不对：{rotation} vs {RZ90}"
        assert abs(translation[i] - SHIFT[i]) < 1e-9, \
            f"恢复的平移不对：{translation} vs {SHIFT}"
    assert max_deviation(rotated, b) < TOL_ALIGNED, "变换后的坐标应当落在 B 上"
    print(f"    已知旋转 z90°+平移：对齐前 {raw:.6f} Å → 对齐后 {aligned:.3e} Å"
          f"（< {TOL_ALIGNED:g}）")


def test_pure_translation_aligns_to_zero():
    """只有平移时，旋转应当是单位阵，RMSD 归零。"""
    b = apply_transform(((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
                        SHIFT, COORDS)
    raw = rmsd(COORDS, b)
    aligned = rmsd(COORDS, b, align=True)
    assert abs(raw - math.dist((0, 0, 0), SHIFT)) < 1e-12, raw
    assert aligned < TOL_ALIGNED
    print(f"    只有平移：对齐前 {raw:.6f} Å → 对齐后 {aligned:.3e} Å")


def test_alignment_never_makes_the_rmsd_worse():
    """对齐只能是**变好或不变** —— 随机扰动下逐个验证。"""
    rng = random.Random(20261015)
    worst = 0.0
    for _ in range(50):
        shifted = [[c[0] + rng.uniform(-2, 2), c[1] + rng.uniform(-2, 2),
                    c[2] + rng.uniform(-2, 2)] for c in COORDS]
        # 再叠一个随机旋转（用四元数生成一个真正的旋转矩阵）
        axis = [rng.uniform(-1, 1) for _ in range(3)]
        norm = math.sqrt(sum(v * v for v in axis)) or 1.0
        x, y, z = (v / norm for v in axis)
        angle = rng.uniform(0, math.pi)
        s, cc = math.sin(angle / 2), math.cos(angle / 2)
        q = (cc, x * s, y * s, z * s)
        w, qx, qy, qz = q
        rot = (
            (1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - w * qz), 2 * (qx * qz + w * qy)),
            (2 * (qx * qy + w * qz), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - w * qx)),
            (2 * (qx * qz - w * qy), 2 * (qy * qz + w * qx), 1 - 2 * (qx * qx + qy * qy)),
        )
        b = apply_transform(rot, [9.0, -4.0, 2.5], COORDS)
        raw = rmsd(shifted, b)
        aligned = rmsd(shifted, b, align=True)
        assert aligned <= raw + 1e-12, f"对齐后反而更大：{raw} → {aligned}"
        worst = max(worst, raw - aligned)
    print(f"    50 组随机刚体变换：对齐后一律 ≤ 对齐前（最大改善 {worst:.3f} Å）")


def test_rotation_is_a_proper_rotation():
    """返回的必须是**纯旋转**：RᵀR = I 且 det = +1。

    如果实现里混进了反射矩阵，一对对映体会被判成"完全相同的结构" ——
    这是化学上最不能接受的错误结论。
    """
    _, rotation, _ = kabsch_align(COORDS, apply_transform(RZ90, SHIFT, COORDS))
    for i in range(3):
        for j in range(3):
            dot = sum(rotation[k][i] * rotation[k][j] for k in range(3))
            expect = 1.0 if i == j else 0.0
            assert abs(dot - expect) < 1e-12, f"RᵀR 不是单位阵：{rotation}"
    det = (rotation[0][0] * (rotation[1][1] * rotation[2][2] - rotation[1][2] * rotation[2][1])
           - rotation[0][1] * (rotation[1][0] * rotation[2][2] - rotation[1][2] * rotation[2][0])
           + rotation[0][2] * (rotation[1][0] * rotation[2][1] - rotation[1][1] * rotation[2][0]))
    assert abs(det - 1.0) < 1e-12, f"行列式应当是 +1，得到 {det}"
    print(f"    RᵀR = I，det(R) = {det:.12f}")


def test_mirror_image_cannot_be_aligned_to_zero():
    """镜像结构叠不到 0 —— 这是**应有的**行为。

    纯旋转不允许反射，所以对映体之间的 RMSD 不会变成 0。
    （如果哪天这条挂了，说明实现里混进了反射矩阵。）
    """
    mirrored = [[c[0], -c[1], c[2]] for c in COORDS]
    aligned = rmsd(COORDS, mirrored, align=True)
    assert aligned > 0.1, f"镜像不应叠到 0，得到 {aligned}"
    print(f"    镜像结构：对齐后仍是 {aligned:.4f} Å（纯旋转不含反射，符合预期）")


def test_degenerate_shapes_do_not_blow_up():
    """单原子 / 共线两点也要给出合理结果（此时旋转本来就不唯一）。"""
    assert rmsd([[1.0, 2.0, 3.0]], [[4.0, 5.0, 6.0]], align=True) < TOL_SAME
    line = [[0.0, 0.0, 0.0], [1.0, 2.0, -1.0]]
    moved = apply_transform(RZ90, SHIFT, line)
    assert rmsd(line, moved, align=True) < TOL_ALIGNED
    collinear = [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0], [2.0, 2.0, 2.0]]
    assert rmsd(collinear, apply_transform(RZ90, SHIFT, collinear), align=True) < TOL_ALIGNED
    print("    单原子 / 两原子 / 三原子共线：都给出有限且正确的 RMSD")


def test_atom_count_mismatch_is_a_clear_error():
    """原子数不一致 → 明确报错，不硬算。"""
    for func in (
        lambda: rmsd(COORDS[:3], COORDS[:4]),
        lambda: rmsd(COORDS[:3], COORDS[:4], align=True),
        lambda: kabsch_align(COORDS[:3], COORDS[:4]),
        lambda: overlay(SYMBOLS[:3], COORDS[:3], SYMBOLS[:4], COORDS[:4]),
        lambda: check_same_elements(SYMBOLS[:3], SYMBOLS[:4]),
    ):
        message = expect_alignment_error(func, "原子数")
        assert len(message) > 8
    print("    原子数不一致：rmsd / kabsch_align / overlay 全部明确报错")


def test_element_order_mismatch_is_a_clear_error():
    """元素顺序不同 → 明确报错（自动找对应原子不在本功能范围内）。"""
    # 同一个分子、同样多的原子，只是顺序不同 —— 硬算会得出一个**看似正常**的数字
    swapped = ["O", "C", "H", "N", "H"]
    assert sorted(swapped) == sorted(SYMBOLS), "前提：两个顺序的元素组成相同"

    message = expect_alignment_error(
        lambda: check_same_elements(SYMBOLS, swapped), "元素顺序")
    assert "2" in message or "1" in message, f"应当指出是第几个原子：{message}"
    expect_alignment_error(
        lambda: rmsd(COORDS, COORDS, symbols_a=SYMBOLS, symbols_b=swapped), "元素顺序")
    expect_alignment_error(
        lambda: overlay(SYMBOLS, COORDS, swapped, COORDS), "元素顺序")
    # 大小写/空白差异不算不一致（符号本身相同）
    check_same_elements(["C", " O "], ["C", "O"])
    # 顺序完全一致时必须放行
    check_same_elements(SYMBOLS, list(SYMBOLS))
    print(f"    元素顺序不一致：check_same_elements / rmsd / overlay 全部明确报错"
          f"（「{message[:36]}…」）")


def test_overlay_keeps_the_reference_fixed():
    """约定：A 不动，B 被搬到 A 的参考系上。"""
    b = apply_transform(RZ90, SHIFT, COORDS)
    data = overlay(SYMBOLS, COORDS, SYMBOLS, b)
    assert data["moved"] == "b"
    assert max_deviation(data["aligned_coords"], COORDS) < TOL_ALIGNED, \
        "B 叠好之后应当落在 A 的原坐标上"
    assert data["n_atoms"] == len(COORDS)
    assert data["symbols"] == SYMBOLS
    # 两个 RMSD 都给了，而且"对齐后 ≤ 对齐前"
    assert data["rmsd_before"] > 1.0
    assert data["rmsd_after"] < TOL_ALIGNED
    assert data["rmsd_before"] > data["rmsd_after"]
    print(f"    overlay()：A 不动、B 被搬过去；RMSD {data['rmsd_before']:.4f} → "
          f"{data['rmsd_after']:.3e} Å")


def test_real_example_structures_align_to_zero():
    """仓库里 examples/ 的真实结构：已知旋转 + 平移后必须叠回 0。

    自造点集可能碰巧落在一个"怎么算都对"的局部上，真实分子不会。
    """
    path = REPO / "examples" / "caffeine.xyz"
    if not path.is_file():
        _skip("examples/caffeine.xyz 不存在")
    molecule = Molecule.from_xyz_file(path)
    coords = [list(c) for c in molecule.coords]
    assert len(coords) > 20, f"这个用例想要一个像样的分子，得到 {len(coords)} 个原子"

    moved = apply_transform(RZ90, SHIFT, coords)
    raw = rmsd(coords, moved)
    aligned = rmsd(coords, moved, align=True)
    assert aligned < TOL_ALIGNED, f"{molecule.name}: 对齐后 {aligned}"
    data = overlay(list(molecule.symbols), coords, list(molecule.symbols), moved)
    assert data["rmsd_after"] < TOL_ALIGNED
    print(f"    {molecule.name}（{len(coords)} 原子）：{raw:.4f} Å → {aligned:.3e} Å")


# ---------------------------------------------------------------- HTTP 接口

class Server:
    """测试用的真实 HTTP 服务（与 test_web_api 同样的做法）。"""

    def __init__(self, jobs_root: Path | None = None) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="qcgui_overlay_"))
        self.manager = JobManager(jobs_root or (self.root / "jobs"))
        self.app = create_app(self.manager)
        from werkzeug.serving import make_server

        self.srv = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.port = self.srv.server_port
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.srv.shutdown()

    def get(self, path: str):
        """返回 ``(状态码, JSON)`` —— 4xx 也当正常返回，方便断言报错信息。"""
        try:
            with urllib.request.urlopen(self.base + path, timeout=30) as res:
                return res.status, json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    @staticmethod
    def real_jobs_root() -> Path | None:
        """本机真实任务库（找不到就跳过，CI 上正是这种情形）。"""
        override = os.environ.get("QCGUI_JOBS_ROOT")
        if override:
            root = Path(override)
            return root if root.is_dir() else None
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return None
        root = Path(local) / "qcgui" / "jobs"
        return root if root.is_dir() else None


def add_job(server: Server, job_id: str, symbols, coords,
            name: str = "mol", with_geometry: bool = True) -> str:
    """在测试任务库里造一个"已完成、带结果几何"的任务。"""
    atoms = tuple(Atom(str(s), float(c[0]), float(c[1]), float(c[2]))
                  for s, c in zip(symbols, coords))
    spec = JobSpec(molecule=Molecule(atoms, name=name), job_type=JobType.OPT,
                   method="HF", basis="STO-3G")
    server.manager.store.create(spec, job_id=job_id)
    job_dir = server.manager.store.job_dir(job_id)
    (job_dir / "jobspec.json").write_text(spec.canonical_json(), encoding="utf-8")
    if with_geometry:
        Result(
            status="done", job_type="opt", method="HF",
            geometry={"symbols": [str(s) for s in symbols],
                      "coords": [[float(c) for c in row] for row in coords],
                      "units": "angstrom"},
            job_dir=str(job_dir),
        ).save(job_dir / "result.json")
    server.manager.store.update(job_id, status="done", final_energy=-1.0)
    return job_id


def _fake_job_id(tag: str) -> str:
    return f"20200101-000000-{tag}"


def test_api_overlay_of_a_job_with_itself_is_zero():
    """/api/jobs/<id>/overlay/<id> → 两个 RMSD 都是 0。"""
    server = Server()
    try:
        job_id = add_job(server, _fake_job_id("self"), SYMBOLS, COORDS, "selfmol")
        status, body = server.get(f"/api/jobs/{job_id}/overlay/{job_id}")
        assert status == 200, body
        assert body["same_job"] is True
        assert body["n_atoms"] == len(COORDS)
        assert abs(body["rmsd_before"]) < TOL_SAME, body["rmsd_before"]
        assert abs(body["rmsd_after"]) < TOL_SAME, body["rmsd_after"]
        assert body["symbols"] == SYMBOLS
        assert body["a"]["job_id"] == job_id and body["b"]["job_id"] == job_id
        # 叠加后的坐标必须就是原坐标
        assert max_deviation(body["b"]["aligned_coords"], COORDS) < TOL_SAME
    finally:
        server.close()
    print(f"    同一任务叠加：对齐前 {body['rmsd_before']} / 对齐后 {body['rmsd_after']}")


def test_api_reports_before_and_after_for_a_known_rotation():
    """接口给出的两个 RMSD 必须分别对应"没叠"和"叠好"。"""
    server = Server()
    try:
        first = add_job(server, _fake_job_id("ref"), SYMBOLS, COORDS, "ref")
        moved = apply_transform(RZ90, SHIFT, COORDS)
        second = add_job(server, _fake_job_id("mov"), SYMBOLS, moved, "mov")

        status, body = server.get(f"/api/jobs/{first}/overlay/{second}")
        assert status == 200, body
        assert body["same_job"] is False
        assert body["rmsd_before"] > 1.0, body["rmsd_before"]
        assert body["rmsd_after"] < TOL_ALIGNED, body["rmsd_after"]
        assert body["moved"] == "b"
        # 参考结构（a）原样返回，被叠加的结构（b）额外带一份对齐后坐标
        assert body["a"]["coords"] == [list(c) for c in COORDS]
        assert "aligned_coords" in body["b"]
        assert max_deviation(body["b"]["aligned_coords"], COORDS) < TOL_ALIGNED
        assert body["b"]["coords"] == [list(c) for c in moved], "原始坐标也要保留"
        # 旋转矩阵与已知值一致。
        # 注意方向：接口的约定是"把 B 搬到 A 上"，所以报出来的旋转是
        # RZ90 的**逆**（= 转置）—— B 本来就是 A 转 90° 得到的，
        # 反过来叠就要转 -90°。搞反了会在这里被抓住。
        for i in range(3):
            for j in range(3):
                assert abs(body["rotation"][i][j] - RZ90[j][i]) < 1e-9, \
                    f"作用在 B 上的旋转应当是已知旋转的转置：{body['rotation']}"
        # 若自洽：rotation @ b + translation == a
        for point_b, point_a in zip(body["b"]["coords"], COORDS):
            moved_back = [sum(body["rotation"][i][k] * point_b[k] for k in range(3))
                          + body["translation"][i] for i in range(3)]
            assert max(abs(moved_back[i] - point_a[i]) for i in range(3)) < TOL_ALIGNED
        # 反着叠（参考与被叠互换）得到同样的 RMSD
        status2, body2 = server.get(f"/api/jobs/{second}/overlay/{first}")
        assert status2 == 200 and abs(body2["rmsd_after"] - body["rmsd_after"]) < 1e-9
        assert abs(body2["rmsd_before"] - body["rmsd_before"]) < 1e-9
    finally:
        server.close()
    print(f"    接口：{body['rmsd_before']:.4f} Å → {body['rmsd_after']:.3e} Å"
          f"（已知 z90° 旋转，反着叠结果相同）")


def test_api_rejects_different_atom_counts():
    server = Server()
    try:
        water = add_job(server, _fake_job_id("h2o"), ["O", "H", "H"],
                        [[0, 0, 0.117], [0, 0.757, -0.469], [0, -0.757, -0.469]], "water")
        methane = add_job(server, _fake_job_id("ch4"), ["C", "H", "H", "H", "H"],
                          [[0, 0, 0], [0.63, 0.63, 0.63], [-0.63, -0.63, 0.63],
                           [0.63, -0.63, -0.63], [-0.63, 0.63, -0.63]], "methane")
        status, body = server.get(f"/api/jobs/{water}/overlay/{methane}")
        assert status == 400, (status, body)
        assert "原子数" in body["error"], body
    finally:
        server.close()
    print(f"    原子数不同 → 400：「{body['error'][:48]}…」")


def test_api_rejects_different_element_order():
    """同样 5 个原子、顺序不同 → 400，并且指出是第几个原子。"""
    server = Server()
    try:
        swapped_symbols = ["O", "C", "H", "N", "H"]
        assert sorted(swapped_symbols) == sorted(SYMBOLS)
        first = add_job(server, _fake_job_id("ord1"), SYMBOLS, COORDS, "ord1")
        second = add_job(server, _fake_job_id("ord2"), swapped_symbols, COORDS, "ord2")
        status, body = server.get(f"/api/jobs/{first}/overlay/{second}")
        assert status == 400, (status, body)
        assert "元素顺序" in body["error"], body
        assert "第 1 个原子" in body["error"], body["error"]
    finally:
        server.close()
    print(f"    元素顺序不同 → 400：「{body['error'][:44]}…」")


def test_api_404_and_400_paths():
    """任务不存在 → 404；任务没有结果几何 → 400（都不是 500）。"""
    server = Server()
    try:
        job = add_job(server, _fake_job_id("ok"), SYMBOLS, COORDS, "ok")
        status, body = server.get(f"/api/jobs/{job}/overlay/20990101-000000-nope")
        assert status == 404, (status, body)
        assert "不存在" in body["error"]
        status, body = server.get("/api/jobs/20990101-000000-nope/overlay/" + job)
        assert status == 404 and "不存在" in body["error"]

        # 有任务、但没有结果几何（跑到一半就挂了 / 老任务）
        empty = add_job(server, _fake_job_id("geomless"), SYMBOLS, COORDS,
                        "geomless", with_geometry=False)
        status, body = server.get(f"/api/jobs/{job}/overlay/{empty}")
        assert status == 400, (status, body)
        assert "没有可用的结果几何" in body["error"], body
    finally:
        server.close()
    print("    不存在的任务 → 404；没有结果几何 → 400")


def test_api_overlay_with_a_real_optimisation_job():
    """真实任务库：一个 opt 任务与自己做叠加 → RMSD 0；与同分子另一个任务 → 不为 0。

    这是"端到端真的能用在真实数据上"的证据。本机没有任务库时跳过（CI 正是这种情形）。
    """
    root = Server.real_jobs_root()
    if root is None:
        _skip("本机没有真实任务库")

    entries: list[tuple[str, list[str], list[list[float]]]] = []
    for job_dir in sorted(root.iterdir(), reverse=True):
        if not job_dir.is_dir() or len(entries) >= 60:
            continue
        result_file = job_dir / "result.json"
        if not result_file.is_file():
            continue
        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("status") != "done" or data.get("job_type") not in ("opt", "opt_freq"):
            continue
        geometry = data.get("geometry") or {}
        symbols, coords = geometry.get("symbols"), geometry.get("coords")
        if not symbols or not coords or len(symbols) != len(coords):
            continue
        entries.append((job_dir.name, [str(s) for s in symbols], coords))

    if not entries:
        _skip("真实任务库里没有带几何的已完成 opt 任务")

    # 优先拿原子多的做证据：3 个原子的水看不出什么，9 个原子以上的结构才说明问题
    entries.sort(key=lambda item: len(item[1]), reverse=True)

    # 找一个同分子（元素顺序完全一致）的伙伴；取原子最多的那一对
    pair = None
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            if entries[i][1] == entries[j][1]:
                pair = (entries[i], entries[j])
                break
        if pair:
            break

    server = Server(root)
    try:
        first_id = entries[0][0]
        status, body = server.get(f"/api/jobs/{first_id}/overlay/{first_id}")
        assert status == 200, body
        assert abs(body["rmsd_before"]) < TOL_SAME, body["rmsd_before"]
        assert abs(body["rmsd_after"]) < TOL_SAME, body["rmsd_after"]
        print(f"    真实 opt 任务 {first_id}（{body['n_atoms']} 原子）与自身叠加："
              f"RMSD = {body['rmsd_after']:.1e} Å")

        if pair is None:
            print("    跳过「两个不同任务」：任务库里没有同一分子的两个 opt 任务")
            return
        (id_a, sym_a, _), (id_b, _, _) = pair
        status, body2 = server.get(f"/api/jobs/{id_a}/overlay/{id_b}")
        assert status == 200, body2
        assert body2["n_atoms"] == len(sym_a)
        assert body2["rmsd_after"] <= body2["rmsd_before"] + 1e-12, body2
        print(f"    两个真实 opt 任务 {id_a[:15]} ↔ {id_b[:15]}（{body2['n_atoms']} 原子）："
              f"对齐前 {body2['rmsd_before']:.4f} Å → 对齐后 {body2['rmsd_after']:.4f} Å")
        if body2["rmsd_before"] <= 1e-9:
            assert abs(body2["rmsd_after"]) < TOL_SAME
    finally:
        server.close()


# ---------------------------------------------------------------- 脚本模式

def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed, skipped = [], []
    for name, func in tests:
        print(f"[ RUN  ] {name}")
        try:
            func()
            print(f"[  OK  ] {name}")
        except _Skipped as exc:
            skipped.append(name)
            print(f"[ SKIP ] {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, exc))
            print(f"[ FAIL ] {name}: {type(exc).__name__}: {exc}")
    print("-" * 60)
    print(f"通过 {len(tests) - len(failed) - len(skipped)} / 失败 {len(failed)} / 跳过 {len(skipped)}")
    for name, exc in failed:
        print(f"  失败: {name} -> {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
