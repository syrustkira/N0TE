from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


governance = load_module(
    "n0te2_startup_governance_path_check",
    ROOT / "governance/check_governance.py",
)
authority = load_module(
    "n0te2_startup_repair_authority_path_check",
    ROOT / "governance/check_incident_repair_authority.py",
)


def test_only_named_startup_docs_are_governance_repair_surfaces() -> None:
    for path in ("AGENTS.md", "docs/STARTUP_RECONCILIATION.md"):
        assert governance.governance_repair_path_allowed(path)
        assert authority.governance_repair_path_allowed(path)


def test_arbitrary_documentation_is_not_governance_repair_surface() -> None:
    for path in (
        "docs/MASTER_CONTINUITY_CONTROLLER.md",
        "README.md",
        "docs/PRODUCT_NORTH_STAR.md",
    ):
        assert not governance.governance_repair_path_allowed(path)
        assert not authority.governance_repair_path_allowed(path)
