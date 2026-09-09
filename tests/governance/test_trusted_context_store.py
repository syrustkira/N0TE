from __future__ import annotations

import copy
import json

import pytest

from governance.trusted_context import (
    FileTrustedContextProvider,
    TrustedContextError,
    validate_trusted_context_snapshot,
)


def snapshot(snapshot_id: str):
    return {
        "snapshot_id": snapshot_id,
        "source_fingerprint": f"sha256:{snapshot_id}",
        "observed_at": "2026-09-09T13:00:00Z",
        "expires_at": "2099-09-09T13:00:00Z",
        "retained_scope_refs": ["REQ-SCOPE-172"],
        "truth_owners": {
            "WHY": "TELLMEN0TE MASTER CONTEXT",
            "WHAT": "N0TE - CURRENT PRODUCT VISION",
            "HOW": "N0TE_PRODUCT_DB",
            "NOW": "N0TE/governance/current_state.json",
            "PROOF": "LIVE_PROVIDER_EVIDENCE",
        },
        "policies": {
            "governance-repair": {
                "required_functions": ["governance"],
                "required_dependencies": {
                    "upstream": [],
                    "downstream": [],
                },
                "allowed_outcome_classes": ["EXTERNAL_STATE"],
                "authority_by_action_class": {
                    "REVERSIBLE": {
                        "requires_human": False,
                        "source_refs": ["governance:bounded-repair"],
                    }
                },
            }
        },
        "approvals": [],
    }


def write_store(tmp_path, payload):
    path = tmp_path / "trusted-context.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return FileTrustedContextProvider(path)


def test_provider_rejects_superseded_snapshot_even_when_old_snapshot_is_unexpired(
    tmp_path,
):
    provider = write_store(
        tmp_path,
        {
            "current_snapshot_id": "ctx-new",
            "snapshots": [snapshot("ctx-old"), snapshot("ctx-new")],
        },
    )

    with pytest.raises(TrustedContextError, match="not current"):
        provider.get("ctx-old")

    assert provider.get("ctx-new").snapshot_id == "ctx-new"


def test_multi_snapshot_store_without_current_pointer_fails_closed(tmp_path):
    provider = write_store(
        tmp_path,
        {
            "snapshots": [snapshot("ctx-old"), snapshot("ctx-new")],
        },
    )

    with pytest.raises(
        TrustedContextError,
        match="requires current_snapshot_id",
    ):
        provider.get("ctx-new")


def test_single_snapshot_store_remains_unambiguous_without_pointer(tmp_path):
    provider = write_store(
        tmp_path,
        {"snapshots": [snapshot("ctx-only")]},
    )

    assert provider.get("ctx-only").snapshot_id == "ctx-only"


def test_duplicate_normalized_authority_classes_fail_closed():
    raw = snapshot("ctx-duplicate-authority")
    profile = raw["policies"]["governance-repair"][
        "authority_by_action_class"
    ]["REVERSIBLE"]
    raw["policies"]["governance-repair"][
        "authority_by_action_class"
    ] = {
        "REVERSIBLE": copy.deepcopy(profile),
        "reversible": {
            "requires_human": True,
            "source_refs": ["artist:explicit-approval"],
        },
    }

    with pytest.raises(
        TrustedContextError,
        match="duplicate normalized action class: REVERSIBLE",
    ):
        validate_trusted_context_snapshot(raw)
