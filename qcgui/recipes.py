"""计算配方（recipes）：把"方法 + 基组 + 关键词 + 块设置"打包成可复用的预设。

参考 ACH-Orca-Workbench（Christian Nelle, TU Dortmund, MIT）的配方机制：
一个配方 = 名称 + 计算类型 + 方法标签 + 一段带坐标占位符的 ORCA 输入模板。
本项目的配方映射到 :class:`JobSpec` 的字段，因此天然复用现有的引擎与解析链。

分两层：
- **内置配方**：随仓库分发，定义在本文件里，不带坐标（坐标由分子给出）
- **用户配方**：保存在 ``paths.data_root()/recipes/*.json``，可增删、可置顶收藏
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .model import JobSpec, JobType, Molecule
from .paths import data_root, ensure

# ------------------------------------------------------------------ 数据结构


@dataclass(frozen=True)
class Recipe:
    """一个可复用的计算预设。"""

    id: str
    name: str
    job_type: JobType
    method: str
    basis: str = ""
    #: 追加到 ORCA 关键字行的裸关键字（如 RIJCOSX、def2/J、D4、TightOpt）
    keywords: tuple[str, ...] = ()
    #: 引擎专属块设置，形如 (("tddft", "nroots 20"),)
    blocks: tuple[tuple[str, str], ...] = ()
    solvent: str | None = None
    note: str = ""
    #: 结果解析状态：ok=引擎已能解析；partial=能跑但结果尚未解析
    parsing: str = "ok"
    builtin: bool = True
    favorite: bool = False
    nprocs: int | None = None
    maxcore_mb: int | None = None

    @property
    def level_of_theory(self) -> str:
        return f"{self.method}/{self.basis}".rstrip("/")

    def orca_keyword_line(self) -> str:
        """给人看的 ORCA 关键字行（不含坐标）。"""
        parts = [self.method, self.basis, *self.keywords]
        return "! " + " ".join(p for p in parts if p)

    def build_spec(self, molecule: Molecule, **overrides) -> JobSpec:
        """由配方 + 分子生成 JobSpec。``overrides`` 可覆盖任意字段。"""
        params = {
            "molecule": molecule,
            "job_type": self.job_type,
            "method": self.method,
            "basis": self.basis,
            "solvent": self.solvent,
            "extra_keywords": self.keywords,
            "extra_blocks": self.blocks,
            "title": molecule.name,
        }
        if self.nprocs:
            params["nprocs"] = self.nprocs
        if self.maxcore_mb:
            params["maxcore_mb"] = self.maxcore_mb
        params.update({k: v for k, v in overrides.items() if v is not None})
        return JobSpec(**params)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["job_type"] = self.job_type.value
        data["keywords"] = list(self.keywords)
        data["blocks"] = [list(b) for b in self.blocks]
        data["level_of_theory"] = self.level_of_theory
        data["orca_keyword_line"] = self.orca_keyword_line()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Recipe":
        """从 JSON 还原配方。

        ``blocks`` 要做形状校验：以前直接 ``b[0], b[1]``，一个用户配方文件里
        写成 ``[["onlyname"]]`` 就抛 IndexError，而调用方的 except 列表里没有它
        → **所有配方接口 + 任何带 recipe_id 的提交全部 500**（独立审计实测）。
        这里统一转成 ValueError，并说清是哪个文件的问题。
        """
        if not isinstance(data, dict):
            raise ValueError(f"配方文件必须是对象，收到 {type(data).__name__}")
        blocks_raw = data.get("blocks") or ()
        if isinstance(blocks_raw, str) or not isinstance(blocks_raw, (list, tuple)):
            raise ValueError("blocks 必须是 [[块名, 内容], ...] 形式")
        blocks: list[tuple[str, str]] = []
        for index, item in enumerate(blocks_raw):
            if isinstance(item, str) or not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValueError(f"blocks 第 {index + 1} 项必须是 [块名, 内容] 两个元素")
            blocks.append((str(item[0]), str(item[1])))

        keywords_raw = data.get("keywords") or ()
        if isinstance(keywords_raw, str) or not isinstance(keywords_raw, (list, tuple)):
            raise ValueError("keywords 必须是字符串列表")
        keywords = tuple(str(k) for k in keywords_raw)

        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            job_type=JobType(data.get("job_type", "energy")),
            method=str(data.get("method", "HF")),
            basis=str(data.get("basis", "") or ""),
            keywords=keywords,
            blocks=tuple(blocks),
            solvent=data.get("solvent"),
            note=str(data.get("note", "")),
            parsing=str(data.get("parsing", "ok")),
            builtin=bool(data.get("builtin", False)),
            favorite=bool(data.get("favorite", False)),
            nprocs=data.get("nprocs"),
            maxcore_mb=data.get("maxcore_mb"),
        )


# ------------------------------------------------------------------ 内置配方

#: 内置配方库。方法/关键词组合参考 ACH-Orca-Workbench 的内置配方，
#: 并筛掉本项目引擎还解析不了结果的那几类（TD-DFT / NMR / EPR 暂列为 partial）。
_BUILTIN: tuple[Recipe, ...] = (
    Recipe(
        id="opt_fast_r2scan3c",
        name="OPT 快速 (r2SCAN-3c)",
        job_type=JobType.OPT,
        method="r2SCAN-3c",
        keywords=("TightOpt",),
        note="复合方法自带基组，日常几何优化首选，性价比最高",
        favorite=True,
    ),
    Recipe(
        id="opt_b3lyp",
        name="OPT (B3LYP-D4/def2-TZVPP)",
        job_type=JobType.OPT,
        method="B3LYP",
        basis="def2-TZVPP",
        keywords=("RIJCOSX", "def2/J", "D4", "TightOpt"),
    ),
    Recipe(
        id="opt_wb97xd4",
        name="OPT (wB97X-D4/def2-TZVP)",
        job_type=JobType.OPT,
        method="wB97X-D4",
        basis="def2-TZVP",
        keywords=("RIJCOSX", "def2/J", "TightOpt"),
    ),
    Recipe(
        id="optfreq_b3lyp",
        name="OPT + FREQ (B3LYP-D4/def2-TZVPP)",
        job_type=JobType.OPT_FREQ,
        method="B3LYP",
        basis="def2-TZVPP",
        keywords=("RIJCOSX", "def2/J", "D4", "TightOpt", "VeryTightSCF"),
        note="一次算完优化与频率，直接拿热力学量",
        favorite=True,
    ),
    Recipe(
        id="freq_b3lyp",
        name="FREQ (B3LYP-D4/def2-TZVPP)",
        job_type=JobType.FREQ,
        method="B3LYP",
        basis="def2-TZVPP",
        keywords=("RIJCOSX", "def2/J", "D4", "VeryTightSCF"),
        note="建议用在已优化的几何上",
    ),
    Recipe(
        id="sp_wb97mv",
        name="SP 能量 (wB97M-V/def2-TZVP)",
        job_type=JobType.ENERGY,
        method="wB97M-V",
        basis="def2-TZVP",
        keywords=("def2/J", "RIJCOSX", "TightSCF"),
    ),
    Recipe(
        id="sp_dlpno_mp2",
        name="SP 能量 (DLPNO-MP2/def2-TZVPP)",
        job_type=JobType.ENERGY,
        method="DLPNO-MP2",
        basis="def2-TZVPP",
        keywords=("def2-TZVPP/C", "def2/JK", "RIJK", "TightSCF"),
    ),
    Recipe(
        id="sp_dlpno_ccsdt",
        name="SP 能量 (DLPNO-CCSD(T)/def2-TZVPP)",
        job_type=JobType.ENERGY,
        method="DLPNO-CCSD(T)",
        basis="def2-TZVPP",
        keywords=("def2-TZVPP/C", "def2/JK", "RIJK", "TightSCF"),
        note="高精度单点；建议在优化好的几何上做",
    ),
    Recipe(
        id="opt_cpcm_water",
        name="OPT 水相 (B3LYP-D4/def2-TZVPP + SMD)",
        job_type=JobType.OPT,
        method="B3LYP",
        basis="def2-TZVPP",
        keywords=("RIJCOSX", "def2/J", "D4", "TightOpt"),
        solvent="water",
    ),
    Recipe(
        id="sp_cpcm_water",
        name="SP 水相 (B3LYP-D4/def2-TZVPP + SMD)",
        job_type=JobType.ENERGY,
        method="B3LYP",
        basis="def2-TZVPP",
        keywords=("RIJCOSX", "def2/J", "D4", "TightSCF"),
        solvent="water",
    ),
    Recipe(
        id="ts_opt_b3lyp",
        name="TS 优化 + 频率 (B3LYP-D4/def2-TZVPP)",
        job_type=JobType.TS_FREQ,
        method="B3LYP",
        basis="def2-TZVPP",
        keywords=("RIJCOSX", "def2/J", "D4"),
        note="需要过渡态初猜；收敛后应有且仅有 1 个虚频",
    ),
    Recipe(
        id="irc_hf",
        name="IRC (HF/def2-SVP)",
        job_type=JobType.IRC,
        method="HF",
        basis="def2-SVP",
        note="从过渡态出发跟踪反应路径；两端能垒会自动算好",
    ),
    Recipe(
        id="irc_r2scan3c",
        name="IRC (r2SCAN-3c)",
        job_type=JobType.IRC,
        method="r2SCAN-3c",
    ),
    Recipe(
        id="tddft_pbe0",
        name="UV-Vis TD-DFT (PBE0/def2-TZVP)",
        job_type=JobType.ENERGY,
        method="PBE0",
        basis="def2-TZVP",
        keywords=("RIJCOSX", "def2/J", "D4"),
        blocks=(("tddft", "nroots 20\nmaxdim 5"),),
        parsing="partial",
        note="能跑通；激发能结果解析尚未实现",
    ),
    Recipe(
        id="tddft_camb3lyp",
        name="UV-Vis TD-DFT (CAM-B3LYP/def2-TZVP)",
        job_type=JobType.ENERGY,
        method="CAM-B3LYP",
        basis="def2-TZVP",
        keywords=("RIJCOSX", "def2/J"),
        blocks=(("tddft", "nroots 20\nmaxdim 5"),),
        parsing="partial",
        note="能跑通；激发能结果解析尚未实现",
    ),
    Recipe(
        id="nmr_1h13c_tpss",
        name="NMR 1H/13C (TPSS/pcSseg-2)",
        job_type=JobType.ENERGY,
        method="TPSS",
        basis="pcSseg-2",
        keywords=("AUTOAUX", "NMR", "VeryTightSCF"),
        blocks=(("eprnmr", "Nuclei = all H {shift}\nNuclei = all C {shift}"),),
        parsing="partial",
        note="能跑通；化学位移结果解析尚未实现",
    ),
    Recipe(
        id="debug_sp_hf_sto3g",
        name="DEBUG SP (HF/STO-3G)",
        job_type=JobType.ENERGY,
        method="HF",
        basis="STO-3G",
        note="几秒钟出结果，用来验证链路是否通",
        favorite=True,
    ),
    Recipe(
        id="debug_opt_hf_sto3g",
        name="DEBUG OPT (HF/STO-3G)",
        job_type=JobType.OPT,
        method="HF",
        basis="STO-3G",
        note="几秒钟出结果，用来验证链路是否通",
    ),
    Recipe(
        id="debug_freq_hf_sto3g",
        name="DEBUG FREQ (HF/STO-3G)",
        job_type=JobType.FREQ,
        method="HF",
        basis="STO-3G",
        note="几秒钟出结果，用来验证链路是否通",
    ),
)


# ------------------------------------------------------------------ 用户配方


def user_recipe_dir() -> Path:
    return data_root() / "recipes"


def _favorites_path() -> Path:
    return data_root() / "recipe_favorites.json"


def load_favorites() -> dict[str, bool]:
    """收藏覆盖表 ``{配方 id: 是否收藏}``。

    用"覆盖"而不是"列表"，是因为内置配方本身带默认收藏 ——
    列表格式只能加不能减，预置的收藏就取消不掉了。
    兼容最早的列表格式（按全部收藏处理）。
    """
    path = _favorites_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict):
        return {str(k): bool(v) for k, v in data.items()}
    if isinstance(data, list):
        return {str(x): True for x in data}
    return {}


def set_favorite(recipe_id: str, favorite: bool) -> dict[str, bool]:
    overrides = load_favorites()
    overrides[str(recipe_id)] = bool(favorite)
    path = ensure(data_root()) / "recipe_favorites.json"
    path.write_text(
        json.dumps(overrides, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return overrides


def _safe_name(text: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", text).strip("_")
    return cleaned[:60] or "recipe"


def load_user_recipes() -> list[Recipe]:
    """读取用户配方目录里的 *.json（坏文件跳过，不影响其余）。"""
    recipes: list[Recipe] = []
    directory = user_recipe_dir()
    if not directory.is_dir():
        return recipes
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue                      # 数组/字符串之类的直接跳过，别让 setdefault 炸
            data.setdefault("id", path.stem)
            data["builtin"] = False
            recipes.append(Recipe.from_dict(data))
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
            # 一个坏的配方文件不该让**所有**配方接口挂掉。独立审计实测过：
            # blocks 写成 [["onlyname"]] 时 IndexError 逃出这个 except，
            # 导致 /api/recipes 和任何带 recipe_id 的提交全 500。
            continue
    return recipes


def save_user_recipe(recipe: Recipe) -> Path:
    directory = ensure(user_recipe_dir())
    payload = recipe.to_dict()
    payload["builtin"] = False
    payload.pop("level_of_theory", None)
    payload.pop("orca_keyword_line", None)
    path = directory / f"{_safe_name(recipe.id)}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def delete_user_recipe(recipe_id: str) -> bool:
    path = user_recipe_dir() / f"{_safe_name(recipe_id)}.json"
    if path.is_file():
        path.unlink()
        return True
    return False


# ------------------------------------------------------------------ 查询接口


def all_recipes() -> list[Recipe]:
    """内置 + 用户配方；收藏的排前面，其余按名称。

    收藏状态存在 ``recipe_favorites.json``，因此内置配方的收藏也能持久化。
    """
    favorites = load_favorites()
    recipes = [
        replace(r, favorite=favorites.get(r.id, r.favorite))
        for r in (list(_BUILTIN) + load_user_recipes())
    ]
    return sorted(recipes, key=lambda r: (not r.favorite, r.name.lower()))


def get_recipe(recipe_id: str) -> Recipe | None:
    for recipe in all_recipes():
        if recipe.id == recipe_id:
            return recipe
    return None


def find_by_name(fragment: str) -> list[Recipe]:
    fragment = fragment.strip().lower()
    if not fragment:
        return all_recipes()
    return [
        r for r in all_recipes()
        if fragment in r.name.lower()
        or fragment in r.method.lower()
        or fragment in r.job_type.value
    ]


def default_recipe() -> Recipe:
    return _BUILTIN[0]
