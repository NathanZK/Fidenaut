import base64
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from scripts import copilot_adapter
from scripts import executor_protocol
from scripts import workflow_supervisor


class CopilotSessionObservationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.home = self.root / "home"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.session_id = "11111111-1111-4111-8111-111111111111"
        self.event_path = (
            self.home
            / ".copilot"
            / "session-state"
            / self.session_id
            / "events.jsonl"
        )
        self.event_path.parent.mkdir(parents=True)
        self.write_events()
        self.expected_marker = self.current_marker()
        self.adapter = copilot_adapter.CopilotAdapter(
            home=self.home, command_runner=lambda *args, **kwargs: None
        )

    def tearDown(self):
        self.temporary.cleanup()

    def write_events(self, workspace=None, session_id=None, events=None):
        workspace = workspace or self.workspace
        session_id = session_id or self.session_id
        if events is None:
            events = [
                {
                    "type": "session.start",
                    "data": {
                        "sessionId": session_id,
                        "context": {"cwd": str(workspace.resolve())},
                    },
                    "id": "start-1",
                },
                {"type": "assistant.message", "id": "event-1"},
            ]
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        self.event_path.write_bytes(
            b"".join(
                json.dumps(event, separators=(",", ":")).encode("utf-8")
                + b"\n"
                for event in events
            )
        )

    def current_marker(self):
        data = self.event_path.read_bytes()
        events = [json.loads(line) for line in data.splitlines()]
        stat_result = self.event_path.stat()
        return {
            "path": str(self.event_path.resolve()),
            "device": stat_result.st_dev,
            "inode": stat_result.st_ino,
            "byte_length": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "event_count": len(events),
            "last_event_id": events[-1]["id"],
        }

    def observe(self, expected_marker=None, workspace=None):
        return self.adapter.observe_session(
            self.session_id,
            self.session_id,
            str((workspace or self.workspace).resolve()),
            self.expected_marker if expected_marker is None else expected_marker,
        )

    def test_creation_key_maps_to_stable_uuid(self):
        first = copilot_adapter.derive_session_id("creation-key")
        second = copilot_adapter.derive_session_id("creation-key")

        self.assertEqual(first, second)
        self.assertEqual(first, str(uuid.UUID(first)))
        self.assertNotEqual(first, copilot_adapter.derive_session_id("other-key"))

    def test_provider_context_creation_fails_without_running_a_prompt(self):
        commands = []
        adapter = copilot_adapter.CopilotAdapter(
            home=self.home,
            command_runner=lambda *args, **kwargs: commands.append(args),
        )

        with self.assertRaises(copilot_adapter.ProviderUnavailable):
            adapter.create(
                "creation-key", "planner", str(self.workspace.resolve())
            )

        self.assertEqual([], commands)

    def test_resume_observation_binds_exact_session_workspace_and_marker(self):
        observation = self.observe()

        self.assertEqual("AVAILABLE", observation["status"])
        self.assertEqual(self.session_id, observation["provider_session_id"])
        self.assertEqual(str(self.workspace.resolve()), observation["workspace_path"])
        self.assertEqual(self.current_marker(), observation["marker"])

    def test_workspace_mismatch_is_unavailable(self):
        other_workspace = self.root / "other-workspace"
        other_workspace.mkdir()

        observation = self.observe(workspace=other_workspace)

        self.assertEqual("UNAVAILABLE", observation["status"])

    def test_absent_session_marker_is_unavailable(self):
        self.event_path.unlink()

        observation = self.observe()

        self.assertEqual("UNAVAILABLE", observation["status"])

    def test_malformed_session_event_is_unknown(self):
        self.event_path.write_bytes(b"{malformed-json\n")

        observation = self.observe()

        self.assertEqual("UNKNOWN", observation["status"])

    def test_duplicate_session_start_is_unknown(self):
        events = [
            {
                "type": "session.start",
                "data": {
                    "sessionId": self.session_id,
                    "context": {"cwd": str(self.workspace.resolve())},
                },
                "id": "start-1",
            },
            {
                "type": "session.start",
                "data": {
                    "sessionId": self.session_id,
                    "context": {"cwd": str(self.workspace.resolve())},
                },
                "id": "start-2",
            },
        ]
        self.write_events(events=events)

        observation = self.observe()

        self.assertEqual("UNKNOWN", observation["status"])

    def test_duplicate_event_json_key_is_unknown(self):
        self.event_path.write_bytes(
            b'{"type":"session.start","id":"start-1","id":"start-2",'
            b'"data":{"sessionId":"11111111-1111-4111-8111-111111111111",'
            b'"context":{"cwd":"' + str(self.workspace).encode("utf-8") + b'"}}}\n'
        )

        observation = self.observe()

        self.assertEqual("UNKNOWN", observation["status"])

    def test_session_start_identity_mismatch_is_unknown(self):
        self.write_events(session_id="22222222-2222-4222-8222-222222222222")

        observation = self.observe()

        self.assertEqual("UNKNOWN", observation["status"])

    def test_unstable_consecutive_marker_reads_are_unknown(self):
        first = self.adapter._read_marker(self.session_id)
        second = {
            "marker": dict(first["marker"], sha256="0" * 64),
            "workspace_path": first["workspace_path"],
        }
        with mock.patch.object(
            self.adapter, "_read_marker", side_effect=[first, second]
        ):
            observation = self.observe()

        self.assertEqual("UNKNOWN", observation["status"])

    def test_marker_read_error_does_not_expose_filesystem_details(self):
        with mock.patch.object(
            self.adapter,
            "_stable_marker",
            side_effect=OSError("private/home/path"),
        ):
            observation = self.observe()

        self.assertEqual("UNKNOWN", observation["status"])
        self.assertNotIn("private/home/path", observation["reason"])

    def test_previously_recorded_marker_mismatch_is_unavailable(self):
        expected = dict(self.expected_marker)
        self.event_path.write_bytes(
            self.event_path.read_bytes()
            + b'{"type":"assistant.message","id":"event-2"}\n'
        )

        observation = self.observe(expected_marker=expected)

        self.assertEqual("UNAVAILABLE", observation["status"])

    def test_symlinked_event_log_is_unavailable(self):
        original = self.root / "events.jsonl"
        original.write_bytes(self.event_path.read_bytes())
        self.event_path.unlink()
        self.event_path.symlink_to(original)

        observation = self.observe()

        self.assertEqual("UNAVAILABLE", observation["status"])

    def make_request(self, tool_policy="READ_ONLY"):
        instructions = self.workspace / "instructions.md"
        instructions.write_text("Review the change.", encoding="utf-8")
        return {
            "role": "planner",
            "workspace_path": str(self.workspace.resolve()),
            "instructions": {
                "path": str(instructions.resolve()),
                "sha256": hashlib.sha256(instructions.read_bytes()).hexdigest(),
            },
            "inputs": [],
            "output_path": str(
                (self.workspace / "ops" / "operation-1" / "result.md").resolve()
            ),
            "timeout_s": 10,
            "tool_policy": tool_policy,
        }

    def context_binding(self):
        return {
            "role": "planner",
            "context_id": "context-1",
            "provider_session_id": self.session_id,
            "workspace_path": str(self.workspace.resolve()),
        }

    @staticmethod
    def fingerprint(request):
        return executor_protocol.request_fingerprint(
            "operation-1", "context-1", request
        )

    @staticmethod
    def runner_result(stdout, outcome="success", exit_code=0, cleanup_verified=True):
        encoded = stdout.encode("utf-8")
        return {
            "outcome": outcome,
            "reason": "process-exited",
            "exit_code": exit_code,
            "cleanup_verified": cleanup_verified,
            "containment": {
                "kind": "posix-process-group",
                "cleanup_scope": "original-process-group",
                "escaped_descendants": "not-observable",
                "descendant_cleanup_verified": False,
            },
            "stdout": {
                "bytes": len(encoded),
                "base64": base64.b64encode(encoded).decode("ascii"),
                "observed_bytes": len(encoded),
                "observed_sha256": hashlib.sha256(encoded).hexdigest(),
            },
        }

    def test_read_only_turn_uses_exact_tools_and_bound_process_settings(self):
        request = self.make_request()
        calls = []

        def runner(command, **options):
            calls.append((command, options))
            with self.event_path.open("ab") as event_file:
                event_file.write(
                    b'{"type":"assistant.message","id":"event-2"}\n'
                )
            return self.runner_result(
                '{"type":"tool.execution_start","id":"start-1",'
                '"data":{"toolCallId":"call-1","tool":"view"}}\n'
                '{"type":"tool.execution_complete","id":"complete-1",'
                '"data":{"toolCallId":"call-1"}}\n'
                '{"type":"assistant.message","id":"output-1",'
                '"data":{"content":"Review complete"}}\n'
            )

        adapter = copilot_adapter.CopilotAdapter(
            home=self.home, command_runner=runner
        )
        outcome = adapter.run_turn(
            op_id="operation-1",
            context=self.context_binding(),
            request_fp=self.fingerprint(request),
            request=request,
            prompt="Review this change.",
            expected_marker=self.expected_marker,
        )

        self.assertEqual("SUCCESS", outcome["status"])
        self.assertEqual(
            {
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
            },
            set(outcome),
        )
        self.assertEqual("operation-1", outcome["op_id"])
        self.assertEqual("context-1", outcome["context_id"])
        self.assertEqual(self.fingerprint(request), outcome["request_fp"])
        self.assertTrue(outcome["inputs_verified"])
        self.assertEqual(1, outcome["tool_calls_observed"])
        self.assertEqual(1, len(calls))
        command, options = calls[0]
        self.assertIn("--resume=" + self.session_id, command)
        self.assertIn("--output-format", command)
        self.assertEqual("json", command[command.index("--output-format") + 1])
        self.assertIn("--available-tools=view,grep,glob", command)
        self.assertIn("--allow-tool=view,grep,glob", command)
        self.assertNotIn("--session-id", command)
        self.assertNotIn("--continue", command)
        self.assertNotIn("--allow-all-tools", command)
        self.assertEqual(request["workspace_path"], options["cwd"])
        self.assertEqual(10000, options["timeout_ms"])
        self.assertEqual("Copilot turn completed.", outcome["diagnostics"])

    def test_write_scoped_policy_is_refused_before_process_start(self):
        request = self.make_request("WRITE_SCOPED")
        calls = []
        adapter = copilot_adapter.CopilotAdapter(
            home=self.home,
            command_runner=lambda *args, **kwargs: calls.append(args),
        )

        with self.assertRaises(copilot_adapter.ProviderUnavailable):
            adapter.run_turn(
                op_id="operation-1",
                context=self.context_binding(),
                request_fp=self.fingerprint(request),
                request=request,
                prompt="Implement this change.",
                expected_marker=self.expected_marker,
            )

        self.assertEqual([], calls)

    def test_request_fingerprint_must_match_operation_context_and_request(self):
        request = self.make_request()
        calls = []
        adapter = copilot_adapter.CopilotAdapter(
            home=self.home,
            command_runner=lambda *args, **kwargs: calls.append(args),
        )

        with self.assertRaises(ValueError):
            adapter.run_turn(
                op_id="operation-1",
                context=self.context_binding(),
                request_fp="a" * 64,
                request=request,
                prompt="Review this change.",
                expected_marker=self.expected_marker,
            )

        self.assertEqual([], calls)

    def test_request_fingerprint_binds_operation_id(self):
        request = self.make_request()
        adapter = copilot_adapter.CopilotAdapter(
            home=self.home, command_runner=lambda *args, **kwargs: self.fail()
        )

        with self.assertRaises(ValueError):
            adapter.run_turn(
                op_id="operation-2",
                context=self.context_binding(),
                request_fp=self.fingerprint(request),
                request=request,
                prompt="Review this change.",
                expected_marker=self.expected_marker,
            )

    def test_request_fingerprint_binds_context_id(self):
        request = self.make_request()
        context = self.context_binding()
        context["context_id"] = "context-2"
        adapter = copilot_adapter.CopilotAdapter(
            home=self.home, command_runner=lambda *args, **kwargs: self.fail()
        )

        with self.assertRaises(ValueError):
            adapter.run_turn(
                op_id="operation-1",
                context=context,
                request_fp=self.fingerprint(request),
                request=request,
                prompt="Review this change.",
                expected_marker=self.expected_marker,
            )

    def test_failed_preflight_does_not_invoke_provider(self):
        request = self.make_request()
        calls = []
        adapter = copilot_adapter.CopilotAdapter(
            home=self.home,
            command_runner=lambda *args, **kwargs: calls.append(args),
        )
        changed_marker = dict(self.expected_marker)
        changed_marker["sha256"] = "0" * 64

        outcome = adapter.run_turn(
            op_id="operation-1",
            context=self.context_binding(),
            request_fp=self.fingerprint(request),
            request=request,
            prompt="Review this change.",
            expected_marker=changed_marker,
        )

        self.assertEqual("UNAVAILABLE", outcome["status"])
        self.assertEqual([], calls)

    def test_failed_preflight_does_not_read_request_inputs(self):
        request = self.make_request()
        request["instructions"]["path"] = str(
            (self.workspace / "missing-instructions.md").resolve()
        )
        adapter = copilot_adapter.CopilotAdapter(
            home=self.home, command_runner=lambda *args, **kwargs: self.fail()
        )
        changed_marker = dict(self.expected_marker, sha256="0" * 64)

        outcome = adapter.run_turn(
            op_id="operation-1",
            context=self.context_binding(),
            request_fp=self.fingerprint(request),
            request=request,
            prompt="Review this change.",
            expected_marker=changed_marker,
        )

        self.assertEqual("UNAVAILABLE", outcome["status"])

    def test_marker_replay_after_turn_stays_unknown(self):
        request = self.make_request()
        previous_bytes = self.event_path.read_bytes()

        def runner(command, **options):
            self.event_path.write_bytes(previous_bytes + previous_bytes)
            return self.runner_result(
                '{"type":"assistant.message","id":"output-1",'
                '"data":{"content":"Review complete"}}\n'
            )

        adapter = copilot_adapter.CopilotAdapter(
            home=self.home, command_runner=runner
        )
        outcome = adapter.run_turn(
            op_id="operation-1",
            context=self.context_binding(),
            request_fp=self.fingerprint(request),
            request=request,
            prompt="Review this change.",
            expected_marker=self.expected_marker,
        )

        self.assertEqual("UNKNOWN", outcome["status"])

    def test_post_turn_marker_error_does_not_expose_filesystem_details(self):
        adapter = copilot_adapter.CopilotAdapter(
            home=self.home, command_runner=lambda *args, **kwargs: None
        )
        with mock.patch.object(
            adapter,
            "_stable_marker",
            side_effect=OSError("private/home/path"),
        ):
            observation = adapter._observe_after_turn(
                "context-1",
                self.session_id,
                str(self.workspace.resolve()),
                self.expected_marker,
            )

        self.assertEqual("UNKNOWN", observation["status"])
        self.assertNotIn("private/home/path", observation["reason"])

    def test_request_workspace_cannot_override_context_binding(self):
        calls = []
        other_workspace = self.root / "other-workspace"
        other_workspace.mkdir()
        request = self.make_request()
        request["workspace_path"] = str(other_workspace.resolve())
        adapter = copilot_adapter.CopilotAdapter(
            home=self.home,
            command_runner=lambda *args, **kwargs: calls.append(args),
        )

        with self.assertRaises(copilot_adapter.ProviderUnavailable):
            adapter.run_turn(
                op_id="operation-1",
                context=self.context_binding(),
                request_fp=self.fingerprint(request),
                request=request,
                prompt="Review this change.",
                expected_marker=self.expected_marker,
            )

        self.assertEqual([], calls)

    def test_changed_instruction_is_rejected_before_process_start(self):
        request = self.make_request()
        pathlib.Path(request["instructions"]["path"]).write_text(
            "changed instruction", encoding="utf-8"
        )
        calls = []
        adapter = copilot_adapter.CopilotAdapter(
            home=self.home,
            command_runner=lambda *args, **kwargs: calls.append(args),
        )

        with self.assertRaises(ValueError):
            adapter.run_turn(
                op_id="operation-1",
                context=self.context_binding(),
                request_fp=self.fingerprint(request),
                request=request,
                prompt="Review this change.",
                expected_marker=self.expected_marker,
            )

        self.assertEqual([], calls)

    def test_nonzero_exit_with_tool_calls_stays_unknown_without_effect_evidence(self):
        request = self.make_request()

        def runner(command, **options):
            with self.event_path.open("ab") as event_file:
                event_file.write(
                    b'{"type":"assistant.message","id":"event-2"}\n'
                )
            return self.runner_result(
                '{"type":"tool.execution_start","id":"tool-1",'
                '"data":{"toolCallId":"call-1","tool":"view"}}\n'
                '{"type":"session.error","id":"error-1",'
                '"data":{"message":"provider failed"}}\n',
                outcome="nonzero-exit",
                exit_code=1,
            )

        adapter = copilot_adapter.CopilotAdapter(
            home=self.home, command_runner=runner
        )
        outcome = adapter.run_turn(
            op_id="operation-1",
            context=self.context_binding(),
            request_fp=self.fingerprint(request),
            request=request,
            prompt="Review this change.",
            expected_marker=self.expected_marker,
        )

        self.assertEqual("UNKNOWN", outcome["status"])
        self.assertEqual(1, outcome["tool_calls_observed"])

    def test_unknown_cli_event_type_cannot_be_classified_as_success(self):
        request = self.make_request()

        def runner(command, **options):
            with self.event_path.open("ab") as event_file:
                event_file.write(
                    b'{"type":"assistant.message","id":"event-2"}\n'
                )
            return self.runner_result(
                '{"type":"provider.custom_event","id":"output-1",'
                '"data":{}}\n'
            )

        adapter = copilot_adapter.CopilotAdapter(
            home=self.home, command_runner=runner
        )
        outcome = adapter.run_turn(
            op_id="operation-1",
            context=self.context_binding(),
            request_fp=self.fingerprint(request),
            request=request,
            prompt="Review this change.",
            expected_marker=self.expected_marker,
        )

        self.assertEqual("UNKNOWN", outcome["status"])

    def test_mismatched_tool_call_ids_cannot_be_classified_as_success(self):
        request = self.make_request()

        def runner(command, **options):
            with self.event_path.open("ab") as event_file:
                event_file.write(
                    b'{"type":"assistant.message","id":"event-2"}\n'
                )
            return self.runner_result(
                '{"type":"tool.execution_start","id":"start-1",'
                '"data":{"toolCallId":"call-1"}}\n'
                '{"type":"tool.execution_complete","id":"complete-1",'
                '"data":{"toolCallId":"call-2"}}\n'
                '{"type":"assistant.message","id":"output-1",'
                '"data":{"content":"Review complete"}}\n'
            )

        adapter = copilot_adapter.CopilotAdapter(
            home=self.home, command_runner=runner
        )
        outcome = adapter.run_turn(
            op_id="operation-1",
            context=self.context_binding(),
            request_fp=self.fingerprint(request),
            request=request,
            prompt="Review this change.",
            expected_marker=self.expected_marker,
        )

        self.assertEqual("UNKNOWN", outcome["status"])

    def test_nonzero_provider_error_without_tool_calls_is_provider_failure(self):
        request = self.make_request()

        def runner(command, **options):
            with self.event_path.open("ab") as event_file:
                event_file.write(
                    b'{"type":"assistant.message","id":"event-2"}\n'
                )
            return self.runner_result(
                '{"type":"session.error","id":"error-1",'
                '"data":{"message":"provider failed"}}\n',
                outcome="nonzero-exit",
                exit_code=1,
            )

        adapter = copilot_adapter.CopilotAdapter(
            home=self.home, command_runner=runner
        )
        outcome = adapter.run_turn(
            op_id="operation-1",
            context=self.context_binding(),
            request_fp=self.fingerprint(request),
            request=request,
            prompt="Review this change.",
            expected_marker=self.expected_marker,
        )

        self.assertEqual("PROVIDER_FAILURE", outcome["status"])
        self.assertEqual(0, outcome["tool_calls_observed"])
        self.assertEqual(
            "Copilot reported a terminal provider error.",
            outcome["diagnostics"],
        )

    def test_agent_failure_requires_executor_effect_accounting(self):
        request = self.make_request()

        def runner(command, **options):
            with self.event_path.open("ab") as event_file:
                event_file.write(
                    b'{"type":"assistant.message","id":"event-2"}\n'
                )
            return self.runner_result(
                '{"type":"tool.execution_start","id":"tool-1",'
                '"data":{"toolCallId":"call-1","tool":"view"}}\n'
                '{"type":"session.error","id":"error-1",'
                '"data":{"message":"provider failed"}}\n',
                outcome="nonzero-exit",
                exit_code=1,
            )

        adapter = copilot_adapter.CopilotAdapter(
            home=self.home, command_runner=runner
        )
        outcome = adapter.run_turn(
            op_id="operation-1",
            context=self.context_binding(),
            request_fp=self.fingerprint(request),
            request=request,
            prompt="Review this change.",
            expected_marker=self.expected_marker,
            executor_effects_accounted=True,
        )

        self.assertEqual("AGENT_FAILURE", outcome["status"])
        self.assertEqual(1, outcome["tool_calls_observed"])

    def test_unverified_process_group_exit_stays_unknown(self):
        request = self.make_request()
        calls = []

        def runner(command, **options):
            calls.append(command)
            result = self.runner_result("", outcome="timeout", exit_code=None)
            result["cleanup_verified"] = False
            return result

        adapter = copilot_adapter.CopilotAdapter(
            home=self.home, command_runner=runner
        )
        outcome = adapter.run_turn(
            op_id="operation-1",
            context=self.context_binding(),
            request_fp=self.fingerprint(request),
            request=request,
            prompt="Review this change.",
            expected_marker=self.expected_marker,
        )

        self.assertEqual("UNKNOWN", outcome["status"])
        self.assertEqual(1, len(calls))

    def test_real_supervisor_kills_descendant_before_unknown_outcome(self):
        request = self.make_request()
        pid_path = self.root / "child.pid"
        child_source = (
            "import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(30)"
        )
        parent_source = (
            "import pathlib,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); "
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
            "time.sleep(30)"
        )

        def runner(command, **options):
            return workflow_supervisor.supervise(
                [
                    sys.executable,
                    "-c",
                    parent_source,
                    str(pid_path),
                    child_source,
                ],
                timeout_ms=200,
                grace_ms=30,
                output_limit_bytes=4096,
            )

        adapter = copilot_adapter.CopilotAdapter(
            home=self.home, command_runner=runner
        )
        outcome = adapter.run_turn(
            op_id="operation-1",
            context=self.context_binding(),
            request_fp=self.fingerprint(request),
            request=request,
            prompt="Review this change.",
            expected_marker=self.expected_marker,
        )

        self.assertEqual("UNKNOWN", outcome["status"])
        self.assertTrue(pid_path.exists())
        child_pid = int(pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail("supervised descendant is still running")

    def test_environment_allowlist_does_not_forward_secret_variables(self):
        request = self.make_request()
        observed_environment = []

        def runner(command, **options):
            observed_environment.append(options["env"])
            with self.event_path.open("ab") as event_file:
                event_file.write(
                    b'{"type":"assistant.message","id":"event-2"}\n'
                )
            return self.runner_result(
                '{"type":"assistant.message","id":"output-1",'
                '"data":{"content":"secret response"}}\n'
            )

        adapter = copilot_adapter.CopilotAdapter(
            home=self.home, command_runner=runner
        )
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "must-not-forward"}):
            outcome = adapter.run_turn(
                op_id="operation-1",
                context=self.context_binding(),
                request_fp=self.fingerprint(request),
                request=request,
                prompt="Review this change.",
                expected_marker=self.expected_marker,
            )

        self.assertEqual("SUCCESS", outcome["status"])
        self.assertNotIn("GITHUB_TOKEN", observed_environment[0])
        self.assertNotIn("must-not-forward", outcome["diagnostics"])

    def test_adapter_terminal_outcome_conforms_to_neutral_executor_fake(self):
        executor_root = self.root / "executor"
        executor_workspace = self.root / "executor-workspace"
        executor_workspace.mkdir()
        instruction_path = executor_workspace / "instructions.md"
        instruction_path.write_text("Review the change.", encoding="utf-8")
        adapter_home = self.root / "executor-home"
        adapter_home.mkdir()
        invocations = []
        runner_calls = []

        def runner(command, **options):
            runner_calls.append(command)
            session_id = next(
                argument[len("--resume="):]
                for argument in command
                if argument.startswith("--resume=")
            )
            event_path = (
                adapter_home
                / ".copilot"
                / "session-state"
                / session_id
                / "events.jsonl"
            )
            with event_path.open("ab") as event_file:
                event_file.write(
                    b'{"type":"assistant.message","id":"turn-result"}\n'
                )
            return self.runner_result(
                '{"type":"assistant.message","id":"output-result",'
                '"data":{"content":"Review complete"}}\n'
            )

        adapter = copilot_adapter.CopilotAdapter(
            home=adapter_home, command_runner=runner
        )

        class AdapterFake:
            def invoke(fake_self, operation, context, request):
                invocations.append(operation["op_id"])
                session_id = context["provider_session_id"]
                event_path = (
                    adapter_home
                    / ".copilot"
                    / "session-state"
                    / session_id
                    / "events.jsonl"
                )
                event_path.parent.mkdir(parents=True, exist_ok=True)
                event_path.write_text(
                    json.dumps(
                        {
                            "type": "session.start",
                            "id": "session-start",
                            "data": {
                                "sessionId": session_id,
                                "context": {
                                    "cwd": context["workspace_path"],
                                },
                            },
                        },
                        separators=(",", ":"),
                    )
                    + "\n",
                    encoding="utf-8",
                )
                content = event_path.read_bytes()
                event = json.loads(content.splitlines()[-1])
                stat_result = event_path.stat()
                marker = {
                    "path": str(event_path.resolve()),
                    "device": stat_result.st_dev,
                    "inode": stat_result.st_ino,
                    "byte_length": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "event_count": 1,
                    "last_event_id": event["id"],
                }
                return adapter.run_turn(
                    op_id=operation["op_id"],
                    context=context,
                    request_fp=operation["request_fp"],
                    request=request,
                    prompt="Review the change.",
                    expected_marker=marker,
                )

        fake = AdapterFake()
        executor = executor_protocol.Executor(executor_root, fake=fake)
        context = executor.create(
            "adapter-neutral-key",
            "planner",
            str(executor_workspace.resolve()),
        )
        request = {
            "role": "planner",
            "workspace_path": str(executor_workspace.resolve()),
            "base": {"repo_head": "0" * 40},
            "instructions": {
                "path": str(instruction_path.resolve()),
                "sha256": hashlib.sha256(
                    instruction_path.read_bytes()
                ).hexdigest(),
            },
            "inputs": [],
            "output_path": str((executor_workspace / "output.md").resolve()),
            "timeout_s": 30,
            "tool_policy": "READ_ONLY",
            "adapter": {
                "name": "copilot",
                "version": "1",
                "model": "test",
                "config_sha256": "2" * 64,
            },
        }
        op_id = uuid.uuid4().hex
        request_fp = executor_protocol.request_fingerprint(
            op_id, context["context_id"], request
        )

        submitted = executor.submit(
            op_id,
            1,
            context["context_id"],
            request_fp,
            request,
        )
        repeated = executor.submit(
            op_id,
            2,
            context["context_id"],
            request_fp,
            request,
        )
        terminal = executor.get_outcome(op_id, fence_through=1)

        self.assertEqual("ACCEPTED", submitted["status"])
        self.assertEqual("ACCEPTED", repeated["status"])
        self.assertEqual("SUCCESS", terminal["status"])
        self.assertEqual([op_id], invocations)
        self.assertEqual(1, len(runner_calls))


if __name__ == "__main__":
    unittest.main()
