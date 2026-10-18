"""结构生成辅助（SMILES → 三维结构）。

依赖 rdkit，惰性导入：缺少 rdkit 时只有这一个功能不可用，不影响其他部分。
注意 rdkit 是化学信息学工具，不是量化引擎，在界面进程里使用不违反隔离原则。
"""

from __future__ import annotations

import math

from .model import MIN_ATOM_GAP, Atom, Molecule

#: 嵌入失败或结果病态时，最多换几个随机种子重试
_EMBED_ATTEMPTS = 8


def _atoms_from_conformer(mol) -> tuple[Atom, ...]:
    conf = mol.GetConformer()
    return tuple(
        Atom(
            atom.GetSymbol(),
            float(conf.GetAtomPosition(idx).x),
            float(conf.GetAtomPosition(idx).y),
            float(conf.GetAtomPosition(idx).z),
        )
        for idx, atom in enumerate(mol.GetAtoms())
    )


def _min_pair_gap(atoms: tuple[Atom, ...]) -> tuple[float, int, int]:
    """最近的两个原子及其距离（用来发现"原子重合"这种病态几何）。"""
    best: tuple[float, int, int] = (float("inf"), -1, -1)
    for i in range(len(atoms)):
        for j in range(i + 1, len(atoms)):
            gap = math.dist(
                (atoms[i].x, atoms[i].y, atoms[i].z),
                (atoms[j].x, atoms[j].y, atoms[j].z),
            )
            if gap < best[0]:
                best = (gap, i, j)
    return best


def molecule_from_smiles(
    smiles: str,
    name: str | None = None,
    seed: int = 42,
    optimize: bool = True,
) -> Molecule:
    """由 SMILES 生成带三维坐标的分子（ETKDG 构象 + MMFF 预优化）。

    实测踩过的坑：RDKit 的 ETKDG 在**高对称小分子**（例如乙烷）上偶尔会把
    两个氢嵌到几乎同一位置（相距 0.002 Å）。随后 MMFF 预优化也救不回来
    —— 重合原子的力是未定义的，它直接"收敛"并把这堆垃圾留下 —— 而 ORCA
    只会报一句 ``Zero distance encountered between atoms``，很难排查。

    所以这里**换随机种子重试**，并显式检查最近原子间距。
    """
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("未安装 rdkit，无法从 SMILES 生成结构") from exc

    smiles = smiles.strip()
    if not smiles:
        raise ValueError("SMILES 不能为空")

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"无法解析 SMILES: {smiles!r}")

    mol = Chem.AddHs(mol)

    best: tuple[Atom, ...] | None = None
    best_gap = -1.0
    for attempt in range(_EMBED_ATTEMPTS):
        candidate = Chem.Mol(mol)
        params = AllChem.ETKDGv3()
        params.randomSeed = int(seed) + attempt
        if AllChem.EmbedMolecule(candidate, params) != 0:
            continue
        if optimize:
            try:
                AllChem.MMFFOptimizeMolecule(candidate, maxIters=1000)
            except Exception:  # MMFF 参数缺失时保留未优化构象
                pass
        atoms = _atoms_from_conformer(candidate)
        gap = _min_pair_gap(atoms)[0]
        if gap > best_gap:
            best, best_gap = atoms, gap
        if gap >= MIN_ATOM_GAP:
            break

    if best is None:
        raise ValueError(f"三维构象生成失败: {smiles!r}")
    if best_gap < MIN_ATOM_GAP:
        raise ValueError(
            f"三维构象生成结果里有原子几乎重合（最近 {best_gap:.3f} Å，"
            f"已尝试 {_EMBED_ATTEMPTS} 个随机种子）: {smiles!r}"
        )

    radicals = sum(a.GetNumRadicalElectrons() for a in mol.GetAtoms())
    return Molecule(
        atoms=best,
        charge=int(Chem.GetFormalCharge(mol)),
        multiplicity=int(radicals) + 1,
        name=(name or smiles)[:60],
    )


def molecule_from_mol(
    text: str,
    name: str | None = None,
    seed: int = 42,
    add_hydrogens: bool = True,
    optimize: bool = True,
) -> Molecule:
    """由 MOL / SDF 文本生成带三维坐标的分子。

    用途：导入的 .mol 常常是**二维**的（绘图软件导出），或者没有显式氢。
    这两种情况都要先补氢并生成三维坐标，否则送去优化会得到不合理结果。

    注意：生成的是"合理构象猜测"，不是 DFT 级几何。调用方应当把这一点
    告诉用户（建议先做一次结构优化）。

    解析本身由 :mod:`qcgui.model.molfile` 完成（纯标准库）；这里只负责
    借 RDKit 做补氢与三维嵌入。
    """
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("未安装 rdkit，无法把二维 MOL 转成三维结构") from exc

    from .model.molfile import MolParseError, parse_mol

    try:
        block = parse_mol(text, name=name)
    except MolParseError as exc:
        raise ValueError(str(exc)) from None

    # 用 MOL 块交给 RDKit：保留键级/电荷，比从 SMILES 重建更忠实于原文件
    rd_mol = Chem.MolFromMolBlock(
        _first_mol_block(text), sanitize=True, removeHs=False, strictParsing=False
    )
    if rd_mol is None:
        # RDKit 解析失败（键级不合理等）时退回"只有原子坐标"的朴素构造；
        # 这样至少还能算单点，而不是整个失败
        molecule = Molecule(
            atoms=tuple(
                Atom(sym, float(c[0]), float(c[1]), float(c[2]))
                for sym, c in zip(block.symbols, block.coords)
            ),
            charge=int(block.charge),
            multiplicity=1,
            name=block.name,
        )
        if add_hydrogens:
            raise ValueError(
                "RDKit 无法解析这个 MOL 的键信息，因此不能自动补氢"
                "（缺氢的结构直接算会出错）。请检查文件，或用包含显式氢的格式。"
            )
        return molecule

    if add_hydrogens:
        rd_mol = Chem.AddHs(rd_mol)

    atoms: tuple[Atom, ...] | None = None
    best_gap = -1.0
    for attempt in range(_EMBED_ATTEMPTS):
        candidate = Chem.Mol(rd_mol)
        params = AllChem.ETKDGv3()
        params.randomSeed = int(seed) + attempt
        if AllChem.EmbedMolecule(candidate, params) != 0:
            continue
        if optimize:
            try:
                AllChem.MMFFOptimizeMolecule(candidate, maxIters=1000)
            except Exception:  # MMFF 参数缺失时保留未优化构象
                pass
        coords = _atoms_from_conformer(candidate)
        gap = _min_pair_gap(coords)[0]
        if gap > best_gap:
            atoms, best_gap = coords, gap
        if gap >= MIN_ATOM_GAP:
            break

    if atoms is None:
        raise ValueError(f"三维构象生成失败: {block.name!r}")
    if best_gap < MIN_ATOM_GAP:
        raise ValueError(
            f"三维构象生成结果里有原子几乎重合（最近 {best_gap:.3f} Å，"
            f"已尝试 {_EMBED_ATTEMPTS} 个随机种子）: {block.name!r}"
        )

    radicals = sum(a.GetNumRadicalElectrons() for a in rd_mol.GetAtoms())
    return Molecule(
        atoms=atoms,
        charge=int(Chem.GetFormalCharge(rd_mol)),
        multiplicity=int(radicals) + 1,
        name=(name or block.name)[:60],
    )


def _first_mol_block(text: str) -> str:
    """取出 SDF 的第一条记录（RDKit 的 MolFromMolBlock 只认单条）。"""
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    return body.split("$$$$")[0]

