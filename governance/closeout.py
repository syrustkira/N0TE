from __future__ import annotations

import argparse
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from governance.model import EVIDENCE_DIMENSIONS, validate_construction_program

MARKER_RE = re.compile(
    r"<!--\s*N0TE_CLOSEOUT\s*(\{.*?\})\s*N0TE_CLOSEOUT\s*-->",
    re.DOTALL,
)
NEVER_AUTOMATE = {"CONSUMER_ACCEPTED", "VALUE_EVIDENCED"}


class CloseoutError(RuntimeError):
    pass


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def extract_request(pr_body: str) -> dict:
    match = MARKER_RE.search(pr_body or "")
    if not match:
        raise CloseoutError("no N0TE_CLOSEOUT request")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise CloseoutError("N0TE_CLOSEOUT payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CloseoutError("N0TE_CLOSEOUT payload must be an object")
    return payload


def _evidence_cell(state="UNPROVEN", refs=(), **extra):
    cell = {"state": state, "evidence_refs": list(refs)}
    cell.update(extra)
    return cell


def _normalize_refs(value, allowed_paths: set[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CloseoutError("evidence_refs must be a list of strings")
    refs = []
    for raw in value:
        ref = raw.strip()
        if not ref:
            raise CloseoutError("evidence_refs may not contain empty values")
        if ref.startswith("N0TE_PRODUCT_DB/SCOPE_LEDGER:"):
            refs.append(ref)
            continue
        if ref not in allowed_paths:
            raise CloseoutError(f"mechanical closeout evidence ref outside work-package paths: {ref}")
        refs.append(ref)
    return list(dict.fromkeys(refs))


def _current_claims(root: Path, current_state: dict, requirement_id: str) -> dict:
    ref = current_state.get("current_requirement_evidence", {}).get(requirement_id)
    if not ref:
        return {dim: _evidence_cell() for dim in EVIDENCE_DIMENSIONS}
    receipt = _load(root / ref)
    claims = receipt.get("claims")
    if not isinstance(claims, dict):
        raise CloseoutError(f"current evidence receipt malformed for {requirement_id}")
    result = {}
    for dim in EVIDENCE_DIMENSIONS:
        cell = claims.get(dim)
        if not isinstance(cell, dict):
            raise CloseoutError(f"current evidence missing {requirement_id}:{dim}")
        result[dim] = deepcopy(cell)
    return result


def _validate_request(request: dict, program: dict, package: dict) -> dict[str, dict]:
    if request.get("schema_version") != 1:
        raise CloseoutError("unsupported closeout schema_version")
    if request.get("program_id") != program.get("program_id"):
        raise CloseoutError("closeout program does not match active program")
    if request.get("work_id") != package.get("work_id"):
        raise CloseoutError("closeout work_id mismatch")
    if request.get("complete") is not True:
        raise CloseoutError("mechanical closeout requires complete=true")
    if package.get("state") != "ACTIVE":
        raise CloseoutError("mechanical closeout may close only an ACTIVE work package")

    allowlist = package.get("mechanical_closeout_allowlist", [])
    if not isinstance(allowlist, list) or not all(isinstance(item, str) for item in allowlist):
        raise CloseoutError("work package mechanical_closeout_allowlist must be a list")
    allowed = set(allowlist)
    if allowed & NEVER_AUTOMATE:
        raise CloseoutError("consumer acceptance and value can never be mechanically allowlisted")

    claims = request.get("claims")
    if not isinstance(claims, dict) or not claims:
        raise CloseoutError("closeout claims object required")
    unknown = set(claims) - set(EVIDENCE_DIMENSIONS)
    if unknown:
        raise CloseoutError(f"unknown closeout evidence dimensions: {sorted(unknown)}")
    forbidden = set(claims) & NEVER_AUTOMATE
    if forbidden:
        raise CloseoutError(f"mechanical closeout cannot prove {sorted(forbidden)}")
    outside = set(claims) - allowed
    if outside:
        raise CloseoutError(f"closeout claims exceed mechanical allowlist: {sorted(outside)}")

    package_paths = set(package.get("paths", []))
    normalized = {}
    for dimension, cell in claims.items():
        if not isinstance(cell, dict) or cell.get("state") != "PROVEN":
            raise CloseoutError(f"mechanical claim must be explicitly PROVEN: {dimension}")
        refs = _normalize_refs(cell.get("evidence_refs", []), package_paths)
        observed = cell.get("observed_result")
        if observed is not None and (not isinstance(observed, str) or not observed.strip()):
            raise CloseoutError(f"observed_result must be non-empty text: {dimension}")
        normalized[dimension] = {
            "state": "PROVEN",
            "evidence_refs": refs,
            **({"observed_result": observed.strip()} if observed else {}),
        }
    return normalized


def _receipt_name(requirement_id: str, work_id: str, main_sha: str) -> str:
    safe_work = re.sub(r"[^A-Za-z0-9._-]+", "-", work_id).strip("-")
    return f"{requirement_id}-{safe_work}-{main_sha[:12]}.json"


def apply_closeout(
    root: Path,
    request: dict,
    *,
    main_sha: str,
    ci_run: int,
    observed_at: str | None = None,
) -> list[str]:
    root = root.resolve()
    current_path = root / "governance/current_state.json"
    current = _load(current_path)
    program_ref = current.get("active_construction_program")
    if not isinstance(program_ref, str):
        raise CloseoutError("no active construction program")

    program_path = (root / program_ref).resolve()
    programs_root = (root / "governance/programs").resolve()
    if program_path.parent != programs_root:
        raise CloseoutError("active construction program must live in governance/programs")
    program = _load(program_path)
    validate_construction_program(program)

    packages = {item["work_id"]: item for item in program["work_packages"]}
    work_id = request.get("work_id")
    package = packages.get(work_id)
    if package is None:
        raise CloseoutError(f"work package not found: {work_id}")

    normalized_claims = _validate_request(request, program, package)
    main_sha = str(main_sha).strip()
    if len(main_sha) < 12:
        raise CloseoutError("main_sha is required")
    try:
        ci_run = int(ci_run)
    except (TypeError, ValueError) as exc:
        raise CloseoutError("ci_run must be an integer") from exc
    stamp = observed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    evidence_dir = root / "governance/evidence"
    generated_refs = []
    changed = []

    current_refs = current.setdefault("current_requirement_evidence", {})
    semantic_owner = "N0TE_PRODUCT_DB/SCOPE_LEDGER"

    for requirement_id in package["requirement_ids"]:
        claims = _current_claims(root, current, requirement_id)
        for dimension, requested_cell in normalized_claims.items():
            refs = list(requested_cell["evidence_refs"])
            refs.extend([f"main:{main_sha}", f"ci-run:{ci_run}"])
            existing_refs = claims.get(dimension, {}).get("evidence_refs", [])
            refs = list(dict.fromkeys([*existing_refs, *refs]))
            claims[dimension] = {
                "state": "PROVEN",
                "evidence_refs": refs,
                **(
                    {"observed_result": requested_cell["observed_result"]}
                    if "observed_result" in requested_cell
                    else {}
                ),
            }

        receipt_name = _receipt_name(requirement_id, package["work_id"], main_sha)
        receipt_ref = f"governance/evidence/{receipt_name}"
        receipt = {
            "schema_version": 1,
            "kind": "REQUIREMENT_EVIDENCE_RECEIPT",
            "requirement_id": requirement_id,
            "semantic_owner": semantic_owner,
            "observed_at": stamp,
            "claims": claims,
            "mechanical_closeout": {
                "program_id": program["program_id"],
                "work_id": package["work_id"],
                "resulting_main_execution_commit": main_sha,
                "resulting_main_execution_ci_run": ci_run,
                "automatic_consumer_acceptance": False,
                "automatic_value_evidence": False,
            },
            "anti_collapse_rule": (
                "Mechanical closeout may promote only dimensions pre-authorized by the "
                "work package. CONSUMER_ACCEPTED and VALUE_EVIDENCED are never automated."
            ),
        }
        _write(evidence_dir / receipt_name, receipt)
        current_refs[requirement_id] = receipt_ref
        generated_refs.append(receipt_ref)
        changed.append(receipt_ref)

    for dimension in normalized_claims:
        package["evidence"][dimension] = {
            "state": "PROVEN",
            "evidence_refs": list(generated_refs),
        }
    package["state"] = "COMPLETE"
    package["mechanical_closeout"] = {
        "resulting_main_execution_commit": main_sha,
        "resulting_main_execution_ci_run": ci_run,
        "evidence_receipts": generated_refs,
    }

    all_complete = all(item.get("state") == "COMPLETE" for item in program["work_packages"])
    if all_complete:
        program["state"] = "COMPLETE"
        completion_name = f"{program['program_id']}-completion-{main_sha[:12]}.json"
        completion_ref = f"governance/evidence/{completion_name}"
        program["completion_receipt"] = completion_ref
        completion = {
            "schema_version": 1,
            "kind": "CONSTRUCTION_PROGRAM_COMPLETION_RECEIPT",
            "program_id": program["program_id"],
            "observed_at": stamp,
            "resulting_main_execution_commit": main_sha,
            "resulting_main_execution_ci_run": ci_run,
            "work_package_ids": [item["work_id"] for item in program["work_packages"]],
            "consumer_acceptance_derived_separately": True,
            "value_evidence_derived_separately": True,
        }
        _write(evidence_dir / completion_name, completion)
        changed.append(completion_ref)
        current.pop("active_construction_program", None)
        current["last_completed_construction_program"] = {
            "program_id": program["program_id"],
            "program_path": program_ref,
            "completion_receipt": completion_ref,
            "resulting_main_execution_commit": main_sha,
            "resulting_main_execution_ci_run": ci_run,
        }

    _write(program_path, program)
    _write(current_path, current)
    changed.extend([program_ref, "governance/current_state.json"])
    return list(dict.fromkeys(changed))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract")
    extract.add_argument("--pr-body-file", required=True)
    extract.add_argument("--output", required=True)

    apply = sub.add_parser("apply")
    apply.add_argument("--root", default=".")
    apply.add_argument("--request", required=True)
    apply.add_argument("--main-sha", required=True)
    apply.add_argument("--ci-run", required=True, type=int)
    apply.add_argument("--observed-at")

    args = parser.parse_args(argv)
    if args.command == "extract":
        body = Path(args.pr_body_file).read_text(encoding="utf-8")
        request = extract_request(body)
        _write(Path(args.output), request)
        return 0

    request = _load(Path(args.request))
    changed = apply_closeout(
        Path(args.root),
        request,
        main_sha=args.main_sha,
        ci_run=args.ci_run,
        observed_at=args.observed_at,
    )
    print("\n".join(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
