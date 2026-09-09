from __future__ import annotations

import argparse
import json
import os
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
_CURRENT_STATE_PATH = "governance/current_state.json"


class ChangeScopeError(ValueError):
    """A repository change falls outside the selected construction authority."""


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


def _active_package_paths(program) -> set[str]:
    if not isinstance(program, dict):
        raise ChangeScopeError("construction program must be an object")
    packages = program.get("work_packages")
    if not isinstance(packages, list):
        raise ChangeScopeError("construction program work_packages must be a list")
    allowed = set()
    for package in packages:
        if not isinstance(package, dict):
            raise ChangeScopeError("construction work package must be an object")
        if package.get("state") != "ACTIVE":
            continue
        allowed.update(_normalize(package.get("paths", [])))
    return allowed


def _control_plane_paths(state) -> set[str]:
    if not isinstance(state, dict):
        raise ChangeScopeError("current state must be an object")
    allowed = {_CURRENT_STATE_PATH}
    program_ref = str(state.get("active_construction_program", "")).strip()
    if program_ref:
        allowed.add(program_ref)
    evidence = state.get("current_requirement_evidence", {})
    if evidence is not None and not isinstance(evidence, dict):
        raise ChangeScopeError("current_requirement_evidence must be an object")
    if isinstance(evidence, dict):
        for ref in evidence.values():
            text = str(ref).strip().replace("\\", "/")
            if text:
                allowed.add(text)
    return allowed


def _require_owner_program_transition(*, actor, repository_owner):
    actor_text = str(actor or "").strip()
    owner_text = str(repository_owner or "").strip()
    if not actor_text or not owner_text:
        raise ChangeScopeError(
            "program transition requires repository-owner identity evidence"
        )
    if actor_text.casefold() != owner_text.casefold():
        raise ChangeScopeError(
            "program transition requires repository-owner authority: "
            f"actor={actor_text} owner={owner_text}"
        )
    return actor_text


def validate_change_scope(
    paths,
    *,
    base_state,
    base_program,
    head_state,
    head_program,
    actor=None,
    repository_owner=None,
):
    """Fail closed when changes exceed selected construction authority.

    A same-program closeout may use paths that were ACTIVE at either side of the
    comparison. A program transition requires external GitHub repository-owner
    identity and then authorizes substantive work only from ACTIVE packages in the
    newly selected head program. Current-state, selected program records, and
    requirement-evidence records are control-plane records; they do not grant
    authority to any other changed path.
    """

    normalized = set(_normalize(paths))
    base_ref = str(base_state.get("active_construction_program", "")).strip()
    head_ref = str(head_state.get("active_construction_program", "")).strip()
    if not head_ref:
        raise ChangeScopeError("head current state has no active construction program")

    if base_ref == head_ref:
        authorized = _active_package_paths(base_program) | _active_package_paths(head_program)
    else:
        _require_owner_program_transition(
            actor=actor,
            repository_owner=repository_owner,
        )
        authorized = _active_package_paths(head_program)

    control = _control_plane_paths(base_state) | _control_plane_paths(head_state)
    unauthorized = sorted(normalized - authorized - control)
    if unauthorized:
        raise ChangeScopeError(
            "changed paths exceed selected construction authority: "
            f"{unauthorized}"
        )
    return tuple(sorted(authorized))


def _git_json(sha: str, path: str):
    if not sha or set(sha) == {"0"}:
        raise ChangeScopeError(f"cannot read governance state from revision: {sha!r}")
    completed = subprocess.run(
        ["git", "show", f"{sha}:{path}"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ChangeScopeError(
            f"invalid JSON at {sha}:{path}"
        ) from exc


def _governance_at_revision(sha: str):
    state = _git_json(sha, _CURRENT_STATE_PATH)
    program_ref = str(state.get("active_construction_program", "")).strip()
    if not program_ref:
        raise ChangeScopeError(
            f"{sha}:{_CURRENT_STATE_PATH} has no active construction program"
        )
    program = _git_json(sha, program_ref)
    return state, program


def validate_git_change_scope(
    *,
    paths,
    base_sha: str | None,
    head_sha: str | None,
    actor=None,
    repository_owner=None,
):
    if not base_sha or not head_sha or set(base_sha) == {"0"}:
        raise ChangeScopeError(
            "base and head revisions are required for construction authority validation"
        )
    base_state, base_program = _governance_at_revision(base_sha)
    head_state, head_program = _governance_at_revision(head_sha)
    return validate_change_scope(
        paths,
        base_state=base_state,
        base_program=base_program,
        head_state=head_state,
        head_program=head_program,
        actor=actor,
        repository_owner=repository_owner,
    )


def plan_from_git(
    *,
    event_name: str,
    base_sha: str | None,
    head_sha: str | None,
    actor=None,
    repository_owner=None,
) -> CIPlan:
    paths = changed_paths(base_sha, head_sha)
    validate_git_change_scope(
        paths=paths,
        base_sha=base_sha,
        head_sha=head_sha,
        actor=actor,
        repository_owner=repository_owner,
    )
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
    parser.add_argument("--actor")
    parser.add_argument("--repository-owner")
    parser.add_argument("--execute-tests", action="store_true")
    args = parser.parse_args(argv)

    if args.paths_file:
        paths = Path(args.paths_file).read_text(encoding="utf-8").splitlines()
        plan = classify(paths, event_name=args.event)
    else:
        repository_owner = args.repository_owner or os.environ.get(
            "GITHUB_REPOSITORY_OWNER"
        )
        if not repository_owner:
            repository = os.environ.get("GITHUB_REPOSITORY", "")
            if "/" in repository:
                repository_owner = repository.split("/", 1)[0]
        plan = plan_from_git(
            event_name=args.event,
            base_sha=args.base_sha,
            head_sha=args.head_sha,
            actor=args.actor or os.environ.get("GITHUB_ACTOR"),
            repository_owner=repository_owner,
        )

    print(f"tier={plan.tier}")
    print(f"pytest_targets={' '.join(plan.pytest_targets)}")
    if args.execute_tests:
        return execute(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
