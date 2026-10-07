#!/usr/bin/env python3
"""Evaluate a signed provider-qualification evidence bundle without execution."""

import base64
import binascii
import datetime
import hashlib
import json
import os
import pathlib
import re
import stat
from collections.abc import Mapping
from decimal import Decimal
from typing import Optional, TypedDict

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


_POLICY_SCHEMA = "fidenaut-provider-qualification-policy-v1"
_EVIDENCE_SCHEMA = "fidenaut-provider-qualification-evidence-v1"
_REPORT_SCHEMA = "fidenaut-provider-qualification-report-v1"
_CLAIM_IDS = (
    "U1.fresh_context_creation",
    "U1.history_session",
    "U1.workspace_filesystem",
    "U1.tools_processes",
    "U1.credentials_environment",
    "U1.shared_state",
    "U3.exact_resume",
    "U3.continuity",
)
_SHARED_INPUT_KINDS = {
    "history",
    "workspace",
    "session_state",
    "tools_processes",
    "credentials_environment",
    "shared_state",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP_RE = re.compile(
    r"(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d+)?(?P<zone>Z|\+00:00)\Z"
)
_ARTIFACT_FIELDS = {"path", "sha256", "byte_length"}
_CLAIM_FIELDS = {
    "claim_id",
    "status",
    "source_kind",
    "observer_id",
    "observed_at",
    "valid_until",
    "subject_context_ids",
    "subject_provider_session_ids",
    "artifacts",
}
_CONTINUITY_FIELDS = {
    "signal_id",
    "criteria_sha256",
    "stability",
    "out_of_band_state",
    "observations",
}
_OBSERVATION_FIELDS = {
    "sequence",
    "observed_at",
    "context_id",
    "provider_session_id",
    "workspace_id",
    "signal_sha256",
    "predecessor_sha256",
    "signal_artifact",
}
_REASON_CODES = {
    "INVALID_INPUT",
    "POLICY_UNTRUSTED",
    "POLICY_EXPIRED",
    "EVIDENCE_INVALID",
    "BINDING_MISMATCH",
    "EVIDENCE_STALE",
    "INTEGRITY_FAILURE",
    "SIGNATURE_INVALID",
    "PROVENANCE_FAILURE",
    "APPROVAL_MISSING",
    "VERIFIER_UNAPPROVED",
    "BOUNDARY_UNAPPROVED",
    "U3_SIGNAL_UNAPPROVED",
    "CLAIM_MISSING",
    "CLAIM_NOT_PASS",
    "FRESH_CONTEXT_UNAVAILABLE",
    "U3_SEQUENCE_INVALID",
    "CLOCK_UNAVAILABLE",
}
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_UTC_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


class _DocumentError(ValueError):
    def __init__(self, reason_code):
        super().__init__(reason_code)
        self.reason_code = reason_code


class _ArtifactError(OSError):
    pass


class QualificationReport(TypedDict):
    schema: str
    qualification_id: Optional[str]
    policy_sha256: Optional[str]
    evidence_sha256: Optional[str]
    evaluated_at: Optional[str]
    outcome: str
    reason_codes: list[str]


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _format_utc(value):
    result = value.astimezone(datetime.timezone.utc).isoformat(timespec="microseconds")
    return result.replace(".000000+00:00", "Z").replace("+00:00", "Z")


def _timestamp_value(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    match = _TIMESTAMP_RE.fullmatch(value)
    if match is None:
        raise ValueError("timestamp must be UTC RFC 3339")
    base = datetime.datetime.fromisoformat(match.group("base") + "+00:00")
    delta = base - _UTC_EPOCH
    whole_seconds = Decimal(delta.days * 86400 + delta.seconds)
    fraction = match.group("fraction")
    if fraction:
        whole_seconds += Decimal("0" + fraction)
    return whole_seconds


def _canonical_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(data):
    return hashlib.sha256(data).digest()


def _digest_hex(data):
    return hashlib.sha256(data).hexdigest()


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _reject_float(_value):
    raise ValueError("floating point values are not permitted")


def _reject_constant(_value):
    raise ValueError("non-finite numbers are not permitted")


def _parse_json(raw, reason_code):
    if not isinstance(raw, bytes):
        raise _DocumentError(reason_code)
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
        _check_string_keys(value)
        return value
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise _DocumentError(reason_code)


def _check_string_keys(value):
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("object keys must be strings")
        for child in value.values():
            _check_string_keys(child)
    elif isinstance(value, list):
        for child in value:
            _check_string_keys(child)


def _expect_object(value, fields, reason_code, optional=()):
    if not isinstance(value, dict):
        raise _DocumentError(reason_code)
    keys = set(value)
    if not set(fields).issubset(keys) or keys - set(fields) - set(optional):
        raise _DocumentError(reason_code)


def _string(value, reason_code, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value):
        raise _DocumentError(reason_code)
    return value


def _integer(value, reason_code, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _DocumentError(reason_code)
    return value


def _boolean(value, reason_code):
    if not isinstance(value, bool):
        raise _DocumentError(reason_code)
    return value


def _digest(value, reason_code, allow_null=False):
    if allow_null and value is None:
        return
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise _DocumentError(reason_code)


def _time(value, reason_code):
    try:
        return _timestamp_value(value)
    except (ValueError, OverflowError):
        raise _DocumentError(reason_code)


def _signature(value, reason_code):
    _expect_object(value, {"algorithm", "key_id", "value"}, reason_code)
    if value["algorithm"] != "Ed25519":
        raise _DocumentError(reason_code)
    _string(value["key_id"], reason_code)
    encoded = _string(value["value"], reason_code)
    try:
        decoded = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise _DocumentError(reason_code)
    if len(decoded) != 64 or base64.b64encode(decoded).decode("ascii") != encoded:
        raise _DocumentError(reason_code)


def _validate_binding(value, reason_code):
    _expect_object(
        value, {"deployment_id", "provider", "window", "contexts"}, reason_code
    )
    _string(value["deployment_id"], reason_code)
    provider = value["provider"]
    _expect_object(provider, {"name", "version", "adapter_revision"}, reason_code)
    for field in ("name", "version", "adapter_revision"):
        _string(provider[field], reason_code)
    window = value["window"]
    _expect_object(window, {"not_before", "not_after"}, reason_code)
    start = _time(window["not_before"], reason_code)
    end = _time(window["not_after"], reason_code)
    if start >= end:
        raise _DocumentError(reason_code)
    contexts = value["contexts"]
    if not isinstance(contexts, list) or len(contexts) < 2:
        raise _DocumentError(reason_code)
    distinct = {"role": set(), "context_id": set(), "provider_session_id": set()}
    for context in contexts:
        _expect_object(
            context,
            {
                "role",
                "context_id",
                "provider_session_id",
                "workspace_id",
                "credential_assignment_id",
            },
            reason_code,
        )
        for field in context:
            _string(context[field], reason_code)
        for field in distinct:
            if context[field] in distinct[field]:
                raise _DocumentError(reason_code)
            distinct[field].add(context[field])


def _validate_policy(value):
    reason = "INVALID_INPUT"
    _expect_object(
        value,
        {
            "schema",
            "qualification_id",
            "binding",
            "validity",
            "shared_inputs",
            "authority",
            "verifier",
            "provider_key_ids",
            "boundary",
            "u3_signal",
            "signature",
        },
        reason,
    )
    if value["schema"] != _POLICY_SCHEMA:
        raise _DocumentError(reason)
    _string(value["qualification_id"], reason)
    _validate_binding(value["binding"], reason)
    validity = value["validity"]
    _expect_object(
        validity, {"not_before", "not_after", "max_evidence_age_seconds"}, reason
    )
    validity_start = _time(validity["not_before"], reason)
    validity_end = _time(validity["not_after"], reason)
    if validity_start >= validity_end:
        raise _DocumentError(reason)
    _integer(validity["max_evidence_age_seconds"], reason, minimum=1)
    shared_inputs = value["shared_inputs"]
    if not isinstance(shared_inputs, list):
        raise _DocumentError(reason)
    shared_descriptors = set()
    for item in shared_inputs:
        _expect_object(item, {"kind", "id"}, reason)
        kind = _string(item["kind"], reason)
        if kind not in _SHARED_INPUT_KINDS:
            raise _DocumentError(reason)
        _string(item["id"], reason)
        descriptor = (kind, item["id"])
        if descriptor in shared_descriptors or any(
            wildcard in item["id"] for wildcard in "*?[]"
        ):
            raise _DocumentError(reason)
        shared_descriptors.add(descriptor)
    authority = value["authority"]
    _expect_object(authority, {"id", "key_id"}, reason)
    _string(authority["id"], reason)
    _string(authority["key_id"], reason)
    verifier = value["verifier"]
    _expect_object(verifier, {"id", "key_id", "independence_approved"}, reason)
    _string(verifier["id"], reason)
    _string(verifier["key_id"], reason)
    _boolean(verifier["independence_approved"], reason)
    if authority["key_id"] == verifier["key_id"]:
        raise _DocumentError(reason)
    provider_key_ids = value["provider_key_ids"]
    if not isinstance(provider_key_ids, list):
        raise _DocumentError(reason)
    provider_keys = set()
    for key_id in provider_key_ids:
        _string(key_id, reason)
        if key_id in provider_keys:
            raise _DocumentError(reason)
        provider_keys.add(key_id)
    if authority["key_id"] in provider_keys or verifier["key_id"] in provider_keys:
        raise _DocumentError(reason)
    _validate_approval_target(
        value["boundary"], "descriptor_path", "descriptor_sha256", reason
    )
    _validate_approval_target(
        value["u3_signal"], "criteria_path", "criteria_sha256", reason
    )
    _signature(value["signature"], reason)
    return value


def _validate_approval_target(value, path_field, digest_field, reason_code):
    _expect_object(value, {"id", "approved", path_field, digest_field}, reason_code)
    _string(value["id"], reason_code)
    _boolean(value["approved"], reason_code)
    _string(value[path_field], reason_code)
    _digest(value[digest_field], reason_code)


def _validate_artifact(value, reason_code):
    _expect_object(value, _ARTIFACT_FIELDS, reason_code)
    _string(value["path"], reason_code)
    _digest(value["sha256"], reason_code)
    _integer(value["byte_length"], reason_code)


def _validate_evidence(value):
    reason = "EVIDENCE_INVALID"
    _expect_object(
        value,
        {
            "schema",
            "qualification_id",
            "policy_sha256",
            "binding",
            "captured_at",
            "valid_until",
            "claims",
            "verifier_signature",
        },
        reason,
        optional={"qualification_approval"},
    )
    if value["schema"] != _EVIDENCE_SCHEMA:
        raise _DocumentError(reason)
    _string(value["qualification_id"], reason)
    _digest(value["policy_sha256"], reason)
    _validate_binding(value["binding"], reason)
    _time(value["captured_at"], reason)
    _time(value["valid_until"], reason)
    claims = value["claims"]
    if not isinstance(claims, list):
        raise _DocumentError(reason)
    seen_claims = set()
    for claim in claims:
        if not isinstance(claim, dict):
            raise _DocumentError(reason)
        claim_id = _string(claim.get("claim_id"), reason)
        if claim_id not in _CLAIM_IDS or claim_id in seen_claims:
            raise _DocumentError(reason)
        seen_claims.add(claim_id)
        fields = _CLAIM_FIELDS | (
            _CONTINUITY_FIELDS if claim_id == "U3.continuity" else set()
        )
        _expect_object(claim, fields, reason)
        status = _string(claim["status"], reason)
        if status not in {"PASS", "FAIL", "UNKNOWN", "UNAVAILABLE"}:
            raise _DocumentError(reason)
        source_kind = _string(claim["source_kind"], reason)
        if source_kind not in {
            "independent_verifier",
            "provider",
            "synthetic",
        }:
            raise _DocumentError(reason)
        _string(claim["observer_id"], reason)
        _time(claim["observed_at"], reason)
        _time(claim["valid_until"], reason)
        for field in ("subject_context_ids", "subject_provider_session_ids"):
            values = claim[field]
            if not isinstance(values, list):
                raise _DocumentError(reason)
            parsed = [_string(item, reason) for item in values]
            if len(parsed) != len(set(parsed)):
                raise _DocumentError(reason)
        artifacts = claim["artifacts"]
        if not isinstance(artifacts, list) or not artifacts:
            raise _DocumentError(reason)
        for item in artifacts:
            _validate_artifact(item, reason)
        if claim_id == "U3.continuity":
            _string(claim["signal_id"], reason)
            _digest(claim["criteria_sha256"], reason)
            stability = _string(claim["stability"], reason)
            if stability not in {"STABLE", "UNSTABLE", "UNKNOWN"}:
                raise _DocumentError(reason)
            out_of_band_state = _string(claim["out_of_band_state"], reason)
            if out_of_band_state not in {
                "NONE",
                "DETECTED",
                "UNKNOWN",
            }:
                raise _DocumentError(reason)
            observations = claim["observations"]
            if not isinstance(observations, list):
                raise _DocumentError(reason)
            for observation in observations:
                _expect_object(observation, _OBSERVATION_FIELDS, reason)
                _integer(observation["sequence"], reason)
                _time(observation["observed_at"], reason)
                for field in (
                    "context_id",
                    "provider_session_id",
                    "workspace_id",
                ):
                    _string(observation[field], reason)
                _digest(observation["signal_sha256"], reason)
                _digest(
                    observation["predecessor_sha256"],
                    reason,
                    allow_null=True,
                )
                _validate_artifact(observation["signal_artifact"], reason)
    _signature(value["verifier_signature"], reason)
    approval = value.get("qualification_approval")
    if approval is not None:
        _expect_object(
            approval,
            {
                "status",
                "authority_id",
                "key_id",
                "approved_at",
                "policy_sha256",
                "manifest_sha256",
                "binding_sha256",
                "outcome",
                "signature",
            },
            reason,
        )
        status = _string(approval["status"], reason)
        if status not in {"APPROVED", "REJECTED"}:
            raise _DocumentError(reason)
        _string(approval["authority_id"], reason)
        _string(approval["key_id"], reason)
        _time(approval["approved_at"], reason)
        for field in ("policy_sha256", "manifest_sha256", "binding_sha256"):
            _digest(approval[field], reason)
        if approval["outcome"] != "qualified":
            raise _DocumentError(reason)
        _signature(approval["signature"], reason)
    return value


def _signature_bytes(value):
    return base64.b64decode(value["value"].encode("ascii"), validate=True)


def _verify_signature(public_key_bytes, signature, payload):
    try:
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
            _signature_bytes(signature), payload
        )
    except (InvalidSignature, ValueError, binascii.Error):
        return False
    return True


def _trusted_key_map(trusted_keys):
    if not isinstance(trusted_keys, Mapping):
        return None, True
    keys = {}
    identifiers_by_role = {"authority": set(), "independent_verifier": set()}
    values_by_role = {"authority": set(), "independent_verifier": set()}
    try:
        for key, value in trusted_keys.items():
            if (
                not isinstance(key, tuple)
                or len(key) != 2
                or key[0] not in identifiers_by_role
                or not isinstance(key[1], str)
                or not key[1]
                or not isinstance(value, bytes)
                or len(value) != 32
            ):
                return None, True
            role, key_id = key
            if key_id in identifiers_by_role[role]:
                return None, True
            identifiers_by_role[role].add(key_id)
            values_by_role[role].add(value)
            keys[key] = value
    except (AttributeError, TypeError, ValueError):
        return None, True
    aliases = bool(
        identifiers_by_role["authority"] & identifiers_by_role["independent_verifier"]
        or values_by_role["authority"] & values_by_role["independent_verifier"]
    )
    return keys, aliases


def _report(
    qualification_id,
    policy_sha256,
    evidence_sha256,
    evaluated_at,
    reason_codes,
):
    reasons = sorted(set(reason_codes))
    if not reasons:
        raise ValueError("a qualification report must have a failure reason")
    return {
        "schema": _REPORT_SCHEMA,
        "qualification_id": qualification_id,
        "policy_sha256": policy_sha256,
        "evidence_sha256": evidence_sha256,
        "evaluated_at": evaluated_at,
        "outcome": "adoption unavailable",
        "reason_codes": reasons,
    }


def _policy_signature_valid(policy, keys):
    signature = policy["signature"]
    authority_key_id = policy["authority"]["key_id"]
    if signature["key_id"] != authority_key_id:
        return False
    public_key = keys.get(("authority", authority_key_id))
    if public_key is None:
        return False
    unsigned_policy = dict(policy)
    unsigned_policy.pop("signature")
    payload = b"fidenaut-policy-v1\n" + _sha256(_canonical_bytes(unsigned_policy))
    return _verify_signature(public_key, signature, payload)


def _manifest(evidence):
    manifest = dict(evidence)
    manifest.pop("verifier_signature", None)
    manifest.pop("qualification_approval", None)
    return manifest


def _evidence_signature_valid(evidence, policy, keys, manifest_digest):
    signature = evidence["verifier_signature"]
    verifier_key_id = policy["verifier"]["key_id"]
    if signature["key_id"] != verifier_key_id:
        return False
    public_key = keys.get(("independent_verifier", verifier_key_id))
    if public_key is None:
        return None
    payload = b"fidenaut-evidence-v1\n" + bytes.fromhex(manifest_digest)
    return _verify_signature(public_key, signature, payload)


def _binding_contexts(policy):
    contexts = policy["binding"]["contexts"]
    return (
        [context["context_id"] for context in contexts],
        [context["provider_session_id"] for context in contexts],
    )


def _validate_shared_contexts(policy):
    allowed = {(item["kind"], item["id"]) for item in policy["shared_inputs"]}
    workspaces = {}
    credentials = {}
    for context in policy["binding"]["contexts"]:
        workspace = context["workspace_id"]
        credentials_id = context["credential_assignment_id"]
        workspaces.setdefault(workspace, 0)
        workspaces[workspace] += 1
        credentials.setdefault(credentials_id, 0)
        credentials[credentials_id] += 1
    if any(count > 1 for count in workspaces.values()):
        if any(
            count > 1 and ("workspace", value) not in allowed
            for value, count in workspaces.items()
        ):
            return False
    if any(count > 1 for count in credentials.values()):
        if any(
            count > 1 and ("credentials_environment", value) not in allowed
            for value, count in credentials.items()
        ):
            return False
    return True


def _artifact_path_parts(path):
    if (
        not isinstance(path, str)
        or not path
        or "\\" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise _ArtifactError("invalid artifact path")
    return path.split("/")


def _open_canonical_directory(path):
    if not path.is_absolute():
        raise _ArtifactError("evidence root must be absolute")
    if not _O_NOFOLLOW or not _O_DIRECTORY:
        raise _ArtifactError("no-follow directory access is unavailable")
    current_fd = os.open(path.anchor, os.O_RDONLY | _O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(
                component,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except OSError as error:
        os.close(current_fd)
        raise _ArtifactError("invalid evidence root") from error


def _open_directory_path(path):
    path = pathlib.Path(path)
    if path == pathlib.Path("."):
        return os.open(".", os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    if path.is_absolute() and path == pathlib.Path(path.anchor):
        return os.open(path.anchor, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    if path.name == "..":
        try:
            canonical = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise _ArtifactError("invalid evidence root") from error
        return _open_canonical_directory(canonical)
    try:
        canonical_parent = path.parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise _ArtifactError("invalid evidence root") from error
    parent_fd = _open_canonical_directory(canonical_parent)
    try:
        root_fd = os.open(
            path.name,
            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            os.close(root_fd)
            raise _ArtifactError("invalid evidence root")
        return root_fd
    except OSError as error:
        raise _ArtifactError("invalid evidence root") from error
    finally:
        os.close(parent_fd)


def _read_artifact(root_fd, path):
    parts = _artifact_path_parts(path)
    parent_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        file_fd = os.open(
            parts[-1],
            os.O_RDONLY | _O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
    except OSError as error:
        os.close(parent_fd)
        raise _ArtifactError("cannot open artifact") from error
    else:
        os.close(parent_fd)
    try:
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise _ArtifactError("artifact is not a regular file")
        digest = hashlib.sha256()
        byte_length = 0
        while True:
            chunk = os.read(file_fd, 65536)
            if not chunk:
                break
            digest.update(chunk)
            byte_length += len(chunk)
        after = os.fstat(file_fd)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after:
            raise _ArtifactError("artifact changed while reading")
        return digest.hexdigest(), byte_length
    except OSError as error:
        if isinstance(error, _ArtifactError):
            raise
        raise _ArtifactError("cannot read artifact") from error
    finally:
        os.close(file_fd)


def _add_artifact(references, artifact):
    path = artifact["path"]
    if path in references:
        raise _ArtifactError("duplicate artifact path")
    references.add(path)
    return path


def _validate_artifacts(evidence_root, policy, evidence):
    references = set()
    required = []
    required.append(
        (
            _add_artifact(references, {"path": policy["boundary"]["descriptor_path"]}),
            policy["boundary"]["descriptor_sha256"],
            None,
        )
    )
    required.append(
        (
            _add_artifact(references, {"path": policy["u3_signal"]["criteria_path"]}),
            policy["u3_signal"]["criteria_sha256"],
            None,
        )
    )
    for claim in evidence["claims"]:
        for artifact_value in claim["artifacts"]:
            required.append(
                (
                    _add_artifact(references, artifact_value),
                    artifact_value["sha256"],
                    artifact_value["byte_length"],
                )
            )
        if claim["claim_id"] == "U3.continuity":
            for observation in claim["observations"]:
                artifact_value = observation["signal_artifact"]
                if artifact_value["sha256"] != observation["signal_sha256"]:
                    raise _ArtifactError("signal digest does not match artifact")
                required.append(
                    (
                        _add_artifact(references, artifact_value),
                        artifact_value["sha256"],
                        artifact_value["byte_length"],
                    )
                )
    root_fd = _open_directory_path(evidence_root)
    try:
        for path, expected_digest, expected_length in required:
            actual_digest, actual_length = _read_artifact(root_fd, path)
            if actual_digest != expected_digest:
                raise _ArtifactError("artifact digest mismatch")
            if expected_length is not None and actual_length != expected_length:
                raise _ArtifactError("artifact length mismatch")
    finally:
        os.close(root_fd)


def _check_claims(policy, evidence, now_value):
    reasons = set()
    expected_context_ids, expected_session_ids = _binding_contexts(policy)
    policy_contexts = {
        context["context_id"]: context for context in policy["binding"]["contexts"]
    }
    claims_by_id = {}
    for claim in evidence["claims"]:
        claims_by_id[claim["claim_id"]] = claim
    for claim_id in _CLAIM_IDS:
        claim = claims_by_id.get(claim_id)
        if claim is None:
            reasons.add("CLAIM_MISSING")
            continue
        if (
            claim["subject_context_ids"] != expected_context_ids
            or claim["subject_provider_session_ids"] != expected_session_ids
        ):
            reasons.add("BINDING_MISMATCH")
        if claim["observer_id"] != policy["verifier"]["id"]:
            reasons.add("PROVENANCE_FAILURE")
        if claim["source_kind"] != "independent_verifier":
            reasons.add("PROVENANCE_FAILURE")
        if claim["status"] != "PASS":
            reasons.add("CLAIM_NOT_PASS")
            if claim_id == "U1.fresh_context_creation":
                reasons.add("FRESH_CONTEXT_UNAVAILABLE")
        if now_value is not None:
            observed = _timestamp_value(claim["observed_at"])
            valid_until = _timestamp_value(claim["valid_until"])
            captured = _timestamp_value(evidence["captured_at"])
            window_start = _timestamp_value(
                policy["binding"]["window"]["not_before"]
            )
            window_end = _timestamp_value(policy["binding"]["window"]["not_after"])
            max_age = Decimal(policy["validity"]["max_evidence_age_seconds"])
            if (
                observed < window_start
                or observed > window_end
                or observed > captured
                or observed > now_value
                or now_value - observed > max_age
                or valid_until < now_value
                or valid_until
                > _timestamp_value(policy["validity"]["not_after"])
            ):
                reasons.add("EVIDENCE_STALE")
    continuity = claims_by_id.get("U3.continuity")
    if continuity is not None:
        if (
            continuity["signal_id"] != policy["u3_signal"]["id"]
            or continuity["criteria_sha256"]
            != policy["u3_signal"]["criteria_sha256"]
        ):
            reasons.add("U3_SIGNAL_UNAPPROVED")
        if (
            continuity["stability"] != "STABLE"
            or continuity["out_of_band_state"] != "NONE"
        ):
            reasons.add("U3_SEQUENCE_INVALID")
        observations_by_context = {}
        for observation in continuity["observations"]:
            context_id = observation["context_id"]
            observations_by_context.setdefault(context_id, []).append(observation)
        if set(observations_by_context) != set(policy_contexts):
            reasons.add("U3_SEQUENCE_INVALID")
        for context_id, context in policy_contexts.items():
            chain = observations_by_context.get(context_id, [])
            if len(chain) < 2:
                reasons.add("U3_SEQUENCE_INVALID")
                continue
            previous_digest = None
            previous_sequence = -1
            previous_time = None
            for observation in chain:
                current_time = _timestamp_value(observation["observed_at"])
                if (
                    observation["provider_session_id"]
                    != context["provider_session_id"]
                    or observation["workspace_id"] != context["workspace_id"]
                    or observation["sequence"] != previous_sequence + 1
                    or (previous_time is not None and current_time <= previous_time)
                    or observation["predecessor_sha256"] != previous_digest
                ):
                    reasons.add("U3_SEQUENCE_INVALID")
                previous_sequence = observation["sequence"]
                previous_time = current_time
                previous_digest = observation["signal_sha256"]
                if now_value is not None:
                    captured = _timestamp_value(evidence["captured_at"])
                    window_start = _timestamp_value(
                        policy["binding"]["window"]["not_before"]
                    )
                    window_end = _timestamp_value(
                        policy["binding"]["window"]["not_after"]
                    )
                    max_age = Decimal(
                        policy["validity"]["max_evidence_age_seconds"]
                    )
                    if (
                        current_time < window_start
                        or current_time > window_end
                        or current_time > captured
                        or current_time > now_value
                        or now_value - current_time > max_age
                    ):
                        reasons.add("EVIDENCE_STALE")
    return reasons


def _validate_temporal_policy(policy, evidence, now_value):
    reasons = set()
    validity_start = _timestamp_value(policy["validity"]["not_before"])
    validity_end = _timestamp_value(policy["validity"]["not_after"])
    window_start = _timestamp_value(policy["binding"]["window"]["not_before"])
    window_end = _timestamp_value(policy["binding"]["window"]["not_after"])
    if validity_start > window_start or validity_end < window_end:
        reasons.add("POLICY_EXPIRED")
    if now_value is None:
        reasons.add("CLOCK_UNAVAILABLE")
        return reasons
    if (
        now_value < validity_start
        or now_value > validity_end
        or validity_start > window_start
        or validity_end < window_end
    ):
        reasons.add("POLICY_EXPIRED")
    captured = _timestamp_value(evidence["captured_at"])
    evidence_until = _timestamp_value(evidence["valid_until"])
    max_age = Decimal(policy["validity"]["max_evidence_age_seconds"])
    if (
        captured < window_start
        or captured > window_end
        or captured > now_value
        or now_value - captured > max_age
        or evidence_until < now_value
        or evidence_until > validity_end
    ):
        reasons.add("EVIDENCE_STALE")
    return reasons


def _validate_approval(
    evidence, policy, policy_digest, manifest_digest, now_value, keys
):
    reasons = set()
    approval = evidence.get("qualification_approval")
    if approval is None:
        reasons.add("APPROVAL_MISSING")
        return reasons
    if (
        approval["status"] != "APPROVED"
        or approval["authority_id"] != policy["authority"]["id"]
        or approval["key_id"] != policy["authority"]["key_id"]
    ):
        reasons.add("APPROVAL_MISSING")
    expected_binding_digest = _digest_hex(_canonical_bytes(policy["binding"]))
    if (
        approval["policy_sha256"] != policy_digest
        or approval["manifest_sha256"] != manifest_digest
        or approval["binding_sha256"] != expected_binding_digest
    ):
        reasons.add("BINDING_MISMATCH")
    signature = approval["signature"]
    if signature["key_id"] != policy["authority"]["key_id"]:
        reasons.add("APPROVAL_MISSING")
    else:
        public_key = keys.get(("authority", policy["authority"]["key_id"]))
        if public_key is None:
            reasons.add("APPROVAL_MISSING")
        else:
            unsigned = dict(approval)
            unsigned.pop("signature")
            payload = b"fidenaut-approval-v1\n" + _sha256(_canonical_bytes(unsigned))
            if not _verify_signature(public_key, signature, payload):
                reasons.add("SIGNATURE_INVALID")
    if now_value is not None:
        approved_at = _timestamp_value(approval["approved_at"])
        captured = _timestamp_value(evidence["captured_at"])
        validity_start = _timestamp_value(policy["validity"]["not_before"])
        validity_end = _timestamp_value(policy["validity"]["not_after"])
        if (
            approved_at < captured
            or approved_at > now_value
            or approved_at < validity_start
            or approved_at > validity_end
        ):
            reasons.add("EVIDENCE_STALE")
    return reasons


def evaluate_qualification(
    policy_bytes: bytes,
    evidence_bytes: bytes,
    evidence_root: pathlib.Path,
    trusted_keys: Mapping[tuple[str, str], bytes],
) -> QualificationReport:
    """Return a fail-closed report for one signed qualification evidence bundle."""
    evaluated_at = None
    now_value = None
    try:
        now = _utc_now()
        if (
            not isinstance(now, datetime.datetime)
            or now.tzinfo is None
            or now.utcoffset() != datetime.timedelta(0)
        ):
            raise ValueError("clock is not UTC")
        evaluated_at = _format_utc(now)
        now_value = _timestamp_value(evaluated_at)
    except (OSError, OverflowError, TypeError, ValueError, RuntimeError):
        pass

    try:
        policy = _validate_policy(_parse_json(policy_bytes, "INVALID_INPUT"))
        policy_bytes_canonical = _canonical_bytes(policy)
    except _DocumentError as error:
        return _report(None, None, None, evaluated_at, [error.reason_code])
    policy_digest = _digest_hex(policy_bytes_canonical)
    qualification_id = policy["qualification_id"]

    try:
        evidence = _validate_evidence(_parse_json(evidence_bytes, "EVIDENCE_INVALID"))
        manifest_digest = _digest_hex(_canonical_bytes(_manifest(evidence)))
    except _DocumentError as error:
        return _report(
            qualification_id,
            policy_digest,
            None,
            evaluated_at,
            [error.reason_code],
        )

    keys, aliases = _trusted_key_map(trusted_keys)
    if keys is None or not _policy_signature_valid(policy, keys):
        reasons = ["POLICY_UNTRUSTED"]
        if now_value is None:
            reasons.append("CLOCK_UNAVAILABLE")
        return _report(
            qualification_id,
            policy_digest,
            manifest_digest,
            evaluated_at,
            reasons,
        )

    reasons = set()
    if now_value is None:
        reasons.add("CLOCK_UNAVAILABLE")
    if evidence["qualification_id"] != qualification_id:
        reasons.add("BINDING_MISMATCH")
    if evidence["policy_sha256"] != policy_digest:
        reasons.add("BINDING_MISMATCH")
    if evidence["binding"] != policy["binding"]:
        reasons.add("BINDING_MISMATCH")
    if not _validate_shared_contexts(policy):
        reasons.add("BINDING_MISMATCH")
    if not policy["verifier"]["independence_approved"]:
        reasons.add("VERIFIER_UNAPPROVED")
    if not policy["boundary"]["approved"]:
        reasons.add("BOUNDARY_UNAPPROVED")
    if not policy["u3_signal"]["approved"]:
        reasons.add("U3_SIGNAL_UNAPPROVED")
    verifier_key_id = policy["verifier"]["key_id"]
    verifier_key = keys.get(("independent_verifier", verifier_key_id))
    signature_result = None
    if verifier_key is None or aliases:
        reasons.add("VERIFIER_UNAPPROVED")
    else:
        signature_result = _evidence_signature_valid(
            evidence, policy, keys, manifest_digest
        )
        if signature_result is False:
            reasons.add("SIGNATURE_INVALID")
    reasons.update(_validate_temporal_policy(policy, evidence, now_value))
    reasons.update(_check_claims(policy, evidence, now_value))
    reasons.update(
        _validate_approval(
            evidence, policy, policy_digest, manifest_digest, now_value, keys
        )
    )
    try:
        _validate_artifacts(evidence_root, policy, evidence)
    except (OSError, ValueError, TypeError):
        reasons.add("INTEGRITY_FAILURE")

    if not reasons:
        return {
            "schema": _REPORT_SCHEMA,
            "qualification_id": qualification_id,
            "policy_sha256": policy_digest,
            "evidence_sha256": manifest_digest,
            "evaluated_at": evaluated_at,
            "outcome": "qualified",
            "reason_codes": [],
        }
    return _report(
        qualification_id,
        policy_digest,
        manifest_digest,
        evaluated_at,
        reasons,
    )
