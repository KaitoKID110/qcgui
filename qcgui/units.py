"""单位换算常量（CODATA 2018 / IUPAC）。

全项目只在这里定义，避免各模块各写一份导致对不上。
"""

from __future__ import annotations

#: Bohr → Å
BOHR_TO_ANGSTROM = 0.529177210903
#: Å → Bohr
ANGSTROM_TO_BOHR = 1.0 / BOHR_TO_ANGSTROM

#: Hartree → kJ/mol
HARTREE_TO_KJMOL = 2625.4996394799
#: Hartree → kcal/mol
HARTREE_TO_KCAL = 627.5094740631
#: Hartree → eV
HARTREE_TO_EV = 27.211386245988
#: Hartree → J
HARTREE_TO_J = 4.3597447222071e-18
#: 阿伏伽德罗常数
AVOGADRO = 6.02214076e23
#: Hartree/K → J/(mol·K)
HARTREE_PER_K_TO_JMOLK = HARTREE_TO_J * AVOGADRO

__all__ = [
    "BOHR_TO_ANGSTROM",
    "ANGSTROM_TO_BOHR",
    "HARTREE_TO_KJMOL",
    "HARTREE_TO_KCAL",
    "HARTREE_TO_EV",
    "HARTREE_TO_J",
    "AVOGADRO",
    "HARTREE_PER_K_TO_JMOLK",
]
