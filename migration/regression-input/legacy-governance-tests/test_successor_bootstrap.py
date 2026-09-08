from __future__ import annotations

import importlib.util
import py_compile
import re
import shutil
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
GOV = REPO / "governance"
PINNED = [
    "check_program.py",
    "check_successor_transition.py",
    "check_successor_preservation.py",
    "check_successor_steady_state.py",
    "check_successor_artifact_identity.py",
    "successor_transition_census.json",
]


def _load(name: str):
    path = GOV / name
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_successor_trusted_modules_compile() -> None:
    for name in PINNED:
        if not name.endswith(".py"):
            continue
        py_compile.compile(str(GOV / name), doraise=True)


def test_successor_census_excludes_closed_and_meta_owner_from_repair_debt() -> None:
    preservation = _load("check_successor_preservation.py")
    census = preservation.trusted_census()
    obligations = preservation.census_obligations(census)

    assert 205 in obligations
    assert 208 in obligations
    assert 244 in obligations
    assert 240 not in obligations
    assert 281 not in obligations
    assert census["closed_since_previous_repository_census"] == [240]
    assert census["meta_governance_owner_issue"] == 281
    assert census["meta_governance_owner_is_repair_debt"] is False


def test_artifact_identity_rejects_candidate_future_gate_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    identity = _load("check_successor_artifact_identity.py")
    candidate = tmp_path / "candidate"
    candidate_governance = candidate / "governance"
    candidate_governance.mkdir(parents=True)
    for name in PINNED:
        shutil.copyfile(GOV / name, candidate_governance / name)

    monkeypatch.setenv("N0TE2_META_GOVERNANCE_REOPEN", "MAIN_STEWARD_LABEL")
    identity.check_identity(candidate)

    steady = candidate_governance / "check_successor_steady_state.py"
    steady.write_text(steady.read_text(encoding="utf-8") + "\n# candidate drift\n", encoding="utf-8")
    with pytest.raises(identity.SuccessorArtifactIdentityError, match="cannot rewrite"):
        identity.check_identity(candidate)


def test_artifact_identity_pins_parent_program_checker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    identity = _load("check_successor_artifact_identity.py")
    candidate = tmp_path / "candidate"
    candidate_governance = candidate / "governance"
    candidate_governance.mkdir(parents=True)
    for name in PINNED:
        shutil.copyfile(GOV / name, candidate_governance / name)

    monkeypatch.setenv("N0TE2_META_GOVERNANCE_REOPEN", "MAIN_STEWARD_LABEL")
    checker = candidate_governance / "check_program.py"
    checker.write_text(checker.read_text(encoding="utf-8") + "\n# weakened candidate checker\n", encoding="utf-8")
    with pytest.raises(identity.SuccessorArtifactIdentityError, match="check_program.py"):
        identity.check_identity(candidate)


def test_artifact_identity_requires_explicit_meta_reopen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    identity = _load("check_successor_artifact_identity.py")
    candidate = tmp_path / "candidate"
    candidate_governance = candidate / "governance"
    candidate_governance.mkdir(parents=True)
    for name in PINNED:
        shutil.copyfile(GOV / name, candidate_governance / name)

    monkeypatch.delenv("N0TE2_META_GOVERNANCE_REOPEN", raising=False)
    with pytest.raises(identity.SuccessorArtifactIdentityError, match="meta-governance reopen"):
        identity.check_identity(candidate)


def test_steward_workflow_has_distinct_legacy_transition_and_steady_modes() -> None:
    workflow = (REPO / ".github/workflows/steward-integration.yml").read_text(encoding="utf-8")
    assert "BASE_REQUIREMENTS_SCHEMA" in workflow
    assert "CANDIDATE_REQUIREMENTS_SCHEMA" in workflow
    assert '"5" ] && [ "$CANDIDATE_REQUIREMENTS_SCHEMA" = "5"' in workflow
    assert '"5" ] && [ "$CANDIDATE_REQUIREMENTS_SCHEMA" = "6"' in workflow
    assert '"6" ] && [ "$CANDIDATE_REQUIREMENTS_SCHEMA" = "6"' in workflow
    assert '$TRUSTED_PATH/governance/check_program.py' in workflow
    assert 'python "$TRUSTED_PATH/governance/check_program.py" --repo .' in workflow
    assert '$TRUSTED_PATH/governance/check_successor_transition.py' in workflow
    assert '$TRUSTED_PATH/governance/check_successor_preservation.py' in workflow
    assert '$TRUSTED_PATH/governance/check_successor_artifact_identity.py' in workflow
    assert '$TRUSTED_PATH/governance/check_successor_steady_state.py' in workflow
    assert "Unsupported governance schema transition" in workflow
    assert "python governance/check_successor_steady_state.py --repo ." in workflow


def test_successor_census_is_transition_evidence_not_live_authority() -> None:
    preservation = _load("check_successor_preservation.py")
    census = preservation.trusted_census()
    assert census["source"] == "GITHUB_RUNTIME_ISSUE_CENSUS"
    assert census["authority_class"] == "TRANSITION_EVIDENCE_ONLY"
    assert any("GitHub runtime remains the owner" in rule for rule in census["rules"])


def test_steady_state_incident_gate_includes_active_touched_and_dependencies() -> None:
    steady = _load("check_successor_steady_state.py")

    class FakeLegacy:
        @staticmethod
        def candidate_changed_paths(_repo: Path) -> list[str]:
            return ["n0te2/touched.py"]

    receipt = {
        "work_packages": [
            {
                "package_id": "DONE-DEPENDENCY",
                "status": "DONE",
                "depends_on_packages": [],
                "allowed_exact_paths": ["n0te2/dependency.py"],
                "allowed_prefixes": [],
            },
            {
                "package_id": "ACTIVE-DISJOINT",
                "status": "ACTIVE",
                "depends_on_packages": ["DONE-DEPENDENCY"],
                "allowed_exact_paths": ["n0te2/active.py"],
                "allowed_prefixes": [],
            },
            {
                "package_id": "DONE-TOUCHED",
                "status": "DONE",
                "depends_on_packages": [],
                "allowed_exact_paths": ["n0te2/touched.py"],
                "allowed_prefixes": [],
            },
            {
                "package_id": "DONE-UNTOUCHED",
                "status": "DONE",
                "depends_on_packages": [],
                "allowed_exact_paths": ["n0te2/old.py"],
                "allowed_prefixes": [],
            },
        ]
    }
    gated = steady._packages_subject_to_incident_gate(
        REPO,
        receipt,
        legacy=FakeLegacy,
        transition=object(),
        program_result={"status": "ACTIVE"},
        verify_git=True,
    )
    assert [package["package_id"] for package in gated] == [
        "DONE-DEPENDENCY",
        "ACTIVE-DISJOINT",
        "DONE-TOUCHED",
    ]


def test_steady_state_incident_dependency_closure_is_transitive() -> None:
    steady = _load("check_successor_steady_state.py")

    class FakeLegacy:
        @staticmethod
        def candidate_changed_paths(_repo: Path) -> list[str]:
            return []

    receipt = {
        "work_packages": [
            {"package_id": "A", "status": "DONE", "depends_on_packages": [], "allowed_exact_paths": ["a"], "allowed_prefixes": []},
            {"package_id": "B", "status": "DONE", "depends_on_packages": ["A"], "allowed_exact_paths": ["b"], "allowed_prefixes": []},
            {"package_id": "C", "status": "ACTIVE", "depends_on_packages": ["B"], "allowed_exact_paths": ["c"], "allowed_prefixes": []},
        ]
    }
    gated = steady._packages_subject_to_incident_gate(
        REPO,
        receipt,
        legacy=FakeLegacy,
        transition=object(),
        program_result={"status": "ACTIVE"},
        verify_git=True,
    )
    assert [package["package_id"] for package in gated] == ["A", "B", "C"]


def test_steady_state_legacy_increment_gets_blast_gate_target(tmp_path: Path) -> None:
    steady = _load("check_successor_steady_state.py")
    transition = _load("check_successor_transition.py")
    governance = tmp_path / "governance"
    governance.mkdir()
    (governance / "completion_graph.json").write_text(
        '{"schema_version":4,"nodes":[{"id":"OPS-02","requirements":"160","state":"PRESERVED"}]}',
        encoding="utf-8",
    )

    class FakeLegacy:
        @staticmethod
        def candidate_changed_paths(_repo: Path) -> list[str]:
            return ["n0te2/disjoint.py"]

    receipt = {
        "status": "ACTIVE",
        "node_id": "OPS-02",
        "increment_id": "OPS-02-LEGACY-01",
        "allowed_exact_paths": ["n0te2/disjoint.py"],
        "allowed_prefixes": [],
    }
    gated = steady._packages_subject_to_incident_gate(
        tmp_path,
        receipt,
        legacy=FakeLegacy,
        transition=transition,
        program_result={"status": "NO_ACTIVE_PROGRAM"},
        verify_git=True,
    )
    assert len(gated) == 1
    assert gated[0]["package_id"] == "LEGACY:OPS-02-LEGACY-01"
    assert gated[0]["requirement_ids"] == ["REQ-SCOPE-160"]
    assert gated[0]["allowed_exact_paths"] == ["n0te2/disjoint.py"]
    assert steady._entry_overlaps_package(
        {"requirement_ids": ["REQ-SCOPE-161"], "exact_paths": [], "prefixes": []},
        gated[0],
    ) is False


def test_frozen_checkpoint_cannot_advance_from_candidate_base() -> None:
    steady = _load("check_successor_steady_state.py")
    base_checkpoint = "2" * 40
    candidate_checkpoint = "3" * 40
    base_receipt = {
        "program_id": "P",
        "work_packages": [
            {
                "package_id": "A",
                "status": "DONE",
                "kind": "GOVERNANCE",
                "requirement_ids": ["REQ-SCOPE-172"],
                "construction_affinity": ["UX-01"],
                "depends_on_packages": [],
                "product_code_allowed": False,
                "allowed_exact_paths": ["governance/a.json"],
                "allowed_prefixes": [],
                "acceptance": {"implementation_state": "COMPLETE", "authority_through_sha": base_checkpoint},
            }
        ],
    }
    candidate = {"program_id": "P", "work_packages": [dict(base_receipt["work_packages"][0])]}
    candidate["work_packages"][0] = dict(candidate["work_packages"][0])
    candidate["work_packages"][0]["acceptance"] = dict(candidate["work_packages"][0]["acceptance"])
    candidate["work_packages"][0]["acceptance"]["authority_through_sha"] = candidate_checkpoint

    class FakeLegacy:
        @staticmethod
        def candidate_base(_repo: Path) -> str:
            return "1" * 40

        @staticmethod
        def git_json(_repo: Path, _ref: str, _path: str) -> dict:
            return base_receipt

        @staticmethod
        def exact_json_equal(left, right) -> bool:
            return left == right

    class FakeProgram:
        HEX40 = re.compile(r"^[0-9a-f]{40}$")

        @staticmethod
        def git(_repo: Path, *_args: str) -> str:
            return "4" * 40

        @staticmethod
        def _is_ancestor(_repo: Path, _older: str, _newer: str) -> bool:
            return True

    with pytest.raises(steady.SuccessorSteadyStateError, match="cannot retroactively advance"):
        steady._validate_frozen_package_history(
            REPO,
            candidate,
            legacy=FakeLegacy,
            program=FakeProgram,
            verify_git=True,
        )


def test_active_package_may_receive_first_frozen_checkpoint() -> None:
    steady = _load("check_successor_steady_state.py")
    base_package = {
        "package_id": "A",
        "status": "ACTIVE",
        "kind": "GOVERNANCE",
        "requirement_ids": ["REQ-SCOPE-172"],
        "construction_affinity": ["UX-01"],
        "depends_on_packages": [],
        "product_code_allowed": False,
        "allowed_exact_paths": ["governance/a.json"],
        "allowed_prefixes": [],
        "acceptance": {"implementation_state": "IN_PROGRESS"},
    }
    base_receipt = {"program_id": "P", "work_packages": [base_package]}
    candidate_package = dict(base_package)
    candidate_package["status"] = "DONE"
    candidate_package["acceptance"] = {"implementation_state": "COMPLETE", "authority_through_sha": "2" * 40}
    candidate = {"program_id": "P", "work_packages": [candidate_package]}

    class FakeLegacy:
        @staticmethod
        def candidate_base(_repo: Path) -> str:
            return "1" * 40

        @staticmethod
        def git_json(_repo: Path, _ref: str, _path: str) -> dict:
            return base_receipt

        @staticmethod
        def exact_json_equal(left, right) -> bool:
            return left == right

    class FakeProgram:
        HEX40 = re.compile(r"^[0-9a-f]{40}$")

        @staticmethod
        def git(_repo: Path, *_args: str) -> str:
            return "3" * 40

        @staticmethod
        def _is_ancestor(_repo: Path, _older: str, _newer: str) -> bool:
            return True

    steady._validate_frozen_package_history(
        REPO,
        candidate,
        legacy=FakeLegacy,
        program=FakeProgram,
        verify_git=True,
    )


def test_schema_six_steady_state_preserves_completion_topology(tmp_path: Path) -> None:
    steady = _load("check_successor_steady_state.py")
    base_graph = {
        "schema_version": 4,
        "nodes": [
            {
                "id": "A",
                "required": True,
                "state": "PRESERVED",
                "depends_on": ["B"],
                "requirements": "160",
                "autonomy": "GREEN",
                "dependency_mode": "ALL",
                "any_of": [],
            },
            {
                "id": "B",
                "required": True,
                "state": "DONE",
                "depends_on": [],
                "requirements": "159",
                "autonomy": "GREEN",
                "dependency_mode": "ALL",
                "any_of": [],
            },
        ],
    }
    candidate_graph = {"schema_version": 4, "nodes": [dict(row) for row in base_graph["nodes"]]}
    candidate_graph["nodes"][0] = dict(candidate_graph["nodes"][0])
    candidate_graph["nodes"][0]["depends_on"] = []
    governance = tmp_path / "governance"
    governance.mkdir()
    import json
    (governance / "completion_graph.json").write_text(json.dumps(candidate_graph), encoding="utf-8")

    class FakeLegacy:
        @staticmethod
        def candidate_base(_repo: Path) -> str:
            return "1" * 40

        @staticmethod
        def git_json(_repo: Path, _ref: str, _path: str) -> dict:
            return base_graph

        @staticmethod
        def exact_json_equal(left, right) -> bool:
            return left == right

    class FakeTransition:
        SUCCESSOR_GRAPH_SCHEMA = 4

    with pytest.raises(steady.SuccessorSteadyStateError, match="cannot rewrite completion topology"):
        steady._preserve_graph_topology_against_base(
            tmp_path,
            legacy=FakeLegacy,
            transition=FakeTransition,
            verify_git=True,
        )


def test_schema_six_steady_state_allows_state_only_progression(tmp_path: Path) -> None:
    steady = _load("check_successor_steady_state.py")
    base_graph = {
        "schema_version": 4,
        "nodes": [
            {
                "id": "A",
                "required": True,
                "state": "PRESERVED",
                "depends_on": [],
                "requirements": "160",
                "autonomy": "GREEN",
                "dependency_mode": "ALL",
                "any_of": [],
            }
        ],
    }
    candidate_graph = {"schema_version": 4, "nodes": [dict(base_graph["nodes"][0])]}
    candidate_graph["nodes"][0]["state"] = "DONE"
    governance = tmp_path / "governance"
    governance.mkdir()
    import json
    (governance / "completion_graph.json").write_text(json.dumps(candidate_graph), encoding="utf-8")

    class FakeLegacy:
        @staticmethod
        def candidate_base(_repo: Path) -> str:
            return "1" * 40

        @staticmethod
        def git_json(_repo: Path, _ref: str, _path: str) -> dict:
            return base_graph

        @staticmethod
        def exact_json_equal(left, right) -> bool:
            return left == right

    class FakeTransition:
        SUCCESSOR_GRAPH_SCHEMA = 4

    steady._preserve_graph_topology_against_base(
        tmp_path,
        legacy=FakeLegacy,
        transition=FakeTransition,
        verify_git=True,
    )


def test_steady_state_parent_blast_must_equal_entry_union() -> None:
    steady = _load("check_successor_steady_state.py")
    transition = _load("check_successor_transition.py")
    incident = {
        "id": "INC-TEST",
        "blast_radius": {
            "policy": "BLOCK_OVERLAPPING_REQUIREMENTS_PATHS_OR_EXPLICIT_DEPENDENCIES",
            "unknown_blocks": True,
            "affected_issue_ids": [208],
            "affected_requirement_ids": ["REQ-SCOPE-160"],
            "affected_exact_paths": [],
            "affected_prefixes": ["n0te2/capability/"],
        },
    }
    entries = [
        {
            "issue_id": 208,
            "state": "OPEN_FIX",
            "requirement_ids": ["REQ-SCOPE-160"],
            "exact_paths": [],
            "prefixes": ["n0te2/capability/"],
        }
    ]
    steady._validated_parent_blast(incident, entries, transition)
    incident["blast_radius"]["affected_requirement_ids"] = []
    with pytest.raises(steady.SuccessorSteadyStateError, match="affected requirements diverge"):
        steady._validated_parent_blast(incident, entries, transition)
