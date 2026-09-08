import unittest
from dataclasses import replace

from n0te.eligibility import (
    EligibilityError,
    ExecutionEligibilityDecision,
    ExecutionEligibilityEvidence,
    ExecutionEligibilityGate,
    ExecutionEligibilityRequest,
)


def request():
    return ExecutionEligibilityRequest(job_id="job:publish-master",route_id="route:distribution",subject_id="provider:distribution/account:artist",capability="release.publish",environment_fingerprint="env:provider-v3:scope-publish",max_evidence_age_seconds=300)


def evidence(**overrides):
    values=dict(job_id="job:publish-master",route_id="route:distribution",subject_id="provider:distribution/account:artist",capability="release.publish",environment_fingerprint="env:provider-v3:scope-publish",evidence_fingerprint="sha256:eligibility-evidence-v1",evidence_ref="provider-capability-check:123",verified=True,entitlement_state="GRANTED",permission_state="GRANTED",evidence_age_seconds=10); values.update(overrides); return ExecutionEligibilityEvidence(**values)


class Core04EExecutionEligibilityTests(unittest.TestCase):
    def test_exact_fresh_verified_evidence_allows_without_granting_authority(self):
        d=ExecutionEligibilityGate.evaluate(request(),evidence()); self.assertEqual(d.status,"ALLOW"); self.assertFalse(d.action_authority_granted); self.assertIn("CAPABILITY_VERIFIED",d.reason_codes); self.assertIn("EVIDENCE_FRESH",d.reason_codes)
    def test_explicit_not_required_entitlement_and_permission_are_allowed(self):
        d=ExecutionEligibilityGate.evaluate(request(),evidence(entitlement_state="NOT_REQUIRED",permission_state="NOT_REQUIRED")); self.assertEqual(d.status,"ALLOW"); self.assertIn("ENTITLEMENT_NOT_REQUIRED",d.reason_codes); self.assertIn("PERMISSION_NOT_REQUIRED",d.reason_codes)
    def test_revoked_or_unknown_access_denies(self):
        for field,value,reason in (("entitlement_state","DENIED","ENTITLEMENT_DENIED"),("entitlement_state","UNKNOWN","ENTITLEMENT_UNKNOWN"),("permission_state","DENIED","PERMISSION_DENIED"),("permission_state","UNKNOWN","PERMISSION_UNKNOWN")):
            with self.subTest(field=field,value=value):
                d=ExecutionEligibilityGate.evaluate(request(),evidence(**{field:value})); self.assertEqual(d.status,"DENY"); self.assertIn(reason,d.reason_codes)
    def test_unverified_capability_denies(self):
        d=ExecutionEligibilityGate.evaluate(request(),evidence(verified=False)); self.assertEqual(d.status,"DENY"); self.assertIn("CAPABILITY_NOT_VERIFIED",d.reason_codes)
    def test_expired_or_changed_environment_is_stale(self):
        expired=ExecutionEligibilityGate.evaluate(request(),evidence(evidence_age_seconds=301)); self.assertEqual(expired.status,"STALE"); self.assertIn("EVIDENCE_EXPIRED",expired.reason_codes)
        changed=ExecutionEligibilityGate.evaluate(request(),evidence(environment_fingerprint="env:new-version")); self.assertEqual(changed.status,"STALE"); self.assertIn("ENVIRONMENT_CHANGED",changed.reason_codes)
    def test_wrong_identity_dimensions_deny(self):
        for field,value,reason in (("job_id","job:other","JOB_MISMATCH"),("route_id","route:other","ROUTE_MISMATCH"),("subject_id","provider:other","SUBJECT_MISMATCH"),("capability","release.read","CAPABILITY_MISMATCH")):
            with self.subTest(field=field):
                d=ExecutionEligibilityGate.evaluate(request(),evidence(**{field:value})); self.assertEqual(d.status,"DENY"); self.assertIn(reason,d.reason_codes)
    def test_deny_dominates_stale_but_preserves_stale_reasons(self):
        d=ExecutionEligibilityGate.evaluate(request(),evidence(verified=False,environment_fingerprint="env:changed",evidence_age_seconds=999)); self.assertEqual(d.status,"DENY"); self.assertIn("CAPABILITY_NOT_VERIFIED",d.reason_codes); self.assertIn("ENVIRONMENT_CHANGED",d.reason_codes); self.assertIn("EVIDENCE_EXPIRED",d.reason_codes)
    def test_bool_and_age_types_are_strict(self):
        with self.assertRaises(TypeError): evidence(verified="false")
        with self.assertRaises(TypeError): evidence(evidence_age_seconds=True)
        with self.assertRaises(TypeError): ExecutionEligibilityRequest(job_id="j",route_id="r",subject_id="s",capability="c",environment_fingerprint="e",max_evidence_age_seconds=True)
    def test_states_and_age_budget_are_validated(self):
        with self.assertRaises(EligibilityError): evidence(entitlement_state="MAYBE")
        with self.assertRaises(EligibilityError): evidence(evidence_age_seconds=-1)
        with self.assertRaises(EligibilityError): replace(request(),max_evidence_age_seconds=0)
    def test_evaluation_is_pure_deterministic_and_has_no_execution_verbs(self):
        req=request(); ev=evidence(); self.assertEqual(ExecutionEligibilityGate.evaluate(req,ev),ExecutionEligibilityGate.evaluate(req,ev)); public={n for n in dir(ExecutionEligibilityGate) if not n.startswith("_") and callable(getattr(ExecutionEligibilityGate,n))}; self.assertEqual(public,{"evaluate"})
        for forbidden in ("execute","send","connect","refresh_token","scan","purchase"): self.assertNotIn(forbidden,public)
        with self.assertRaises(EligibilityError): ExecutionEligibilityDecision(status="ALLOW",job_id="j",route_id="r",subject_id="s",capability="c",evidence_fingerprint="f",evidence_ref="e",reason_codes=("OK",),action_authority_granted=True)


if __name__ == "__main__": unittest.main()
