from __future__ import annotations
import argparse
import json
from pathlib import Path
from governance.model import (
    evaluate_construction_program,
    validate_requirement_evidence,
    validate_scope_projection,
)

ROOT = Path(__file__).resolve().parents[1]
PROGRAMS_ROOT = (ROOT / "governance" / "programs").resolve()
EVIDENCE_ROOT = (ROOT / "governance" / "evidence").resolve()


def load_current_state():
    return json.loads((ROOT / "governance/current_state.json").read_text())


def evaluate_current_construction_program():
    current_state = load_current_state()
    program_ref = current_state.get("active_construction_program")
    if program_ref is None:
        return None
    program_path = (ROOT / str(program_ref)).resolve()
    if program_path.parent != PROGRAMS_ROOT or program_path.suffix != ".json":
        raise ValueError("active construction program must be a JSON file in governance/programs")
    program = json.loads(program_path.read_text())
    return program, evaluate_construction_program(program)


def load_current_requirement_evidence(canonical_ids):
    current_state = load_current_state()
    refs = current_state.get("current_requirement_evidence", {})
    if not isinstance(refs, dict):
        raise ValueError("current_requirement_evidence must be an object")
    canonical = set(canonical_ids)
    current = {}
    for requirement_id, evidence_ref in refs.items():
        if requirement_id not in canonical:
            raise ValueError(f"current evidence references unknown requirement: {requirement_id}")
        evidence_path = (ROOT / str(evidence_ref)).resolve()
        if evidence_path.parent != EVIDENCE_ROOT or evidence_path.suffix != ".json":
            raise ValueError(
                "current requirement evidence must be a JSON file in governance/evidence"
            )
        receipt = json.loads(evidence_path.read_text())
        if receipt.get("requirement_id") != requirement_id:
            raise ValueError(
                f"current evidence requirement mismatch: {requirement_id}"
            )
        claims = receipt.get("claims")
        validate_requirement_evidence(
            [requirement_id],
            {"requirements": {requirement_id: claims}},
        )
        current[requirement_id] = receipt
    return current


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--canonical-manifest")
    args = ap.parse_args()
    repo_manifest = json.loads((ROOT / "governance/canonical_scope_manifest.json").read_text())
    evidence = json.loads((ROOT / "governance/evidence_state.json").read_text())
    repo_ids = repo_manifest["retained_requirement_ids"]
    if args.canonical_manifest:
        fresh = json.loads(Path(args.canonical_manifest).read_text())
        validate_scope_projection(fresh["retained_requirement_ids"], repo_ids)
    elif not args.offline:
        raise SystemExit("Fresh externally retrieved canonical manifest required for normal governance/work selection")
    validate_requirement_evidence(repo_ids, evidence)
    current_evidence = load_current_requirement_evidence(repo_ids)
    if current_evidence:
        print(
            "current requirement evidence="
            + ",".join(sorted(current_evidence))
        )
    current_program = evaluate_current_construction_program()
    if current_program is not None:
        program, result = current_program
        eligible = ",".join(result["eligible_work_package_ids"]) or "NONE"
        print(f"current construction program {program['program_id']} eligible={eligible}")
    print("offline structural governance validation passed" if args.offline else "fresh canonical governance validation passed")


if __name__ == "__main__":
    main()
