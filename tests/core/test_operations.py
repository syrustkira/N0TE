import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from n0te import (
    ActionIntent,
    AuthorityService,
    DuplicateExecutionError,
    HeadquartersMemory,
    LineageCorruptionError,
    NetworkPolicy,
    NetworkRoute,
    OperationError,
    ValidationError,
)
from n0te.eligibility import (
    ExecutionEligibilityEvidence,
    ExecutionEligibilityGate,
    ExecutionEligibilityRequest,
)


def local_intent(version_id: str) -> ActionIntent:
    return ActionIntent(action_id="action:local:rename-version",job_id="job:rename-version",action_class="REVERSIBLE",description="Rename this exact local Song version",target_ref=f"version:{version_id}",revision_fingerprint="sha256:revision-v1",payload_fingerprint="sha256:rename-payload-v1")


def outbound_intent(version_id: str) -> ActionIntent:
    return ActionIntent(action_id="action:provider:publish",job_id="job:publish-master",action_class="IRREVERSIBLE",description="Publish this exact approved master",target_ref=f"version:{version_id}",revision_fingerprint="sha256:revision-v1",payload_fingerprint="sha256:master-payload-v1",destination="provider:distribution:selected-release",purpose="Publish this exact approved master",data_categories=("MASTER_AUDIO","RELEASE_METADATA"))


class Core04DOperationJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name); self.hq=HeadquartersMemory.create(self.root,"Artist"); self.profile_id=self.hq.store.profile_id
        self.song=self.hq.store.create_song("Journal Song"); self.version=self.hq.store.create_version(self.song.id,label="v1"); self.intent=local_intent(self.version.id); self.approval=AuthorityService.bind_approval(self.intent,"artist-confirmation:operation:1")
    def tearDown(self):
        try: self.hq.close()
        except Exception: pass
        self.temp.cleanup()
    def prepare(self,key="idem:local:1"):
        return self.hq.operations.prepare(idempotency_key=key,intent=self.intent,approval=self.approval,song_id=self.song.id,version_id=self.version.id)
    def claim(self,operation_id):
        return self.hq.operations.claim_execution(operation_id,intent=self.intent,approval=self.approval,claim_evidence_ref="execution-gate:local:1")

    def test_prepare_is_durable_and_idempotent_for_exact_same_identity(self):
        first=self.prepare(); self.assertEqual(first,self.prepare()); self.assertEqual(first.recorded_state,"PREPARED"); self.assertEqual(first.attempt_count,0); self.assertIsNone(first.effective_outcome); self.assertIsNone(first.transport_route_id)
        self.hq.close(); self.hq=HeadquartersMemory.open(self.root,self.profile_id); self.assertEqual(self.hq.operations.get(first.operation_id),first); self.assertEqual(self.hq.operations.by_idempotency_key("idem:local:1"),first)

    def test_prepare_rejects_stale_approval_and_key_reuse_for_different_operation(self):
        self.prepare(); changed=replace(self.intent,payload_fingerprint="sha256:different")
        with self.assertRaises(OperationError): self.hq.operations.prepare(idempotency_key="idem:stale",intent=changed,approval=self.approval)
        changed_approval=AuthorityService.bind_approval(changed,"artist-confirmation:operation:2")
        with self.assertRaises(OperationError): self.hq.operations.prepare(idempotency_key="idem:local:1",intent=changed,approval=changed_approval)

    def test_local_operation_cannot_bind_outbound_eligibility_identity(self):
        with self.assertRaises(OperationError): self.hq.operations.prepare(idempotency_key="idem:local-with-route",intent=self.intent,approval=self.approval,transport_route_id="route:internet",eligibility_subject_id="provider:x",eligibility_capability="x")

    def test_exact_operation_can_be_claimed_only_once(self):
        prepared=self.prepare(); claimed=self.claim(prepared.operation_id); self.assertEqual(claimed.recorded_state,"EXECUTING"); self.assertEqual(claimed.attempt_count,1)
        with self.assertRaises(DuplicateExecutionError): self.claim(prepared.operation_id)
        self.assertEqual([e.event_type for e in self.hq.operations.events(prepared.operation_id)],["PREPARED","EXECUTION_CLAIMED"])

    def test_outbound_claim_requires_exact_transport_and_fresh_eligibility(self):
        intent=outbound_intent(self.version.id); approval=AuthorityService.bind_approval(intent,"artist:publish:approval"); subject="provider:distribution/account:artist"; capability="release.publish"; environment="env:provider-v3:scope-publish"
        with self.assertRaises(OperationError): self.hq.operations.prepare(idempotency_key="idem:publish:missing-eligibility",intent=intent,approval=approval,transport_route_id="route:distribution")
        op=self.hq.operations.prepare(idempotency_key="idem:publish:1",intent=intent,approval=approval,song_id=self.song.id,version_id=self.version.id,transport_route_id="route:distribution",eligibility_subject_id=subject,eligibility_capability=capability)
        self.assertEqual((op.transport_route_id,op.eligibility_subject_id,op.eligibility_capability),("route:distribution",subject,capability))
        transport=NetworkPolicy("CONNECTED").evaluate(NetworkRoute("route:distribution","INTERNET","Distribution provider"))
        req=ExecutionEligibilityRequest(job_id=intent.job_id,route_id="route:distribution",subject_id=subject,capability=capability,environment_fingerprint=environment,max_evidence_age_seconds=300)
        ev=ExecutionEligibilityEvidence(job_id=intent.job_id,route_id="route:distribution",subject_id=subject,capability=capability,environment_fingerprint=environment,evidence_fingerprint="sha256:eligibility-v1",evidence_ref="provider-capability-check:1",verified=True,entitlement_state="GRANTED",permission_state="GRANTED",evidence_age_seconds=10)
        eligibility=ExecutionEligibilityGate.evaluate(req,ev)
        with self.assertRaises(OperationError): self.hq.operations.claim_execution(op.operation_id,intent=intent,approval=approval,claim_evidence_ref="missing",transport_decision=transport)
        stale=ExecutionEligibilityGate.evaluate(req,replace(ev,evidence_age_seconds=301)); self.assertEqual(stale.status,"STALE")
        with self.assertRaises(OperationError): self.hq.operations.claim_execution(op.operation_id,intent=intent,approval=approval,claim_evidence_ref="stale",transport_decision=transport,eligibility_decision=stale)
        wrong_subject=ExecutionEligibilityGate.evaluate(replace(req,subject_id="provider:other"),replace(ev,subject_id="provider:other")); self.assertEqual(wrong_subject.status,"ALLOW")
        with self.assertRaises(OperationError): self.hq.operations.claim_execution(op.operation_id,intent=intent,approval=approval,claim_evidence_ref="wrong-subject",transport_decision=transport,eligibility_decision=wrong_subject)
        wrong_transport=NetworkPolicy("CONNECTED").evaluate(NetworkRoute("route:other","INTERNET","Other route"))
        with self.assertRaises(OperationError): self.hq.operations.claim_execution(op.operation_id,intent=intent,approval=approval,claim_evidence_ref="wrong-route",transport_decision=wrong_transport,eligibility_decision=eligibility)
        claimed=self.hq.operations.claim_execution(op.operation_id,intent=intent,approval=approval,claim_evidence_ref=eligibility.evidence_ref,transport_decision=transport,eligibility_decision=eligibility)
        self.assertEqual(claimed.recorded_state,"EXECUTING"); self.assertFalse(transport.action_authority_granted); self.assertFalse(eligibility.action_authority_granted)

    def test_idempotency_key_is_bound_to_route_subject_and_capability(self):
        intent=outbound_intent(self.version.id); approval=AuthorityService.bind_approval(intent,"artist:publish:approval")
        self.hq.operations.prepare(idempotency_key="idem:bound",intent=intent,approval=approval,transport_route_id="route:A",eligibility_subject_id="provider:A",eligibility_capability="release.publish")
        for fields in (dict(transport_route_id="route:B",eligibility_subject_id="provider:A",eligibility_capability="release.publish"),dict(transport_route_id="route:A",eligibility_subject_id="provider:B",eligibility_capability="release.publish"),dict(transport_route_id="route:A",eligibility_subject_id="provider:A",eligibility_capability="release.read")):
            with self.subTest(fields=fields):
                with self.assertRaises(OperationError): self.hq.operations.prepare(idempotency_key="idem:bound",intent=intent,approval=approval,**fields)

    def test_success_receipt_persists_and_prevents_retry(self):
        op=self.prepare(); self.claim(op.operation_id); success=self.hq.operations.complete_success(op.operation_id,receipt_ref="provider-receipt:abc",evidence_ref="provider-observation:success",result_fingerprint="sha256:provider-result")
        self.assertEqual((success.recorded_state,success.effective_outcome,success.receipt_ref),("SUCCEEDED","SUCCEEDED","provider-receipt:abc"))
        with self.assertRaises(DuplicateExecutionError): self.claim(op.operation_id)
        self.hq.close(); self.hq=HeadquartersMemory.open(self.root,self.profile_id); reopened=self.hq.operations.get(op.operation_id); self.assertEqual(reopened.receipt_ref,"provider-receipt:abc"); self.assertEqual(reopened.attempt_count,1)

    def test_unknown_never_retries_and_reconciliation_preserves_unknown_history(self):
        op=self.prepare("idem:unknown"); self.claim(op.operation_id); unknown=self.hq.operations.mark_unknown(op.operation_id,evidence_ref="timeout:ambiguous"); self.assertEqual(unknown.effective_outcome,"UNKNOWN")
        with self.assertRaises(DuplicateExecutionError): self.claim(op.operation_id)
        reconciled=self.hq.operations.reconcile_unknown(op.operation_id,observed_outcome="SUCCEEDED",evidence_ref="provider-status:confirmed",receipt_ref="provider-receipt:reconciled",result_fingerprint="sha256:result")
        self.assertEqual(reconciled.recorded_state,"UNKNOWN"); self.assertEqual(reconciled.effective_outcome,"SUCCEEDED"); self.assertTrue(reconciled.reconciled)
        self.assertEqual([e.event_type for e in self.hq.operations.events(op.operation_id)],["PREPARED","EXECUTION_CLAIMED","UNKNOWN","RECONCILED_SUCCEEDED"])

    def test_song_version_binding_cannot_cross_song(self):
        other=self.hq.store.create_song("Other Song"); other_version=self.hq.store.create_version(other.id,label="other-v1")
        with self.assertRaises(ValidationError): self.hq.operations.prepare(idempotency_key="idem:cross",intent=self.intent,approval=self.approval,song_id=self.song.id,version_id=other_version.id)

    def test_operation_identity_and_history_are_sql_immutable(self):
        op=self.prepare("idem:immutable")
        with self.assertRaises(sqlite3.IntegrityError): self.hq.store._conn.execute("UPDATE operations SET idempotency_key='changed' WHERE id=?",(op.operation_id,))
        self.hq.store._conn.rollback(); event_id=self.hq.operations.events(op.operation_id)[0].id
        with self.assertRaises(sqlite3.IntegrityError): self.hq.store._conn.execute("DELETE FROM operation_events WHERE id=?",(event_id,))
        self.hq.store._conn.rollback()

    def test_activity_records_operation_lifecycle_in_order(self):
        checkpoint=self.hq.activity.checkpoint(); op=self.prepare("idem:activity"); self.claim(op.operation_id); self.hq.operations.mark_unknown(op.operation_id,evidence_ref="ambiguous"); self.hq.operations.reconcile_unknown(op.operation_id,observed_outcome="FAILED",evidence_ref="reconcile")
        events=[e.event_type for e in self.hq.activity.for_song(self.song.id,after_sequence=checkpoint) if e.object_type=="OPERATION" and e.object_id==op.operation_id]
        self.assertEqual(events,["OPERATION_PREPARED","OPERATION_EXECUTION_CLAIMED","OPERATION_UNKNOWN","OPERATION_RECONCILED_FAILED"])

    def test_reopen_rejects_tampered_operation_event_sequence(self):
        op=self.prepare("idem:tamper"); self.hq.store._conn.execute("INSERT INTO operation_events(id,operation_id,event_type,evidence_ref) VALUES('opevt_tampered',?,'PREPARED','tamper')",(op.operation_id,)); self.hq.store._conn.commit(); self.hq.close()
        with self.assertRaises(LineageCorruptionError): HeadquartersMemory.open(self.root,self.profile_id)


if __name__ == "__main__": unittest.main()
