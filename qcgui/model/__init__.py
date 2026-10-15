"""引擎无关的数据模型。"""

from .geometry import (
    ScanSpec,
    connectivity_change,
    dihedral,
    measure,
    parse_scan_blocks,
    scan_warnings,
)
from .jobspec import JobSpec, JobType
from .molecule import MAX_COORD_ANGSTROM, MIN_ATOM_GAP, Atom, Molecule
from .result import (
    SEVERITY_BLOCKING,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    VERDICT_LIMITED,
    VERDICT_OK,
    VERDICT_UNUSABLE,
    Issue,
    Result,
)

__all__ = [
    "Atom", "Molecule", "JobSpec", "JobType", "Result", "Issue",
    "MIN_ATOM_GAP", "MAX_COORD_ANGSTROM",
    "SEVERITY_BLOCKING", "SEVERITY_MAJOR", "SEVERITY_MINOR",
    "VERDICT_OK", "VERDICT_LIMITED", "VERDICT_UNUSABLE",
    "ScanSpec", "connectivity_change", "dihedral", "measure",
    "parse_scan_blocks", "scan_warnings",
]
