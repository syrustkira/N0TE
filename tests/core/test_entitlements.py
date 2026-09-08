from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from n0te.capabilities import CapabilityCandidate
from n0te.entitlements import EntitlementTruthError, EntitlementTruthService
from n0te.lineage import LineageCorruptionError
from n0te.memory import HeadquartersMemory


def _service(hq: HeadquartersMemory) -> EntitlementTruthService:
    return EntitlementTruthService(hq.store, hq.evidence)


def _provider_claim(
    hq: HeadquartersMemory,
    service: EntitlementTruthService,
    *,
    route_id: str = "provider:cloud-master",
    capability: str = "audio.master",
    access_kind: str = "PROVIDER_PLAN",
    entitlement_state: str = "GRANTED",
    permission_state: str = "NOT_REQUIRED",
    observed_at_epoch_seconds: int = 100,
    expires_at_epoch_seconds: int | None = None,
    quota_remaining: float | int | None = None,
    quota_unit: str | None = None,
    environment_fingerprint: str | None = None,
    source_ref: str = "provider-receipt:plan-42",
    supersedes: tuple[str, ...] = (),
):
    payload = service.evidence_payload(
        route_id=route_id,
        capability=capability,
        access_kind=access_kind,
        entitlement_state=entitlement_state,
        permission_state=permission_state,
        observed_at_epoch_seconds=observed_at_epoch_seconds,
        expires_at_epoch_seconds=expires_at_epoch_seconds,
        quota_remaining=quota_remaining,
        quota_unit=quota_unit,
        environment_fingerprint=environment_fingerprint,
    )
    return hq.evidence.record_claim(
        scope_kind="PROFILE",
        scope_id=hq.store.profile_id,
        key=service.claim_key(
            route_id=route_id,
            capability=capability,
            access_kind=access_kind,
        ),
        value=payload,
        source_kind="PROVIDER_VERIFIED",
        source_ref=source_ref,
        confidence=1.0,
        twin_domain="TECHNICAL",
        supersedes=supersedes,
    )


def test_no_entitlement_evidence_is_unknown_and_non_authorizing(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Unknown Access Artist")
    try:
        snapshot = _service(hq).snapshot(
            route_id="owned:compressor",
            capability="audio.compress",
            access_kind="PLUGIN",
            as_of_epoch_seconds=100,
        )
        assert snapshot.resolution_status == "UNKNOWN"
        assert snapshot.validity_state == "UNKNOWN"
        assert snapshot.entitlement_state == "UNKNOWN"
        assert snapshot.permission_state == "UNKNOWN"
        assert snapshot.eligibility_entitlement_state == "UNKNOWN"
        assert snapshot.eligibility_permission_state == "UNKNOWN"
        assert snapshot.strongest_source_class == "NONE"
        assert snapshot.provider_verified is False
        assert snapshot.strong_access_evidence is False
        assert snapshot.action_authority_granted is False
        assert snapshot.execution_authority_granted is False
        assert snapshot.purchase_authority_granted is False
        assert snapshot.activation_authority_granted is False
        assert snapshot.quota_spend_authority_granted is False
        assert snapshot.provider_write_authority_granted is False
        assert snapshot.external_action_authority_granted is False
    finally:
        hq.close()


def test_installed_or_paid_capability_does_not_infer_entitlement(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Installed Artist")
    try:
        candidate = CapabilityCandidate(
            candidate_id="candidate-plugin",
            route_kind="OWNED_TOOL",
            capability="audio.compress",
            display_name="Installed Compressor",
            brand="Example",
            verified=True,
            compatible=True,
            evidence_ref="runtime-probe:installed",
            evidence_age_seconds=0,
            task_fit=1.0,
            editability=1.0,
            locality=1.0,
            privacy=1.0,
            latency=1.0,
            reversibility=1.0,
            cost_efficiency=0.2,
            portability=0.5,
            paid=True,
        )
        assert candidate.verified is True
        assert candidate.compatible is True
        assert candidate.paid is True

        snapshot = _service(hq).snapshot(
            route_id="owned:compressor",
            capability=candidate.capability,
            access_kind="PLUGIN",
            as_of_epoch_seconds=100,
        )
        assert snapshot.resolution_status == "UNKNOWN"
        assert snapshot.eligibility_entitlement_state == "UNKNOWN"
    finally:
        hq.close()


def test_user_declaration_is_preserved_but_not_upgraded_to_verified_grant(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Declared Access Artist")
    try:
        service = _service(hq)
        fact = service.record_fact(
            route_id="owned:synth",
            capability="instrument.play",
            access_kind="INSTRUMENT",
            entitlement_state="GRANTED",
            permission_state="NOT_REQUIRED",
            observed_at_epoch_seconds=10,
            source_kind="USER_DECLARED",
        )
        snapshot = service.snapshot(
            route_id="owned:synth",
            capability="instrument.play",
            access_kind="INSTRUMENT",
            as_of_epoch_seconds=20,
        )
        assert fact.source_truth_class == "DECLARED"
        assert snapshot.resolution_status == "RESOLVED"
        assert snapshot.validity_state == "CURRENT"
        assert snapshot.entitlement_state == "GRANTED"
        assert snapshot.strongest_source_class == "DECLARED"
        assert snapshot.strong_access_evidence is False
        assert snapshot.eligibility_entitlement_state == "UNKNOWN"
        assert snapshot.eligibility_permission_state == "UNKNOWN"
    finally:
        hq.close()


def test_service_cannot_self_mint_provider_verified_entitlement(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Provider Boundary Artist")
    try:
        with pytest.raises(EntitlementTruthError, match="cannot self-mint"):
            _service(hq).record_fact(
                route_id="provider:master",
                capability="audio.master",
                access_kind="PROVIDER_PLAN",
                entitlement_state="GRANTED",
                observed_at_epoch_seconds=10,
                source_kind="PROVIDER_VERIFIED",
                source_ref="provider:receipt",
            )
    finally:
        hq.close()


def test_canonical_provider_verified_claim_is_consumed_without_upgrading_source(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Verified Plan Artist")
    try:
        service = _service(hq)
        claim = _provider_claim(hq, service)
        fact = service.consume_claim(claim.id)
        snapshot = service.snapshot(
            route_id="provider:cloud-master",
            capability="audio.master",
            access_kind="PROVIDER_PLAN",
            as_of_epoch_seconds=110,
        )
        assert fact.claim_id == claim.id
        assert fact.source_truth_class == "PROVIDER_VERIFIED"
        assert snapshot.provider_verified is True
        assert snapshot.strong_access_evidence is True
        assert snapshot.entitlement_state == "GRANTED"
        assert snapshot.permission_state == "NOT_REQUIRED"
        assert snapshot.eligibility_entitlement_state == "GRANTED"
        assert snapshot.eligibility_permission_state == "NOT_REQUIRED"
        assert snapshot.execution_authority_granted is False
    finally:
        hq.close()


def test_observed_access_requires_provenance_and_inference_cannot_verify_grant(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Observed Access Artist")
    try:
        service = _service(hq)
        with pytest.raises(EntitlementTruthError, match="requires source_ref"):
            service.record_fact(
                route_id="host:feature",
                capability="daw.feature.comping",
                access_kind="DAW_FEATURE",
                entitlement_state="GRANTED",
                observed_at_epoch_seconds=10,
                source_kind="OBSERVED",
            )

        service.record_fact(
            route_id="host:feature",
            capability="daw.feature.comping",
            access_kind="DAW_FEATURE",
            entitlement_state="GRANTED",
            observed_at_epoch_seconds=10,
            source_kind="INFERRED",
            source_ref="inference:edition-name",
        )
        inferred = service.snapshot(
            route_id="host:feature",
            capability="daw.feature.comping",
            access_kind="DAW_FEATURE",
            as_of_epoch_seconds=20,
        )
        assert inferred.entitlement_state == "GRANTED"
        assert inferred.strongest_source_class == "INFERRED"
        assert inferred.eligibility_entitlement_state == "UNKNOWN"
    finally:
        hq.close()


def test_expiry_is_distinct_from_unknown_and_does_not_leave_verified_access(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Trial Artist")
    try:
        service = _service(hq)
        service.record_fact(
            route_id="plugin:trial",
            capability="instrument.play",
            access_kind="TRIAL",
            entitlement_state="GRANTED",
            observed_at_epoch_seconds=100,
            expires_at_epoch_seconds=200,
            source_kind="OBSERVED",
            source_ref="activation:trial-window",
        )
        current = service.snapshot(
            route_id="plugin:trial",
            capability="instrument.play",
            access_kind="TRIAL",
            as_of_epoch_seconds=199,
        )
        expired = service.snapshot(
            route_id="plugin:trial",
            capability="instrument.play",
            access_kind="TRIAL",
            as_of_epoch_seconds=200,
        )
        assert current.validity_state == "CURRENT"
        assert current.eligibility_entitlement_state == "GRANTED"
        assert expired.validity_state == "EXPIRED"
        assert expired.resolution_status == "RESOLVED"
        assert expired.entitlement_state == "UNKNOWN"
        assert expired.eligibility_entitlement_state == "UNKNOWN"
        assert expired.strong_access_evidence is False
    finally:
        hq.close()


def test_conflicting_current_evidence_stays_conflict_until_explicit_supersession(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Conflict Artist")
    try:
        service = _service(hq)
        declared = service.record_fact(
            route_id="owned:license",
            capability="audio.process",
            access_kind="LICENSE",
            entitlement_state="GRANTED",
            observed_at_epoch_seconds=10,
            source_kind="USER_DECLARED",
        )
        denied = service.record_fact(
            route_id="owned:license",
            capability="audio.process",
            access_kind="LICENSE",
            entitlement_state="DENIED",
            observed_at_epoch_seconds=20,
            source_kind="OBSERVED",
            source_ref="license-manager:inactive",
        )
        conflict = service.snapshot(
            route_id="owned:license",
            capability="audio.process",
            access_kind="LICENSE",
            as_of_epoch_seconds=30,
        )
        assert conflict.resolution_status == "CONFLICT"
        assert set(conflict.active_fact_ids) == {declared.claim_id, denied.claim_id}
        assert conflict.entitlement_state == "UNKNOWN"
        assert conflict.eligibility_entitlement_state == "UNKNOWN"

        replacement = service.record_fact(
            route_id="owned:license",
            capability="audio.process",
            access_kind="LICENSE",
            entitlement_state="GRANTED",
            observed_at_epoch_seconds=40,
            source_kind="OBSERVED",
            source_ref="license-manager:activated",
            supersedes=(declared.claim_id, denied.claim_id),
        )
        resolved = service.snapshot(
            route_id="owned:license",
            capability="audio.process",
            access_kind="LICENSE",
            as_of_epoch_seconds=50,
        )
        assert resolved.resolution_status == "RESOLVED"
        assert resolved.active_fact_ids == (replacement.claim_id,)
        assert resolved.entitlement_state == "GRANTED"
        assert resolved.eligibility_entitlement_state == "GRANTED"
    finally:
        hq.close()


def test_permission_is_separate_from_entitlement_and_denial_fails_closed(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Permission Artist")
    try:
        service = _service(hq)
        _provider_claim(
            hq,
            service,
            route_id="provider:publish",
            capability="release.submit",
            access_kind="PERMISSION",
            entitlement_state="GRANTED",
            permission_state="DENIED",
        )
        snapshot = service.snapshot(
            route_id="provider:publish",
            capability="release.submit",
            access_kind="PERMISSION",
            as_of_epoch_seconds=110,
        )
        assert snapshot.entitlement_state == "GRANTED"
        assert snapshot.permission_state == "DENIED"
        assert snapshot.eligibility_entitlement_state == "GRANTED"
        assert snapshot.eligibility_permission_state == "DENIED"
        assert snapshot.provider_write_authority_granted is False
        assert snapshot.external_action_authority_granted is False
    finally:
        hq.close()


def test_quota_truth_is_explicit_and_conflicting_quota_does_not_authorize_spend(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Quota Artist")
    try:
        service = _service(hq)
        first = service.record_fact(
            route_id="provider:generate",
            capability="audio.generate",
            access_kind="PROVIDER_QUOTA",
            entitlement_state="GRANTED",
            observed_at_epoch_seconds=10,
            quota_remaining=25,
            quota_unit="credits",
            source_kind="MEASURED",
            source_ref="provider-meter:poll-1",
        )
        single = service.snapshot(
            route_id="provider:generate",
            capability="audio.generate",
            access_kind="PROVIDER_QUOTA",
            as_of_epoch_seconds=11,
        )
        assert single.quota_status == "RESOLVED"
        assert single.quota_remaining == 25.0
        assert single.quota_unit == "credits"
        assert single.entitlement_state == "GRANTED"
        assert single.eligibility_entitlement_state == "UNKNOWN"
        assert single.quota_spend_authority_granted is False

        service.record_fact(
            route_id="provider:generate",
            capability="audio.generate",
            access_kind="PROVIDER_QUOTA",
            entitlement_state="GRANTED",
            observed_at_epoch_seconds=12,
            quota_remaining=24,
            quota_unit="credits",
            source_kind="MEASURED",
            source_ref="provider-meter:poll-2",
        )
        conflict = service.snapshot(
            route_id="provider:generate",
            capability="audio.generate",
            access_kind="PROVIDER_QUOTA",
            as_of_epoch_seconds=13,
        )
        assert first.claim_id in conflict.active_fact_ids
        assert conflict.quota_status == "CONFLICT"
        assert conflict.resolution_status == "CONFLICT"
        assert conflict.quota_remaining is None
        assert conflict.quota_spend_authority_granted is False
    finally:
        hq.close()


def test_environment_binding_is_preserved_as_evidence_not_execution_authority(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Activation Artist")
    try:
        service = _service(hq)
        fact = service.record_fact(
            route_id="host:edition",
            capability="daw.feature.surround",
            access_kind="ACTIVATION",
            entitlement_state="GRANTED",
            observed_at_epoch_seconds=100,
            environment_fingerprint="host-fingerprint-123",
            source_kind="OBSERVED",
            source_ref="activation-probe:local",
        )
        snapshot = service.snapshot(
            route_id="host:edition",
            capability="daw.feature.surround",
            access_kind="ACTIVATION",
            as_of_epoch_seconds=110,
        )
        assert fact.environment_fingerprint == "host-fingerprint-123"
        assert snapshot.eligibility_entitlement_state == "GRANTED"
        assert snapshot.execution_authority_granted is False
        assert snapshot.activation_authority_granted is False
    finally:
        hq.close()


def test_snapshot_fingerprint_detects_new_or_superseding_evidence(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Snapshot Artist")
    try:
        service = _service(hq)
        original = service.record_fact(
            route_id="owned:tool",
            capability="audio.process",
            access_kind="PLUGIN",
            entitlement_state="GRANTED",
            observed_at_epoch_seconds=10,
            source_kind="OBSERVED",
            source_ref="license-probe:1",
        )
        snapshot = service.snapshot(
            route_id="owned:tool",
            capability="audio.process",
            access_kind="PLUGIN",
            as_of_epoch_seconds=20,
        )
        service.assert_current(snapshot)
        service.record_fact(
            route_id="owned:tool",
            capability="audio.process",
            access_kind="PLUGIN",
            entitlement_state="DENIED",
            observed_at_epoch_seconds=30,
            source_kind="OBSERVED",
            source_ref="license-probe:2",
            supersedes=(original.claim_id,),
        )
        with pytest.raises(EntitlementTruthError, match="stale or was modified"):
            service.assert_current(snapshot)
    finally:
        hq.close()


def test_malformed_owned_namespace_provider_claim_fails_closed(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Malformed Entitlement Artist")
    try:
        service = _service(hq)
        key = service.claim_key(
            route_id="provider:bad",
            capability="audio.generate",
            access_kind="PROVIDER_PLAN",
        )
        hq.evidence.record_claim(
            scope_kind="PROFILE",
            scope_id=hq.store.profile_id,
            key=key,
            value={"schema_version": 1, "route_id": "provider:bad"},
            source_kind="PROVIDER_VERIFIED",
            source_ref="provider:bad-receipt",
            confidence=1.0,
            twin_domain="TECHNICAL",
        )
        with pytest.raises(LineageCorruptionError, match="payload shape"):
            service.snapshot(
                route_id="provider:bad",
                capability="audio.generate",
                access_kind="PROVIDER_PLAN",
                as_of_epoch_seconds=100,
            )
    finally:
        hq.close()


def test_provider_claim_without_provenance_fails_closed_when_consumed(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Missing Provenance Artist")
    try:
        service = _service(hq)
        payload = service.evidence_payload(
            route_id="provider:no-ref",
            capability="audio.generate",
            access_kind="PROVIDER_PLAN",
            entitlement_state="GRANTED",
            observed_at_epoch_seconds=10,
        )
        claim = hq.evidence.record_claim(
            scope_kind="PROFILE",
            scope_id=hq.store.profile_id,
            key=service.claim_key(
                route_id="provider:no-ref",
                capability="audio.generate",
                access_kind="PROVIDER_PLAN",
            ),
            value=payload,
            source_kind="PROVIDER_VERIFIED",
            source_ref=None,
            confidence=1.0,
            twin_domain="TECHNICAL",
        )
        with pytest.raises(LineageCorruptionError, match="requires source_ref"):
            service.consume_claim(claim.id)
    finally:
        hq.close()


def test_non_profile_entitlement_claim_cannot_be_consumed_as_profile_truth(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Scope Artist")
    try:
        service = _service(hq)
        payload = service.evidence_payload(
            route_id="owned:scope",
            capability="audio.process",
            access_kind="LICENSE",
            entitlement_state="GRANTED",
            observed_at_epoch_seconds=10,
        )
        claim = hq.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=hq.store.primary_artist_id,
            key=service.claim_key(
                route_id="owned:scope",
                capability="audio.process",
                access_kind="LICENSE",
            ),
            value=payload,
            source_kind="USER_DECLARED",
            confidence=1.0,
            twin_domain="TECHNICAL",
        )
        with pytest.raises(LineageCorruptionError, match="active profile"):
            service.consume_claim(claim.id)
        snapshot = service.snapshot(
            route_id="owned:scope",
            capability="audio.process",
            access_kind="LICENSE",
            as_of_epoch_seconds=20,
        )
        assert snapshot.resolution_status == "UNKNOWN"
    finally:
        hq.close()


def test_entitlement_truth_survives_restart_without_parallel_database(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Restart Entitlement Artist")
    profile_id = hq.store.profile_id
    try:
        service = _service(hq)
        fact = service.record_fact(
            route_id="owned:restart",
            capability="audio.process",
            access_kind="LICENSE",
            entitlement_state="GRANTED",
            observed_at_epoch_seconds=10,
            source_kind="OBSERVED",
            source_ref="license-probe:restart",
        )
        claim_id = fact.claim_id
    finally:
        hq.close()

    reopened = HeadquartersMemory.open(root, profile_id)
    try:
        service = _service(reopened)
        restored = service.snapshot(
            route_id="owned:restart",
            capability="audio.process",
            access_kind="LICENSE",
            as_of_epoch_seconds=20,
        )
        assert restored.active_fact_ids == (claim_id,)
        assert restored.entitlement_state == "GRANTED"
        assert restored.eligibility_entitlement_state == "GRANTED"
        tables = {
            str(row["name"])
            for row in reopened.store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert not any(name.startswith("entitlement") for name in tables)
    finally:
        reopened.close()


def test_canonical_evidence_immutability_prevents_entitlement_rewrite(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Immutable Access Artist")
    try:
        fact = _service(hq).record_fact(
            route_id="owned:immutable",
            capability="audio.process",
            access_kind="LICENSE",
            entitlement_state="GRANTED",
            observed_at_epoch_seconds=10,
            source_kind="USER_DECLARED",
        )
        with pytest.raises(sqlite3.IntegrityError, match="evidence claims are immutable"):
            with hq.store._tx():
                hq.store._conn.execute(
                    "UPDATE evidence_claims SET value_json='{}' WHERE id=?",
                    (fact.claim_id,),
                )
    finally:
        hq.close()


def test_future_observation_is_not_current_before_its_observed_time(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Temporal Access Artist")
    try:
        service = _service(hq)
        fact = service.record_fact(
            route_id="owned:future-license",
            capability="audio.process",
            access_kind="LICENSE",
            entitlement_state="GRANTED",
            observed_at_epoch_seconds=200,
            source_kind="OBSERVED",
            source_ref="license-probe:future",
        )
        before = service.snapshot(
            route_id="owned:future-license",
            capability="audio.process",
            access_kind="LICENSE",
            as_of_epoch_seconds=199,
        )
        at_observation = service.snapshot(
            route_id="owned:future-license",
            capability="audio.process",
            access_kind="LICENSE",
            as_of_epoch_seconds=200,
        )
        assert fact.claim_id in before.active_fact_ids
        assert before.validity_state == "UNKNOWN"
        assert before.resolution_status == "UNKNOWN"
        assert before.entitlement_state == "UNKNOWN"
        assert before.strong_access_evidence is False
        assert before.eligibility_entitlement_state == "UNKNOWN"
        assert at_observation.validity_state == "CURRENT"
        assert at_observation.entitlement_state == "GRANTED"
        assert at_observation.eligibility_entitlement_state == "GRANTED"
    finally:
        hq.close()


def test_supersession_time_cannot_regress_and_external_regression_fails_closed(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Regressing Access Artist")
    try:
        service = _service(hq)
        original = service.record_fact(
            route_id="owned:ordered-license",
            capability="audio.process",
            access_kind="LICENSE",
            entitlement_state="GRANTED",
            observed_at_epoch_seconds=20,
            source_kind="OBSERVED",
            source_ref="license-probe:20",
        )
        with pytest.raises(EntitlementTruthError, match="cannot regress"):
            service.record_fact(
                route_id="owned:ordered-license",
                capability="audio.process",
                access_kind="LICENSE",
                entitlement_state="DENIED",
                observed_at_epoch_seconds=10,
                source_kind="OBSERVED",
                source_ref="license-probe:10",
                supersedes=(original.claim_id,),
            )
        still_current = service.snapshot(
            route_id="owned:ordered-license",
            capability="audio.process",
            access_kind="LICENSE",
            as_of_epoch_seconds=30,
        )
        assert still_current.active_fact_ids == (original.claim_id,)
        assert still_current.entitlement_state == "GRANTED"

        provider_newer = _provider_claim(
            hq,
            service,
            route_id="provider:ordered-plan",
            capability="audio.generate",
            access_kind="PROVIDER_PLAN",
            observed_at_epoch_seconds=40,
            source_ref="provider-receipt:40",
        )
        _provider_claim(
            hq,
            service,
            route_id="provider:ordered-plan",
            capability="audio.generate",
            access_kind="PROVIDER_PLAN",
            entitlement_state="DENIED",
            observed_at_epoch_seconds=30,
            source_ref="provider-receipt:30",
            supersedes=(provider_newer.id,),
        )
        with pytest.raises(LineageCorruptionError, match="observation time regressed"):
            service.snapshot(
                route_id="provider:ordered-plan",
                capability="audio.generate",
                access_kind="PROVIDER_PLAN",
                as_of_epoch_seconds=50,
            )
    finally:
        hq.close()
