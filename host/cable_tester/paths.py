"""Locate the project root from inside the package.

Code lives under ``host/cable_tester``, while every artefact a user opens or
deletes by hand stays at the project root: ``reports/``, ``calibration_profiles/``
and ``requirements.txt``. Resolving them here means a module can be moved between
layers without silently relocating a user's data.
"""

from __future__ import annotations

from pathlib import Path

# host/cable_tester/paths.py -> host/cable_tester -> host -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]

REPORTS = PROJECT_ROOT / "reports"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
CALIBRATION_PROFILES = PROJECT_ROOT / "calibration_profiles"

# Report directories, one per feature that writes reports.
CALIBRATION_REPORTS = REPORTS / "calibration"
TOPOLOGY_REPORTS = REPORTS / "topology"
AUXILIARY_REPORTS = REPORTS / "auxiliary_loop"
FOUR_WIRE_REPORTS = REPORTS / "four_wire_loop"
PAIRWISE_REPORTS = REPORTS / "pairwise_resistance"
