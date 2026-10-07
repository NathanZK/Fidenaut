#!/usr/bin/env python3
"""Durable provider-neutral executor contract primitives and deterministic fake."""

import contextlib
import datetime
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import re
import uuid


CONTEXT_FORMAT = "fidenaut-executor-context-v1"
OPERATION_FORMAT = "fidenaut-executor-op-v1"


class ExecutorError(Exception):
    """The executor could not safely complete or observe an operation."""


class ExecutorConflict(ExecutorError):
    """An idempotency key or operation identity was reused inconsistently."""


class WorkspaceInvalid(ExecutorError):
    """The requested workspace path is invalid or ambiguous."""


class DurableStateError(ExecutorError):
    """Persisted executor state is malformed or cannot be established."""


class _LockBusy(Exception):
    """A non-blocking lock is held by another executor operation."""


class CrashInjected(RuntimeError):
    """A deterministic fault hook simulates process loss at a named boundary."""


class Executor:
    """A local, durable fake executor with exact context identity."""

    def __init__(self, root, fake=None, crash_at=None):
        try:
            self.root = pathlib.Path(root).expanduser().resolve()
        except (OSError, RuntimeError) as error:
            raise ExecutorError("executor root path is invalid") from error
        self.contexts = self.root / "contexts"
        self.operations = self.root / "ops"
        self.operation_locks = self.root / "locks" / "operations"
        self.execution_locks = self.root / "locks" / "executions"
        self.creation_locks = self.root / "locks" / "creation"
        self.context_locks = self.root / "locks" / "contexts"
        self.fake = fake
        self.crash_at = crash_at
        try:
            self.contexts.mkdir(parents=True, exist_ok=True)
            self.operations.mkdir(parents=True, exist_ok=True)
            self.creation_locks.mkdir(parents=True, exist_ok=True)
            self.operation_locks.mkdir(parents=True, exist_ok=True)
            self.execution_locks.mkdir(parents=True, exist_ok=True)
            self.context_locks.mkdir(parents=True, exist_ok=True)
            if self.fake is None:
                self.fake = DeterministicFake(self.root)
        except OSError as error:
            raise ExecutorError("cannot initialize executor storage") from error

    def create(self, creation_key, role, workspace_path):
        """Create or recover one context identified by an idempotency key."""
        if not isinstance(creation_key, str) or not creation_key:
            raise ExecutorError("creation_key must be a non-empty string")
        if not isinstance(role, str) or not role:
            raise ExecutorError("role must be a non-empty string")
        canonical_workspace = self._canonical_workspace(workspace_path)
        try:
            key_digest = hashlib.sha256(
                creation_key.encode("utf-8")
            ).hexdigest()
        except UnicodeError as error:
            raise ExecutorError("creation_key is not valid UTF-8") from error
        record_path = self.contexts / (key_digest + ".json")
        lock_path = self.creation_locks / (key_digest + ".lock")

        with self._locked(lock_path):
            if os.path.lexists(str(record_path)):
                context = self._read_context(record_path)
                if (
                    context["creation_key"] != creation_key
                    or context["role"] != role
                    or context["workspace_path"] != canonical_workspace
                ):
                    raise ExecutorConflict(
                        "creation_key was reused with different context parameters"
                    )
                return context

            context_id = uuid.uuid4().hex
            context = {
                "format": CONTEXT_FORMAT,
                "context_id": context_id,
                "creation_key": creation_key,
                "role": role,
                "workspace_path": canonical_workspace,
                "provider_session_id": "fake-session-" + context_id,
                "generation": 1,
                "created_at": self._timestamp(),
            }
            with self._locked(self._context_lock_path(context_id)):
                self._checkpoint("before_context_write")
                self._atomic_write(record_path, context)
                self._checkpoint("after_context_write")
            return context

    def resume(self, context_id):
        """Observe only the exact requested context; never create a substitute."""
        if not isinstance(context_id, str) or not context_id:
            raise ExecutorError("context_id must be a non-empty string")
        try:
            context = self._find_context(context_id)
        except DurableStateError as error:
            return {
                "status": "UNKNOWN",
                "context_id": context_id,
                "reason": str(error),
            }
        if context is None:
            return {"status": "UNAVAILABLE", "context_id": context_id}
        if not pathlib.Path(context["workspace_path"]).is_dir():
            return {
                "status": "UNAVAILABLE",
                "context_id": context_id,
                "reason": "bound workspace is unavailable",
            }

        try:
            with self._locked(
                self._context_lock_path(context_id), blocking=False
            ):
                return {"status": "AVAILABLE", "context": context}
        except _LockBusy:
            return {"status": "BUSY", "context_id": context_id}
        except ExecutorError as error:
            return {
                "status": "UNKNOWN",
                "context_id": context_id,
                "reason": str(error),
            }

    def submit(
        self, op_id, delivery_attempt, context_id, request_fp, request
    ):
        """Idempotently admit and execute one identified fake operation."""
        self._validate_op_id(op_id)
        if (
            not isinstance(delivery_attempt, int)
            or isinstance(delivery_attempt, bool)
            or delivery_attempt < 1
        ):
            raise ExecutorError("delivery_attempt must be a positive integer")
        if not isinstance(context_id, str) or not context_id:
            raise ExecutorError("context_id must be a non-empty string")
        self._validate_request(request)
        expected_fingerprint = request_fingerprint(op_id, context_id, request)
        if request_fp != expected_fingerprint:
            raise ExecutorConflict("request fingerprint does not match request")

        with self._locked(self._operation_lock_path(op_id)):
            operation = self._read_operation_if_present(op_id)
            if operation is not None and operation["state"] is not None:
                self._assert_operation_identity(
                    operation, op_id, context_id, request_fp
                )
                return {"status": "ACCEPTED", "op_id": op_id}
            fence = operation["fence"] if operation is not None else 0
            if delivery_attempt <= fence:
                return {
                    "status": "NOT_ACCEPTED",
                    "op_id": op_id,
                    "reason": "FENCED",
                }
            context = self._find_context(context_id)
            if context is None:
                raise ExecutorConflict("context_id is unavailable")
            self._assert_request_context(request, context)
            context_lock_path = self._context_lock_path(context_id)
            try:
                with self._locked(context_lock_path, blocking=False):
                    with self._locked(
                        self._execution_lock_path(op_id), blocking=False
                    ):
                        accepted = {
                            "format": OPERATION_FORMAT,
                            "op_id": op_id,
                            "context_id": context_id,
                            "request_fp": request_fp,
                            "fence": fence,
                            "state": "accepted",
                            "owner": None,
                            "outcome": None,
                        }
                        self._checkpoint("before_admission_write")
                        self._atomic_write(
                            self._operation_path(op_id), accepted
                        )
                        self._checkpoint("after_admission_write")
                        running = dict(accepted)
                        running["state"] = "running"
                        running["owner"] = {
                            "pid": os.getpid(),
                            "start_time": self._timestamp(),
                        }
                        self._checkpoint("before_running_write")
                        self._atomic_write(
                            self._operation_path(op_id), running
                        )
                        self._checkpoint("after_running_write")
                        self._checkpoint("before_fake_invocation")
                        outcome = self.fake.invoke(running, context, request)
                        self._checkpoint("after_fake_invocation")
                        self._validate_outcome(
                            outcome, running, context, request
                        )
                        terminal = dict(running)
                        terminal["state"] = "terminal"
                        terminal["outcome"] = outcome
                        self._checkpoint("before_terminal_write")
                        self._atomic_write(
                            self._operation_path(op_id), terminal
                        )
                        self._checkpoint("after_terminal_write")
                        self._checkpoint("before_response")
                        return {"status": "ACCEPTED", "op_id": op_id}
            except _LockBusy:
                return {
                    "status": "NOT_ACCEPTED",
                    "op_id": op_id,
                    "reason": "BUSY",
                }

    def get_outcome(self, op_id, fence_through):
        """Observe an operation without executing it and fence absent attempts."""
        self._validate_op_id(op_id)
        if (
            not isinstance(fence_through, int)
            or isinstance(fence_through, bool)
            or fence_through < 0
        ):
            raise ExecutorError("fence_through must be a non-negative integer")
        lock_path = self._operation_lock_path(op_id)
        try:
            with self._locked(lock_path, blocking=False):
                operation = self._read_operation_if_present(op_id)
                if operation is None:
                    fenced = {
                        "format": OPERATION_FORMAT,
                        "op_id": op_id,
                        "context_id": None,
                        "request_fp": None,
                        "fence": fence_through,
                        "state": None,
                        "owner": None,
                        "outcome": None,
                    }
                    self._checkpoint("before_fence_write")
                    self._atomic_write(self._operation_path(op_id), fenced)
                    self._checkpoint("after_fence_write")
                    return {"status": "NOT_ACCEPTED", "op_id": op_id}
                if operation["state"] is None:
                    operation["fence"] = max(
                        operation["fence"], fence_through
                    )
                    self._checkpoint("before_fence_write")
                    self._atomic_write(self._operation_path(op_id), operation)
                    self._checkpoint("after_fence_write")
                return self._operation_observation(
                    operation, self._execution_is_active(op_id)
                )
        except _LockBusy:
            operation = self._read_operation_if_present(op_id)
            if operation is None:
                return {
                    "status": "UNKNOWN",
                    "op_id": op_id,
                    "reason": "operation admission is in progress",
                }
            return self._operation_observation(
                operation, self._execution_is_active(op_id)
            )

    def _operation_observation(self, operation, execution_active):
        if operation["state"] is None:
            return {"status": "NOT_ACCEPTED", "op_id": operation["op_id"]}
        if operation["state"] == "terminal":
            return operation["outcome"]
        if operation["state"] == "running":
            if execution_active:
                return {
                    "status": "RUNNING",
                    "op_id": operation["op_id"],
                    "context_id": operation["context_id"],
                    "request_fp": operation["request_fp"],
                }
            return {
                "status": "UNKNOWN",
                "op_id": operation["op_id"],
                "reason": "execution lock released before terminal outcome",
            }
        if operation["state"] == "accepted":
            if execution_active:
                return {
                    "status": "ACCEPTED",
                    "op_id": operation["op_id"],
                    "context_id": operation["context_id"],
                    "request_fp": operation["request_fp"],
                }
            return {
                "status": "UNKNOWN",
                "op_id": operation["op_id"],
                "reason": "execution lock released before invocation",
            }
        raise DurableStateError("operation state is unsupported")

    def _execution_is_active(self, op_id):
        try:
            with self._locked(
                self._execution_lock_path(op_id), blocking=False
            ):
                return False
        except _LockBusy:
            return True

    def _read_operation_if_present(self, op_id):
        path = self._operation_path(op_id)
        if not os.path.lexists(str(path)):
            return None
        if path.is_symlink():
            raise DurableStateError("operation record path is a symlink")
        try:
            with path.open("r", encoding="utf-8") as operation_file:
                operation = json.load(
                    operation_file, object_pairs_hook=self._unique_object
                )
        except (OSError, UnicodeError, ValueError) as error:
            raise DurableStateError(
                "operation record is unavailable or malformed"
            ) from error
        expected_fields = {
            "format",
            "op_id",
            "context_id",
            "request_fp",
            "fence",
            "state",
            "owner",
            "outcome",
        }
        if not isinstance(operation, dict) or set(operation) != expected_fields:
            raise DurableStateError("operation record has an invalid shape")
        if operation["format"] != OPERATION_FORMAT:
            raise DurableStateError("operation record version is unsupported")
        if operation["op_id"] != op_id:
            raise DurableStateError("operation record ID does not match path")
        if (
            not isinstance(operation["fence"], int)
            or isinstance(operation["fence"], bool)
            or operation["fence"] < 0
        ):
            raise DurableStateError("operation record has an invalid fence")
        if operation["state"] is None:
            if any(
                operation[field] is not None
                for field in ("context_id", "request_fp", "owner", "outcome")
            ):
                raise DurableStateError("fence-only operation has bound identity")
        elif operation["state"] in ("accepted", "running", "terminal"):
            if not isinstance(operation["context_id"], str):
                raise DurableStateError("operation context identity is invalid")
            context = self._find_context(operation["context_id"])
            if context is None:
                raise DurableStateError("operation context record is missing")
            if not isinstance(operation["request_fp"], str):
                raise DurableStateError("operation fingerprint is invalid")
            if not self._is_digest(operation["request_fp"], 64):
                raise DurableStateError("operation fingerprint is malformed")
            owner = operation["owner"]
            if operation["state"] == "accepted":
                if owner is not None:
                    raise DurableStateError("accepted operation has an owner")
            else:
                if (
                    not isinstance(owner, dict)
                    or set(owner) != {"pid", "start_time"}
                    or not isinstance(owner["pid"], int)
                    or isinstance(owner["pid"], bool)
                    or owner["pid"] < 1
                    or not isinstance(owner["start_time"], str)
                ):
                    raise DurableStateError("operation owner is malformed")
                self._validate_timestamp(owner["start_time"])
            if operation["state"] == "terminal":
                if not isinstance(operation["outcome"], dict):
                    raise DurableStateError("terminal outcome is missing")
                self._validate_stored_outcome(operation, context)
            elif operation["outcome"] is not None:
                raise DurableStateError("non-terminal operation has an outcome")
        else:
            raise DurableStateError("operation record has an unsupported state")
        return operation

    def _validate_stored_outcome(self, operation, context):
        outcome = operation["outcome"]
        if (
            outcome.get("status")
            not in (
                "SUCCESS",
                "AGENT_FAILURE",
                "PROVIDER_FAILURE",
                "EXECUTOR_FAILURE",
                "CANCELLED",
            )
            or outcome.get("op_id") != operation["op_id"]
            or outcome.get("context_id") != operation["context_id"]
            or outcome.get("request_fp") != operation["request_fp"]
            or outcome.get("provider_session_id")
            != context["provider_session_id"]
            or outcome.get("workspace_path") != context["workspace_path"]
        ):
            raise DurableStateError("terminal outcome identity is inconsistent")
        required = {
            "status",
            "op_id",
            "context_id",
            "request_fp",
            "provider_session_id",
            "workspace_path",
            "output_path",
            "exit_code",
            "inputs_verified",
            "tool_calls_observed",
            "started_at",
            "ended_at",
            "diagnostics",
        }
        if set(outcome) != required:
            raise DurableStateError("terminal outcome has an invalid shape")
        for field in (
            "provider_session_id",
            "workspace_path",
            "output_path",
            "started_at",
            "ended_at",
            "diagnostics",
        ):
            if not isinstance(outcome[field], str) or not outcome[field]:
                raise DurableStateError("terminal outcome has an invalid " + field)
        if (
            not isinstance(outcome["inputs_verified"], bool)
            or not isinstance(outcome["tool_calls_observed"], int)
            or isinstance(outcome["tool_calls_observed"], bool)
            or outcome["tool_calls_observed"] < 0
            or (
                outcome["exit_code"] is not None
                and (
                    not isinstance(outcome["exit_code"], int)
                    or isinstance(outcome["exit_code"], bool)
                )
            )
        ):
            raise DurableStateError("terminal outcome has invalid execution fields")
        for field in ("started_at", "ended_at"):
            try:
                self._validate_timestamp(outcome[field])
            except DurableStateError as error:
                raise DurableStateError(
                    "terminal outcome has an invalid timestamp"
                ) from error
        if outcome["status"] == "SUCCESS" and outcome["exit_code"] != 0:
            raise DurableStateError("successful outcome has nonzero exit code")

    def _assert_operation_identity(self, operation, op_id, context_id, request_fp):
        if (
            operation["op_id"] != op_id
            or operation["context_id"] != context_id
            or operation["request_fp"] != request_fp
        ):
            raise ExecutorConflict("operation identity was reused inconsistently")

    def _checkpoint(self, name):
        if self.crash_at == name:
            raise CrashInjected("injected crash at " + name)

    def _assert_request_context(self, request, context):
        if (
            request["role"] != context["role"]
            or request["workspace_path"] != context["workspace_path"]
        ):
            raise ExecutorConflict("request does not match bound context")

    def _validate_request(self, request):
        fields = {
            "role",
            "workspace_path",
            "base",
            "instructions",
            "inputs",
            "output_path",
            "timeout_s",
            "tool_policy",
            "adapter",
        }
        if not isinstance(request, dict) or set(request) != fields:
            raise ExecutorConflict("request has missing or unknown fields")
        for field in ("role", "workspace_path", "output_path"):
            if not isinstance(request[field], str) or not request[field]:
                raise ExecutorConflict("request has invalid " + field)
        if request["tool_policy"] not in ("READ_ONLY", "WRITE_SCOPED"):
            raise ExecutorConflict("request has invalid tool_policy")
        if (
            not isinstance(request["timeout_s"], int)
            or isinstance(request["timeout_s"], bool)
            or request["timeout_s"] < 1
        ):
            raise ExecutorConflict("request has invalid timeout_s")
        for field in ("base", "instructions", "adapter"):
            if not isinstance(request[field], dict):
                raise ExecutorConflict("request has invalid " + field)
        if set(request["base"]) != {"repo_head"} or not self._is_digest(
            request["base"]["repo_head"], 40
        ):
            raise ExecutorConflict("request has invalid base")
        if set(request["instructions"]) != {"path", "sha256"}:
            raise ExecutorConflict("request has invalid instructions")
        self._validate_input(request["instructions"], with_length=False)
        if not isinstance(request["inputs"], list):
            raise ExecutorConflict("request has invalid inputs")
        for item in request["inputs"]:
            self._validate_input(item, with_length=True)
        adapter_fields = {"name", "version", "model", "config_sha256"}
        if set(request["adapter"]) != adapter_fields or any(
            not isinstance(request["adapter"][field], str)
            or not request["adapter"][field]
            for field in ("name", "version", "model")
        ):
            raise ExecutorConflict("request has invalid adapter")
        if not self._is_digest(request["adapter"]["config_sha256"], 64):
            raise ExecutorConflict("request has invalid adapter config digest")
        if not self._valid_absolute_path(request["workspace_path"], True):
            raise ExecutorConflict("request workspace path is not canonical")
        if not self._valid_absolute_path(request["output_path"], False):
            raise ExecutorConflict("request output path is not canonical")

    def _validate_input(self, value, with_length):
        expected = {"kind", "path", "sha256", "byte_length"}
        if not with_length:
            expected.remove("kind")
            expected.remove("byte_length")
        if not isinstance(value, dict) or set(value) != expected:
            raise ExecutorConflict("request has malformed input reference")
        if "kind" in value and (
            not isinstance(value["kind"], str) or not value["kind"]
        ):
            raise ExecutorConflict("request has invalid input kind")
        if not isinstance(value["path"], str) or not self._valid_absolute_path(
            value["path"], True
        ):
            raise ExecutorConflict("request has invalid input path")
        if not self._is_digest(value["sha256"], 64):
            raise ExecutorConflict("request has invalid input digest")
        try:
            actual_digest, size = self._digest_file(value["path"])
        except (OSError, ValueError) as error:
            raise ExecutorConflict("request input bytes are unavailable") from error
        if actual_digest != value["sha256"]:
            raise ExecutorConflict("request input digest does not match bytes")
        if with_length and (
            not isinstance(value["byte_length"], int)
            or isinstance(value["byte_length"], bool)
            or value["byte_length"] < 0
        ):
            raise ExecutorConflict("request has invalid input byte length")
        if with_length and size != value["byte_length"]:
            raise ExecutorConflict("request input length does not match bytes")

    def _valid_absolute_path(self, value, must_exist):
        path = pathlib.Path(value)
        if not path.is_absolute():
            return False
        try:
            if not must_exist and path.is_symlink():
                return False
            resolved = path.resolve(strict=must_exist)
            if must_exist:
                return str(resolved) == value
            return str(path.parent.resolve(strict=True) / path.name) == value
        except (OSError, RuntimeError, ValueError):
            return False

    def _is_digest(self, value, length):
        return (
            isinstance(value, str)
            and len(value) == length
            and re.fullmatch("[0-9a-f]+", value) is not None
        )

    def _digest_file(self, path):
        digest = hashlib.sha256()
        length = 0
        with pathlib.Path(path).open("rb") as input_file:
            while True:
                chunk = input_file.read(64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                length += len(chunk)
        return digest.hexdigest(), length

    def _validate_outcome(self, outcome, operation, context, request):
        expected = {
            "SUCCESS",
            "AGENT_FAILURE",
            "PROVIDER_FAILURE",
            "EXECUTOR_FAILURE",
            "CANCELLED",
        }
        fields = {
            "status",
            "op_id",
            "context_id",
            "request_fp",
            "provider_session_id",
            "workspace_path",
            "output_path",
            "exit_code",
            "inputs_verified",
            "tool_calls_observed",
            "started_at",
            "ended_at",
            "diagnostics",
        }
        if (
            not isinstance(outcome, dict)
            or set(outcome) != fields
            or outcome.get("status") not in expected
        ):
            raise ExecutorError("fake returned an invalid terminal outcome")
        expected_bindings = {
            "op_id": operation["op_id"],
            "context_id": context["context_id"],
            "request_fp": operation["request_fp"],
            "provider_session_id": context["provider_session_id"],
            "workspace_path": context["workspace_path"],
            "output_path": request["output_path"],
        }
        if any(
            outcome.get(field) != value
            for field, value in expected_bindings.items()
        ):
            raise ExecutorError("fake outcome does not match operation identity")
        if (
            outcome["exit_code"] is not None
            and (
                not isinstance(outcome["exit_code"], int)
                or isinstance(outcome["exit_code"], bool)
            )
        ):
            raise ExecutorError("fake returned an invalid exit code")
        if not isinstance(outcome["inputs_verified"], bool):
            raise ExecutorError("fake returned an invalid input verification flag")
        if (
            not isinstance(outcome["tool_calls_observed"], int)
            or isinstance(outcome["tool_calls_observed"], bool)
            or outcome["tool_calls_observed"] < 0
        ):
            raise ExecutorError("fake returned an invalid tool call count")
        if not isinstance(outcome["diagnostics"], str):
            raise ExecutorError("fake returned invalid diagnostics")
        for timestamp in ("started_at", "ended_at"):
            if not isinstance(outcome[timestamp], str):
                raise ExecutorError("fake returned an invalid timestamp")
            try:
                parsed = datetime.datetime.fromisoformat(
                    outcome[timestamp].replace("Z", "+00:00")
                )
            except ValueError as error:
                raise ExecutorError("fake returned an invalid timestamp") from error
            if (
                parsed.tzinfo is None
                or parsed.utcoffset() != datetime.timedelta(0)
            ):
                raise ExecutorError("fake timestamp is not RFC 3339 UTC")
        if (
            outcome["status"] == "SUCCESS"
            and outcome["exit_code"] != 0
        ):
            raise ExecutorError("successful outcome has nonzero exit code")

    def _operation_path(self, op_id):
        return self.operations / (op_id + ".json")

    def _operation_lock_path(self, op_id):
        return self.operation_locks / (op_id + ".lock")

    def _execution_lock_path(self, op_id):
        return self.execution_locks / (op_id + ".lock")

    def _validate_op_id(self, op_id):
        if (
            not isinstance(op_id, str)
            or len(op_id) != 32
            or any(character not in "0123456789abcdef" for character in op_id)
        ):
            raise ExecutorError("op_id must be a lowercase UUID hex value")

    def _validate_context_id(self, context_id):
        if (
            not isinstance(context_id, str)
            or len(context_id) != 32
            or any(character not in "0123456789abcdef" for character in context_id)
        ):
            raise DurableStateError("context record has an invalid context_id")

    def _timestamp(self):
        return (
            datetime.datetime.now(datetime.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def _validate_timestamp(self, value):
        try:
            timestamp = datetime.datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )
        except ValueError as error:
            raise DurableStateError("timestamp is invalid") from error
        if (
            timestamp.tzinfo is None
            or timestamp.utcoffset() != datetime.timedelta(0)
        ):
            raise DurableStateError("timestamp is not RFC 3339 UTC")

    def _canonical_workspace(self, workspace_path):
        if not isinstance(workspace_path, str) or not workspace_path:
            raise WorkspaceInvalid("workspace_path must be a non-empty path")
        path = pathlib.Path(workspace_path)
        if not path.is_absolute():
            raise WorkspaceInvalid("workspace_path must be absolute")
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as error:
            raise WorkspaceInvalid("workspace_path does not exist") from error
        if resolved != path or not resolved.is_dir():
            raise WorkspaceInvalid(
                "workspace_path must be a resolved directory without symlinks"
            )
        return str(resolved)

    def _find_context(self, context_id):
        matches = []
        try:
            for path in sorted(self.contexts.glob("*.json")):
                context = self._read_context(path)
                if context["context_id"] == context_id:
                    matches.append(context)
        except OSError as error:
            raise DurableStateError("cannot read context records") from error
        if len(matches) > 1:
            raise DurableStateError("context ID has multiple durable records")
        return matches[0] if matches else None

    def _read_context(self, path):
        if path.is_symlink():
            raise DurableStateError("context record path is a symlink")
        try:
            with path.open("r", encoding="utf-8") as record_file:
                context = json.load(
                    record_file, object_pairs_hook=self._unique_object
                )
        except (OSError, UnicodeError, ValueError) as error:
            raise DurableStateError(
                "context record is unavailable or malformed"
            ) from error
        expected_fields = {
            "format",
            "context_id",
            "creation_key",
            "role",
            "workspace_path",
            "provider_session_id",
            "generation",
            "created_at",
        }
        if not isinstance(context, dict) or set(context) != expected_fields:
            raise DurableStateError("context record has an invalid shape")
        if context["format"] != CONTEXT_FORMAT:
            raise DurableStateError("context record version is unsupported")
        for field in (
            "context_id",
            "creation_key",
            "role",
            "workspace_path",
            "provider_session_id",
            "created_at",
        ):
            if not isinstance(context[field], str) or not context[field]:
                raise DurableStateError("context record has an invalid " + field)
        if (
            not isinstance(context["generation"], int)
            or isinstance(context["generation"], bool)
            or context["generation"] < 1
        ):
            raise DurableStateError("context record has an invalid generation")
        self._validate_context_id(context["context_id"])
        if context["provider_session_id"] != (
            "fake-session-" + context["context_id"]
        ):
            raise DurableStateError("context provider identity is inconsistent")
        try:
            workspace_path = pathlib.Path(context["workspace_path"])
            canonical_workspace = str(workspace_path.resolve())
        except (OSError, RuntimeError, ValueError) as error:
            raise DurableStateError("context workspace path is invalid") from error
        if (
            not workspace_path.is_absolute()
            or canonical_workspace != context["workspace_path"]
        ):
            raise DurableStateError("context workspace path is not canonical")
        try:
            timestamp = datetime.datetime.fromisoformat(
                context["created_at"].replace("Z", "+00:00")
            )
        except ValueError as error:
            raise DurableStateError("context creation timestamp is invalid") from error
        if timestamp.tzinfo is None:
            raise DurableStateError("context creation timestamp has no timezone")
        if timestamp.utcoffset() != datetime.timedelta(0):
            raise DurableStateError("context creation timestamp is not UTC")
        try:
            expected_name = hashlib.sha256(
                context["creation_key"].encode("utf-8")
            ).hexdigest() + ".json"
        except UnicodeError as error:
            raise DurableStateError(
                "context creation key is not valid UTF-8"
            ) from error
        if path.name != expected_name:
            raise DurableStateError("context creation key does not match record path")
        return context

    def _context_lock_path(self, context_id):
        digest = hashlib.sha256(context_id.encode("utf-8")).hexdigest()
        return self.context_locks / (digest + ".lock")

    @staticmethod
    @contextlib.contextmanager
    def _locked(path, blocking=True):
        descriptor = None
        try:
            if not hasattr(os, "O_NOFOLLOW"):
                raise ExecutorError("lock filesystem lacks no-follow support")
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
            descriptor = os.open(str(path), flags, 0o600)
            with os.fdopen(descriptor, "r+b") as lock_file:
                descriptor = None
                operation = fcntl.LOCK_EX
                if not blocking:
                    operation |= fcntl.LOCK_NB
                try:
                    fcntl.flock(lock_file.fileno(), operation)
                except OSError as error:
                    if not blocking and error.errno in (
                        errno.EACCES,
                        errno.EAGAIN,
                    ):
                        raise _LockBusy from error
                    raise ExecutorError("cannot acquire executor lock") from error
                try:
                    yield
                finally:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    except OSError as error:
                        raise ExecutorError("cannot release executor lock") from error
        except OSError as error:
            raise ExecutorError("cannot access executor lock") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _atomic_write(path, value):
        temporary_path = path.parent / (".tmp-" + uuid.uuid4().hex)
        try:
            descriptor = os.open(
                str(temporary_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                json.dump(
                    value,
                    temporary_file,
                    sort_keys=True,
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(str(temporary_path), str(path))
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except (OSError, TypeError, ValueError, UnicodeError) as error:
            raise ExecutorError("cannot durably write executor state") from error
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result


class DeterministicFake:
    """A persistent fake that records one invocation and effect per operation."""

    def __init__(
        self, root, crash_at=None, hook=None, outcome_status="SUCCESS"
    ):
        try:
            self.root = pathlib.Path(root).expanduser().resolve() / "fake"
            self.root.mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError) as error:
            raise ExecutorError(
                "cannot initialize deterministic fake storage"
            ) from error
        self.crash_at = crash_at
        self.hook = hook
        if outcome_status not in (
            "SUCCESS",
            "AGENT_FAILURE",
            "PROVIDER_FAILURE",
            "EXECUTOR_FAILURE",
            "CANCELLED",
        ):
            raise ValueError("unsupported fake terminal outcome")
        self.outcome_status = outcome_status

    def invoke(self, operation, context, request):
        """Record the call and return a deterministic successful outcome."""
        if not isinstance(operation, dict) or "op_id" not in operation:
            raise ExecutorError("fake operation identity is missing")
        Executor._validate_op_id(self, operation["op_id"])
        operation_path = (
            self.root.parent / "ops" / (operation["op_id"] + ".json")
        )
        if operation_path.is_symlink():
            raise DurableStateError("fake operation record is a symlink")
        try:
            with operation_path.open("r", encoding="utf-8") as operation_file:
                persisted = json.load(
                    operation_file,
                    object_pairs_hook=Executor._unique_object,
                )
        except (OSError, UnicodeError, ValueError) as error:
            raise DurableStateError(
                "fake cannot verify durable operation admission"
            ) from error
        if persisted.get("state") != "running":
            raise ExecutorError("fake invoked before durable running state")

        counter_path = self._counter_path(operation["op_id"])
        counter_lock = counter_path.with_suffix(".lock")
        with Executor._locked(counter_lock):
            counts = self._read_counts(counter_path)
            counts["invocations"] += 1
            Executor._atomic_write(counter_path, counts)
        self._checkpoint("after_invocation")
        if self.hook is not None:
            self.hook("before_effect", operation["op_id"])
        self._checkpoint("before_effect")
        with Executor._locked(counter_lock):
            counts = self._read_counts(counter_path)
            counts["effects"] += 1
            Executor._atomic_write(counter_path, counts)
        self._checkpoint("after_effect")
        if self.hook is not None:
            self.hook("after_effect", operation["op_id"])

        now = (
            datetime.datetime.now(datetime.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        return {
            "status": self.outcome_status,
            "op_id": operation["op_id"],
            "context_id": context["context_id"],
            "request_fp": operation["request_fp"],
            "provider_session_id": context["provider_session_id"],
            "workspace_path": context["workspace_path"],
            "output_path": request["output_path"],
            "exit_code": 0 if self.outcome_status == "SUCCESS" else 1,
            "inputs_verified": True,
            "tool_calls_observed": 0,
            "started_at": now,
            "ended_at": now,
            "diagnostics": (
                "deterministic fake completed with " + self.outcome_status
            ),
        }

    def invocation_count(self, op_id):
        return self._read_counts(self._counter_path(op_id))["invocations"]

    def effect_count(self, op_id):
        return self._read_counts(self._counter_path(op_id))["effects"]

    def _counter_path(self, op_id):
        Executor._validate_op_id(self, op_id)
        return self.root / (op_id + ".json")

    def _checkpoint(self, name):
        if self.crash_at == name:
            raise CrashInjected("injected fake crash at " + name)

    def _read_counts(self, path):
        if not os.path.lexists(str(path)):
            return {"invocations": 0, "effects": 0}
        if path.is_symlink():
            raise DurableStateError("fake counter path is a symlink")
        try:
            with path.open("r", encoding="utf-8") as counter_file:
                counts = json.load(
                    counter_file, object_pairs_hook=Executor._unique_object
                )
        except (OSError, UnicodeError, ValueError) as error:
            raise DurableStateError("fake counters are unavailable") from error
        if (
            not isinstance(counts, dict)
            or set(counts) != {"invocations", "effects"}
            or any(
                not isinstance(counts[field], int)
                or isinstance(counts[field], bool)
                or counts[field] < 0
                for field in ("invocations", "effects")
            )
        ):
            raise DurableStateError("fake counters are malformed")
        return counts

def request_fingerprint(op_id, context_id, request):
    """Return the canonical SHA-256 identity for an operation request."""
    try:
        canonical = json.dumps(
            {
                "op_id": op_id,
                "context_id": context_id,
                "request": request,
            },
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ExecutorConflict("request cannot be canonically encoded") from error
    return hashlib.sha256(canonical).hexdigest()
