#!/usr/bin/env python3
"""Fail-closed Copilot CLI adapter primitives."""

import base64
import datetime
import errno
import hashlib
import json
import math
import os
import pathlib
import re
import stat
import shutil
import uuid

from scripts import executor_protocol
from scripts import workflow_supervisor


_SESSION_NAMESPACE = uuid.UUID("b5e0c56f-0fc2-4f22-95d9-8e87c18b4fa9")
_MARKER_FIELDS = {
    "path",
    "device",
    "inode",
    "byte_length",
    "sha256",
    "event_count",
    "last_event_id",
}
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_CLI_EVENT_TYPES = {
    "assistant.message",
    "assistant.turn_start",
    "assistant.turn_end",
    "session.compaction_complete",
    "session.error",
    "session.mode_changed",
    "session.model_change",
    "session.plan_changed",
    "session.shutdown",
    "session.start",
    "session.task_complete",
    "session.warning",
    "tool.execution_complete",
    "tool.execution_start",
}


class ProviderAdapterError(Exception):
    """The adapter could not safely perform or observe provider work."""


class ProviderUnavailable(ProviderAdapterError):
    """The requested provider capability or exact context is unavailable."""


class ProviderUnknown(ProviderAdapterError):
    """The adapter cannot establish provider state or an operation outcome."""


def derive_session_id(creation_key):
    """Derive a stable UUID without treating it as proof of a provider session."""
    if not isinstance(creation_key, str) or not creation_key:
        raise ValueError("creation_key must be a non-empty string")
    try:
        creation_key.encode("utf-8")
    except UnicodeError as error:
        raise ValueError("creation_key must be valid UTF-8") from error
    return str(uuid.uuid5(_SESSION_NAMESPACE, creation_key))


class CopilotAdapter:
    """Observe exact existing Copilot sessions; session creation stays gated."""

    def __init__(self, home=None, command_runner=None, executable="copilot"):
        try:
            self.home = pathlib.Path(
                pathlib.Path.home() if home is None else home
            ).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as error:
            raise ProviderUnknown("cannot establish Copilot home directory") from error
        if not self.home.is_dir():
            raise ProviderUnknown("Copilot home path is not a directory")
        if not isinstance(executable, str) or not executable:
            raise ValueError("executable must be a non-empty string")
        if command_runner is None:
            resolved_executable = shutil.which(executable)
            if resolved_executable is None:
                raise ProviderUnavailable("Copilot CLI executable is unavailable")
            try:
                executable = str(
                    pathlib.Path(resolved_executable).resolve(strict=True)
                )
            except (OSError, RuntimeError, ValueError) as error:
                raise ProviderUnavailable(
                    "Copilot CLI executable cannot be established"
                ) from error
            if (
                not pathlib.Path(executable).is_file()
                or not os.access(executable, os.X_OK)
            ):
                raise ProviderUnavailable(
                    "Copilot CLI executable is not runnable"
                )
        self.executable = executable
        self.command_runner = (
            self._supervise if command_runner is None else command_runner
        )

    def create(self, creation_key, role, workspace_path):
        """Reject creation until Copilot supports an empty-session operation."""
        derive_session_id(creation_key)
        if not isinstance(role, str) or not role:
            raise ValueError("role must be a non-empty string")
        self._canonical_workspace(workspace_path)
        raise ProviderUnavailable(
            "Copilot CLI does not provide safe empty-session creation"
        )

    def observe_session(
        self, context_id, provider_session_id, workspace_path, expected_marker
    ):
        """Verify exact session/workspace identity and its executor marker."""
        if not isinstance(context_id, str) or not context_id:
            raise ValueError("context_id must be a non-empty string")
        self._validate_session_id(provider_session_id)
        workspace = self._canonical_workspace(workspace_path)
        self._validate_marker(expected_marker)

        try:
            current = self._stable_marker(provider_session_id)
        except FileNotFoundError:
            return self._observation(
                "UNAVAILABLE",
                context_id,
                provider_session_id,
                workspace,
                "session marker is absent",
            )
        except OSError as error:
            status = (
                "UNAVAILABLE"
                if error.errno in (errno.ELOOP, errno.ENOTDIR)
                else "UNKNOWN"
            )
            return self._observation(
                status,
                context_id,
                provider_session_id,
                workspace,
                "session marker cannot be safely opened",
            )
        except (UnicodeError, ValueError, ProviderUnknown):
            return self._observation(
                "UNKNOWN",
                context_id,
                provider_session_id,
                workspace,
                "session marker cannot be established",
            )

        if current["workspace_path"] != workspace:
            return self._observation(
                "UNAVAILABLE",
                context_id,
                provider_session_id,
                workspace,
                "session workspace does not match its binding",
            )
        marker = current["marker"]
        if marker != expected_marker:
            return self._observation(
                "UNAVAILABLE",
                context_id,
                provider_session_id,
                workspace,
                "session marker does not match the executor checkpoint",
            )

        return {
            "status": "AVAILABLE",
            "context_id": context_id,
            "provider_session_id": provider_session_id,
            "workspace_path": workspace,
            "marker": marker,
        }

    def run_turn(
        self,
        *,
        op_id,
        context,
        request_fp,
        request,
        prompt,
        expected_marker,
        executor_effects_accounted=False,
    ):
        """Run one bounded exact-resume turn and normalize its observation."""
        context_id, provider_session_id, bound_workspace = self._validate_turn(
            op_id, context, request_fp, request, prompt
        )
        if not isinstance(executor_effects_accounted, bool):
            raise ValueError("executor_effects_accounted must be a boolean")
        workspace = self._canonical_workspace(bound_workspace)
        if self._canonical_workspace(request["workspace_path"]) != workspace:
            raise ProviderUnavailable(
                "request workspace does not match the context binding"
            )
        if request["tool_policy"] != "READ_ONLY":
            raise ProviderUnavailable(
                "Copilot WRITE_SCOPED tool policy is not qualified"
            )
        if request["timeout_s"] > 3600:
            raise ValueError("timeout_s exceeds the supported one-hour bound")
        if not isinstance(request["output_path"], str):
            raise ValueError("output_path must be an absolute path")
        output_path = pathlib.Path(request["output_path"])
        if (
            not output_path.is_absolute()
            or str(output_path.resolve(strict=False)) != request["output_path"]
        ):
            raise ValueError("output_path must be an absolute canonical path")
        if not isinstance(provider_session_id, str):
            raise ValueError("provider_session_id must be a string")
        self._validate_session_id(provider_session_id)

        context_observation = self.observe_session(
            context_id,
            provider_session_id,
            workspace,
            expected_marker,
        )
        if context_observation["status"] != "AVAILABLE":
            return dict(
                context_observation,
                op_id=op_id,
                request_fp=request_fp,
            )

        self._verify_request_inputs(request)
        command = self._turn_command(provider_session_id, workspace, prompt)
        started_at = self._timestamp()
        try:
            process_result = self.command_runner(
                command,
                timeout_ms=int(math.ceil(request["timeout_s"] * 1000)),
                grace_ms=1000,
                output_limit_bytes=1024 * 1024,
                stderr_limit_bytes=64 * 1024,
                cwd=workspace,
                env=self._command_environment(),
            )
        except OSError:
            return self._unknown_outcome(
                op_id, context_id, provider_session_id, request_fp,
                workspace, "provider process outcome is unavailable"
            )
        if not isinstance(process_result, dict):
            return self._unknown_outcome(
                op_id, context_id, provider_session_id, request_fp,
                workspace, "process supervisor returned malformed state"
            )
        if process_result.get("cleanup_verified") is not True:
            return self._unknown_outcome(
                op_id, context_id, provider_session_id, request_fp,
                workspace, "provider process-group exit is unverified"
            )
        process_status = process_result.get("outcome")
        exit_code = process_result.get("exit_code")
        if process_status == "startup-failure" and exit_code is None:
            return self._terminal_outcome(
                "EXECUTOR_FAILURE",
                op_id,
                context_id,
                provider_session_id,
                request_fp,
                workspace,
                request["output_path"],
                None,
                True,
                0,
                started_at,
                "provider process could not be started",
            )
        containment = process_result.get("containment")
        if (
            not isinstance(containment, dict)
            or containment.get("kind") != "posix-process-group"
            or containment.get("cleanup_scope") != "original-process-group"
        ):
            return self._unknown_outcome(
                op_id, context_id, provider_session_id, request_fp,
                workspace, "provider process-group containment is unavailable"
            )
        try:
            output = self._parse_cli_output(process_result.get("stdout"))
        except ProviderUnknown as error:
            return self._unknown_outcome(
                op_id, context_id, provider_session_id, request_fp,
                workspace, str(error)
            )

        post_turn = self._observe_after_turn(
            context_id,
            provider_session_id,
            workspace,
            expected_marker,
        )
        if post_turn["status"] != "AVAILABLE":
            return self._unknown_outcome(
                op_id, context_id, provider_session_id, request_fp,
                workspace, post_turn.get("reason", "post-turn state is unknown"),
                output["tool_calls_observed"],
            )
        if process_status == "success" and exit_code == 0:
            if (
                not output["assistant_message_seen"]
                or output["tool_calls_completed"]
                != output["tool_calls_observed"]
            ):
                return self._unknown_outcome(
                    op_id, context_id, provider_session_id, request_fp,
                    workspace, "provider output or tool completion is incomplete",
                    output["tool_calls_observed"],
                )
            return self._terminal_outcome(
                "SUCCESS",
                op_id,
                context_id,
                provider_session_id,
                request_fp,
                workspace,
                request["output_path"],
                exit_code,
                True,
                output["tool_calls_observed"],
                started_at,
                "Copilot turn completed.",
            )
        if (
            process_status == "nonzero-exit"
            and isinstance(exit_code, int)
            and not isinstance(exit_code, bool)
            and exit_code != 0
        ):
            if (
                output["tool_calls_observed"] == 0
                and output["provider_error"]
            ):
                status = "PROVIDER_FAILURE"
            elif (
                output["tool_calls_observed"] > 0
                and executor_effects_accounted
            ):
                status = "AGENT_FAILURE"
            else:
                return self._unknown_outcome(
                    op_id, context_id, provider_session_id, request_fp,
                    workspace,
                    "provider failure effects are not established",
                    output["tool_calls_observed"],
                )
            return self._terminal_outcome(
                status,
                op_id,
                context_id,
                provider_session_id,
                request_fp,
                workspace,
                request["output_path"],
                exit_code,
                True,
                output["tool_calls_observed"],
                started_at,
                "Copilot reported a terminal provider error.",
            )
        return self._unknown_outcome(
            op_id, context_id, provider_session_id, request_fp,
            workspace, "provider turn did not reach a classifiable terminal state",
            output["tool_calls_observed"],
        )

    def _observe_after_turn(
        self, context_id, provider_session_id, workspace, expected_marker
    ):
        try:
            current = self._stable_marker(provider_session_id, expected_marker)
        except (OSError, UnicodeError, ValueError, ProviderUnknown):
            return self._observation(
                "UNKNOWN",
                context_id,
                provider_session_id,
                workspace,
                "post-turn marker cannot be established",
            )
        marker = current["marker"]
        if current["workspace_path"] != workspace:
            reason = "post-turn session workspace changed"
        elif (
            marker["path"] != expected_marker["path"]
            or marker["device"] != expected_marker["device"]
            or marker["inode"] != expected_marker["inode"]
            or marker["byte_length"] <= expected_marker["byte_length"]
            or marker["event_count"] <= expected_marker["event_count"]
            or marker["last_event_id"] == expected_marker["last_event_id"]
            or current.get("prefix_sha256") != expected_marker["sha256"]
            or current.get("prefix_event_count")
            != expected_marker["event_count"]
            or current.get("prefix_last_event_id")
            != expected_marker["last_event_id"]
        ):
            reason = "post-turn session marker did not advance continuously"
        else:
            return {
                "status": "AVAILABLE",
                "context_id": context_id,
                "provider_session_id": provider_session_id,
                "workspace_path": workspace,
                "marker": marker,
            }
        return self._observation(
            "UNKNOWN",
            context_id,
            provider_session_id,
            workspace,
            reason,
        )

    @staticmethod
    def _supervise(command, **options):
        return workflow_supervisor.supervise(command, **options)

    def _turn_command(self, session_id, workspace, prompt):
        return [
            self.executable,
            "--resume=" + session_id,
            "-C",
            workspace,
            "-p",
            prompt,
            "--available-tools=view,grep,glob",
            "--allow-tool=view,grep,glob",
            "--output-format",
            "json",
            "--no-custom-instructions",
            "--disable-builtin-mcps",
            "--no-remote",
            "--no-remote-export",
            "--no-ask-user",
            "--no-auto-update",
            "--no-bash-env",
            "--stream",
            "off",
        ]

    def _command_environment(self):
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(self.home),
        }
        for name in ("TMPDIR", "LANG", "LC_ALL"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        return environment

    def _verify_request_inputs(self, request):
        instructions = request.get("instructions")
        if not isinstance(instructions, dict):
            raise ValueError("instructions must be an object")
        self._verify_file(
            instructions.get("path"),
            instructions.get("sha256"),
            None,
        )
        inputs = request.get("inputs")
        if not isinstance(inputs, list):
            raise ValueError("inputs must be a list")
        for item in inputs:
            if not isinstance(item, dict):
                raise ValueError("input entry must be an object")
            self._verify_file(
                item.get("path"),
                item.get("sha256"),
                item.get("byte_length"),
            )

    @staticmethod
    def _verify_file(path_value, expected_digest, expected_length):
        if (
            not isinstance(path_value, str)
            or not pathlib.Path(path_value).is_absolute()
            or not isinstance(expected_digest, str)
            or not _DIGEST_PATTERN.fullmatch(expected_digest)
        ):
            raise ValueError("input file identity is malformed")
        path = pathlib.Path(path_value)
        try:
            if path.is_symlink() or str(path.resolve(strict=True)) != path_value:
                raise ValueError("input file path is not canonical")
            if not hasattr(os, "O_NOFOLLOW"):
                raise ValueError("no-follow input access is unavailable")
            file_fd = os.open(
                str(path), os.O_RDONLY | os.O_NOFOLLOW
            )
            try:
                file_stat = os.fstat(file_fd)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ValueError("input path is not a regular file")
                digest = hashlib.sha256()
                byte_length = 0
                with os.fdopen(file_fd, "rb", closefd=False) as input_file:
                    while True:
                        chunk = input_file.read(64 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        byte_length += len(chunk)
                after_stat = os.fstat(file_fd)
            finally:
                os.close(file_fd)
            path_stat = path.stat()
        except OSError as error:
            raise ValueError("input file cannot be verified") from error
        if (
            file_stat.st_dev != after_stat.st_dev
            or file_stat.st_ino != after_stat.st_ino
            or file_stat.st_size != after_stat.st_size
            or file_stat.st_mtime_ns != after_stat.st_mtime_ns
            or file_stat.st_ctime_ns != after_stat.st_ctime_ns
            or byte_length != after_stat.st_size
            or path_stat.st_dev != after_stat.st_dev
            or path_stat.st_ino != after_stat.st_ino
            or digest.hexdigest() != expected_digest
            or (
                expected_length is not None
                and (
                    not isinstance(expected_length, int)
                    or isinstance(expected_length, bool)
                    or expected_length != byte_length
                )
            )
        ):
            raise ValueError("input file changed or does not match its digest")

    @staticmethod
    def _validate_turn(op_id, context, request_fp, request, prompt):
        if not isinstance(op_id, str) or not op_id:
            raise ValueError("op_id must be a non-empty string")
        if not isinstance(context, dict):
            raise ValueError("context must be an object")
        context_id = context.get("context_id")
        if not isinstance(context_id, str) or not context_id:
            raise ValueError("context.context_id must be a non-empty string")
        role = context.get("role")
        if not isinstance(role, str) or not role:
            raise ValueError("context.role must be a non-empty string")
        provider_session_id = context.get("provider_session_id")
        if not isinstance(provider_session_id, str):
            raise ValueError("context.provider_session_id must be a string")
        workspace_path = context.get("workspace_path")
        if not isinstance(workspace_path, str):
            raise ValueError("context.workspace_path must be a string")
        if (
            not isinstance(request_fp, str)
            or not _DIGEST_PATTERN.fullmatch(request_fp)
        ):
            raise ValueError("request_fp must be a SHA-256 digest")
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("prompt must be a non-empty string")
        if request.get("tool_policy") not in ("READ_ONLY", "WRITE_SCOPED"):
            raise ValueError("request tool_policy is unsupported")
        if request.get("role") != role:
            raise ProviderUnavailable("request role does not match its context")
        try:
            expected_fingerprint = executor_protocol.request_fingerprint(
                op_id, context_id, request
            )
        except executor_protocol.ExecutorConflict as error:
            raise ValueError("request cannot be canonically encoded") from error
        if request_fp != expected_fingerprint:
            raise ValueError("request fingerprint does not match its identity")
        timeout = request.get("timeout_s")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout < 1
        ):
            raise ValueError("timeout_s is outside its supported range")
        if not isinstance(request.get("output_path"), str):
            raise ValueError("output_path must be a string")
        return context_id, provider_session_id, workspace_path

    @staticmethod
    def _parse_cli_output(stdout):
        if not isinstance(stdout, dict):
            raise ProviderUnknown("provider output is missing")
        try:
            encoded = base64.b64decode(stdout["base64"], validate=True)
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderUnknown("provider output is malformed") from error
        if (
            not isinstance(stdout.get("bytes"), int)
            or isinstance(stdout["bytes"], bool)
            or stdout["bytes"] != len(encoded)
            or not isinstance(stdout.get("observed_bytes"), int)
            or isinstance(stdout["observed_bytes"], bool)
            or stdout["observed_bytes"] != len(encoded)
            or not isinstance(stdout.get("observed_sha256"), str)
            or hashlib.sha256(encoded).hexdigest() != stdout["observed_sha256"]
            or not encoded
            or not encoded.endswith(b"\n")
        ):
            raise ProviderUnknown("provider output is incomplete or inconsistent")
        tool_calls = 0
        completed_tool_calls = 0
        pending_tool_calls = set()
        provider_errors = []
        assistant_message_seen = False
        event_ids = set()
        for line in encoded.splitlines():
            try:
                event = json.loads(
                    line.decode("utf-8"),
                    object_pairs_hook=CopilotAdapter._unique_object,
                )
            except (UnicodeError, ValueError) as error:
                raise ProviderUnknown("provider output event is malformed") from error
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ProviderUnknown("provider output event has an invalid shape")
            event_id = event.get("id")
            if (
                not isinstance(event_id, str)
                or not event_id
                or event_id in event_ids
            ):
                raise ProviderUnknown("provider output event ID is invalid")
            event_ids.add(event_id)
            event_type = event["type"]
            data = event.get("data")
            if event_type not in _CLI_EVENT_TYPES:
                raise ProviderUnknown("provider output event type is unsupported")
            if event_type == "tool.execution_start":
                if (
                    not isinstance(data, dict)
                    or not isinstance(data.get("toolCallId"), str)
                    or not data["toolCallId"]
                    or data["toolCallId"] in pending_tool_calls
                ):
                    raise ProviderUnknown("provider tool event is malformed")
                pending_tool_calls.add(data["toolCallId"])
                tool_calls += 1
            elif event_type == "tool.execution_complete":
                if (
                    not isinstance(data, dict)
                    or not isinstance(data.get("toolCallId"), str)
                    or data["toolCallId"] not in pending_tool_calls
                ):
                    raise ProviderUnknown("provider tool result is malformed")
                pending_tool_calls.remove(data["toolCallId"])
                completed_tool_calls += 1
            elif event_type == "session.error":
                if not isinstance(data, dict) or not isinstance(
                    data.get("message"), str
                ):
                    raise ProviderUnknown("provider error event is malformed")
                provider_errors.append(data["message"])
            elif event_type == "assistant.message":
                if not isinstance(data, dict) or not isinstance(
                    data.get("content"), str
                ):
                    raise ProviderUnknown("provider message event is malformed")
                assistant_message_seen = True
        if provider_errors and assistant_message_seen:
            raise ProviderUnknown("provider output has contradictory terminal events")
        return {
            "tool_calls_observed": tool_calls,
            "tool_calls_completed": completed_tool_calls,
            "provider_error": bool(provider_errors),
            "assistant_message_seen": assistant_message_seen,
        }

    def _terminal_outcome(
        self,
        status,
        op_id,
        context_id,
        provider_session_id,
        request_fp,
        workspace,
        output_path,
        exit_code,
        inputs_verified,
        tool_calls_observed,
        started_at,
        diagnostics,
    ):
        return {
            "status": status,
            "op_id": op_id,
            "context_id": context_id,
            "request_fp": request_fp,
            "provider_session_id": provider_session_id,
            "workspace_path": workspace,
            "output_path": output_path,
            "exit_code": exit_code,
            "inputs_verified": inputs_verified,
            "tool_calls_observed": tool_calls_observed,
            "started_at": started_at,
            "ended_at": self._timestamp(),
            "diagnostics": diagnostics,
        }

    @staticmethod
    def _unknown_outcome(
        op_id,
        context_id,
        provider_session_id,
        request_fp,
        workspace,
        reason,
        tool_calls_observed=0,
    ):
        return {
            "status": "UNKNOWN",
            "op_id": op_id,
            "context_id": context_id,
            "request_fp": request_fp,
            "provider_session_id": provider_session_id,
            "workspace_path": workspace,
            "tool_calls_observed": tool_calls_observed,
            "reason": reason,
        }

    def _stable_marker(self, session_id, prefix_marker=None):
        first = self._read_marker(session_id, prefix_marker)
        second = self._read_marker(session_id, prefix_marker)
        if first != second:
            raise ProviderUnknown("session marker changed during observation")
        return second

    def _read_marker(self, session_id, prefix_marker=None):
        components = (".copilot", "session-state", session_id)
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise ProviderUnknown("safe session-state file access is unavailable")

        directory_fd = os.open(
            str(self.home), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            for component in components:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(
                "events.jsonl",
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                file_stat = os.fstat(file_fd)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ProviderUnknown("session marker is not a regular file")
                with os.fdopen(file_fd, "rb", closefd=False) as event_file:
                    (
                        byte_length,
                        digest,
                        event_count,
                        last_event_id,
                        workspace,
                        prefix_sha256,
                        prefix_event_count,
                        prefix_last_event_id,
                    ) = self._parse_events(
                        event_file, session_id, prefix_marker
                    )
                after_stat = os.fstat(file_fd)
            finally:
                os.close(file_fd)
        finally:
            os.close(directory_fd)

        if (
            file_stat.st_dev != after_stat.st_dev
            or file_stat.st_ino != after_stat.st_ino
            or file_stat.st_size != after_stat.st_size
            or file_stat.st_mtime_ns != after_stat.st_mtime_ns
            or file_stat.st_ctime_ns != after_stat.st_ctime_ns
            or byte_length != after_stat.st_size
        ):
            raise ProviderUnknown("session marker changed while being read")
        marker = {
            "path": str(
                self.home
                / ".copilot"
                / "session-state"
                / session_id
                / "events.jsonl"
            ),
            "device": after_stat.st_dev,
            "inode": after_stat.st_ino,
            "byte_length": byte_length,
            "sha256": digest,
            "event_count": event_count,
            "last_event_id": last_event_id,
        }
        observation = {"marker": marker, "workspace_path": workspace}
        if prefix_marker is not None:
            observation.update(
                {
                    "prefix_sha256": prefix_sha256,
                    "prefix_event_count": prefix_event_count,
                    "prefix_last_event_id": prefix_last_event_id,
                }
            )
        return observation

    @staticmethod
    def _parse_events(event_file, session_id, prefix_marker=None):
        digest = hashlib.sha256()
        prefix_digest = hashlib.sha256()
        byte_length = 0
        event_count = 0
        event_ids = set()
        session_starts = []
        last_event_id = None
        prefix_complete = prefix_marker is None
        prefix_event_count = None
        prefix_last_event_id = None
        prefix_length = (
            prefix_marker["byte_length"] if prefix_marker is not None else None
        )
        for line in event_file:
            if not line.endswith(b"\n"):
                raise ProviderUnknown("session event log is incomplete")
            line_start = byte_length
            next_length = byte_length + len(line)
            try:
                event = json.loads(
                    line[:-1].decode("utf-8"),
                    object_pairs_hook=CopilotAdapter._unique_object,
                )
            except (UnicodeError, ValueError) as error:
                raise ProviderUnknown("session event log is malformed") from error
            if not isinstance(event, dict):
                raise ProviderUnknown("session event is not an object")
            event_id = event.get("id")
            if not isinstance(event_id, str) or not event_id:
                raise ProviderUnknown("session event ID is missing")
            if event_id in event_ids:
                raise ProviderUnknown("session event IDs are duplicated")
            event_ids.add(event_id)
            event_count += 1
            previous_event_id = last_event_id
            last_event_id = event_id
            if event.get("type") == "session.start":
                session_starts.append(event)
            if prefix_length is not None and not prefix_complete:
                if line_start < prefix_length:
                    if next_length > prefix_length:
                        raise ProviderUnknown(
                            "executor marker ends within an event record"
                        )
                    prefix_digest.update(line)
                    if next_length == prefix_length:
                        prefix_complete = True
                        prefix_event_count = event_count
                        prefix_last_event_id = event_id
                elif line_start == prefix_length:
                    prefix_complete = True
                    prefix_event_count = event_count - 1
                    prefix_last_event_id = previous_event_id
            digest.update(line)
            byte_length = next_length
        if event_count == 0:
            raise ProviderUnknown("session event log is empty")
        if prefix_length is not None:
            if not prefix_complete or prefix_length > byte_length:
                raise ProviderUnknown("executor marker is not a file prefix")
            if prefix_length == byte_length and prefix_event_count is None:
                prefix_event_count = event_count
                prefix_last_event_id = last_event_id
        if len(session_starts) != 1:
            raise ProviderUnknown("session start event is missing or duplicated")
        start_data = session_starts[0].get("data")
        if not isinstance(start_data, dict):
            raise ProviderUnknown("session start event data is malformed")
        if start_data.get("sessionId") != session_id:
            raise ProviderUnknown("session start event ID is inconsistent")
        context = start_data.get("context")
        if not isinstance(context, dict):
            raise ProviderUnknown("session start workspace is unavailable")
        workspace = context.get("cwd")
        if not isinstance(workspace, str) or not pathlib.Path(workspace).is_absolute():
            raise ProviderUnknown("session start workspace is malformed")
        try:
            canonical_workspace = str(pathlib.Path(workspace).resolve(strict=True))
        except (OSError, RuntimeError, ValueError) as error:
            raise ProviderUnknown(
                "session start workspace cannot be resolved"
            ) from error
        if canonical_workspace != workspace or not pathlib.Path(workspace).is_dir():
            raise ProviderUnknown("session start workspace is not canonical")
        return (
            byte_length,
            digest.hexdigest(),
            event_count,
            last_event_id,
            canonical_workspace,
            prefix_digest.hexdigest() if prefix_marker is not None else None,
            prefix_event_count,
            prefix_last_event_id,
        )

    @staticmethod
    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    @staticmethod
    def _validate_session_id(session_id):
        if (
            not isinstance(session_id, str)
            or not _SESSION_ID_PATTERN.fullmatch(session_id)
        ):
            raise ValueError("provider_session_id is not a safe exact-session ID")

    @staticmethod
    def _validate_marker(marker):
        if not isinstance(marker, dict) or set(marker) != _MARKER_FIELDS:
            raise ValueError("expected_marker has an invalid shape")
        for field in ("path", "last_event_id"):
            if not isinstance(marker[field], str) or not marker[field]:
                raise ValueError("expected_marker has an invalid " + field)
        for field in ("device", "inode", "byte_length", "event_count"):
            if (
                not isinstance(marker[field], int)
                or isinstance(marker[field], bool)
                or marker[field] < 0
            ):
                raise ValueError("expected_marker has an invalid " + field)
        if marker["inode"] < 1 or marker["event_count"] < 1:
            raise ValueError("expected_marker has an invalid identity")
        if (
            not isinstance(marker["sha256"], str)
            or not _DIGEST_PATTERN.fullmatch(marker["sha256"])
        ):
            raise ValueError("expected_marker has an invalid sha256")

    @staticmethod
    def _canonical_workspace(workspace_path):
        if not isinstance(workspace_path, str) or not workspace_path:
            raise ValueError("workspace_path must be a non-empty absolute path")
        path = pathlib.Path(workspace_path)
        if not path.is_absolute():
            raise ValueError("workspace_path must be absolute")
        try:
            canonical = str(path.resolve(strict=True))
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError("workspace_path cannot be resolved") from error
        if canonical != workspace_path or not path.is_dir():
            raise ValueError("workspace_path must be a canonical directory")
        return canonical

    @staticmethod
    def _observation(
        status, context_id, provider_session_id, workspace_path, reason
    ):
        return {
            "status": status,
            "context_id": context_id,
            "provider_session_id": provider_session_id,
            "workspace_path": workspace_path,
            "reason": reason,
        }

    @staticmethod
    def _timestamp():
        return datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        )
