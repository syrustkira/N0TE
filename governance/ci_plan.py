from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

FULL = "full"
GOVERNANCE = "governance"
TARGETED = "targeted"

_HIGH_RISK_PREFIXES = ("n0te/", ".github/workflows/")
_HIGH_RISK_FILES = {
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "governance/model.py",
    "governance/check_governance.py",
    "governance/constitutional_migration.json",
    "governance/authority.json",
    "governance/ci_plan.py",
    "governance/closeout.py",
}
_MECHANICAL_GOVERNANCE_PREFIXES = (
    "governance/evidence/",
    "governance/programs/",
    "migration/receipts/",
)
_MECHANICAL_GOVERNANCE_FILES = {
    "governance/current_state.json",
}
_DOC_PREFIXES = ("docs/", "migration/")
_DOC_FILES = {"README.md"}
_TEST_ROOTS = {
    "acceptance",
    "business",
    "career",
    "daw",
    "governance",
    "ops",
    "unit",
}


@dataclass(frozen=True)
class CIPlan:
    tier: str
    pytest_targets: tuple[str, ...]
    changed_paths: tuple[str, ...]


def _normalize(paths) -> tuple[str, ...]:
    result = []
    for value in paths:
        text = str(value).strip().replace("\\", "/")
        if text:
            result.append(text)
    return tuple(dict.fromkeys(result))


def changed_paths(base_sha: str | None, head_sha: str | None) -> tuple[str, ...]:
    if not base_sha or not head_sha or set(base_sha) == {"0"}:
        return ()
    completed = subprocess.run(
        ["git", "diff", "--name-only", f"{base_sha}...{head_sha}"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return _normalize(completed.stdout.splitlines())


def _is_high_risk(path: str) -> bool:
    return path in _HIGH_RISK_FILES or path.startswith(_HIGH_RISK_PREFIXES)


def _is_mechanical_governance(path: str) -> bool:
    return (
        path in _MECHANICAL_GOVERNANCE_FILES
        or path.startswith(_MECHANICAL_GOVERNANCE_PREFIXES)
        or path == "tests/governance"
        or path.startswith("tests/governance/")
    )


def _is_docs_only(path: str) -> bool:
    return path in _DOC_FILES or path.startswith(_DOC_PREFIXES)


def _targeted_roots(paths: tuple[str, ...]) -> tuple[str, ...]:
    roots = {"tests/governance"}
    for path in paths:
        parts = path.split("/")
        if len(parts) < 2 or parts[0] != "tests":
            continue
        root = parts[1]
        if root not in _TEST_ROOTS:
            return ("tests",)
        roots.add(f"tests/{root}")
    return tuple(sorted(roots))


def classify(paths, *, event_name: str) -> CIPlan:
    normalized = _normalize(paths)
    if not normalized:
        return CIPlan(FULL, ("tests",), normalized)

    if any(_is_high_risk(path) for path in normalized):
        return CIPlan(FULL, ("tests",), normalized)

    if all(_is_mechanical_governance(path) or _is_docs_only(path) for path in normalized):
        return CIPlan(GOVERNANCE, ("tests/governance",), normalized)

    if all(path.startswith("tests/") for path in normalized):
        targets = _targeted_roots(normalized)
        tier = FULL if targets == ("tests",) else TARGETED
        return CIPlan(tier, targets, normalized)

    return CIPlan(FULL, ("tests",), normalized)


def plan_from_git(*, event_name: str, base_sha: str | None, head_sha: str | None) -> CIPlan:
    paths = changed_paths(base_sha, head_sha)
    return classify(paths, event_name=event_name)


def execute(plan: CIPlan) -> int:
    command = [sys.executable, "-m", "pytest", "-q", *plan.pytest_targets]
    print(
        f"N0TE CI tier={plan.tier} targets={','.join(plan.pytest_targets)} "
        f"changed={len(plan.changed_paths)}"
    )
    return subprocess.call(command)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    parser.add_argument("--base-sha")
    parser.add_argument("--head-sha")
    parser.add_argument("--paths-file")
    parser.add_argument("--execute-tests", action="store_true")
    args = parser.parse_args(argv)

    if args.paths_file:
        paths = Path(args.paths_file).read_text(encoding="utf-8").splitlines()
        plan = classify(paths, event_name=args.event)
    else:
        plan = plan_from_git(
            event_name=args.event,
            base_sha=args.base_sha,
            head_sha=args.head_sha,
        )

    print(f"tier={plan.tier}")
    print(f"pytest_targets={' '.join(plan.pytest_targets)}")
    if args.execute_tests:
        return execute(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
