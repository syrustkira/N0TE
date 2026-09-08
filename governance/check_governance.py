from __future__ import annotations
import argparse
import json
from pathlib import Path
from governance.model import validate_requirement_evidence, validate_scope_projection

ROOT = Path(__file__).resolve().parents[1]


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
    print("offline structural governance validation passed" if args.offline else "fresh canonical governance validation passed")


if __name__ == "__main__":
    main()
