"""配方（recipes）测试。

    pytest tests/test_recipes.py -v
    python tests/test_recipes.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from qcgui.model import JobType, Molecule  # noqa: E402
from qcgui.recipes import (  # noqa: E402
    Recipe,
    all_recipes,
    default_recipe,
    delete_user_recipe,
    find_by_name,
    get_recipe,
    load_favorites,
    save_user_recipe,
    set_favorite,
)

WATER_XYZ = """3
water
O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
"""


class _Skipped(Exception):
    pass


def _skip(reason: str):
    try:
        import pytest

        pytest.skip(reason)
    except ImportError:
        raise _Skipped(reason)


def _isolated_home():
    """把配方/收藏写到临时目录，别动真实配置。"""
    home = Path(tempfile.mkdtemp(prefix="qcgui_recipes_"))
    os.environ["QCGUI_HOME"] = str(home)
    return home


# ------------------------------------------------------------------ 库

def test_builtin_library_is_sane():
    recipes = all_recipes()
    assert len(recipes) >= 15, len(recipes)

    ids = [r.id for r in recipes]
    assert len(ids) == len(set(ids)), "配方 id 有重复"

    for recipe in recipes:
        assert recipe.name.strip()
        assert isinstance(recipe.job_type, JobType)
        assert recipe.method.strip(), recipe.id
        # 关键字不能带坐标或换行 —— 它们会被拼进 ORCA 的关键字行
        for kw in recipe.keywords:
            assert "\n" not in kw and "!" not in kw, (recipe.id, kw)
        # 块名不能带 %
        for block_name, _ in recipe.blocks:
            assert not block_name.startswith("%"), recipe.id

    print(f"    {len(recipes)} 个配方，id 唯一，关键字行干净")


def test_recipes_cover_the_expected_calc_types():
    types = {r.job_type for r in all_recipes()}
    for expected in (JobType.ENERGY, JobType.OPT, JobType.FREQ, JobType.OPT_FREQ,
                     JobType.TS_FREQ, JobType.IRC):
        assert expected in types, f"配方库缺少 {expected.value}"
    print("    覆盖 SP / OPT / FREQ / OPT+FREQ / TS+FREQ / IRC")


def test_recipes_declare_parsing_status():
    """能跑但结果解析不了的配方必须显式标出来，不能让人以为有结果。"""
    partial = [r for r in all_recipes() if r.parsing != "ok"]
    for recipe in partial:
        assert recipe.parsing == "partial", recipe.id
        assert recipe.note, f"{recipe.id} 标了 partial 却没写说明"
    print(f"    {len(partial)} 个配方标注为『结果解析未实现』并有说明")


def test_favorites_sort_first_and_persist():
    _isolated_home()
    # 内置配方本身带若干默认收藏，所以这里验证的是"收藏组整体排在非收藏组之前"
    # 以及"新收藏 / 取消收藏都能生效（含取消内置的预置收藏）"
    set_favorite("opt_b3lyp", True)
    after = all_recipes()
    assert get_recipe("opt_b3lyp").favorite is True
    assert load_favorites().get("opt_b3lyp") is True

    indices_fav = [i for i, r in enumerate(after) if r.favorite]
    indices_other = [i for i, r in enumerate(after) if not r.favorite]
    assert indices_fav and indices_other
    assert max(indices_fav) < min(indices_other), "收藏的必须整体排在前面"

    # 关键：预置的收藏也要能取消掉
    set_favorite("opt_fast_r2scan3c", False)
    assert get_recipe("opt_fast_r2scan3c").favorite is False, "预置收藏取消不掉"

    set_favorite("opt_b3lyp", False)
    assert get_recipe("opt_b3lyp").favorite is False
    print("    收藏置顶；新收藏与取消（含预置项）都能生效")


def test_user_recipe_roundtrip():
    _isolated_home()
    recipe = Recipe(
        id="my_custom", name="我的配方", job_type=JobType.OPT,
        method="PBE0", basis="def2-SVP", keywords=("D4", "TightOpt"),
        blocks=(("geom", "Calc_Hess true"),), solvent="thf",
        builtin=False, note="用户自定义",
    )
    path = save_user_recipe(recipe)
    assert path.is_file()

    loaded = get_recipe("my_custom")
    assert loaded is not None
    assert loaded.name == "我的配方"
    assert loaded.method == "PBE0"
    assert loaded.keywords == ("D4", "TightOpt")
    assert loaded.blocks == (("geom", "Calc_Hess true"),)
    assert loaded.solvent == "thf"
    assert loaded.builtin is False

    # 序列化往返
    again = Recipe.from_dict(json.loads(json.dumps(recipe.to_dict())))
    assert again.keywords == recipe.keywords and again.blocks == recipe.blocks

    assert delete_user_recipe("my_custom") is True
    assert get_recipe("my_custom") is None
    assert delete_user_recipe("my_custom") is False
    print("    用户配方可保存 / 读回 / 删除，序列化往返一致")


def test_builtin_recipes_cannot_be_deleted():
    _isolated_home()
    assert get_recipe("opt_b3lyp").builtin is True
    assert delete_user_recipe("opt_b3lyp") is False
    print("    内置配方不能被删除")


def test_search_matches_name_method_and_type():
    assert any(r.id == "opt_b3lyp" for r in find_by_name("B3LYP"))
    assert any(r.job_type is JobType.IRC for r in find_by_name("irc"))
    assert find_by_name("不存在的关键词") == []
    assert len(find_by_name("")) == len(all_recipes())
    print("    搜索可按名称 / 方法 / 任务类型匹配")


# ------------------------------------------------------------------ 生成 JobSpec

def test_build_spec_carries_keywords_and_blocks():
    _isolated_home()
    molecule = Molecule.from_xyz(WATER_XYZ)
    recipe = get_recipe("optfreq_b3lyp")
    spec = recipe.build_spec(molecule)

    assert spec.job_type is recipe.job_type
    assert spec.method == recipe.method
    assert spec.basis == recipe.basis
    assert spec.extra_keywords == recipe.keywords
    assert spec.molecule is molecule
    assert spec.title == molecule.name

    # 覆盖项优先于配方默认值
    overridden = recipe.build_spec(molecule, method="PBE0", solvent="water")
    assert overridden.method == "PBE0"
    assert overridden.solvent == "water"
    assert overridden.extra_keywords == recipe.keywords, "覆盖方法不应丢掉关键词"
    print(f"    build_spec 带出关键词 {recipe.keywords}，覆盖项生效")


def test_tddft_recipe_carries_its_block():
    _isolated_home()
    molecule = Molecule.from_xyz(WATER_XYZ)
    recipe = get_recipe("tddft_pbe0")
    spec = recipe.build_spec(molecule)
    assert spec.extra_blocks, "TD-DFT 配方必须带 %tddft 块"
    name, body = spec.extra_blocks[0]
    assert name == "tddft" and "nroots" in body
    print(f"    TD-DFT 配方带块设置: %{name} -> {body.splitlines()[0]}")


def test_solvated_recipe_sets_solvent():
    _isolated_home()
    recipe = get_recipe("opt_cpcm_water")
    assert recipe.solvent == "water"
    spec = recipe.build_spec(Molecule.from_xyz(WATER_XYZ))
    assert spec.solvent == "water"
    print("    溶剂化配方正确带出 solvent")


def test_default_recipe_is_usable():
    _isolated_home()
    recipe = default_recipe()
    assert recipe.builtin
    spec = recipe.build_spec(Molecule.from_xyz(WATER_XYZ))
    assert spec.method and spec.job_type
    print(f"    默认配方: {recipe.name}")


def test_no_recipe_duplicates_the_task_keyword():
    """配方不得重复引擎按 ``job_type`` 自动加的任务关键字。

    ORCA 对重复关键字是**直接报错**而不是忽略：

        INPUT ERROR
        UNRECOGNIZED OR DUPLICATED KEYWORD(S) IN SIMPLE INPUT LINE
                        FREQ

    内置的 OPT+FREQ / FREQ / TS 三个配方都犯过这个错（抄关键字行时把任务词
    也抄进去了），用户一点"运行"整个任务就起不来。
    """
    from qcgui.engines.orca import _JOB_KEYWORDS

    offenders = []
    for recipe in all_recipes():
        task_words = {k.upper() for k in _JOB_KEYWORDS[recipe.job_type]}
        clash = task_words & {k.upper() for k in recipe.keywords}
        if clash:
            offenders.append((recipe.id, sorted(clash)))
    assert not offenders, f"配方重复了任务关键字: {offenders}"
    print(f"    {len(all_recipes())} 个配方都没有重复 job_type 关键字")


def test_generated_keyword_line_has_no_duplicates():
    """端到端：用配方生成输入时，重复关键字必须被去掉。"""
    from qcgui.engines import get_engine
    from qcgui.engines.orca import _dedupe_keywords

    _isolated_home()
    molecule = Molecule.from_xyz(WATER_XYZ)
    spec = get_recipe("optfreq_b3lyp").build_spec(molecule)

    text = get_engine("orca").build_input_text(spec)
    keyword_line = next(l for l in text.splitlines() if l.startswith("! "))
    words = [w.upper() for w in keyword_line[2:].split()]
    assert words.count("FREQ") == 1, keyword_line
    assert words.count("OPT") == 1, keyword_line
    assert "FREQ" in words and "OPT" in words, keyword_line

    # 就算硬塞重复的也要被去掉
    assert _dedupe_keywords(["Opt", "Freq", "FREQ", "opt", "D4"]) == ["Opt", "Freq", "D4"]
    print(f"    关键字行无重复: {keyword_line}")


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
