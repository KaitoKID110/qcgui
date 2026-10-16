"""红外谱图（功能 C：频率 → 谱线）的测试。

分三块，对应三类"看起来对但其实不对"的风险：

1. **展宽数学**（纯函数）：峰位不能被展宽挪动、FWHM 要真的可控。
   "看起来更宽"不算验证 —— 所以这里用**测试自己实现**的半高全宽测量
   （线性插值找半高交点）去量，绝不复用被测代码里的任何东西，
   否则"fwhm 越大峰越宽"就成了自证。
2. **渲染出来的 PNG 不是空白**：本项目已经三次栽在"DOM 里有但屏幕上没有"，
   对图片来说同一个坑是"文件在但一片白"。所以数像素，不看 src 非空。
3. **真浏览器**：谱图区真的出现在结果面板上、图真的加载出来、虚频行有醒目标记。
   找不到 Chrome 时整体跳过。

     python tests/test_spectrum.py
     pytest tests/test_spectrum.py -v

.. important::
    本模块不 import qcgui.engines（与界面进程同样的约束）——
    ``test_web_process_stays_engine_free`` 在**子进程**里复核这一点。
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.jobs import JobManager  # noqa: E402
from qcgui.model import JobSpec, JobType, Molecule, Result  # noqa: E402
from qcgui.model.spectrum import (  # noqa: E402
    DEFAULT_STEP,
    KIND_GAUSSIAN,
    KIND_LORENTZIAN,
    NO_INTENSITY_NOTE,
    broaden,
    default_range,
    gaussian_broadened,
    lorentzian_broadened,
    normalize_peaks,
    peaks_from_frequencies,
    strongest_index,
)
from qcgui.web.app import create_app  # noqa: E402

WATER_XYZ = """3
water
O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
"""

#: 6 个平动/转动 0 模态 + 1 个虚频 + 8 个实频（形状参考真实 opt_freq 任务）
FREQS = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
         -146.98, 320.4, 617.2, 928.1, 1103.6, 1450.9, 1700.3, 2930.5, 3200.7]
INTS = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
        12.4, 5.1, 31.7, 14.9, 120.3, 42.8, 88.6, 25.3, 79.1]

# ------------------------------------------------------------------ 真实 ORCA 实测值
#
# 2026-10-16 本机实跑（HF/STO-3G 水，``%pal nprocs 1``，D:\orca1\orca.exe，exit=0），
# 用来给"频率从哪来、数值对不对"留一份真凭据，而不是只信自己造的夹具：
#
# * ``THERMOCHEMISTRY_Energies[0].FREQ``（``orca_2json probe.gbw -property``
#   产出的 ``probe.property.json``，即引擎层真正读的那份结构化输出）
#   = 6 个 0.00 + 2043.234 / 4488.351 / 4790.412 cm⁻¹ —— 与 ``probe.out`` 里
#   ``VIBRATIONAL FREQUENCIES`` 表逐行一致（数值未做任何换算或缩放）；
# * 同一份 ``.out`` 的 ``IR SPECTRUM`` 表给出强度 11.42 / 37.32 / 19.93 km/mol。
#   **这三个数只存在于人读文本里**：结构化输出里既没有 ``ntens`` 也没有 ``IR``
#   键（实测 ``probe.property.json`` 顶层只有 Calculation_Info / Calculation_Status
#   / Calculation_Timings / Geometries）。所以引擎层**不能**在不违反"结果只从
#   结构化输出取"的前提下补上强度 —— 详见报告。
ORCA_WATER_FREQS = [0.0] * 6 + [2043.2341884, 4488.3510718, 4790.4118223]
#: 只在 .out 文本里出现过的那三个强度，用来验"有强度"时的口径（夹具用）。
ORCA_WATER_INTS = [11.42, 37.32, 19.93]


class _Skipped(Exception):
    pass


def _skip(reason: str):
    try:
        import pytest

        pytest.skip(reason)
    except ImportError:
        raise _Skipped(reason)


def _have_pil() -> bool:
    try:
        import PIL  # noqa: F401

        return True
    except ImportError:
        return False


# ------------------------------------------------------------------ 量 FWHM


def measure_fwhm(xs, ys) -> float:
    """在一条谱线上**量出**半高全宽（cm⁻¹）。

    刻意不复用 qcgui.model.spectrum 里的任何东西：如果让实现自己报 FWHM，
    那"fwhm 越大峰越宽"这条验收就成了自证。这里从头做：
    找最高点 → 左右各找第一个跌破半高的采样点 → 线性插值出半高交点。
    """
    peak = max(ys)
    if peak <= 0:
        return 0.0
    half = peak / 2.0
    top = ys.index(peak)

    def cross(indices):
        prev_x, prev_y = xs[top], ys[top]
        for k in indices:
            if ys[k] < half:
                denom = prev_y - ys[k]
                t = (prev_y - half) / denom if denom > 0 else 0.0
                return prev_x + (xs[k] - prev_x) * t
            prev_x, prev_y = xs[k], ys[k]
        return None

    left = cross(range(top - 1, -1, -1))
    right = cross(range(top + 1, len(ys)))
    assert left is not None and right is not None, "半高交点在采样范围内找不到"
    return right - left


def count_local_maxima(ys) -> int:
    """局部极大个数（忽略完全相等的平台）。"""
    n = 0
    for k in range(1, len(ys) - 1):
        if ys[k] > ys[k - 1] and ys[k] >= ys[k + 1]:
            n += 1
    return n


def _argmax_position(xs, ys, near: float) -> float:
    """离 ``near`` 最近的局部区域里的最高点位置（用来比较展宽前后峰位）。"""
    best, best_y = None, -1.0
    for x, y in zip(xs, ys):
        if abs(x - near) < 60 and y > best_y:      # 60 cm⁻¹ 内归同一个峰
            best, best_y = x, y
    assert best is not None
    return best


# ------------------------------------------------------------------ 1. 展宽数学


def test_single_peak_maximum_sits_on_the_peak_position():
    """单个峰：展宽后最大值必须落在峰位上，误差 < 步长。"""
    step = 1.0
    xs, ys = gaussian_broadened([(1000.0, 1.0)], 800.0, 1200.0, step, 20.0)
    assert len(xs) == len(ys) and len(xs) > 100
    top = xs[ys.index(max(ys))]
    assert abs(top - 1000.0) < step, f"峰位被挪到了 {top}"
    assert abs(max(ys) - 1.0) < 1e-9, max(ys)      # 峰高 = 强度
    print(f"    单峰：峰位 {top} cm-1，峰高 {max(ys):.6f}（误差 {abs(top - 1000.0):g} < 步长 {step:g}）")


def test_peak_position_error_under_step_when_peak_is_off_grid():
    """峰位不在采样点上时，取到的最高点与真峰位的差仍要小于一个步长。"""
    step = 3.0
    xs, ys = gaussian_broadened([(1000.0, 1.0)], 800.0, 1200.0, step, 20.0)
    top = xs[ys.index(max(ys))]
    assert abs(top - 1000.0) < step, f"峰位误差 {abs(top - 1000.0)} 超过步长 {step}"
    print(f"    离格峰位：取到 {top}，与 1000 相差 {abs(top - 1000.0):g} < 步长 {step:g}")


def test_two_far_apart_peaks_give_exactly_two_maxima():
    """两个隔得很远的峰 → 曲线上正好两个局部极大，且各自贴近原峰位。"""
    xs, ys = gaussian_broadened([(1000.0, 1.0), (3000.0, 0.6)], 800.0, 3200.0, 1.0, 20.0)
    assert count_local_maxima(ys) == 2, f"局部极大个数 {count_local_maxima(ys)}"
    assert abs(_argmax_position(xs, ys, 1000.0) - 1000.0) < 1.0
    assert abs(_argmax_position(xs, ys, 3000.0) - 3000.0) < 1.0
    print("    双峰：两个局部极大，位置 1000 / 3000")


def test_measured_fwhm_follows_the_fwhm_parameter():
    """**直接量**半高全宽：量出来的值要贴着参数，且参数变大时必须变宽。"""
    measured = {}
    for fwhm in (10.0, 20.0, 40.0):
        xs, ys = gaussian_broadened([(1500.0, 1.0)], 1200.0, 1800.0, 0.5, fwhm)
        measured[fwhm] = measure_fwhm(xs, ys)
        # 高斯线型的半高全宽就是参数本身，允许一个采样步长的插值误差
        assert abs(measured[fwhm] - fwhm) < 0.5 + 0.02 * fwhm, (fwhm, measured[fwhm])
    assert measured[20.0] > measured[10.0] * 1.5, measured
    assert measured[40.0] > measured[20.0] * 1.8, measured
    print("    高斯实测 FWHM：" + " / ".join(
        f"参数 {k:g} → 量出 {v:.2f}" for k, v in measured.items()))


def test_measured_fwhm_follows_the_fwhm_parameter_for_lorentzian():
    """洛伦兹线型同样要"参数多大、量出来就多大"。"""
    measured = {}
    for fwhm in (10.0, 30.0):
        xs, ys = lorentzian_broadened([(1500.0, 1.0)], 1200.0, 1800.0, 0.5, fwhm)
        measured[fwhm] = measure_fwhm(xs, ys)
        assert abs(measured[fwhm] - fwhm) < 0.5 + 0.02 * fwhm, (fwhm, measured[fwhm])
    assert measured[30.0] > measured[10.0] * 2.0
    print("    洛伦兹实测 FWHM：" + " / ".join(
        f"参数 {k:g} → 量出 {v:.2f}" for k, v in measured.items()))


def test_broadening_does_not_move_peak_positions():
    """卷积不改峰位：窄展宽和宽展宽下，同一个峰的最高点位置必须一致。

    这条和"FWHM 可控"是配套的 —— 只调宽度而不挪位置，才谈得上"峰位可信"。
    """
    peaks = [(1000.0, 1.0), (2500.0, 0.5)]
    narrow_xs, narrow_ys = gaussian_broadened(peaks, 800.0, 2700.0, 1.0, 2.0)
    wide_xs, wide_ys = gaussian_broadened(peaks, 800.0, 2700.0, 1.0, 80.0)
    assert len(narrow_xs) == len(wide_xs)

    for target in (1000.0, 2500.0):
        narrow = _argmax_position(narrow_xs, narrow_ys, target)
        wide = _argmax_position(wide_xs, wide_ys, target)
        assert abs(narrow - target) < 1.0, (target, narrow)
        assert abs(wide - target) < 1.0, (target, wide)
        assert abs(narrow - wide) < 1.0, (target, narrow, wide)
    # 两个峰之间必须真的有谷（不然"两个峰"就糊成一个了，峰位比较没有意义）
    mid = narrow_ys.index(min(narrow_ys[100:200]))
    assert narrow_ys[mid] < 0.1
    print("    窄/宽展宽下峰位都是 1000 与 2500（< 1 个步长）")


def test_lorentzian_tails_are_heavier_than_gaussian():
    """洛伦兹的长尾比高斯重（线型选错了谱形会不对，这条守住差异）。"""
    x_min, x_max = 900.0, 1100.0
    gx, gy = gaussian_broadened([(1000.0, 1.0)], x_min, x_max, 1.0, 20.0)
    lx, ly = lorentzian_broadened([(1000.0, 1.0)], x_min, x_max, 1.0, 20.0)
    # 距峰 100 cm⁻¹（5 倍 FWHM）处
    k = gx.index(1100.0)
    assert ly[k] > gy[k] * 3, (gy[k], ly[k])
    assert abs(measure_fwhm(gx, gy) - measure_fwhm(lx, ly)) < 1.0
    print(f"    5 倍 FWHM 处：高斯 {gy[k]:.5f} < 洛伦兹 {ly[k]:.5f}")


def test_empty_peaks_return_empty_curve():
    """空输入：不崩，返回空曲线（调用方据此显示"没有峰"）。"""
    for fn in (gaussian_broadened, lorentzian_broadened):
        xs, ys = fn([], 400.0, 4000.0, 2.0, 20.0)
        assert xs == [] and ys == [], (fn.__name__, len(xs))
    xs, ys = broaden([], 400.0, 4000.0, 2.0, 20.0, KIND_GAUSSIAN)
    assert (xs, ys) == ([], [])
    print("    空峰列表 → ([], [])（两种线型都验了）")


def test_all_zero_intensities_give_flat_zero_curve():
    """全零强度：照样出采样点，但曲线全是 0（不是崩，也不是全 1）。"""
    xs, ys = gaussian_broadened([(1000.0, 0.0), (2000.0, 0.0)], 800.0, 2200.0, 2.0, 20.0)
    assert len(xs) == len(ys) and len(xs) > 100
    assert max(ys) == 0.0 and min(ys) == 0.0
    print(f"    全零强度 → {len(xs)} 个采样点、最大值 0")


def test_missing_intensity_defaults_to_one():
    """强度缺失按 1.0 处理，但**必须**通过 has_intensity 告诉上层这是补的。"""
    assert normalize_peaks([(1000.0, None), (2000.0, 5.0)]) == [(1000.0, 1.0), (2000.0, 5.0)]
    # 裸数字（只有频率）也算"没有强度"
    assert normalize_peaks([1000.0, 2000.0]) == [(1000.0, 1.0), (2000.0, 1.0)]

    peaks, has = peaks_from_frequencies([1000.0, 2000.0])
    assert has is False, "没有强度时必须报告 has_intensity=False"
    assert [i for _, i in peaks] == [1.0, 1.0]

    peaks2, has2 = peaks_from_frequencies([1000.0, 2000.0], [3.0, 7.0])
    assert has2 is True
    assert [i for _, i in peaks2] == [3.0, 7.0]
    print("    缺强度 → 1.0 且 has_intensity=False；有强度 → 原值且 True")


def test_intensity_length_mismatch_counts_as_missing():
    """强度个数与频率个数对不上 → 当作没有强度（错位会让整张谱的峰高全错）。"""
    peaks, has = peaks_from_frequencies([1000.0, 2000.0, 3000.0], [5.0])
    assert has is False, "长度对不上时不能硬配"
    assert [i for _, i in peaks] == [1.0, 1.0, 1.0]
    print("    长度不一致 → 按无强度处理")


def test_zero_modes_are_dropped_and_imaginary_kept():
    """平动/转动的 0 模态要剔掉，负波数的虚频必须留着。

    剔 0 模态：ORCA 自己也不把它们算作振动（"first frequency considered to be
    a vibration"），画成峰会多出一根假的低波数峰。
    留虚频：过渡态就是靠它判断的，少了它这个功能就白做。
    """
    peaks, _ = peaks_from_frequencies(FREQS)
    assert len(peaks) == 9, [p for p, _ in peaks]
    assert all(abs(p) > 1e-6 for p, _ in peaks)
    assert any(p < 0 for p, _ in peaks)
    # 不剔的话 15 个全在
    keep_all, _ = peaks_from_frequencies(FREQS, drop_zero_modes=False)
    assert len(keep_all) == 15
    print("    0 模态剔除（15 → 9），虚频 -146.98 保留")


def test_default_range_always_includes_the_imaginary_frequency():
    """波数窗口必须把虚频包进去，否则最关键的那根峰会被切掉。"""
    peaks, _ = peaks_from_frequencies(FREQS)
    x_min, x_max = default_range(peaks)
    assert x_min < -146.98 < x_max, (x_min, x_max)
    assert x_max > 3200.7, (x_min, x_max)
    # 没有峰时给一个默认中红外窗口，而不是抛异常
    assert default_range([]) == (0.0, 4000.0)
    # 单峰窗口不要缩成一条缝
    lo, hi = default_range([(1000.0, 1.0)])
    assert hi - lo >= 200.0, (lo, hi)
    print(f"    窗口 {x_min:g} ~ {x_max:g} cm-1（含虚频 -146.98）")


def test_strongest_peak_is_the_largest_intensity():
    """最强峰 = 强度最大的那个（用来在图上标"最强峰"）。"""
    peaks, _ = peaks_from_frequencies(FREQS, INTS)
    idx = strongest_index(peaks)
    assert peaks[idx][0] == 1103.6 and peaks[idx][1] == 120.3, peaks[idx]
    assert strongest_index([]) is None
    print(f"    最强峰 {peaks[idx][0]} cm-1（强度 {peaks[idx][1]}）")


def test_invalid_arguments_are_rejected():
    """参数不合法要报错，而不是画出一张看着正常的错图。"""
    cases = [
        dict(x_min=1000.0, x_max=1000.0, step=1.0, fwhm=20.0),   # 上下界相等
        dict(x_min=1200.0, x_max=1000.0, step=1.0, fwhm=20.0),   # 上界小于下界
        dict(x_min=800.0, x_max=1200.0, step=0.0, fwhm=20.0),    # 步长 0
        dict(x_min=800.0, x_max=1200.0, step=-1.0, fwhm=20.0),   # 步长负
        dict(x_min=800.0, x_max=1200.0, step=1.0, fwhm=0.0),     # 宽度 0
        dict(x_min=800.0, x_max=400000.0, step=0.001, fwhm=20.0),  # 点数爆炸
    ]
    for case in cases:
        try:
            gaussian_broadened([(1000.0, 1.0)], **case)
        except ValueError:
            continue
        raise AssertionError(f"该报错却没报：{case}")
    try:
        broaden([(1000.0, 1.0)], 800.0, 1200.0, 1.0, 20.0, "triangle")
    except ValueError:
        pass
    else:
        raise AssertionError("未知线型该报错")
    # 峰列表本身坏了也要报错（不然会静默画出别的峰）
    try:
        normalize_peaks([("x", 1.0)])
    except ValueError:
        pass
    else:
        raise AssertionError("峰位不是数该报错")
    print("    6 组非法参数 + 未知线型 + 坏峰位全部报 ValueError")


def test_broaden_dispatches_to_the_named_lineshape():
    peaks = [(1000.0, 1.0), (2000.0, 0.4)]
    assert broaden(peaks, 800.0, 2200.0, 1.0, 20.0, KIND_GAUSSIAN) \
        == gaussian_broadened(peaks, 800.0, 2200.0, 1.0, 20.0)
    assert broaden(peaks, 800.0, 2200.0, 1.0, 20.0, KIND_LORENTZIAN) \
        == lorentzian_broadened(peaks, 800.0, 2200.0, 1.0, 20.0)
    print("    broaden(kind=...) 与两个专用函数结果一致")


# ------------------------------------------------------------------ 2. 渲染


def _render(**over):
    from qcgui.web.spectrum_plot import render_spectrum_png

    peaks, has = peaks_from_frequencies(FREQS, over.pop("ints", None))
    args = dict(x_min=-250.0, x_max=3350.0, step=DEFAULT_STEP, fwhm=20.0,
                kind=KIND_GAUSSIAN, has_intensity=has, title="红外谱图")
    args.update(over)
    return render_spectrum_png(peaks, **args)


def test_png_is_not_a_blank_image():
    """画出来的 PNG 必须**有内容**：非白像素占比、蓝色谱线、红色虚频标记都要有。

    只看"文件存在/size > 0"是不够的 —— 一张全白的 PNG 也满足那两条，
    而本项目正是在"数据对了但屏幕上没有"上栽过三次。
    """
    if not _have_pil():
        _skip("本机没有 Pillow")
    from PIL import Image

    png = _render(ints=INTS)
    assert png[:8] == b"\x89PNG\r\n\x1a\n", png[:8]
    img = Image.open(io.BytesIO(png)).convert("RGB")
    assert img.size == (880, 450), img.size
    px = img.load()

    nonwhite = curve = imag = 0
    for y in range(img.size[1]):
        for x in range(img.size[0]):
            r, g, b = px[x, y]
            if r < 245 or g < 245 or b < 245:
                nonwhite += 1
            # 谱线色 (47,111,208) / 虚频色 (208,58,58)
            if abs(r - 47) < 26 and abs(g - 111) < 26 and abs(b - 208) < 26:
                curve += 1
            if abs(r - 208) < 30 and g < 100 and b < 100:
                imag += 1
    total = img.size[0] * img.size[1]
    assert nonwhite / total > 0.01, f"图几乎是白的：非白像素 {nonwhite}/{total}"
    assert curve > 500, f"没画到谱线（蓝色像素只有 {curve}）"
    assert imag > 50, f"虚频的红色标记没画出来（{imag} 个红像素）"
    print(f"    PNG {img.size[0]}x{img.size[1]}：非白 {nonwhite / total * 100:.2f}%、"
          f"谱线像素 {curve}、虚频红像素 {imag}")


def test_png_of_zero_intensity_still_renders():
    """强度全为 0 也要出图（坐标框 + 一句说明），不能抛异常也不能空文件。"""
    if not _have_pil():
        _skip("本机没有 Pillow")
    from PIL import Image

    png = _render(ints=[0.0] * len(FREQS))
    img = Image.open(io.BytesIO(png))
    assert img.size == (880, 450)
    assert len(png) > 3000, len(png)
    print(f"    全零强度也能出图（{len(png)} 字节）")


def test_png_lorentzian_is_a_different_picture():
    """换线型必须真的换一张图（否则下拉框是摆设）。"""
    if not _have_pil():
        _skip("本机没有 Pillow")
    a = _render(ints=INTS, kind=KIND_GAUSSIAN)
    b = _render(ints=INTS, kind=KIND_LORENTZIAN)
    assert a != b
    print(f"    高斯 {len(a)} 字节 ≠ 洛伦兹 {len(b)} 字节")


# ------------------------------------------------------------------ 3. 接口


class Server:
    """真 HTTP 服务（随机端口），测的是接口而不是直接调函数。"""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="qcgui_spec_"))
        self.manager = JobManager(self.root / "jobs")
        self.app = create_app(self.manager)
        from werkzeug.serving import make_server

        self.srv = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.port = self.srv.server_port
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()

    def add_job(self, job_id: str, frequencies, intensities=None) -> None:
        spec = JobSpec(molecule=Molecule.from_xyz(WATER_XYZ, name="water"),
                       job_type=JobType.FREQ, method="r2SCAN-3c", basis="")
        self.manager.store.create(spec, job_id=job_id)
        job_dir = self.manager.store.job_dir(job_id)
        Result(status="done", engine="orca", engine_version="6.1.1",
               job_type="freq", method="r2SCAN-3c", final_energy=-76.0,
               geometry={"symbols": ["O", "H", "H"],
                         "coords": [[0, 0, 0.1173], [0, 0.7572, -0.4692],
                                    [0, -0.7572, -0.4692]], "units": "angstrom"},
               frequencies=list(frequencies),
               ir_intensities=list(intensities or []),
               job_dir=str(job_dir)).save(job_dir / "result.json")
        self.manager.store.update(job_id, status="done", final_energy=-76.0)

    def get(self, path: str, timeout: float = 30.0):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as res:
            return res.status, res.headers, res.read()


def _json_get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=30.0) as res:
        return json.loads(res.read().decode("utf-8"))


def test_real_orca_water_frequencies_flow_through():
    """**真实 ORCA 输出**（HF/STO-3G 水）走完"频率 → 峰 → 波数窗口"这条链。

    这条测试的价值在于数据是真的：上面 ``ORCA_WATER_FREQS`` 抄自本机实跑的
    ``probe.property.json``。它同时钉住三件容易悄悄跑偏的事：

    1. 6 个平动/转动 0 模态被剔掉，只剩 3 个真振动（水是非线性的，正好 3N-6=3）；
    2. 频率数值**不被改写**（HF/STO-3G 就是偏大，2043/4488/4790 —— 不许因为
       "看着不像文献值"就顺手缩放，协议硬规矩 5）；
    3. 窗口必须把 4790 这个大数包进去（只按默认 0~4000 画会把 OH 伸缩切掉）。
    """
    peaks, has_intensity = peaks_from_frequencies(ORCA_WATER_FREQS, None)
    assert has_intensity is False
    assert len(peaks) == 3, peaks
    assert [round(p, 1) for p, _ in peaks] == [2043.2, 4488.4, 4790.4]
    # 0 模态一个都不许漏进来
    assert all(abs(p) > 1e-6 for p, _ in peaks)

    x_min, x_max = default_range(peaks)
    assert x_min <= 2043.23 and x_max >= 4790.41, (x_min, x_max)

    # 把 .out 里那三个真实强度按位给上：最强峰必须是 4488.35（37.32 km/mol）
    with_ints, has_intensity2 = peaks_from_frequencies(
        ORCA_WATER_FREQS, [0.0] * 6 + ORCA_WATER_INTS
    )
    assert has_intensity2 is True
    best = strongest_index(with_ints)
    assert with_ints[best][0] == ORCA_WATER_FREQS[-2]      # 4488.3510718，原值不许被改写
    assert with_ints[best][1] == 37.32
    assert best == 1, best
    print(f"    真实 ORCA 水：0 模态 6 个已剔，3 个振动 "
          f"{[round(p, 1) for p, _ in peaks]}，窗口 {x_min} ~ {x_max}"
          f"，最强峰 {with_ints[best][0]:.1f}")


def test_missing_ir_intensity_downgrades_the_task_verdict():
    """无强度谱 → 任务结论必须是"可用但有限制"，**不能是绿色可用**（硬规矩 3）。

    为什么这条比"界面显示了一句提示"更重要：用户不读提示，只看那个绿点。
    ORCA 的 IR 强度只在 ``.out`` 的 ``IR SPECTRUM`` 定宽表里，结构化输出
    （``orca_2json`` 的 ``THERMOCHEMISTRY_Energies``）里没有，所以真实频率任务
    一律拿不到强度 —— 这时候一条**等高峰谱**很容易被读成真实红外强度。

    判据只降级到 ``limited``（不是 ``unusable``）：频率、热化学、虚频判断都还是
    有效的，坏掉的只是"峰高"这一项，说"结果不可用"同样是假话。
    """
    server = Server()
    try:
        server.add_job("20200101-000000-verdictreal", FREQS, INTS)
        server.add_job("20200101-000001-verdictnone", FREQS)

        real = _json_get(server.base, "/api/jobs/20200101-000000-verdictreal")
        assert real["result"]["verdict"] == "ok", real["result"]["verdict_reason"]

        plain = _json_get(server.base, "/api/jobs/20200101-000001-verdictnone")
        res = plain["result"]
        assert res["verdict"] == "limited", res["verdict"]
        assert res["verdict_label"] == "结果可用，但有限制", res["verdict_label"]
        assert "无强度数据" in res["verdict_reason"], res["verdict_reason"]
        assert "频率与热化学本身有效" in res["verdict_reason"], res["verdict_reason"]
        print(f"    有强度 → {real['result']['verdict']}；"
              f"无强度 → {res['verdict']}（{res['verdict_reason']}）")
    finally:
        server.close()


def test_api_payload_reports_intensity_and_imaginary():
    """``/spectrum`` 的数据：峰表、虚频标记、强度有无、最强峰。"""
    server = Server()
    try:
        server.add_job("20200101-000000-specreal", FREQS, INTS)
        server.add_job("20200101-000001-specnone", FREQS)
        data = _json_get(server.base, "/api/jobs/20200101-000000-specreal/spectrum")
        assert data["has_intensity"] is True
        assert data["note"] == "", data["note"]
        assert data["imaginary_count"] == 1
        assert data["imaginary_wavenumbers"] == [-146.98]
        assert data["dropped_zero_modes"] == 6
        assert len(data["peaks"]) == 9
        assert data["strongest"]["wavenumber"] == 1103.6
        assert sum(1 for p in data["peaks"] if p["imaginary"]) == 1
        assert sum(1 for p in data["peaks"] if p["strongest"]) == 1
        assert all(p["intensity_available"] for p in data["peaks"])
        assert data["png_url"].endswith("spectrum.png?kind=gaussian&fwhm=20")
        assert data["x_min"] < -146.98 < data["x_max"]

        # 没有强度的任务：note 必须是那句"无强度谱"，且不标最强峰（补的 1.0 不算）
        plain = _json_get(server.base, "/api/jobs/20200101-000001-specnone/spectrum")
        assert plain["has_intensity"] is False
        assert plain["note"] == NO_INTENSITY_NOTE, plain["note"]
        assert "无强度数据" in plain["note"]
        assert plain["strongest"] is None
        assert not any(p["intensity_available"] for p in plain["peaks"])
        assert all(p["intensity"] == 1.0 for p in plain["peaks"])
        print(f"    有强度：最强 {data['strongest']['wavenumber']}；"
              f"无强度：note 已标明、不标最强峰")

        # 参数生效
        wide = _json_get(server.base, "/api/jobs/20200101-000000-specreal/spectrum"
                                        "?kind=lorentzian&fwhm=60")
        assert wide["kind"] == KIND_LORENTZIAN and wide["fwhm"] == 60.0
        assert wide["kind_label"] == "洛伦兹 (Lorentzian)"
    finally:
        server.close()


def test_api_png_endpoint_serves_a_real_image():
    """``/spectrum.png`` 走真 HTTP 拿到 PNG，且**不是空白图**。"""
    server = Server()
    try:
        server.add_job("20200101-000000-specreal", FREQS, INTS)
        status, headers, body = server.get("/api/jobs/20200101-000000-specreal/spectrum.png")
        assert status == 200, status
        assert headers.get("Content-Type") == "image/png", headers.get("Content-Type")
        assert body[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(body) > 10_000, len(body)
    finally:
        server.close()

    if not _have_pil():
        _skip("本机没有 Pillow（接口已验到，像素级检查跳过）")
    from PIL import Image

    img = Image.open(io.BytesIO(body)).convert("RGB")
    px = img.load()
    nonwhite = 0
    for y in range(0, img.size[1], 2):
        for x in range(0, img.size[0], 2):
            r, g, b = px[x, y]
            if r < 245 or g < 245 or b < 245:
                nonwhite += 1
    assert nonwhite > 500, f"HTTP 拿到的图几乎是白的（{nonwhite} 个非白像素）"
    print(f"    HTTP PNG {img.size[0]}x{img.size[1]}：抽样非白像素 {nonwhite}")


def test_api_rejects_bad_input_and_missing_frequencies():
    """错误要明确：没有频率 → 404，参数不合法 → 400，任务不存在 → 404。"""
    server = Server()
    try:
        server.add_job("20200101-000000-specreal", FREQS, INTS)
        # 一个没有频率的任务
        spec = JobSpec(molecule=Molecule.from_xyz(WATER_XYZ), job_type=JobType.ENERGY,
                       method="HF", basis="STO-3G")
        server.manager.store.create(spec, job_id="20200101-000002-nofreq")
        (server.manager.store.job_dir("20200101-000002-nofreq") / "result.json").write_text(
            json.dumps({"status": "done", "job_type": "energy", "method": "HF",
                        "final_energy": -75.0}), encoding="utf-8")

        cases = [
            ("/api/jobs/20200101-000000-specreal/spectrum?kind=triangle", 400),
            ("/api/jobs/20200101-000000-specreal/spectrum?fwhm=abc", 400),
            ("/api/jobs/20200101-000000-specreal/spectrum?fwhm=-5", 400),
            ("/api/jobs/20200101-000000-specreal/spectrum?fwhm=99999", 400),
            ("/api/jobs/20200101-000002-nofreq/spectrum", 404),
            ("/api/jobs/nope/spectrum", 404),
            ("/api/jobs/20200101-000002-nofreq/spectrum.png", 404),
        ]
        for path, want in cases:
            try:
                server.get(path)
            except urllib.error.HTTPError as exc:
                assert exc.code == want, (path, exc.code, want)
                body = json.loads(exc.read().decode("utf-8"))
                assert body.get("error"), (path, body)
            else:
                raise AssertionError(f"{path} 该返回 {want}")
        print(f"    {len(cases)} 组错误请求都返回了明确的中文错误")
    finally:
        server.close()


_ENGINE_FREE_PROBE = r'''
import sys, tempfile, json
from pathlib import Path
sys.path.insert(0, r"{repo}")
from qcgui.jobs import JobManager
from qcgui.web.app import create_app
from qcgui.model import JobSpec, JobType, Molecule, Result

root = Path(tempfile.mkdtemp(prefix="qcgui_probe_"))
mgr = JobManager(root / "jobs")
jid = "20200101-000000-probe"
mgr.store.create(JobSpec(molecule=Molecule.from_xyz("2\nH2\nH 0 0 0\nH 0 0 0.74\n"),
                         job_type=JobType.FREQ, method="HF", basis="STO-3G"), job_id=jid)
job_dir = mgr.store.job_dir(jid)
Result(status="done", job_type="freq", frequencies=[1000.0, 2000.0, -50.0],
       job_dir=str(job_dir)).save(job_dir / "result.json")
app = create_app(mgr)
c = app.test_client()
print("spectrum", c.get("/api/jobs/" + jid + "/spectrum").status_code)
print("png", c.get("/api/jobs/" + jid + "/spectrum.png").status_code)
bad = [m for m in sys.modules if m.startswith("qcgui.engines")]
print("ENGINES", "IMPORTED:" + ",".join(bad) if bad else "CLEAN")
'''


def test_web_process_stays_engine_free():
    """架构铁律：界面进程绝不 import 引擎（在**子进程**里复核，不靠声明）。"""
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", _ENGINE_FREE_PROBE.format(repo=str(REPO))],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, env=env, cwd=str(REPO),
    )
    out = proc.stdout or ""
    assert proc.returncode == 0, f"探针进程失败：{proc.returncode}\n{out}\n{proc.stderr}"
    assert "spectrum 200" in out, out
    assert "ENGINES CLEAN" in out, f"界面进程 import 了引擎：{out}"
    print("    子进程里跑完 /spectrum 与 /spectrum.png，qcgui.engines 未被导入")


# ------------------------------------------------------------------ 4. 真浏览器

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)


def _browser() -> str:
    explicit = os.environ.get("QCGUI_CHROME")
    if explicit and Path(explicit).is_file():
        return explicit
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    _skip("找不到 Chrome/Edge，跳过真浏览器验证")


def _run_spectrum_hook(base: str, job_id: str, timeout: int = 240) -> str:
    """在无头浏览器里打开结果面板并跑 sptest 钩子，取回测试输出。"""
    chrome = _browser()
    profile = Path(tempfile.mkdtemp(prefix="qcgui_spec_chrome_"))
    url = f"{base}/?nosse=1&job={job_id}&sptest={job_id}"
    proc = subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         "--window-size=1600,1200", "--virtual-time-budget=30000",
         f"--user-data-dir={profile}", "--dump-dom", url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    dom = proc.stdout or ""
    match = re.search(r"__SPTEST__(.*?)__END__", dom, re.S)
    assert match, f"没拿到红外谱图测试输出（页面可能没跑起来）\nDOM 片段:\n{dom[:800]}"
    return match.group(1)


def test_spectrum_section_shows_up_in_a_real_browser():
    """真浏览器：谱图区出现在结果面板上、图**不是空白**、虚频行有标记。

    分两份任务：
    * 有强度的 → 图上要标最强峰；
    * 没有强度的 → 界面上必须写明"无强度数据"，且**不许**标最强峰
      （补出来的 1.0 不是真实强度，标出来就是误导）。
    """
    server = Server()
    try:
        server.add_job("20200101-000000-specreal", FREQS, INTS)
        server.add_job("20200101-000001-specnone", FREQS)

        text = _run_spectrum_hook(server.base, "20200101-000000-specreal")
        print("    " + "\n    ".join(p.strip() for p in text.split(" | ")))
        assert "ERROR" not in text, text
        for key in ("visible=PASS", "after_verdict=PASS", "img_loaded=PASS",
                    "nonblank=PASS", "curve_pixels=PASS", "imag_marked=PASS",
                    "strongest_marked=PASS", "fwhm_control=PASS",
                    "peak_positions_kept=PASS"):
            assert key in text, f"缺少 {key}：{text}"
        assert "natural=880x450" in text, text
        assert "rows=9 imag_rows=1" in text, text
        # 有真实强度 → 结论仍是绿色可用（不能把好任务误判成"有限制"）
        assert "verdict_class=ok" in text, text
        ink = float(re.search(r"ink=([\d.]+)%", text).group(1))
        assert ink > 1.0, f"图上的非白像素只有 {ink}% —— 多半是空白图"

        plain = _run_spectrum_hook(server.base, "20200101-000001-specnone")
        print("    " + "\n    ".join(p.strip() for p in plain.split(" | ")))
        assert "ERROR" not in plain, plain
        assert "nonblank=PASS" in plain, plain
        assert "imag_marked=PASS" in plain, plain
        assert "strongest_marked=NONE" in plain, plain
        assert "无强度数据" in plain, f"界面上没有写明是无强度谱：{plain}"
        # 真浏览器里复核结论降级：横幅必须是"有限制"，理由就是无强度数据
        assert "verdict_class=limited" in plain, plain
        assert 'verdict_why="无强度数据' in plain, plain
        assert "n_imaginary" not in plain
        print("    有强度任务：标了最强峰、结论可用；"
              "无强度任务：写明无强度谱、不标最强峰、结论降级为 limited")
    finally:
        server.close()


def _run_spectrum_fail_hook(base: str, job_id: str, timeout: int = 240) -> str:
    """在无头浏览器里跑"谱图加载失败兜底"钩子，取回测试输出。"""
    chrome = _browser()
    profile = Path(tempfile.mkdtemp(prefix="qcgui_specfail_chrome_"))
    url = f"{base}/?nosse=1&job={job_id}&sptestfail={job_id}"
    proc = subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         "--window-size=1600,1200", "--virtual-time-budget=30000",
         f"--user-data-dir={profile}", "--dump-dom", url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    dom = proc.stdout or ""
    match = re.search(r"__SPTF__(.*?)__END__", dom, re.S)
    assert match, f"没拿到兜底测试输出（页面可能没跑起来）\nDOM 片段:\n{dom[:800]}"
    return match.group(1)


def test_spectrum_image_failure_is_explained_in_a_real_browser():
    """硬规矩 3：谱图画不出来的时候，用户看到的必须是**原因**，不是一个破图。

    刻意不依赖本机有没有 Pillow（这台机器现在就没有）：把 ``<img>`` 指到一条
    越界的 FWHM 上，服务端必然 400 并给出中文原因。要验三件事：
    1. 界面上出现了那句原因（而不是空白或破图图标）；
    2. 破图被藏起来了；
    3. **峰值表还在** —— 图画不出来不该把频率数字一起吞掉，那是用户真正要的结果。
    """
    server = Server()
    try:
        server.add_job("20200101-000000-specrealfail", FREQS, INTS)
        text = _run_spectrum_fail_hook(server.base, "20200101-000000-specrealfail")
        print("    " + "\n    ".join(p.strip() for p in text.split(" | ")))
        assert "ERROR" not in text, text
        assert "explained=PASS" in text, text
        assert "img_hidden=PASS" in text, text
        assert "table_kept=PASS" in text, text
        assert "半高全宽" in text, text
    finally:
        server.close()


# ------------------------------------------------------------------ 脚本模式


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
