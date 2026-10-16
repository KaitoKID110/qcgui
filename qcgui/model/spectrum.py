"""红外谱线：把分立的谐振频率卷积成一条连续谱（纯计算，不导入引擎）。

为什么要单独一层：ORCA 给出来的是一**堆分立的谐振频率**（数学上是狄拉克 δ 线），
直接画出来是几根竖线，没法看。化学家看图时看的是"带宽度"的连续谱 ——
GaussView 的做法就是把每个频率按高斯/洛伦兹线型展宽后叠加。展宽是纯数学
（跟跑的是 ORCA 还是高斯无关），所以放在模型层：网页端画图、以后导出图片 /
CLI 都复用它，不各写一套。

约定与 :class:`~qcgui.model.result.Result` 保持一致：
* 波数 cm⁻¹，**虚频是负数**（过渡态应当正好 1 个虚频）；
* 强度缺失时按 ``1.0`` 处理，**由调用方（界面）明确标注"无强度谱"** ——
  把缺失的强度当成真实强度画出来是误导，本项目最忌讳"看起来对但其实是假的"。

"强度缺失"这条特意做成**显式返回值**（``has_intensity``），而不是悄悄补 1.0：
补 1.0 是必须的（否则画不出谱），但必须让上层知道这是补的。
"""

from __future__ import annotations

import math

#: 展宽线型。键名同时用作接口参数与前端下拉框的值，别随手改。
KIND_GAUSSIAN = "gaussian"
KIND_LORENTZIAN = "lorentzian"
KINDS = (KIND_GAUSSIAN, KIND_LORENTZIAN)

KIND_LABELS = {
    KIND_GAUSSIAN: "高斯 (Gaussian)",
    KIND_LORENTZIAN: "洛伦兹 (Lorentzian)",
}

#: 默认半高全宽 (cm⁻¹)。20 是红外谱图里最常见的展宽档（GaussView 默认量级），
#: 既能看出峰的分裂，又不至于把邻近的峰糊成一坨。
DEFAULT_FWHM = 20.0
#: 默认采样步长 (cm⁻¹)。2 足够平滑（20 cm⁻¹ 宽的峰有 10 个采样点），
#: 一整段 4000 cm⁻¹ 的谱也就 2000 个点，画图开销可以忽略。
DEFAULT_STEP = 2.0
DEFAULT_KIND = KIND_GAUSSIAN

#: 采样点数上限。界面上步长是可填的，一个手滑填成 0.001 会直接吃光内存 ——
#: 宁可报错也不要让界面卡死。
MAX_POINTS = 400_000

#: 平动/转动模态的阈值：ORCA 把它们打成 0.00 cm⁻¹，并在输出里明说
#: "The first frequency considered to be a vibration is ..."。它们不是振动，
#: 画成峰会多出一根假的低波数峰，所以默认剔掉。
ZERO_MODE_TOL = 1e-6

#: 图/标题上写波数单位用 ASCII 的 "cm-1"：微软雅黑里没有上标减号 U+207B，
#: 直接写 "cm⁻¹" 在 Pillow 里会画成一个方框（实测过）。网页上用 HTML 的 ⁻¹ 不受此限。
CM_UNIT = "cm-1"

#: "无强度谱"的说明文字。**图上和页面上必须是同一句** ——
#: 两处各写一份，早晚会分叉，而这句话正是"别把峰高读成强度"的唯一提醒。
NO_INTENSITY_NOTE = (
    "无强度数据：每个峰按等强度 1.0 画（只表示峰的位置，不代表真实吸收强度）"
)


def normalize_peaks(peaks) -> list[tuple[float, float]]:
    """把各种写法的峰列表规范成 ``[(波数, 强度), ...]``。

    接受 ``(波数, 强度)`` 对、也接受裸的数字（当作强度缺失 → 1.0），
    因为"只有频率没有强度"正是本项目的现状（引擎层还没解析 IR 强度）。
    强度取 ``None`` / NaN / inf 都当作缺失 → 1.0；负强度没有物理意义，
    按 0 处理（不画反向峰），免得图上一个朝下的峰把人看懵。
    """
    out: list[tuple[float, float]] = []
    for item in peaks or ():
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            pos, raw = float(item), None
        else:
            try:
                pos = float(item[0])
                raw = item[1] if len(item) > 1 else None
            except (TypeError, ValueError, IndexError, KeyError) as exc:
                raise ValueError(f"峰的第 {len(out) + 1} 项看不懂：{item!r}") from exc
        if not math.isfinite(pos):
            raise ValueError(f"第 {len(out) + 1} 个峰的波数不是有限数：{pos!r}")

        intensity = 1.0
        if raw is not None:
            try:
                intensity = float(raw)
            except (TypeError, ValueError):
                intensity = 1.0
            if not math.isfinite(intensity):
                intensity = 1.0      # NaN/inf 一律当"没给强度"
            elif intensity < 0.0:
                intensity = 0.0
        out.append((pos, intensity))
    return out


def peaks_from_frequencies(
    frequencies,
    intensities=None,
    *,
    drop_zero_modes: bool = True,
    zero_tol: float = ZERO_MODE_TOL,
) -> tuple[list[tuple[float, float]], bool]:
    """从 ``Result.frequencies`` （+ 可选强度）整理出峰列表。

    返回 ``(peaks, has_intensity)``：

    * ``has_intensity=False`` 表示这一路**没有**强度数据，每个峰按 1.0 处理。
      界面必须据此显示"无强度谱"，不能装成真实强度。
    * 强度个数和频率个数对不上时也当作没有强度（数据不齐时宁可不画强度，
      也不要错位 —— 错位会让整张谱的峰高全是错的，而且看不出来）。

    ``drop_zero_modes`` 默认开：剔掉平动/转动的 0 cm⁻¹ 模态（见 :data:`ZERO_MODE_TOL`）。
    虚频是负的大数，不会被误剔。
    """
    freqs = [float(f) for f in (frequencies or [])]
    ints: list[float] | None = None
    if intensities is not None and len(intensities) == len(freqs) and freqs:
        ints = [float(i) for i in intensities]

    peaks: list[tuple[float, float]] = []
    for k, freq in enumerate(freqs):
        if drop_zero_modes and abs(freq) <= zero_tol:
            continue
        peaks.append((freq, 1.0 if ints is None else ints[k]))
    return peaks, ints is not None


def default_range(
    peaks,
    *,
    pad_ratio: float = 0.03,
    min_pad: float = 50.0,
    snap: float = 50.0,
    min_span: float = 200.0,
) -> tuple[float, float]:
    """给一组峰挑一个好看的波数窗口 ``(x_min, x_max)``。

    要点：
    * **必须包含虚频**（负波数）—— 过渡态的虚频常常在 -100 ~ -1500，
      窗口从 0 起会把最关键的那根峰切掉，用户就看不到"任务到底对不对"。
    * 按 ``snap``（默认 50 cm⁻¹）对齐到整数，刻度标签才是整十整百。
    * 没有峰时给一个默认的中红外窗口，而不是抛异常 —— 界面拿它占位。
    """
    normalized = normalize_peaks(peaks)
    if not normalized:
        return 0.0, 4000.0

    lo = min(p for p, _ in normalized)
    hi = max(p for p, _ in normalized)
    pad = max(min_pad, (hi - lo) * pad_ratio)
    x_min = math.floor((lo - pad) / snap) * snap
    x_max = math.ceil((hi + pad) / snap) * snap
    if x_max - x_min < min_span:                 # 单峰时窗口别缩成一条缝
        center = (x_min + x_max) / 2
        x_min = math.floor((center - min_span / 2) / snap) * snap
        x_max = x_min + min_span
    return float(x_min), float(x_max)


def strongest_index(peaks) -> int | None:
    """强度最大的那个峰的下标；没有峰返回 ``None``。

    只在**真有强度**时用来标"最强峰"（调用方判断）—— 强度全是补的 1.0 时
    "最强"没有意义，标出来是假信息。
    """
    normalized = normalize_peaks(peaks)
    if not normalized:
        return None
    best = 0
    for k in range(1, len(normalized)):
        if normalized[k][1] > normalized[best][1]:
            best = k
    return best


def _check_args(x_min: float, x_max: float, step: float, fwhm: float) -> None:
    if not all(math.isfinite(v) for v in (x_min, x_max, step, fwhm)):
        raise ValueError("波数范围/步长/半高全宽必须都是有限数")
    if x_max <= x_min:
        raise ValueError(f"波数范围上界必须大于下界：{x_min} → {x_max}")
    if step <= 0:
        raise ValueError(f"步长必须为正：{step}")
    if fwhm <= 0:
        raise ValueError(f"半高全宽必须为正：{fwhm}")
    if (x_max - x_min) / step > MAX_POINTS:
        raise ValueError(
            f"步长 {step} 太细：{x_max - x_min:g} cm⁻¹ 要采 "
            f"{int((x_max - x_min) / step) + 1} 个点（上限 {MAX_POINTS}）"
        )


def _grid(x_min: float, x_max: float, step: float) -> list[float]:
    """等距采样点，**含**两端。

    下标用整数乘出来（而不是累加浮点），否则 2000 个点累下来会漂移到
    ``x_max`` 之外，最后一根刻度对不上。终点用 ``ceil`` 兜一下，
    保证 ``x_max`` 一定在采样范围内。
    """
    n = int(math.ceil((x_max - x_min) / step - 1e-9))
    return [x_min + k * step for k in range(n + 1)]


def broaden(
    peaks,
    x_min: float,
    x_max: float,
    step: float,
    fwhm: float,
    kind: str = DEFAULT_KIND,
) -> tuple[list[float], list[float]]:
    """按 ``kind`` 线型展宽，返回 ``(xs, ys)``。

    每条线型在峰中心处的高度**正好等于该峰的强度**（做归一化），
    所以"最强峰"在图上的高度就是它的强度，不用再乘系数。

    空峰列表 → ``([], [])``（没有峰就没有曲线，调用方据此显示"无峰"），
    其它参数不合法 → ``ValueError``。
    """
    if kind not in KINDS:
        raise ValueError(f"不认识的线型 {kind!r}，可选 {', '.join(KINDS)}")

    normalized = normalize_peaks(peaks)
    if not normalized:
        return [], []

    _check_args(x_min, x_max, step, fwhm)
    xs = _grid(x_min, x_max, step)
    ys = [0.0] * len(xs)

    if kind == KIND_GAUSSIAN:
        # 半高全宽 → 标准差：FWHM = 2*sqrt(2 ln2)*sigma
        sigma = fwhm / (2.0 * math.sqrt(2.0 * math.log(2.0)))
        coef = 1.0 / (2.0 * sigma * sigma)
        for pos, intensity in normalized:
            if intensity == 0.0:
                continue
            for k, x in enumerate(xs):
                d = x - pos
                ys[k] += intensity * math.exp(-d * d * coef)
    else:
        # 洛伦兹：FWHM = 2*gamma，g^2/(d^2+g^2) 在 d=0 处正好是 1
        gamma = fwhm / 2.0
        g2 = gamma * gamma
        for pos, intensity in normalized:
            if intensity == 0.0:
                continue
            for k, x in enumerate(xs):
                d = x - pos
                ys[k] += intensity * g2 / (d * d + g2)
    return xs, ys


def gaussian_broadened(
    peaks,
    x_min: float,
    x_max: float,
    step: float,
    fwhm: float,
) -> tuple[list[float], list[float]]:
    """高斯展宽。峰形集中、旁瓣小，是红外谱图的默认线型。"""
    return broaden(peaks, x_min, x_max, step, fwhm, KIND_GAUSSIAN)


def lorentzian_broadened(
    peaks,
    x_min: float,
    x_max: float,
    step: float,
    fwhm: float,
) -> tuple[list[float], list[float]]:
    """洛伦兹展宽。长尾更重（更接近真实谱线的自然线宽）。"""
    return broaden(peaks, x_min, x_max, step, fwhm, KIND_LORENTZIAN)
