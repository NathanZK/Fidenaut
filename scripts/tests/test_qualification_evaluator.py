import datetime
import base64
import hashlib
import json
import os
import pathlib
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import agent_workflow
from scripts import copilot_adapter
from scripts import qualification_evaluator


NOW = datetime.datetime(2026, 10, 8, 12, 0, tzinfo=datetime.timezone.utc)
CLAIM_IDS = [
    "U1.fresh_context_creation",
    "U1.history_session",
    "U1.workspace_filesystem",
    "U1.tools_processes",
    "U1.credentials_environment",
    "U1.shared_state",
    "U3.exact_resume",
    "U3.continuity",
]


def canonical_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sign(private_key, domain, value):
    signature = private_key.sign(
        domain + b"\n" + hashlib.sha256(canonical_bytes(value)).digest()
    )
    return base64.b64encode(signature).decode("ascii")


def public_bytes(private_key):
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def artifact(root, path, content):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return {
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "byte_length": len(content),
    }


def fixture(root):
    authority_key = Ed25519PrivateKey.generate()
    verifier_key = Ed25519PrivateKey.generate()
    contexts = [
        {
            "role": role,
            "context_id": "context-" + role,
            "provider_session_id": "session-" + role,
            "workspace_id": "workspace-" + role,
            "credential_assignment_id": "credentials-" + role,
        }
        for role in ("planner", "reviewer")
    ]
    policy = {
        "schema": "fidenaut-provider-qualification-policy-v1",
        "qualification_id": "qualification-1",
        "binding": {
            "deployment_id": "deployment-1",
            "provider": {
                "name": "provider",
                "version": "1",
                "adapter_revision": "revision-1",
            },
            "window": {
                "not_before": "2026-10-08T00:00:00Z",
                "not_after": "2026-10-09T00:00:00Z",
            },
            "contexts": contexts,
        },
        "validity": {
            "not_before": "2026-10-01T00:00:00Z",
            "not_after": "2026-11-01T00:00:00Z",
            "max_evidence_age_seconds": 3600,
        },
        "shared_inputs": [],
        "authority": {"id": "authority-1", "key_id": "authority-key"},
        "verifier": {
            "id": "verifier-1",
            "key_id": "verifier-key",
            "independence_approved": True,
        },
        "provider_key_ids": [],
        "boundary": {
            "id": "boundary-1",
            "approved": True,
            "descriptor_path": "boundary.json",
            "descriptor_sha256": "",
        },
        "u3_signal": {
            "id": "signal-1",
            "approved": True,
            "criteria_path": "criteria.json",
            "criteria_sha256": "",
        },
    }
    boundary = b'{"boundary":"test-only"}'
    criteria = b'{"criteria":"test-only"}'
    policy["boundary"]["descriptor_sha256"] = hashlib.sha256(boundary).hexdigest()
    policy["u3_signal"]["criteria_sha256"] = hashlib.sha256(criteria).hexdigest()
    artifact(root, "boundary.json", boundary)
    artifact(root, "criteria.json", criteria)
    unsigned_policy = dict(policy)
    policy["signature"] = {
        "algorithm": "Ed25519",
        "key_id": "authority-key",
        "value": sign(authority_key, b"fidenaut-policy-v1", unsigned_policy),
    }
    policy_bytes = canonical_bytes(policy)
    evidence = {
        "schema": "fidenaut-provider-qualification-evidence-v1",
        "qualification_id": policy["qualification_id"],
        "policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
        "binding": json.loads(canonical_bytes(policy["binding"])),
        "captured_at": "2026-10-08T11:45:00Z",
        "valid_until": "2026-10-08T12:45:00Z",
        "claims": [],
        "qualification_approval": None,
    }
    subject_context_ids = [context["context_id"] for context in contexts]
    subject_session_ids = [
        context["provider_session_id"] for context in contexts
    ]
    for claim_id in CLAIM_IDS:
        claim = {
            "claim_id": claim_id,
            "status": "PASS",
            "source_kind": "independent_verifier",
            "observer_id": "verifier-1",
            "observed_at": "2026-10-08T11:40:00Z",
            "valid_until": "2026-10-08T12:30:00Z",
            "subject_context_ids": subject_context_ids,
            "subject_provider_session_ids": subject_session_ids,
            "artifacts": [
                artifact(
                    root,
                    "claims/" + claim_id.replace(".", "-") + ".json",
                    claim_id.encode("ascii"),
                )
            ],
        }
        if claim_id == "U3.continuity":
            claim.update(
                {
                    "signal_id": "signal-1",
                    "criteria_sha256": policy["u3_signal"]["criteria_sha256"],
                    "stability": "STABLE",
                    "out_of_band_state": "NONE",
                    "observations": [],
                }
            )
            for context in contexts:
                predecessor = None
                for sequence in range(2):
                    observed_at = (
                        "2026-10-08T11:10:00Z"
                        if sequence == 0
                        else "2026-10-08T11:20:00Z"
                    )
                    content = (
                        context["context_id"] + ":" + str(sequence)
                    ).encode("ascii")
                    signal_artifact = artifact(
                        root,
                        "signals/{}-{}.json".format(
                            context["context_id"], sequence
                        ),
                        content,
                    )
                    signal_digest = signal_artifact["sha256"]
                    claim["observations"].append(
                        {
                            "sequence": sequence,
                            "observed_at": observed_at,
                            "context_id": context["context_id"],
                            "provider_session_id": context[
                                "provider_session_id"
                            ],
                            "workspace_id": context["workspace_id"],
                            "signal_sha256": signal_digest,
                            "predecessor_sha256": predecessor,
                            "signal_artifact": signal_artifact,
                        }
                    )
                    predecessor = signal_digest
        evidence["claims"].append(claim)
    manifest = dict(evidence)
    verifier_payload = dict(manifest)
    verifier_payload.pop("qualification_approval")
    evidence["verifier_signature"] = {
        "algorithm": "Ed25519",
        "key_id": "verifier-key",
        "value": sign(
            verifier_key, b"fidenaut-evidence-v1", verifier_payload
        ),
    }
    manifest_bytes = canonical_bytes(verifier_payload)
    binding_digest = hashlib.sha256(canonical_bytes(policy["binding"])).hexdigest()
    approval = {
        "status": "APPROVED",
        "authority_id": "authority-1",
        "key_id": "authority-key",
        "approved_at": "2026-10-08T11:50:00Z",
        "policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "binding_sha256": binding_digest,
        "outcome": "qualified",
    }
    evidence["qualification_approval"] = dict(
        approval,
        signature={
            "algorithm": "Ed25519",
            "key_id": "authority-key",
            "value": sign(
                authority_key,
                b"fidenaut-approval-v1",
                approval,
            ),
        },
    )
    trusted_keys = {
        ("authority", "authority-key"): public_bytes(authority_key),
        ("independent_verifier", "verifier-key"): public_bytes(verifier_key),
    }
    return (
        policy,
        evidence,
        policy_bytes,
        canonical_bytes(evidence),
        trusted_keys,
        authority_key,
        verifier_key,
    )


def resign_bundle(policy, evidence, authority_key, verifier_key):
    unsigned_policy = dict(policy)
    unsigned_policy.pop("signature", None)
    policy["signature"] = {
        "algorithm": "Ed25519",
        "key_id": policy["authority"]["key_id"],
        "value": sign(authority_key, b"fidenaut-policy-v1", unsigned_policy),
    }
    policy_bytes = canonical_bytes(policy)
    evidence["policy_sha256"] = hashlib.sha256(policy_bytes).hexdigest()
    evidence.pop("verifier_signature", None)
    manifest = dict(evidence)
    manifest.pop("qualification_approval", None)
    manifest_digest = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    evidence["verifier_signature"] = {
        "algorithm": "Ed25519",
        "key_id": policy["verifier"]["key_id"],
        "value": base64.b64encode(
            verifier_key.sign(
                b"fidenaut-evidence-v1" + b"\n" + bytes.fromhex(manifest_digest)
            )
        ).decode("ascii"),
    }
    approval = evidence.get("qualification_approval")
    if approval is not None:
        approval = dict(approval)
        approval.pop("signature", None)
        approval["authority_id"] = policy["authority"]["id"]
        approval["key_id"] = policy["authority"]["key_id"]
        approval["policy_sha256"] = hashlib.sha256(policy_bytes).hexdigest()
        approval["manifest_sha256"] = manifest_digest
        approval["binding_sha256"] = hashlib.sha256(
            canonical_bytes(policy["binding"])
        ).hexdigest()
        evidence["qualification_approval"] = dict(
            approval,
            signature={
                "algorithm": "Ed25519",
                "key_id": policy["authority"]["key_id"],
                "value": sign(
                    authority_key, b"fidenaut-approval-v1", approval
                ),
            },
        )
    return policy_bytes, canonical_bytes(evidence)


class QualificationEvaluatorInputTest(unittest.TestCase):
    def evaluate_mutation(self, mutate=None, evaluated_at=NOW):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (
                policy,
                evidence,
                _,
                _,
                trusted_keys,
                authority_key,
                verifier_key,
            ) = fixture(root)
            if mutate is not None:
                mutate(policy, evidence, trusted_keys)
            policy_bytes, evidence_bytes = resign_bundle(
                policy, evidence, authority_key, verifier_key
            )
            with mock.patch.object(
                qualification_evaluator, "_utc_now", return_value=evaluated_at
            ):
                return qualification_evaluator.evaluate_qualification(
                    policy_bytes, evidence_bytes, root, trusted_keys
                )

    def test_malformed_policy_returns_the_exact_fail_closed_report(self):
        evaluated_at = datetime.datetime(
            2026, 10, 8, 12, 0, tzinfo=datetime.timezone.utc
        )
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                qualification_evaluator, "_utc_now", return_value=evaluated_at
            ):
                report = qualification_evaluator.evaluate_qualification(
                    b"{}", b"{}", pathlib.Path(directory), {}
                )

        self.assertEqual(
            {
                "schema": "fidenaut-provider-qualification-report-v1",
                "qualification_id": None,
                "policy_sha256": None,
                "evidence_sha256": None,
                "evaluated_at": "2026-10-08T12:00:00Z",
                "outcome": "adoption unavailable",
                "reason_codes": ["INVALID_INPUT"],
            },
            report,
        )

    def test_public_api_and_report_type_are_explicit(self):
        self.assertEqual(
            {
                "schema",
                "qualification_id",
                "policy_sha256",
                "evidence_sha256",
                "evaluated_at",
                "outcome",
                "reason_codes",
            },
            set(qualification_evaluator.QualificationReport.__annotations__),
        )
        self.assertIs(
            qualification_evaluator.QualificationReport,
            qualification_evaluator.evaluate_qualification.__annotations__["return"],
        )

    def test_duplicate_policy_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            report = qualification_evaluator.evaluate_qualification(
                b'{"schema":"one","schema":"two"}',
                b"{}",
                pathlib.Path(directory),
                {},
            )

        self.assertEqual(["INVALID_INPUT"], report["reason_codes"])

    def test_unknown_policy_fields_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                policy,
                evidence,
                _,
                evidence_bytes,
                trusted_keys,
                _,
                _,
            ) = fixture(pathlib.Path(directory))
            policy["unexpected"] = True
            policy_bytes = canonical_bytes(policy)

            report = qualification_evaluator.evaluate_qualification(
                policy_bytes, evidence_bytes, pathlib.Path(directory), trusted_keys
            )

        self.assertEqual(["INVALID_INPUT"], report["reason_codes"])

    def test_policy_signature_requires_authority_role_key(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                _,
                _,
                policy_bytes,
                evidence_bytes,
                trusted_keys,
                _,
                _,
            ) = fixture(pathlib.Path(directory))
            trusted_keys.pop(("authority", "authority-key"))
            trusted_keys[("independent_verifier", "authority-key")] = bytes(32)

            report = qualification_evaluator.evaluate_qualification(
                policy_bytes, evidence_bytes, pathlib.Path(directory), trusted_keys
            )

        self.assertEqual(["POLICY_UNTRUSTED"], report["reason_codes"])

    def test_invalid_policy_signature_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                policy,
                _,
                _,
                evidence_bytes,
                trusted_keys,
                _,
                _,
            ) = fixture(pathlib.Path(directory))
            policy["signature"]["value"] = base64.b64encode(bytes(64)).decode(
                "ascii"
            )

            report = qualification_evaluator.evaluate_qualification(
                canonical_bytes(policy),
                evidence_bytes,
                pathlib.Path(directory),
                trusted_keys,
            )

        self.assertEqual(["POLICY_UNTRUSTED"], report["reason_codes"])

    def test_noncanonical_base64_signature_fails_schema_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                policy,
                _,
                _,
                evidence_bytes,
                trusted_keys,
                _,
                _,
            ) = fixture(pathlib.Path(directory))
            alphabet = (
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
            )
            signature = policy["signature"]["value"]
            last_index = alphabet.index(signature[-3])
            noncanonical_index = (last_index & 0b110000) | 1
            policy["signature"]["value"] = (
                signature[:-3] + alphabet[noncanonical_index] + "=="
            )

            report = qualification_evaluator.evaluate_qualification(
                canonical_bytes(policy),
                evidence_bytes,
                pathlib.Path(directory),
                trusted_keys,
            )

        self.assertEqual(["INVALID_INPUT"], report["reason_codes"])

    def test_invalid_evidence_signature_fails_after_valid_policy_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                _,
                evidence,
                policy_bytes,
                _,
                trusted_keys,
                _,
                _,
            ) = fixture(pathlib.Path(directory))
            evidence["qualification_approval"] = None
            evidence.pop("verifier_signature")
            evidence["verifier_signature"] = {
                "algorithm": "Ed25519",
                "key_id": "verifier-key",
                "value": base64.b64encode(bytes(64)).decode("ascii"),
            }
            evidence_bytes = canonical_bytes(evidence)
            report = qualification_evaluator.evaluate_qualification(
                policy_bytes, evidence_bytes, pathlib.Path(directory), trusted_keys
            )

        self.assertIn("SIGNATURE_INVALID", report["reason_codes"])
        self.assertNotIn("POLICY_UNTRUSTED", report["reason_codes"])

    def test_valid_signatures_reach_missing_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                _,
                evidence,
                policy_bytes,
                _,
                trusted_keys,
                _,
                _,
            ) = fixture(pathlib.Path(directory))
            evidence["qualification_approval"] = None
            report = qualification_evaluator.evaluate_qualification(
                policy_bytes,
                canonical_bytes(evidence),
                pathlib.Path(directory),
                trusted_keys,
            )

        self.assertIn("APPROVAL_MISSING", report["reason_codes"])
        self.assertNotIn("SIGNATURE_INVALID", report["reason_codes"])
        self.assertNotIn("POLICY_UNTRUSTED", report["reason_codes"])

    def test_non_string_approval_status_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                _,
                evidence,
                policy_bytes,
                _,
                trusted_keys,
                _,
                _,
            ) = fixture(pathlib.Path(directory))
            evidence["qualification_approval"]["status"] = []
            report = qualification_evaluator.evaluate_qualification(
                policy_bytes,
                canonical_bytes(evidence),
                pathlib.Path(directory),
                trusted_keys,
            )

        self.assertEqual(["EVIDENCE_INVALID"], report["reason_codes"])

    def test_synthetic_fixture_qualified_branch_is_not_real_u1_u3_evidence(self):
        """Ephemeral test keys and artifacts prove mechanics, not qualification."""
        with tempfile.TemporaryDirectory() as directory:
            (
                _,
                _,
                policy_bytes,
                evidence_bytes,
                trusted_keys,
                _,
                _,
            ) = fixture(pathlib.Path(directory))
            with mock.patch.object(
                qualification_evaluator, "_utc_now", return_value=NOW
            ):
                report = qualification_evaluator.evaluate_qualification(
                    policy_bytes,
                    evidence_bytes,
                    pathlib.Path(directory),
                    trusted_keys,
                )

        self.assertEqual("qualified", report["outcome"], report["reason_codes"])
        self.assertEqual([], report["reason_codes"])
        self.assertEqual(
            {
                "schema",
                "qualification_id",
                "policy_sha256",
                "evidence_sha256",
                "evaluated_at",
                "outcome",
                "reason_codes",
            },
            set(report),
        )
        self.assertEqual(
            hashlib.sha256(policy_bytes).hexdigest(), report["policy_sha256"]
        )
        manifest = dict(json.loads(evidence_bytes.decode("utf-8")))
        manifest.pop("verifier_signature")
        manifest.pop("qualification_approval")
        self.assertEqual(
            hashlib.sha256(canonical_bytes(manifest)).hexdigest(),
            report["evidence_sha256"],
        )

    def test_missing_required_claim_is_reported(self):
        def remove_claim(_policy, evidence, _trusted_keys):
            evidence["claims"] = [
                claim
                for claim in evidence["claims"]
                if claim["claim_id"] != "U1.tools_processes"
            ]

        report = self.evaluate_mutation(remove_claim)

        self.assertEqual(["CLAIM_MISSING"], report["reason_codes"])

    def test_evidence_qualification_binding_mismatch_is_reported(self):
        def mismatch_qualification(_policy, evidence, _trusted_keys):
            evidence["qualification_id"] = "another-qualification"

        report = self.evaluate_mutation(mismatch_qualification)

        self.assertEqual(["BINDING_MISMATCH"], report["reason_codes"])

    def test_deployment_binding_mismatch_is_reported(self):
        def mismatch_deployment(_policy, evidence, _trusted_keys):
            evidence["binding"]["deployment_id"] = "another-deployment"

        report = self.evaluate_mutation(mismatch_deployment)

        self.assertEqual(["BINDING_MISMATCH"], report["reason_codes"])

    def test_context_binding_mismatch_is_reported(self):
        def mismatch_context(_policy, evidence, _trusted_keys):
            evidence["binding"]["contexts"][0]["context_id"] = "another-context"

        report = self.evaluate_mutation(mismatch_context)

        self.assertEqual(["BINDING_MISMATCH"], report["reason_codes"])

    def test_session_binding_mismatch_is_reported(self):
        def mismatch_session(_policy, evidence, _trusted_keys):
            evidence["binding"]["contexts"][0]["provider_session_id"] = (
                "another-session"
            )

        report = self.evaluate_mutation(mismatch_session)

        self.assertEqual(["BINDING_MISMATCH"], report["reason_codes"])

    def test_claim_subject_binding_mismatch_is_reported(self):
        def mismatch_claim_subject(_policy, evidence, _trusted_keys):
            evidence["claims"][0]["subject_provider_session_ids"][0] = (
                "another-session"
            )

        report = self.evaluate_mutation(mismatch_claim_subject)

        self.assertEqual(["BINDING_MISMATCH"], report["reason_codes"])

    def test_provider_asserted_claim_fails_provenance(self):
        def provider_claim(_policy, evidence, _trusted_keys):
            evidence["claims"][0]["source_kind"] = "provider"

        report = self.evaluate_mutation(provider_claim)

        self.assertEqual(["PROVENANCE_FAILURE"], report["reason_codes"])

    def test_unmatched_observer_fails_provenance(self):
        def forged_observer(_policy, evidence, _trusted_keys):
            evidence["claims"][0]["observer_id"] = "self-attested"

        report = self.evaluate_mutation(forged_observer)

        self.assertEqual(["PROVENANCE_FAILURE"], report["reason_codes"])

    def test_synthetic_claim_fails_provenance(self):
        def synthetic_claim(_policy, evidence, _trusted_keys):
            evidence["claims"][0]["source_kind"] = "synthetic"

        report = self.evaluate_mutation(synthetic_claim)

        self.assertEqual(["PROVENANCE_FAILURE"], report["reason_codes"])

    def test_missing_independent_verifier_trust_fails_closed(self):
        def remove_verifier_key(_policy, _evidence, trusted_keys):
            trusted_keys.pop(("independent_verifier", "verifier-key"))

        report = self.evaluate_mutation(remove_verifier_key)

        self.assertEqual(["VERIFIER_UNAPPROVED"], report["reason_codes"])

    def test_authority_and_verifier_key_alias_is_rejected(self):
        def alias_verifier_key(_policy, _evidence, trusted_keys):
            trusted_keys[("independent_verifier", "verifier-key")] = trusted_keys[
                ("authority", "authority-key")
            ]

        report = self.evaluate_mutation(alias_verifier_key)

        self.assertEqual(["VERIFIER_UNAPPROVED"], report["reason_codes"])

    def test_missing_human_approval_fails_closed(self):
        def remove_approval(_policy, evidence, _trusted_keys):
            evidence["qualification_approval"] = None

        report = self.evaluate_mutation(remove_approval)

        self.assertEqual(["APPROVAL_MISSING"], report["reason_codes"])

    def test_rejected_human_approval_fails_closed(self):
        def reject_approval(_policy, evidence, _trusted_keys):
            evidence["qualification_approval"]["status"] = "REJECTED"

        report = self.evaluate_mutation(reject_approval)

        self.assertEqual(["APPROVAL_MISSING"], report["reason_codes"])

    def test_unapproved_boundary_fails_closed(self):
        def unapprove_boundary(policy, _evidence, _trusted_keys):
            policy["boundary"]["approved"] = False

        report = self.evaluate_mutation(unapprove_boundary)

        self.assertEqual(["BOUNDARY_UNAPPROVED"], report["reason_codes"])

    def test_unapproved_u3_signal_fails_closed(self):
        def unapprove_signal(policy, _evidence, _trusted_keys):
            policy["u3_signal"]["approved"] = False

        report = self.evaluate_mutation(unapprove_signal)

        self.assertEqual(["U3_SIGNAL_UNAPPROVED"], report["reason_codes"])

    def test_fresh_context_nonpass_is_reported(self):
        def unavailable_fresh_context(_policy, evidence, _trusted_keys):
            evidence["claims"][0]["status"] = "UNAVAILABLE"

        report = self.evaluate_mutation(unavailable_fresh_context)

        self.assertEqual(
            ["CLAIM_NOT_PASS", "FRESH_CONTEXT_UNAVAILABLE"],
            report["reason_codes"],
        )

    def test_unlisted_repeated_workspace_is_binding_failure(self):
        def duplicate_workspace(policy, evidence, _trusted_keys):
            policy["binding"]["contexts"][1]["workspace_id"] = (
                policy["binding"]["contexts"][0]["workspace_id"]
            )
            evidence["binding"] = policy["binding"]
            continuity = next(
                claim
                for claim in evidence["claims"]
                if claim["claim_id"] == "U3.continuity"
            )
            for observation in continuity["observations"]:
                if observation["context_id"] == "context-reviewer":
                    observation["workspace_id"] = (
                        policy["binding"]["contexts"][1]["workspace_id"]
                    )

        report = self.evaluate_mutation(duplicate_workspace)

        self.assertEqual(["BINDING_MISMATCH"], report["reason_codes"])

    def test_shared_input_wildcard_is_rejected(self):
        def wildcard_shared_input(policy, _evidence, _trusted_keys):
            policy["shared_inputs"] = [
                {"kind": "workspace", "id": "workspace-*"}
            ]

        report = self.evaluate_mutation(wildcard_shared_input)

        self.assertEqual(["INVALID_INPUT"], report["reason_codes"])

    def test_non_string_shared_input_kind_fails_closed(self):
        def malformed_shared_kind(policy, _evidence, _trusted_keys):
            policy["shared_inputs"] = [{"kind": [], "id": "workspace-1"}]

        report = self.evaluate_mutation(malformed_shared_kind)

        self.assertEqual(["INVALID_INPUT"], report["reason_codes"])

    def test_non_string_claim_enums_fail_closed(self):
        def malformed_claim_enum(_policy, evidence, _trusted_keys):
            evidence["claims"][0]["status"] = []

        report = self.evaluate_mutation(malformed_claim_enum)

        self.assertEqual(["EVIDENCE_INVALID"], report["reason_codes"])

    def test_non_string_provenance_enum_fails_closed(self):
        def malformed_provenance(_policy, evidence, _trusted_keys):
            evidence["claims"][0]["source_kind"] = {}

        report = self.evaluate_mutation(malformed_provenance)

        self.assertEqual(["EVIDENCE_INVALID"], report["reason_codes"])

    def test_fifo_open_is_requested_nonblocking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (
                policy,
                evidence,
                _,
                _,
                trusted_keys,
                authority_key,
                verifier_key,
            ) = fixture(root)
            fifo_path = root / "artifact-pipe"
            os.mkfifo(str(fifo_path))
            evidence["claims"][0]["artifacts"][0]["path"] = "artifact-pipe"
            policy_bytes, evidence_bytes = resign_bundle(
                policy, evidence, authority_key, verifier_key
            )
            original_open = os.open

            def require_nonblocking_open(path, flags, *args, **kwargs):
                if path == "artifact-pipe":
                    if not flags & getattr(os, "O_NONBLOCK", 0):
                        raise AssertionError("FIFO opened in blocking mode")
                    raise OSError("test stops before FIFO read")
                return original_open(path, flags, *args, **kwargs)

            with mock.patch.object(
                qualification_evaluator.os,
                "open",
                side_effect=require_nonblocking_open,
            ):
                with mock.patch.object(
                    qualification_evaluator, "_utc_now", return_value=NOW
                ):
                    report = qualification_evaluator.evaluate_qualification(
                        policy_bytes, evidence_bytes, root, trusted_keys
                    )

        self.assertEqual(["INTEGRITY_FAILURE"], report["reason_codes"])

    def test_stale_evidence_and_claim_expiry_fail_closed(self):
        report = self.evaluate_mutation(
            evaluated_at=NOW + datetime.timedelta(minutes=31)
        )

        self.assertEqual(["EVIDENCE_STALE"], report["reason_codes"])

    def test_policy_must_be_valid_for_the_complete_qualification_window(self):
        def shorten_policy_validity(policy, _evidence, _trusted_keys):
            policy["validity"]["not_after"] = "2026-10-08T12:00:00Z"

        report = self.evaluate_mutation(shorten_policy_validity)

        self.assertIn("POLICY_EXPIRED", report["reason_codes"])

    def test_unavailable_clock_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                _,
                _,
                policy_bytes,
                evidence_bytes,
                trusted_keys,
                _,
                _,
            ) = fixture(pathlib.Path(directory))
            with mock.patch.object(
                qualification_evaluator, "_utc_now", side_effect=OSError
            ):
                report = qualification_evaluator.evaluate_qualification(
                    policy_bytes,
                    evidence_bytes,
                    pathlib.Path(directory),
                    trusted_keys,
                )

        self.assertIsNone(report["evaluated_at"])
        self.assertEqual(["CLOCK_UNAVAILABLE"], report["reason_codes"])

    def test_expired_policy_fails_closed(self):
        report = self.evaluate_mutation(
            evaluated_at=datetime.datetime(
                2026, 12, 1, tzinfo=datetime.timezone.utc
            )
        )

        self.assertIn("POLICY_EXPIRED", report["reason_codes"])

    def test_future_approval_time_fails_closed(self):
        def future_approval(_policy, evidence, _trusted_keys):
            evidence["qualification_approval"]["approved_at"] = (
                "2026-10-08T12:01:00Z"
            )

        report = self.evaluate_mutation(future_approval)

        self.assertEqual(["EVIDENCE_STALE"], report["reason_codes"])

    def test_tampered_claim_artifact_fails_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (
                _,
                _,
                policy_bytes,
                evidence_bytes,
                trusted_keys,
                _,
                _,
            ) = fixture(root)
            target = root / "claims" / "U1-fresh_context_creation.json"
            target.write_bytes(b"tampered")
            with mock.patch.object(
                qualification_evaluator, "_utc_now", return_value=NOW
            ):
                report = qualification_evaluator.evaluate_qualification(
                    policy_bytes, evidence_bytes, root, trusted_keys
                )

        self.assertEqual(["INTEGRITY_FAILURE"], report["reason_codes"])

    def test_artifact_traversal_fails_integrity(self):
        def traverse_boundary(policy, _evidence, _trusted_keys):
            policy["boundary"]["descriptor_path"] = "../boundary.json"

        report = self.evaluate_mutation(traverse_boundary)

        self.assertEqual(["INTEGRITY_FAILURE"], report["reason_codes"])

    def test_symlinked_evidence_root_fails_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "root"
            root.mkdir()
            (
                _,
                _,
                policy_bytes,
                evidence_bytes,
                trusted_keys,
                _,
                _,
            ) = fixture(root)
            symlink_root = pathlib.Path(directory) / "root-link"
            symlink_root.symlink_to(root, target_is_directory=True)
            with mock.patch.object(
                qualification_evaluator, "_utc_now", return_value=NOW
            ):
                report = qualification_evaluator.evaluate_qualification(
                    policy_bytes, evidence_bytes, symlink_root, trusted_keys
                )

        self.assertEqual(["INTEGRITY_FAILURE"], report["reason_codes"])

    def test_symlinked_artifact_component_fails_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "root"
            root.mkdir()
            (
                policy,
                evidence,
                _,
                _,
                trusted_keys,
                authority_key,
                verifier_key,
            ) = fixture(root)
            external = pathlib.Path(directory) / "outside"
            external.mkdir()
            external_file = external / "claim.json"
            external_file.write_bytes(b"outside")
            (root / "claim-link").symlink_to(external, target_is_directory=True)
            evidence["claims"][0]["artifacts"] = [
                artifact(root, "claim-link/claim.json", b"outside")
            ]
            policy_bytes, evidence_bytes = resign_bundle(
                policy, evidence, authority_key, verifier_key
            )
            with mock.patch.object(
                qualification_evaluator, "_utc_now", return_value=NOW
            ):
                report = qualification_evaluator.evaluate_qualification(
                    policy_bytes, evidence_bytes, root, trusted_keys
                )

        self.assertEqual(["INTEGRITY_FAILURE"], report["reason_codes"])

    def test_missing_u3_context_chain_fails_closed(self):
        def remove_reviewer_chain(_policy, evidence, _trusted_keys):
            continuity = next(
                claim
                for claim in evidence["claims"]
                if claim["claim_id"] == "U3.continuity"
            )
            continuity["observations"] = [
                item
                for item in continuity["observations"]
                if item["context_id"] != "context-reviewer"
            ]

        report = self.evaluate_mutation(remove_reviewer_chain)

        self.assertEqual(["U3_SEQUENCE_INVALID"], report["reason_codes"])

    def test_broken_u3_predecessor_fails_closed(self):
        def break_predecessor(_policy, evidence, _trusted_keys):
            continuity = next(
                claim
                for claim in evidence["claims"]
                if claim["claim_id"] == "U3.continuity"
            )
            continuity["observations"][1]["predecessor_sha256"] = "0" * 64

        report = self.evaluate_mutation(break_predecessor)

        self.assertEqual(["U3_SEQUENCE_INVALID"], report["reason_codes"])

    def test_u3_workspace_mismatch_fails_closed(self):
        def mismatch_workspace(_policy, evidence, _trusted_keys):
            continuity = next(
                claim
                for claim in evidence["claims"]
                if claim["claim_id"] == "U3.continuity"
            )
            continuity["observations"][0]["workspace_id"] = "wrong-workspace"

        report = self.evaluate_mutation(mismatch_workspace)

        self.assertEqual(["U3_SEQUENCE_INVALID"], report["reason_codes"])

    def test_u3_session_mismatch_fails_closed(self):
        def mismatch_session(_policy, evidence, _trusted_keys):
            continuity = next(
                claim
                for claim in evidence["claims"]
                if claim["claim_id"] == "U3.continuity"
            )
            continuity["observations"][0]["provider_session_id"] = "wrong-session"

        report = self.evaluate_mutation(mismatch_session)

        self.assertEqual(["U3_SEQUENCE_INVALID"], report["reason_codes"])

    def test_nonincreasing_u3_sequence_fails_closed(self):
        def repeat_sequence(_policy, evidence, _trusted_keys):
            continuity = next(
                claim
                for claim in evidence["claims"]
                if claim["claim_id"] == "U3.continuity"
            )
            continuity["observations"][1]["sequence"] = 0

        report = self.evaluate_mutation(repeat_sequence)

        self.assertEqual(["U3_SEQUENCE_INVALID"], report["reason_codes"])

    def test_nonincreasing_u3_time_fails_closed(self):
        def repeat_time(_policy, evidence, _trusted_keys):
            continuity = next(
                claim
                for claim in evidence["claims"]
                if claim["claim_id"] == "U3.continuity"
            )
            continuity["observations"][1]["observed_at"] = continuity[
                "observations"
            ][0]["observed_at"]

        report = self.evaluate_mutation(repeat_time)

        self.assertEqual(["U3_SEQUENCE_INVALID"], report["reason_codes"])

    def test_unstable_u3_marker_fails_closed(self):
        def unstable_marker(_policy, evidence, _trusted_keys):
            continuity = next(
                claim
                for claim in evidence["claims"]
                if claim["claim_id"] == "U3.continuity"
            )
            continuity["stability"] = "UNSTABLE"

        report = self.evaluate_mutation(unstable_marker)

        self.assertEqual(["U3_SEQUENCE_INVALID"], report["reason_codes"])

    def test_duplicate_artifact_paths_fail_integrity(self):
        def duplicate_path(_policy, evidence, _trusted_keys):
            evidence["claims"][1]["artifacts"][0] = dict(
                evidence["claims"][0]["artifacts"][0]
            )

        report = self.evaluate_mutation(duplicate_path)

        self.assertEqual(["INTEGRITY_FAILURE"], report["reason_codes"])

    def test_artifact_length_mismatch_fails_integrity(self):
        def wrong_length(_policy, evidence, _trusted_keys):
            evidence["claims"][0]["artifacts"][0]["byte_length"] += 1

        report = self.evaluate_mutation(wrong_length)

        self.assertEqual(["INTEGRITY_FAILURE"], report["reason_codes"])

    def test_missing_boundary_descriptor_fails_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (
                _,
                _,
                policy_bytes,
                evidence_bytes,
                trusted_keys,
                _,
                _,
            ) = fixture(root)
            (root / "boundary.json").unlink()
            with mock.patch.object(
                qualification_evaluator, "_utc_now", return_value=NOW
            ):
                report = qualification_evaluator.evaluate_qualification(
                    policy_bytes, evidence_bytes, root, trusted_keys
                )

        self.assertEqual(["INTEGRITY_FAILURE"], report["reason_codes"])

    def test_missing_u3_criteria_fails_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (
                _,
                _,
                policy_bytes,
                evidence_bytes,
                trusted_keys,
                _,
                _,
            ) = fixture(root)
            (root / "criteria.json").unlink()
            with mock.patch.object(
                qualification_evaluator, "_utc_now", return_value=NOW
            ):
                report = qualification_evaluator.evaluate_qualification(
                    policy_bytes, evidence_bytes, root, trusted_keys
                )

        self.assertEqual(["INTEGRITY_FAILURE"], report["reason_codes"])

    def test_trust_keys_in_evidence_are_rejected(self):
        def inject_keys(_policy, evidence, _trusted_keys):
            evidence["trusted_keys"] = {"authority": "not trusted"}

        report = self.evaluate_mutation(inject_keys)

        self.assertEqual(["EVIDENCE_INVALID"], report["reason_codes"])

    def test_evaluator_has_no_provider_workflow_network_or_write_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (
                _,
                _,
                policy_bytes,
                evidence_bytes,
                trusted_keys,
                _,
                _,
            ) = fixture(root)
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            with (
                mock.patch.object(
                    copilot_adapter.CopilotAdapter, "create"
                ) as create,
                mock.patch.object(
                    copilot_adapter.CopilotAdapter, "run_turn"
                ) as run_turn,
                mock.patch.object(agent_workflow, "main") as workflow_main,
                mock.patch.object(subprocess, "Popen") as process,
                mock.patch.object(subprocess, "run") as process_run,
                mock.patch.object(socket, "socket") as network_socket,
            ):
                with mock.patch.object(
                    qualification_evaluator, "_utc_now", return_value=NOW
                ):
                    qualification_evaluator.evaluate_qualification(
                        policy_bytes, evidence_bytes, root, trusted_keys
                    )

            create.assert_not_called()
            run_turn.assert_not_called()
            workflow_main.assert_not_called()
            process.assert_not_called()
            process_run.assert_not_called()
            network_socket.assert_not_called()
            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
