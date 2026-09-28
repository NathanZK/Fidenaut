import base64
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from scripts import workflow_supervisor as supervisor


PYTHON = sys.executable


def python_command(source, *arguments):
    return [PYTHON, "-c", source, *map(str, arguments)]


def decoded(result, stream):
    return base64.b64decode(result[stream]["base64"])


def process_is_gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def wait_for_path(path, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.005)
    raise AssertionError("timed out waiting for %s" % path)


class WorkflowSupervisorTest(unittest.TestCase):
    def run_supervised(self, command, **overrides):
        options = {
            "timeout_ms": 2000,
            "grace_ms": 100,
            "output_limit_bytes": 4096,
        }
        options.update(overrides)
        return supervisor.supervise(command, **options)

    def assert_process_gone(self, pid):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if process_is_gone(pid):
                return
            time.sleep(0.02)
        self.fail("supervised descendant %s is still running" % pid)

    def test_managed_runtime_assigns_distinct_resumable_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = supervisor.ManagedExecutionRuntime(pathlib.Path(directory))
            planner = runtime.establish("run-60", "planner")
            reviewer = runtime.establish("run-60", "reviewer")

            first = runtime.execute(planner, "plan", {"revision": 1})
            second = runtime.execute(planner, "plan-revision", {"revision": 2})
            reviewed = runtime.execute(reviewer, "review", {"artifact": "plan"})

            self.assertNotEqual(
                planner["instance_id"], reviewer["instance_id"]
            )
            self.assertEqual(planner["instance_id"], first["instance_id"])
            self.assertEqual(planner["instance_id"], second["instance_id"])
            self.assertEqual(2, second["history_length"])
            self.assertEqual(reviewer["instance_id"], reviewed["instance_id"])
            self.assertEqual(1, reviewed["history_length"])
            self.assertEqual("runtime-executed", second["execution_evidence"])

            resumed = runtime.resume(planner)
            self.assertEqual(planner["instance_id"], resumed["instance_id"])
            self.assertEqual(2, resumed["history_length"])

            runtime.close()

    def test_managed_runtime_rejects_aliasing_and_preserves_original_on_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = supervisor.ManagedExecutionRuntime(pathlib.Path(directory))
            planner = runtime.establish("run-60", "planner")
            runtime.execute(planner, "plan", {"revision": 1})
            alias = dict(planner)
            alias["role"] = "reviewer"

            with self.assertRaises(ValueError):
                runtime.assert_distinct([planner, alias])

            runtime.stop(planner)
            with self.assertRaises(supervisor.RuntimeUnavailable):
                runtime.resume(planner)
            replacement = runtime.replace(planner, "planner")
            self.assertNotEqual(
                planner["instance_id"], replacement["instance_id"]
            )
            self.assertEqual(planner["instance_id"], replacement["replaces"])
            runtime.close()

    def test_managed_role_work_outlives_controller_wait_and_is_not_reexecuted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            runtime = supervisor.ManagedExecutionRuntime(root / "runtime")
            binding = runtime.establish("issue-63", "planner")
            output = root / "plan.md"
            counter = root / "executions"
            source = (
                "import pathlib,sys,time; "
                "counter=pathlib.Path(sys.argv[1]); "
                "counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1'); "
                "time.sleep(2.15); "
                "pathlib.Path(sys.argv[2]).write_text('completed')"
            )
            started = time.monotonic()
            receipt = runtime.execute(
                binding,
                "plan",
                {"output": str(output)},
                command=python_command(source, counter, output),
                cwd=root,
                output=output,
                timeout=5,
                request_id="long-role-operation",
            )
            self.assertGreaterEqual(time.monotonic() - started, 2)
            self.assertEqual("completed", output.read_text())
            self.assertEqual("1", counter.read_text())
            self.assertEqual(binding["instance_id"], receipt["instance_id"])
            self.assertEqual("planner", receipt["role"])
            self.assertEqual("long-role-operation", receipt["request_id"])
            self.assertEqual(1, receipt["history_length"])
            self.assertEqual(
                receipt,
                runtime.recover(
                    binding,
                    receipt["request_id"],
                    receipt["request_sha256"],
                ),
            )
            retried = runtime.execute(
                binding,
                "plan",
                {"output": str(output)},
                command=python_command(source, counter, output),
                cwd=root,
                output=output,
                timeout=5,
                request_id="long-role-operation",
            )
            self.assertEqual(receipt, retried)
            runtime.close()
            history = json.loads(
                pathlib.Path(binding["history_path"]).read_text()
            )
            self.assertEqual(1, len(history))
            self.assertEqual("completed", history[0]["status"])
            self.assertEqual(
                "idempotent-replay-response-written",
                history[0]["recovery_status"],
            )
            self.assertEqual(3, history[0]["recovery_attempts"])
            self.assertEqual("1", counter.read_text())

    def test_managed_runtime_recovers_after_controller_disconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            runtime = supervisor.ManagedExecutionRuntime(root / "runtime")
            binding = runtime.establish("issue-63", "planner")
            output = root / "plan.md"
            started = root / "started"
            release = root / "release"
            counter = root / "executions"
            source = (
                "import pathlib,sys,time; "
                "started,release,counter,output=map(pathlib.Path,sys.argv[1:]); "
                "started.write_text('yes'); "
                "deadline=time.monotonic()+5; "
                "exec(\"while not release.exists() and time.monotonic()<deadline: time.sleep(.01)\"); "
                "counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1'); "
                "output.write_text('completed')"
            )
            request = runtime._execution_request(
                binding,
                "plan",
                {"output": str(output)},
                command=python_command(source, started, release, counter, output),
                cwd=root,
                output=output,
                timeout=5,
                request_id="disconnected-role-operation",
            )
            response_errors = []

            def disconnecting_request():
                try:
                    runtime._request(binding, request, timeout=0.2)
                except supervisor.RuntimeResponseTimeout as error:
                    response_errors.append(error)

            controller = threading.Thread(target=disconnecting_request)
            controller.start()
            wait_for_path(started)
            controller.join(timeout=2)
            self.assertFalse(controller.is_alive())
            self.assertEqual(1, len(response_errors))
            self.assertFalse(output.exists())
            release.write_text("continue")

            receipt = runtime.recover(
                binding,
                request["request_id"],
                request["request_sha256"],
                timeout=5,
            )
            self.assertEqual(binding["instance_id"], receipt["instance_id"])
            self.assertEqual("1", counter.read_text())
            self.assertEqual("completed", output.read_text())
            self.assertEqual(1, receipt["history_length"])
            runtime.close()
            history = json.loads(pathlib.Path(binding["history_path"]).read_text())
            self.assertEqual("completed", history[0]["status"])
            self.assertEqual(1, history[0]["recovery_attempts"])
            self.assertEqual("recovery-response-written", history[0]["recovery_status"])
            self.assertEqual("written", history[0]["response_write_status"])

    def test_managed_runtime_rejects_stale_mismatched_and_wrong_context_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            runtime = supervisor.ManagedExecutionRuntime(root / "runtime")
            planner = runtime.establish("issue-63", "planner")
            reviewer = runtime.establish("issue-63", "reviewer")
            output = root / "plan.md"
            counter = root / "executions"
            command = python_command(
                "import pathlib,sys; "
                "counter,output=map(pathlib.Path,sys.argv[1:]); "
                "counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1'); "
                "output.write_text('completed')",
                counter,
                output,
            )
            receipt = runtime.execute(
                planner,
                "plan",
                {"output": str(output)},
                command=command,
                cwd=root,
                output=output,
                timeout=5,
                request_id="bound-role-operation",
            )
            self.assertIsNone(
                runtime.recover(planner, "stale-operation", receipt["request_sha256"])
            )
            with self.assertRaises(supervisor.RuntimeUnavailable):
                runtime.recover(
                    planner,
                    receipt["request_id"],
                    "0" * 64,
                )
            with self.assertRaises(supervisor.RuntimeUnavailable):
                runtime.recover(
                    reviewer,
                    receipt["request_id"],
                    receipt["request_sha256"],
                )
            with self.assertRaises(supervisor.RuntimeUnavailable):
                runtime.execute(
                    planner,
                    "different-operation",
                    {"output": str(output)},
                    command=command,
                    cwd=root,
                    output=output,
                    timeout=5,
                    request_id=receipt["request_id"],
                )
            duplicate = runtime.execute(
                planner,
                "plan",
                {"output": str(output)},
                command=command,
                cwd=root,
                output=output,
                timeout=5,
                request_id=receipt["request_id"],
            )
            self.assertEqual(receipt, duplicate)
            runtime.close()
            history = json.loads(
                pathlib.Path(planner["history_path"]).read_text()
            )
            self.assertEqual(1, len(history))
            self.assertEqual(1, history[0]["recovery_attempts"])
            self.assertEqual(
                "idempotent-replay-response-written",
                history[0]["recovery_status"],
            )
            self.assertEqual("1", counter.read_text())

    def test_distinct_logical_operations_with_identical_contents_execute_once_each(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            runtime = supervisor.ManagedExecutionRuntime(root / "runtime")
            binding = runtime.establish("issue-63", "planner")
            output = root / "plan.md"
            counter = root / "executions"
            command = python_command(
                "import pathlib,sys; "
                "counter,output=map(pathlib.Path,sys.argv[1:]); "
                "counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1'); "
                "output.write_text('identical-request-content')",
                counter,
                output,
            )

            def execute(operation_id):
                return runtime.execute(
                    binding,
                    "plan",
                    {"output": str(output)},
                    command=command,
                    cwd=root,
                    output=output,
                    timeout=5,
                    request_id=operation_id,
                )

            first = execute("logical-operation-1")
            retry = execute("logical-operation-1")
            new_operation = execute("logical-operation-2")
            later_revision = execute("logical-plan-revision-2")
            self.assertEqual(first, retry)
            self.assertEqual("logical-operation-1", first["request_id"])
            self.assertEqual("logical-operation-2", new_operation["request_id"])
            self.assertEqual(
                "logical-plan-revision-2", later_revision["request_id"]
            )
            self.assertEqual(1, first["history_length"])
            self.assertEqual(2, new_operation["history_length"])
            self.assertEqual(3, later_revision["history_length"])
            self.assertEqual(first["request_sha256"], new_operation["request_sha256"])
            self.assertEqual(first["request_sha256"], later_revision["request_sha256"])
            self.assertEqual("3", counter.read_text())
            runtime.close()

    def test_operation_ids_are_reserved_across_role_and_replacement_contexts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            runtime = supervisor.ManagedExecutionRuntime(root / "runtime")
            planner = runtime.establish("issue-63", "planner")
            reviewer = runtime.establish("issue-63", "reviewer")
            output = root / "plan.md"
            counter = root / "executions"
            command = python_command(
                "import pathlib,sys; "
                "counter,output=map(pathlib.Path,sys.argv[1:]); "
                "counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1'); "
                "output.write_text('completed')",
                counter,
                output,
            )

            def execute(binding):
                return runtime.execute(
                    binding,
                    "plan",
                    {"output": str(output)},
                    command=command,
                    cwd=root,
                    output=output,
                    timeout=5,
                    request_id="globally-owned-operation",
                )

            receipt = execute(planner)
            with self.assertRaises(supervisor.RuntimeUnavailable):
                execute(reviewer)
            replacement = runtime.replace(planner, "planner")
            with self.assertRaises(supervisor.RuntimeUnavailable):
                execute(replacement)
            self.assertEqual("1", counter.read_text())
            self.assertEqual(
                [],
                json.loads(pathlib.Path(reviewer["history_path"]).read_text())
                if pathlib.Path(reviewer["history_path"]).exists()
                else [],
            )
            self.assertEqual(
                [],
                json.loads(pathlib.Path(replacement["history_path"]).read_text())
                if pathlib.Path(replacement["history_path"]).exists()
                else [],
            )
            self.assertEqual(
                receipt,
                runtime.execute(
                    planner,
                    "plan",
                    {"output": str(output)},
                    command=command,
                    cwd=root,
                    output=output,
                    timeout=5,
                    request_id="globally-owned-operation",
                ),
            )
            self.assertEqual("1", counter.read_text())
            runtime.close()

    def test_managed_runtime_fails_closed_when_completion_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            runtime = supervisor.ManagedExecutionRuntime(root / "runtime")
            binding = runtime.establish("issue-63", "planner")
            started = root / "started"
            release = root / "release"
            output = root / "plan.md"
            source = (
                "import pathlib,sys,time; "
                "started,release,output=map(pathlib.Path,sys.argv[1:]); "
                "started.write_text('yes'); "
                "exec(\"while not release.exists(): time.sleep(.01)\"); "
                "output.write_text('should-not-complete')"
            )
            request = runtime._execution_request(
                binding,
                "plan",
                {"output": str(output)},
                command=python_command(source, started, release, output),
                cwd=root,
                output=output,
                timeout=5,
                request_id="ambiguous-role-operation",
            )
            request_errors = []

            def blocked_request():
                try:
                    runtime._request(binding, request, timeout=5)
                except supervisor.RuntimeUnavailable as error:
                    request_errors.append(error)

            controller = threading.Thread(target=blocked_request)
            controller.start()
            wait_for_path(started)
            os.killpg(binding["process_group"], signal.SIGKILL)
            controller.join(timeout=2)
            self.assertFalse(controller.is_alive())
            self.assertTrue(request_errors)
            with self.assertRaises(supervisor.RuntimeUnavailable):
                runtime.recover(
                    binding,
                    request["request_id"],
                    request["request_sha256"],
                )
            history = json.loads(
                pathlib.Path(binding["history_path"]).read_text()
            )
            self.assertEqual("running", history[0]["status"])
            self.assertFalse(output.exists())
            runtime.close()

    def test_success_returns_bounded_structured_output(self):
        result = self.run_supervised(
            python_command(
                "import sys; "
                "sys.stdout.buffer.write(b'out'); "
                "sys.stderr.buffer.write(b'err')"
            )
        )
        self.assertEqual("success", result["outcome"])
        self.assertEqual(0, result["exit_code"])
        self.assertEqual(b"out", decoded(result, "stdout"))
        self.assertEqual(b"err", decoded(result, "stderr"))
        self.assertEqual(supervisor.RESULT_FORMAT, result["format"])
        self.assertEqual(
            {
                "timeout_ms": 2000,
                "grace_ms": 100,
                "stdout_bytes": 4096,
                "stderr_bytes": 4096,
            },
            result["limits"],
        )
        self.assertTrue(result["cleanup_verified"])
        self.assertFalse(
            result["containment"]["descendant_cleanup_verified"]
        )

    def test_nonzero_exit_is_not_a_supervisor_failure(self):
        result = self.run_supervised(python_command("raise SystemExit(7)"))
        self.assertEqual("nonzero-exit", result["outcome"])
        self.assertEqual(7, result["exit_code"])
        self.assertIsNone(result["terminating_signal"])

    def test_process_signal_is_reported(self):
        result = self.run_supervised(
            python_command(
                "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
            )
        )
        self.assertEqual("signal", result["outcome"])
        self.assertEqual(signal.SIGTERM, result["terminating_signal"])

    def test_timeout_terminates_descendant_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = pathlib.Path(directory) / "child.pid"
            source = (
                "import pathlib, subprocess, sys, time; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
                "time.sleep(60)"
            )
            result = self.run_supervised(
                python_command(source, pid_file), timeout_ms=150
            )
            child_pid = int(pid_file.read_text())
            self.assertEqual("timeout", result["outcome"])
            self.assertTrue(result["cleanup_verified"])
            self.assert_process_gone(child_pid)

    def test_timeout_counts_selector_setup_time(self):
        original_register = supervisor.selectors.DefaultSelector.register
        real_monotonic = supervisor.time.monotonic
        start = real_monotonic()
        calls = []
        registration_completed = False
        expiration_reported = False

        def register(selector, *args, **kwargs):
            nonlocal registration_completed
            calls.append(True)
            result = original_register(selector, *args, **kwargs)
            registration_completed = True
            return result

        def monotonic():
            nonlocal expiration_reported
            if registration_completed and not expiration_reported:
                expiration_reported = True
                return start + 1
            return start if not registration_completed else real_monotonic()

        with mock.patch.object(
            supervisor.selectors.DefaultSelector,
            "register",
            register,
        ), mock.patch.object(
            supervisor.time,
            "monotonic",
            monotonic,
        ):
            result = self.run_supervised(
                python_command("import time; time.sleep(60)"),
                timeout_ms=10,
            )
        self.assertEqual(1, len(calls))
        self.assertTrue(expiration_reported)
        self.assertEqual("timeout", result["outcome"])
        self.assertTrue(result["cleanup_verified"])

    def test_ignored_sigterm_escalates_to_sigkill(self):
        result = self.run_supervised(
            python_command(
                "import signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(60)"
            ),
            timeout_ms=100,
            grace_ms=50,
        )
        self.assertEqual("timeout", result["outcome"])
        self.assertTrue(result["forced_termination"])
        self.assertEqual(signal.SIGKILL, result["terminating_signal"])
        self.assertTrue(result["cleanup_verified"])

    def test_parent_exit_with_live_descendant_is_terminated(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = pathlib.Path(directory) / "child.pid"
            source = (
                "import pathlib, subprocess, sys; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
            )
            result = self.run_supervised(python_command(source, pid_file))
            child_pid = int(pid_file.read_text())
            self.assertEqual("terminated", result["outcome"])
            self.assertEqual("process-group-remained", result["reason"])
            self.assertTrue(result["cleanup_verified"])
            self.assert_process_gone(child_pid)

    def test_output_limit_terminates_instead_of_truncating_success(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = pathlib.Path(directory) / "child.pid"
            source = (
                "import pathlib, signal, subprocess, sys, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
                "sys.stdout.buffer.write(b'x'*8192); sys.stdout.flush(); "
                "time.sleep(60)"
            )
            result = self.run_supervised(
                python_command(source, pid_file),
                output_limit_bytes=128,
                grace_ms=30,
            )
            child_pid = int(pid_file.read_text())
            self.assertEqual("output-limit", result["outcome"])
            self.assertEqual(
                128, result["stdout"]["bytes"] + result["stderr"]["bytes"]
            )
            self.assertTrue(result["forced_termination"])
            self.assertTrue(result["cleanup_verified"])
            self.assert_process_gone(child_pid)

    def test_stdout_sink_consumes_the_whole_stream_without_terminating(self):
        payload = b"y" * 8192
        chunks = []
        source = (
            "import sys; "
            "sys.stdout.buffer.write(b'y'*8192); sys.stdout.flush()"
        )
        result = self.run_supervised(
            python_command(source),
            output_limit_bytes=128,
            stdout_sink=chunks.append,
        )

        self.assertEqual("success", result["outcome"])
        self.assertEqual("process-exited", result["reason"])
        self.assertEqual(0, result["exit_code"])
        self.assertFalse(result["forced_termination"])
        self.assertEqual(payload, b"".join(chunks))
        self.assertEqual(0, result["stdout"]["bytes"])
        self.assertEqual("", result["stdout"]["base64"])
        self.assertEqual(len(payload), result["stdout"]["observed_bytes"])
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            result["stdout"]["observed_sha256"],
        )

    def test_stderr_limit_still_terminates_while_a_stdout_sink_is_active(self):
        source = (
            "import sys, time; "
            "sys.stderr.buffer.write(b'e'*8192); sys.stderr.flush(); "
            "time.sleep(60)"
        )
        result = self.run_supervised(
            python_command(source),
            output_limit_bytes=1024,
            stderr_limit_bytes=128,
            grace_ms=30,
            stdout_sink=lambda _chunk: None,
        )

        self.assertEqual("output-limit", result["outcome"])
        self.assertEqual("per-stream-output-limit", result["reason"])
        self.assertEqual(128, result["stderr"]["bytes"])
        self.assertGreater(result["stderr"]["observed_bytes"], 128)

    def test_observed_identity_matches_retained_bytes_without_a_sink(self):
        payload = b"observed"
        source = "import sys; sys.stdout.buffer.write(b'observed')"
        result = self.run_supervised(python_command(source), output_limit_bytes=1024)

        self.assertEqual(payload, decoded(result, "stdout"))
        self.assertEqual(len(payload), result["stdout"]["observed_bytes"])
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            result["stdout"]["observed_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(b"").hexdigest(), result["stderr"]["observed_sha256"]
        )

    def test_noncallable_stdout_sink_is_rejected_before_start(self):
        with self.assertRaises(ValueError):
            supervisor.supervise(
                python_command("pass"),
                timeout_ms=1000,
                grace_ms=10,
                output_limit_bytes=1024,
                stdout_sink="not callable",
            )

    def test_concurrent_stream_overflow_has_independent_deterministic_budgets(self):
        outcomes = []
        for _ in range(10):
            with tempfile.TemporaryDirectory() as directory:
                stdout_ready = pathlib.Path(directory) / "stdout-ready"
                stderr_ready = pathlib.Path(directory) / "stderr-ready"
                writer = (
                    "import os,pathlib,sys,time; "
                    "os.write(int(sys.argv[1]),sys.argv[3].encode()*4096); "
                    "pathlib.Path(sys.argv[2]).write_text('ready'); "
                    "time.sleep(60)"
                )
                source = (
                    "import subprocess,sys,time; "
                    "subprocess.Popen([sys.executable,'-c',sys.argv[3],'1',sys.argv[1],'o']); "
                    "subprocess.Popen([sys.executable,'-c',sys.argv[3],'2',sys.argv[2],'e']); "
                    "time.sleep(60)"
                )
                original_select = supervisor.selectors.DefaultSelector.select

                def wait_then_select(selector, timeout=None):
                    wait_for_path(stdout_ready)
                    wait_for_path(stderr_ready)
                    return original_select(selector, timeout)

                with mock.patch.object(
                    supervisor.selectors.DefaultSelector,
                    "select",
                    wait_then_select,
                ):
                    result = self.run_supervised(
                        python_command(
                            source, stdout_ready, stderr_ready, writer
                        ),
                        output_limit_bytes=128,
                    )
                outcomes.append(result)
        for result in outcomes:
            self.assertEqual("output-limit", result["outcome"])
            self.assertEqual(b"o" * 128, decoded(result, "stdout"))
            self.assertEqual(b"e" * 128, decoded(result, "stderr"))
        retained = {
            (decoded(result, "stdout"), decoded(result, "stderr"))
            for result in outcomes
        }
        self.assertEqual({(b"o" * 128, b"e" * 128)}, retained)

    def test_asymmetric_stdout_and_stderr_limits_are_independent(self):
        cases = (
            (
                "import sys,time; "
                "sys.stderr.buffer.write(b'e'*64); sys.stderr.flush(); "
                "sys.stdout.buffer.write(b'o'*129); sys.stdout.flush(); "
                "time.sleep(60)",
                b"o" * 128,
                b"e" * 64,
            ),
            (
                "import sys,time; "
                "sys.stdout.buffer.write(b'o'*128); sys.stdout.flush(); "
                "sys.stderr.buffer.write(b'e'*65); sys.stderr.flush(); "
                "time.sleep(60)",
                b"o" * 128,
                b"e" * 64,
            ),
        )
        for source, expected_stdout, expected_stderr in cases:
            with self.subTest(source=source):
                result = self.run_supervised(
                    python_command(source),
                    output_limit_bytes=128,
                    stderr_limit_bytes=64,
                )
                self.assertEqual("output-limit", result["outcome"])
                self.assertEqual(expected_stdout, decoded(result, "stdout"))
                self.assertEqual(expected_stderr, decoded(result, "stderr"))
                self.assertEqual(128, result["limits"]["stdout_bytes"])
                self.assertEqual(64, result["limits"]["stderr_bytes"])

    def test_default_stderr_limit_remains_equal_to_stdout_limit(self):
        result = self.run_supervised(
            python_command(
                "import sys,time; "
                "sys.stdout.buffer.write(b'o'*129); sys.stdout.flush(); "
                "sys.stderr.buffer.write(b'e'*129); sys.stderr.flush(); "
                "time.sleep(60)"
            ),
            output_limit_bytes=128,
        )

        self.assertEqual("output-limit", result["outcome"])
        self.assertEqual(b"o" * 128, decoded(result, "stdout"))
        self.assertEqual(b"e" * 128, decoded(result, "stderr"))
        self.assertEqual(
            {"timeout_ms": 2000, "grace_ms": 100, "stdout_bytes": 128, "stderr_bytes": 128},
            result["limits"],
        )

    def test_invalid_stderr_limit_is_rejected_before_start(self):
        for value in (0, -1, False, "64"):
            with self.subTest(value=value), mock.patch.object(
                supervisor.subprocess, "Popen"
            ) as popen:
                with self.assertRaises(ValueError):
                    self.run_supervised(
                        python_command("raise SystemExit(99)"),
                        stderr_limit_bytes=value,
                    )
                popen.assert_not_called()

    def test_timeout_cleans_multiple_children_and_grandchild(self):
        with tempfile.TemporaryDirectory() as directory:
            first_file = pathlib.Path(directory) / "first.pid"
            second_file = pathlib.Path(directory) / "second.pid"
            grandchild_file = pathlib.Path(directory) / "grandchild.pid"
            grandchild_source = (
                "import signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(60)"
            )
            second_source = (
                "import pathlib,signal,subprocess,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "child=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
                "time.sleep(60)"
            )
            source = (
                "import pathlib,signal,subprocess,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "first=subprocess.Popen([sys.executable,'-c',sys.argv[4]]); "
                "second=subprocess.Popen([sys.executable,'-c',sys.argv[5],sys.argv[3],sys.argv[4]]); "
                "pathlib.Path(sys.argv[1]).write_text(str(first.pid)); "
                "pathlib.Path(sys.argv[2]).write_text(str(second.pid)); "
                "time.sleep(60)"
            )
            result = self.run_supervised(
                python_command(
                    source,
                    first_file,
                    second_file,
                    grandchild_file,
                    grandchild_source,
                    second_source,
                ),
                timeout_ms=300,
                grace_ms=30,
            )
            pids = [
                int(first_file.read_text()),
                int(second_file.read_text()),
                int(grandchild_file.read_text()),
            ]
            self.assertEqual("timeout", result["outcome"])
            self.assertTrue(result["forced_termination"])
            self.assertTrue(result["cleanup_verified"])
            for pid in pids:
                self.assert_process_gone(pid)

    def test_escaped_process_group_is_not_claimed_as_contained(self):
        with tempfile.TemporaryDirectory() as directory:
            escaped_file = pathlib.Path(directory) / "escaped.pid"
            escaped_source = (
                "import os,pathlib,sys,time; "
                "os.setpgid(0,0); "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                "time.sleep(60)"
            )
            source = (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]]); "
                "time.sleep(60)"
            )
            result = self.run_supervised(
                python_command(source, escaped_file, escaped_source),
                timeout_ms=300,
            )
            escaped_pid = int(escaped_file.read_text())
            try:
                self.assertEqual("timeout", result["outcome"])
                self.assertTrue(result["cleanup_verified"])
                self.assertEqual(
                    "original-process-group",
                    result["containment"]["cleanup_scope"],
                )
                self.assertEqual(
                    "not-observable",
                    result["containment"]["escaped_descendants"],
                )
                self.assertFalse(
                    result["containment"]["descendant_cleanup_verified"]
                )
                self.assertFalse(process_is_gone(escaped_pid))
            finally:
                try:
                    os.kill(escaped_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.assert_process_gone(escaped_pid)

    def test_startup_failure_is_structured_and_deterministic(self):
        command = ["/definitely/not/a/chess-echo-command"]
        first = self.run_supervised(command)
        second = self.run_supervised(command)
        self.assertEqual(first, second)
        self.assertEqual("startup-failure", first["outcome"])
        self.assertEqual("FileNotFoundError", first["supervisor_error"])

    def test_cancellation_terminates_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = pathlib.Path(directory) / "child.pid"
            cancel = threading.Event()
            timer = threading.Timer(0.15, cancel.set)
            source = (
                "import pathlib, subprocess, sys, time; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
                "time.sleep(60)"
            )
            timer.start()
            try:
                result = supervisor.supervise(
                    python_command(source, pid_file),
                    timeout_ms=2000,
                    grace_ms=100,
                    output_limit_bytes=4096,
                    cancel_event=cancel,
                )
            finally:
                timer.cancel()
            child_pid = int(pid_file.read_text())
            self.assertEqual("terminated", result["outcome"])
            self.assertEqual("cancelled", result["reason"])
            self.assertTrue(result["cleanup_verified"])
            self.assert_process_gone(child_pid)

    def test_pre_cancelled_call_does_not_spawn(self):
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(supervisor.subprocess, "Popen") as popen:
            result = supervisor.supervise(
                python_command("raise SystemExit(99)"),
                timeout_ms=2000,
                grace_ms=100,
                output_limit_bytes=4096,
                cancel_event=cancel,
            )
        popen.assert_not_called()
        self.assertEqual("terminated", result["outcome"])
        self.assertEqual("cancelled-before-start", result["reason"])

    def test_deadline_expired_during_signal_setup_does_not_spawn(self):
        original_capture = supervisor._capture_external_signals

        def delayed_capture():
            time.sleep(0.03)
            return original_capture()

        with mock.patch.object(
            supervisor, "_capture_external_signals", delayed_capture
        ), mock.patch.object(supervisor.subprocess, "Popen") as popen:
            result = supervisor.supervise(
                python_command("raise SystemExit(99)"),
                timeout_ms=5,
                grace_ms=100,
                output_limit_bytes=4096,
            )
        popen.assert_not_called()
        self.assertEqual("timeout", result["outcome"])
        self.assertEqual("execution-timeout-before-start", result["reason"])

    def test_keyboard_interrupt_cleans_up_before_propagating(self):
        original_select = supervisor.selectors.DefaultSelector.select
        original_popen = supervisor.subprocess.Popen
        processes = []

        def interrupt(selector, timeout=None):
            if ready.exists():
                raise KeyboardInterrupt
            return original_select(selector, timeout)

        def record_process(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process

        with tempfile.TemporaryDirectory() as directory:
            ready = pathlib.Path(directory) / "ready"
            command = python_command(
                "import pathlib,signal,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "pathlib.Path(sys.argv[1]).write_text('ready'); "
                "time.sleep(60)",
                ready,
            )
            with mock.patch.object(
                supervisor.subprocess, "Popen", record_process
            ), mock.patch.object(
                supervisor.selectors.DefaultSelector, "select", interrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    self.run_supervised(command, grace_ms=30)
        self.assertEqual(1, len(processes))
        self.assertIsNotNone(processes[0].poll())
        self.assertFalse(supervisor._group_exists(processes[0].pid))

    def test_repeated_interruptions_cannot_abandon_signal_escalation(self):
        original_signal = supervisor._signal_group
        interruptions = []
        processes = []
        original_popen = supervisor.subprocess.Popen

        def interrupted_signal(process_group, signal_number):
            if len(interruptions) < 4:
                interruptions.append(signal_number)
                raise KeyboardInterrupt
            return original_signal(process_group, signal_number)

        def record_process(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process

        with tempfile.TemporaryDirectory() as directory:
            ready = pathlib.Path(directory) / "ready"
            cancel = threading.Event()

            def cancel_when_ready():
                wait_for_path(ready)
                cancel.set()

            timer = threading.Thread(target=cancel_when_ready)
            timer.start()
            with mock.patch.object(
                supervisor.subprocess, "Popen", record_process
            ), mock.patch.object(
                supervisor, "_signal_group", interrupted_signal
            ):
                result = supervisor.supervise(
                    python_command(
                        "import pathlib,signal,sys,time; "
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        "pathlib.Path(sys.argv[1]).write_text('ready'); "
                        "time.sleep(60)",
                        ready,
                    ),
                    timeout_ms=2000,
                    grace_ms=30,
                    output_limit_bytes=4096,
                    cancel_event=cancel,
                )
            timer.join()
        self.assertEqual("terminated", result["outcome"])
        self.assertEqual(4, len(interruptions))
        self.assertTrue(result["forced_termination"])
        self.assertTrue(result["cleanup_verified"])
        self.assertIsNotNone(processes[0].poll())

    def test_real_sigterm_cleans_group_before_supervisor_terminates(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = pathlib.Path(directory) / "child.pid"
            child_source = (
                "import os,pathlib,signal,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                "time.sleep(60)"
            )
            driver_source = (
                "import sys,threading,time; "
                "from scripts import workflow_supervisor as s; "
                "threading.Thread(target=time.sleep,args=(60,),daemon=True).start(); "
                "s.supervise([sys.executable,'-c',sys.argv[2],sys.argv[1]],"
                "timeout_ms=10000,grace_ms=30,output_limit_bytes=4096)"
            )
            driver = subprocess.Popen(
                python_command(driver_source, pid_file, child_source),
                cwd=str(pathlib.Path(__file__).parents[2]),
            )
            try:
                wait_for_path(pid_file)
                child_pid = int(pid_file.read_text())
                os.kill(driver.pid, signal.SIGTERM)
                self.assertEqual(-signal.SIGTERM, driver.wait(timeout=3))
                self.assert_process_gone(child_pid)
            finally:
                if driver.poll() is None:
                    os.kill(driver.pid, signal.SIGKILL)
                    driver.wait()

    def test_later_sigterm_is_not_swallowed_during_signal_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = pathlib.Path(directory) / "child.pid"
            child_source = (
                "import os,pathlib,signal,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                "time.sleep(60)"
            )
            driver_source = (
                "import signal,sys; "
                "from scripts import workflow_supervisor as s; "
                "signal.signal(signal.SIGHUP, lambda signum, frame: None); "
                "s.supervise([sys.executable,'-c',sys.argv[2],sys.argv[1]],"
                "timeout_ms=10000,grace_ms=300,output_limit_bytes=4096)"
            )
            driver = subprocess.Popen(
                python_command(driver_source, pid_file, child_source),
                cwd=str(pathlib.Path(__file__).parents[2]),
            )
            try:
                wait_for_path(pid_file)
                child_pid = int(pid_file.read_text())
                os.kill(driver.pid, signal.SIGHUP)
                time.sleep(0.05)
                os.kill(driver.pid, signal.SIGTERM)
                self.assertEqual(-signal.SIGTERM, driver.wait(timeout=3))
                self.assert_process_gone(child_pid)
            finally:
                if driver.poll() is None:
                    os.kill(driver.pid, signal.SIGKILL)
                    driver.wait()

    def test_primary_base_exception_survives_restoration_failure(self):
        with mock.patch.object(
            supervisor,
            "_supervise_posix",
            side_effect=SystemExit("primary"),
        ), mock.patch.object(
            supervisor,
            "_restore_external_signals",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(SystemExit) as raised:
                self.run_supervised(python_command("raise SystemExit(99)"))
        self.assertEqual("primary", str(raised.exception))

    def test_primary_exception_records_captured_secondary_signals(self):
        previous = {
            signal.SIGTERM: signal.SIG_DFL,
        }
        with mock.patch.object(
            supervisor,
            "_capture_external_signals",
            return_value=([signal.SIGTERM], previous),
        ), mock.patch.object(
            supervisor,
            "_restore_external_signals",
            return_value=None,
        ), mock.patch.object(
            supervisor,
            "_supervise_posix",
            side_effect=SystemExit("primary"),
        ):
            with self.assertRaises(SystemExit) as raised:
                self.run_supervised(python_command("raise SystemExit(99)"))
        self.assertEqual(
            (signal.SIGTERM,),
            raised.exception.workflow_supervisor_signals,
        )

    def test_released_process_group_is_never_signaled_again(self):
        process = mock.Mock()
        process.wait.return_value = 0
        ownership = supervisor._ProcessGroupOwnership(12345)
        ownership.released = True
        with mock.patch.object(supervisor, "_signal_group") as signal_group:
            forced, cleanup_verified = supervisor._terminate(
                process, ownership, None, {"stdout": bytearray(), "stderr": bytearray()}, 10
            )
        signal_group.assert_not_called()
        self.assertFalse(forced)
        self.assertTrue(cleanup_verified)

    def test_supervisor_failure_is_structured_after_cleanup(self):
        original_select = supervisor.selectors.DefaultSelector.select
        original_popen = supervisor.subprocess.Popen
        processes = []

        def fail(selector, timeout=None):
            if ready.exists():
                raise OSError("synthetic persistent selector failure")
            return original_select(selector, timeout)

        def record_process(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process

        with tempfile.TemporaryDirectory() as directory:
            ready = pathlib.Path(directory) / "ready"
            command = python_command(
                "import pathlib,signal,sys,time; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "pathlib.Path(sys.argv[1]).write_text('ready'); "
                "time.sleep(60)",
                ready,
            )
            with mock.patch.object(
                supervisor.subprocess, "Popen", record_process
            ), mock.patch.object(
                supervisor.selectors.DefaultSelector, "select", fail
            ):
                result = self.run_supervised(command, grace_ms=30)
        self.assertEqual("supervisor-failure", result["outcome"])
        self.assertEqual("OSError", result["supervisor_error"])
        self.assertTrue(result["cleanup_verified"])
        self.assertTrue(result["forced_termination"])
        self.assertIsNotNone(processes[0].poll())
        self.assertFalse(supervisor._group_exists(processes[0].pid))

    def test_selector_setup_failure_cleans_up_started_process(self):
        original_popen = supervisor.subprocess.Popen
        processes = []

        def record_process(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process

        with mock.patch.object(
            supervisor.subprocess, "Popen", record_process
        ), mock.patch.object(
            supervisor.selectors.DefaultSelector,
            "register",
            side_effect=OSError("synthetic registration failure"),
        ):
            result = self.run_supervised(
                python_command("import time; time.sleep(60)")
            )
        self.assertEqual("supervisor-failure", result["outcome"])
        self.assertEqual("supervision-setup-error", result["reason"])
        self.assertTrue(result["cleanup_verified"])
        self.assertIsNotNone(processes[0].poll())
        self.assertFalse(supervisor._group_exists(processes[0].pid))

    def test_repeated_results_have_the_same_schema_and_values(self):
        command = python_command("print('stable')")
        first = self.run_supervised(command)
        second = self.run_supervised(command)
        self.assertEqual(first, second)
        self.assertEqual(
            {
                "cleanup_verified",
                "command_sha256",
                "containment",
                "exit_code",
                "forced_termination",
                "format",
                "limits",
                "outcome",
                "reason",
                "stderr",
                "stdout",
                "supervisor_error",
                "terminating_signal",
            },
            set(first),
        )
        self.assertNotIn("pid", json.dumps(first))
        self.assertNotIn("timestamp", json.dumps(first))

    def test_unsupported_platform_fails_without_starting_process(self):
        with mock.patch.object(supervisor.os, "name", "nt"):
            result = self.run_supervised(python_command("raise SystemExit(99)"))
        self.assertEqual("unsupported", result["outcome"])
        self.assertEqual(
            "process-session-isolation-unavailable", result["reason"]
        )

    def test_non_main_thread_fails_closed_before_starting_process(self):
        results = []

        def run():
            results.append(
                self.run_supervised(python_command("raise SystemExit(99)"))
            )

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        self.assertEqual("unsupported", results[0]["outcome"])
        self.assertEqual(
            "process-wide-signal-guard-unavailable", results[0]["reason"]
        )


if __name__ == "__main__":
    unittest.main()
