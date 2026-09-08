import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_json(path: str):
    return json.loads((ROOT / path).read_text())


def test_fast_pipeline_activation_is_bounded_and_conditioned_on_resulting_main():
    state = load_json("governance/current_state.json")
    pipeline = state["construction_pipeline"]
    assert pipeline["state"] == "ACTIVATION_CANDIDATE"
    assert pipeline["pull_request_verification"] == "EXACT_HEAD"
    assert pipeline["branch_push_duplication"] == "DISABLED_EXCEPT_MAIN"
    assert pipeline["independent_pre_migration_proof"]["candidate_pr_ci_run"] == 34271460031
    assert pipeline["independent_pre_migration_proof"]["candidate_main_ci_run"] == 34271647509
    assert set(pipeline["mechanical_closeout"]["never_automatic"]) == {
        "CONSUMER_ACCEPTED",
        "VALUE_EVIDENCED",
    }

    authority = load_json("governance/authority.json")
    pending = authority["pending_constitutional_migration"]
    assert pending["migration_id"] == "CONST-FAST-CONSTRUCTION-PIPELINE-2026-09-08"
    assert pending["state"] == "ACTIVE_UNTIL_VERIFIED_ACTIVATION"
    assert pending["ordinary_product_path_bypass"] is False
    assert pending["permanent_bypass"] is False


def test_active_produce_package_predeclares_only_safe_mechanical_closeout_dimensions():
    program = load_json("governance/programs/PROGRAM-PERSONAL-PRODUCTION-002.json")
    package = next(
        item
        for item in program["work_packages"]
        if item["work_id"] == "WP-079-090-PRODUCE-REACHABILITY"
    )
    assert package["state"] == "ACTIVE"
    assert set(package["mechanical_closeout_allowlist"]) == {
        "IMPLEMENTED",
        "INTEGRATED",
        "REACHABLE",
        "VERIFIED",
        "AUTHORITY_SAFE",
    }
    assert "RECOVERABLE" not in package["mechanical_closeout_allowlist"]
    assert "CONSUMER_ACCEPTED" not in package["mechanical_closeout_allowlist"]
    assert "VALUE_EVIDENCED" not in package["mechanical_closeout_allowlist"]


def test_ci_workflow_removes_duplicate_branch_pushes_and_keeps_exact_head_and_main_proof():
    workflow = (ROOT / ".github/workflows/governance.yml").read_text()
    assert "pull_request:" in workflow
    assert "push:\n    branches: [main]" in workflow
    assert "github.event.pull_request.head.sha || github.sha" in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow
    assert "cache: pip" in workflow
    assert "governance/ci_plan.py" in workflow
    assert "structural:" in workflow
    assert "tests:" in workflow


def test_mechanical_closeout_validates_exact_generated_tree_before_fast_forward():
    workflow = (ROOT / ".github/workflows/mechanical-closeout.yml").read_text()
    assert "workflow_run:" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "governance/closeout.py apply" in workflow
    assert "python -m pytest -q tests/governance" in workflow
    assert "PYTHONPATH=. python governance/check_governance.py --offline" in workflow
    assert "git ls-remote origin refs/heads/main" in workflow
    assert 'if [ "$remote_main" != "$MAIN_SHA" ]' in workflow
    assert "git push origin HEAD:main" in workflow
    assert "--force" not in workflow
