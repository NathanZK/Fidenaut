import fcntl
import hashlib
import json
import pathlib
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest import mock

from scripts import executor_protocol


class ExecutorContextTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name) / "executor"
        self.workspace = (pathlib.Path(self.temporary.name) / "workspace").resolve()
        self.workspace.mkdir()
        self.executor = executor_protocol.Executor(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def create_context(self, **overrides):
        values = {
            "creation_key": "creation-1",
            "role": "planner",
            "workspace_path": str(self.workspace),
        }
        values.update(overrides)
        return self.executor.create(**values)

    def test_create_replay_returns_same_durable_context(self):
        created = self.create_context()

        restarted = executor_protocol.Executor(self.root)
        replayed = restarted.create(
            "creation-1", "planner", str(self.workspace)
        )

        self.assertEqual(created, replayed)
        self.assertEqual("fidenaut-executor-context-v1", created["format"])
        self.assertEqual("creation-1", created["creation_key"])
        self.assertEqual(1, created["generation"])
        self.assertTrue(created["context_id"])
        self.assertTrue(created["provider_session_id"])

    def test_reusing_creation_key_with_changed_request_conflicts(self):
        self.create_context()

        with self.assertRaises(executor_protocol.ExecutorConflict):
            self.create_context(role="reviewer")

    def test_create_rejects_missing_or_symlink_workspace(self):
        missing = self.workspace / "missing"
        with self.assertRaises(executor_protocol.WorkspaceInvalid):
            self.create_context(workspace_path=str(missing))
        with self.assertRaises(executor_protocol.WorkspaceInvalid):
            self.create_context(workspace_path=str(self.workspace) + "\0")

        alias = pathlib.Path(self.temporary.name) / "workspace-link"
        alias.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaises(executor_protocol.WorkspaceInvalid):
            self.create_context(workspace_path=str(alias))

    def test_resume_reports_available_unavailable_and_busy(self):
        context = self.create_context()

        self.assertEqual(
            {"status": "AVAILABLE", "context": context},
            self.executor.resume(context["context_id"]),
        )
        self.assertEqual(
            {"status": "UNAVAILABLE", "context_id": "missing-context"},
            self.executor.resume("missing-context"),
        )

        lock_path = (
            self.root
            / "locks"
            / "contexts"
            / (
                hashlib.sha256(
                    context["context_id"].encode("utf-8")
                ).hexdigest()
                + ".lock"
            )
        )
        with lock_path.open("a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            self.assertEqual(
                {"status": "BUSY", "context_id": context["context_id"]},
                self.executor.resume(context["context_id"]),
            )

    def test_resume_fails_closed_on_corrupt_record(self):
        context = self.create_context()
        record_path = next((self.root / "contexts").glob("*.json"))
        record_path.write_text("{truncated", encoding="utf-8")

        observation = self.executor.resume(context["context_id"])

        self.assertEqual("UNKNOWN", observation["status"])
        self.assertIn("reason", observation)

    def test_missing_workspace_makes_resume_unavailable_without_losing_identity(self):
        context = self.create_context()
        (self.workspace / "instructions.md").unlink(missing_ok=True)
        self.workspace.rmdir()

        observation = self.executor.resume(context["context_id"])

        self.assertEqual("UNAVAILABLE", observation["status"])
        self.assertEqual(context["context_id"], observation["context_id"])

    def test_resume_fails_closed_on_duplicate_keys(self):
        context = self.create_context()
        record_path = next((self.root / "contexts").glob("*.json"))
        record_path.write_text(
            '{"format":"fidenaut-executor-context-v1",'
            '"format":"fidenaut-executor-context-v1"}',
            encoding="utf-8",
        )
        duplicate_observation = self.executor.resume(context["context_id"])
        self.assertEqual("UNKNOWN", duplicate_observation["status"])

    def test_resume_fails_closed_on_invalid_unicode_creation_key(self):
        context = self.create_context()
        surrogate_record = next((self.root / "contexts").glob("*.json"))
        malformed = json.loads(surrogate_record.read_text(encoding="utf-8"))
        malformed["creation_key"] = "\ud800"
        surrogate_record.write_text(json.dumps(malformed), encoding="utf-8")
        surrogate_observation = self.executor.resume(context["context_id"])
        self.assertEqual("UNKNOWN", surrogate_observation["status"])

    def test_resume_fails_closed_on_unknown_version(self):
        context = self.create_context()
        second_record = next((self.root / "contexts").glob("*.json"))
        unsupported = json.loads(second_record.read_text(encoding="utf-8"))
        unsupported["format"] = "fidenaut-executor-context-v999"
        second_record.write_text(json.dumps(unsupported), encoding="utf-8")
        version_observation = self.executor.resume(context["context_id"])
        self.assertEqual("UNKNOWN", version_observation["status"])

    def test_create_persists_strict_json_record_atomically(self):
        context = self.create_context()
        records = list((self.root / "contexts").glob("*.json"))

        self.assertEqual(1, len(records))
        persisted = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(context, persisted)
        self.assertEqual(
            {"format", "context_id", "creation_key", "role", "workspace_path",
             "provider_session_id", "generation", "created_at"},
            set(persisted),
        )

    def test_atomic_write_fsyncs_file_before_directory(self):
        real_fsync = executor_protocol.os.fsync
        synced = []

        def record_fsync(descriptor):
            synced.append(executor_protocol.os.fstat(descriptor).st_mode)
            return real_fsync(descriptor)

        with mock.patch.object(executor_protocol.os, "fsync", record_fsync):
            self.create_context()

        self.assertEqual(2, len(synced))
        self.assertTrue(stat.S_ISREG(synced[0]))
        self.assertTrue(stat.S_ISDIR(synced[1]))

    def test_failed_file_fsync_does_not_publish_context(self):
        with mock.patch.object(
            executor_protocol.os, "fsync", side_effect=OSError("disk failure")
        ):
            with self.assertRaises(executor_protocol.ExecutorError):
                self.create_context()

        self.assertEqual([], list((self.root / "contexts").glob("*.json")))

    def test_lock_failure_is_explicit(self):
        with mock.patch.object(
            executor_protocol.fcntl,
            "flock",
            side_effect=OSError("locking unavailable"),
        ):
            with self.assertRaises(executor_protocol.ExecutorError):
                self.create_context()

    def test_symlinked_lock_file_is_rejected(self):
        key = "creation-symlink-lock"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        lock_path = self.root / "locks" / "creation" / (digest + ".lock")
        target = pathlib.Path(self.temporary.name) / "external-lock"
        target.write_text("", encoding="utf-8")
        lock_path.symlink_to(target)

        with self.assertRaises(executor_protocol.ExecutorError):
            self.executor.create(key, "planner", str(self.workspace))
        self.assertEqual("", target.read_text(encoding="utf-8"))

    def test_directory_fsync_failure_is_reported_but_replay_is_idempotent(self):
        real_fsync = executor_protocol.os.fsync
        calls = []

        def fail_directory_fsync(descriptor):
            calls.append(executor_protocol.os.fstat(descriptor).st_mode)
            if len(calls) == 2:
                raise OSError("directory fsync failure")
            return real_fsync(descriptor)

        with mock.patch.object(
            executor_protocol.os, "fsync", fail_directory_fsync
        ):
            with self.assertRaises(executor_protocol.ExecutorError):
                self.create_context()

        self.assertEqual(2, len(calls))
        self.assertEqual(1, len(list((self.root / "contexts").glob("*.json"))))
        recovered = executor_protocol.Executor(self.root).create(
            "creation-1", "planner", str(self.workspace)
        )
        self.assertEqual("creation-1", recovered["creation_key"])

    def test_concurrent_same_key_creation_returns_one_context(self):
        other = executor_protocol.Executor(self.root)
        results = []
        errors = []

        def create(executor):
            try:
                results.append(
                    executor.create(
                        "creation-concurrent", "planner", str(self.workspace)
                    )
                )
            except Exception as error:
                errors.append(error)

        workers = [
            threading.Thread(target=create, args=(self.executor,)),
            threading.Thread(target=create, args=(other,)),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(2)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertEqual(results[0], results[1])

    def test_context_crash_before_and_after_write_is_idempotently_recoverable(
        self,
    ):
        prewrite_root = pathlib.Path(self.temporary.name) / "prewrite"
        prewrite = executor_protocol.Executor(
            prewrite_root, crash_at="before_context_write"
        )
        with self.assertRaises(executor_protocol.CrashInjected):
            prewrite.create("prewrite", "planner", str(self.workspace))
        self.assertEqual(
            [],
            list((prewrite_root / "contexts").glob("*.json")),
        )
        created = executor_protocol.Executor(prewrite_root).create(
            "prewrite", "planner", str(self.workspace)
        )

        postwrite_root = pathlib.Path(self.temporary.name) / "postwrite"
        postwrite = executor_protocol.Executor(
            postwrite_root, crash_at="after_context_write"
        )
        with self.assertRaises(executor_protocol.CrashInjected):
            postwrite.create("postwrite", "planner", str(self.workspace))
        persisted = json.loads(
            next((postwrite_root / "contexts").glob("*.json")).read_text(
                encoding="utf-8"
            )
        )
        recovered = executor_protocol.Executor(postwrite_root).create(
            "postwrite", "planner", str(self.workspace)
        )

        self.assertNotEqual(created["context_id"], recovered["context_id"])
        self.assertEqual(persisted, recovered)
        self.assertEqual("postwrite", recovered["creation_key"])


class ExecutorOperationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name) / "executor"
        self.workspace = (
            pathlib.Path(self.temporary.name) / "workspace"
        ).resolve()
        self.workspace.mkdir()
        (self.workspace / "instructions.md").write_text(
            "Instructions", encoding="utf-8"
        )
        self.fake = executor_protocol.DeterministicFake(self.root)
        self.executor = executor_protocol.Executor(self.root, fake=self.fake)
        self.context = self.executor.create(
            "creation-1", "planner", str(self.workspace)
        )

    def tearDown(self):
        self.temporary.cleanup()

    def request(self):
        return {
            "role": "planner",
            "workspace_path": str(self.workspace),
            "base": {"repo_head": "0" * 40},
            "instructions": {
                "path": str(self.workspace / "instructions.md"),
                "sha256": hashlib.sha256(b"Instructions").hexdigest(),
            },
            "inputs": [],
            "output_path": str(self.workspace / "output.md"),
            "timeout_s": 30,
            "tool_policy": "READ_ONLY",
            "adapter": {
                "name": "fake",
                "version": "1",
                "model": "deterministic",
                "config_sha256": "2" * 64,
            },
        }

    def submit(self, op_id=None, attempt=1, request=None, context_id=None):
        op_id = op_id or uuid.uuid4().hex
        request = request or self.request()
        context_id = context_id or self.context["context_id"]
        fingerprint = executor_protocol.request_fingerprint(
            op_id, context_id, request
        )
        return op_id, self.executor.submit(
            op_id,
            attempt,
            context_id,
            fingerprint,
            request,
        )

    def test_submit_persists_before_one_fake_invocation_and_outcome_repeats(self):
        op_id, submitted = self.submit()

        self.assertEqual("ACCEPTED", submitted["status"])
        self.assertEqual(1, self.fake.invocation_count(op_id))
        first = self.executor.get_outcome(op_id, fence_through=1)
        repeated = self.executor.get_outcome(op_id, fence_through=1)
        self.assertEqual("SUCCESS", first["status"])
        self.assertEqual(first, repeated)
        self.assertEqual(1, self.fake.invocation_count(op_id))

    def test_same_operation_redelivery_does_not_invoke_twice(self):
        op_id = uuid.uuid4().hex
        self.submit(op_id=op_id)
        _, repeated = self.submit(op_id=op_id)

        self.assertEqual("ACCEPTED", repeated["status"])
        self.assertEqual(1, self.fake.invocation_count(op_id))

    def test_changed_fingerprint_or_context_conflicts_without_invocation(self):
        op_id, _ = self.submit()
        request = self.request()
        fingerprint = executor_protocol.request_fingerprint(
            op_id, self.context["context_id"], request
        )
        changed = self.request()
        changed["timeout_s"] = 60
        with self.assertRaises(executor_protocol.ExecutorConflict):
            self.executor.submit(
                op_id,
                2,
                self.context["context_id"],
                fingerprint,
                changed,
            )
        with self.assertRaises(executor_protocol.ExecutorConflict):
            self.executor.submit(
                op_id,
                2,
                "f" * 32,
                fingerprint,
                self.request(),
            )
        self.assertEqual(1, self.fake.invocation_count(op_id))

    def test_invalid_fingerprint_conflicts_without_invocation(self):
        op_id = uuid.uuid4().hex
        with self.assertRaises(executor_protocol.ExecutorConflict):
            self.executor.submit(
                op_id,
                1,
                self.context["context_id"],
                "0" * 64,
                self.request(),
            )
        self.assertEqual(0, self.fake.invocation_count(op_id))

    def test_nul_output_path_is_rejected_as_request_conflict(self):
        op_id = uuid.uuid4().hex
        request = self.request()
        request["output_path"] += "\0"
        fingerprint = executor_protocol.request_fingerprint(
            op_id, self.context["context_id"], request
        )

        with self.assertRaises(executor_protocol.ExecutorConflict):
            self.executor.submit(
                op_id,
                1,
                self.context["context_id"],
                fingerprint,
                request,
            )
        self.assertEqual(0, self.fake.invocation_count(op_id))

    def test_request_fingerprint_uses_canonical_ascii_json(self):
        op_id = uuid.uuid4().hex
        context_id = self.context["context_id"]
        left = {"text": "café", "nested": {"z": 1, "a": 2}}
        right = {"nested": {"a": 2, "z": 1}, "text": "café"}

        self.assertEqual(
            executor_protocol.request_fingerprint(op_id, context_id, left),
            executor_protocol.request_fingerprint(op_id, context_id, right),
        )
        with self.assertRaises(executor_protocol.ExecutorConflict):
            executor_protocol.request_fingerprint(
                op_id, context_id, {"not_json": float("nan")}
            )

    def test_input_bytes_must_match_the_bound_digest_and_length(self):
        op_id = uuid.uuid4().hex
        request = self.request()
        request["inputs"] = [
            {
                "kind": "artifact",
                "path": str(self.workspace / "instructions.md"),
                "sha256": "0" * 64,
                "byte_length": 12,
            }
        ]
        fingerprint = executor_protocol.request_fingerprint(
            op_id, self.context["context_id"], request
        )

        with self.assertRaises(executor_protocol.ExecutorConflict):
            self.executor.submit(
                op_id,
                1,
                self.context["context_id"],
                fingerprint,
                request,
            )
        self.assertEqual(0, self.fake.invocation_count(op_id))

    def test_valid_input_digest_and_length_are_verified(self):
        op_id = uuid.uuid4().hex
        request = self.request()
        request["inputs"] = [
            {
                "kind": "artifact",
                "path": str(self.workspace / "instructions.md"),
                "sha256": hashlib.sha256(b"Instructions").hexdigest(),
                "byte_length": len(b"Instructions"),
            }
        ]
        fingerprint = executor_protocol.request_fingerprint(
            op_id, self.context["context_id"], request
        )

        self.executor.submit(
            op_id,
            1,
            self.context["context_id"],
            fingerprint,
            request,
        )

        self.assertTrue(self.executor.get_outcome(op_id, 1)["inputs_verified"])
        self.assertEqual(1, self.fake.invocation_count(op_id))

    def test_terminal_record_with_conflicting_identity_fails_closed(self):
        op_id, _ = self.submit()
        operation_path = self.root / "ops" / (op_id + ".json")
        operation = json.loads(operation_path.read_text(encoding="utf-8"))
        operation["outcome"]["op_id"] = uuid.uuid4().hex
        operation_path.write_text(json.dumps(operation), encoding="utf-8")

        with self.assertRaises(executor_protocol.DurableStateError):
            self.executor.get_outcome(op_id, 1)
        self.assertEqual(1, self.fake.invocation_count(op_id))

    def test_corrupt_operation_record_is_not_treated_as_absent(self):
        op_id, _ = self.submit()
        operation_path = self.root / "ops" / (op_id + ".json")
        operation_path.write_text("{partial", encoding="utf-8")

        with self.assertRaises(executor_protocol.DurableStateError):
            self.executor.get_outcome(op_id, 1)
        self.assertEqual(1, self.fake.invocation_count(op_id))

    def test_terminal_failure_is_immutable_on_same_operation_replay(self):
        failing_fake = executor_protocol.DeterministicFake(
            self.root, outcome_status="AGENT_FAILURE"
        )
        executor = executor_protocol.Executor(self.root, fake=failing_fake)
        op_id = uuid.uuid4().hex
        request = self.request()
        fingerprint = executor_protocol.request_fingerprint(
            op_id, self.context["context_id"], request
        )
        executor.submit(
            op_id, 1, self.context["context_id"], fingerprint, request
        )
        outcome = executor.get_outcome(op_id, 1)
        replay = executor.submit(
            op_id, 2, self.context["context_id"], fingerprint, request
        )

        self.assertEqual("AGENT_FAILURE", outcome["status"])
        self.assertEqual("ACCEPTED", replay["status"])
        self.assertEqual(outcome, executor.get_outcome(op_id, 2))
        self.assertEqual(1, failing_fake.invocation_count(op_id))

    def test_terminal_outcome_remains_queryable_if_workspace_disappears(self):
        op_id, _ = self.submit()
        expected = self.executor.get_outcome(op_id, 1)
        (self.workspace / "instructions.md").unlink()
        self.workspace.rmdir()

        self.assertEqual(
            "UNAVAILABLE",
            self.executor.resume(self.context["context_id"])["status"],
        )
        self.assertEqual(expected, self.executor.get_outcome(op_id, 1))

    def test_missing_operation_lookup_fences_attempt_before_later_delivery(self):
        op_id = uuid.uuid4().hex
        observation = self.executor.get_outcome(op_id, fence_through=3)

        self.assertEqual("NOT_ACCEPTED", observation["status"])
        _, stale = self.submit(op_id=op_id, attempt=3)
        self.assertEqual("NOT_ACCEPTED", stale["status"])
        self.assertEqual(0, self.fake.invocation_count(op_id))
        _, eligible = self.submit(op_id=op_id, attempt=4)
        self.assertEqual("ACCEPTED", eligible["status"])
        self.assertEqual(1, self.fake.invocation_count(op_id))

    def test_busy_context_refuses_without_invocation_and_fences_delivery(self):
        op_id = uuid.uuid4().hex
        lock_path = (
            self.root
            / "locks"
            / "contexts"
            / (
                hashlib.sha256(
                    self.context["context_id"].encode("utf-8")
                ).hexdigest()
                + ".lock"
            )
        )
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            _, refused = self.submit(op_id=op_id)

        self.assertEqual("NOT_ACCEPTED", refused["status"])
        self.assertEqual(0, self.fake.invocation_count(op_id))
        fenced = self.executor.get_outcome(op_id, fence_through=1)
        self.assertEqual("NOT_ACCEPTED", fenced["status"])
        _, stale = self.submit(op_id=op_id, attempt=1)
        self.assertEqual("NOT_ACCEPTED", stale["status"])
        _, eligible = self.submit(op_id=op_id, attempt=2)
        self.assertEqual("ACCEPTED", eligible["status"])
        self.assertEqual(1, self.fake.invocation_count(op_id))

    def test_lookup_does_not_execute_and_restart_preserves_terminal(self):
        op_id = uuid.uuid4().hex
        self.executor.get_outcome(op_id, fence_through=0)
        self.assertEqual(0, self.fake.invocation_count(op_id))
        self.submit(op_id=op_id, attempt=1)
        expected = self.executor.get_outcome(op_id, fence_through=1)

        restarted_fake = executor_protocol.DeterministicFake(self.root)
        restarted = executor_protocol.Executor(self.root, fake=restarted_fake)
        observed = restarted.get_outcome(op_id, fence_through=9)

        self.assertEqual(expected, observed)
        self.assertEqual(1, restarted_fake.invocation_count(op_id))

    def test_crash_after_admission_is_unknown_and_never_reinvoked(self):
        op_id = uuid.uuid4().hex
        request = self.request()
        fingerprint = executor_protocol.request_fingerprint(
            op_id, self.context["context_id"], request
        )
        interrupted = executor_protocol.Executor(
            self.root, fake=self.fake, crash_at="after_admission_write"
        )

        with self.assertRaises(executor_protocol.CrashInjected):
            interrupted.submit(
                op_id, 1, self.context["context_id"], fingerprint, request
            )
        restarted = executor_protocol.Executor(self.root, fake=self.fake)

        self.assertEqual(
            "UNKNOWN",
            restarted.get_outcome(op_id, 1)["status"],
        )
        self.assertEqual(0, self.fake.invocation_count(op_id))
        self.assertEqual(
            "ACCEPTED",
            restarted.submit(
                op_id, 1, self.context["context_id"], fingerprint, request
            )["status"],
        )
        self.assertEqual("UNKNOWN", restarted.get_outcome(op_id, 1)["status"])
        self.assertEqual(0, self.fake.invocation_count(op_id))

    def test_crash_before_running_write_recovers_as_unknown_without_invocation(self):
        op_id = uuid.uuid4().hex
        request = self.request()
        fingerprint = executor_protocol.request_fingerprint(
            op_id, self.context["context_id"], request
        )
        interrupted = executor_protocol.Executor(
            self.root, fake=self.fake, crash_at="before_running_write"
        )

        with self.assertRaises(executor_protocol.CrashInjected):
            interrupted.submit(
                op_id, 1, self.context["context_id"], fingerprint, request
            )

        restarted = executor_protocol.Executor(self.root, fake=self.fake)
        self.assertEqual("UNKNOWN", restarted.get_outcome(op_id, 1)["status"])
        self.assertEqual(0, self.fake.invocation_count(op_id))

    def test_fake_crash_after_invocation_before_effect_is_unknown(self):
        op_id = uuid.uuid4().hex
        request = self.request()
        fingerprint = executor_protocol.request_fingerprint(
            op_id, self.context["context_id"], request
        )
        interrupted = executor_protocol.Executor(
            self.root,
            fake=executor_protocol.DeterministicFake(
                self.root, crash_at="after_invocation"
            ),
        )

        with self.assertRaises(executor_protocol.CrashInjected):
            interrupted.submit(
                op_id, 1, self.context["context_id"], fingerprint, request
            )

        restarted = executor_protocol.Executor(
            self.root, fake=executor_protocol.DeterministicFake(self.root)
        )
        self.assertEqual("UNKNOWN", restarted.get_outcome(op_id, 1)["status"])
        self.assertEqual(1, restarted.fake.invocation_count(op_id))
        self.assertEqual(0, restarted.fake.effect_count(op_id))

    def test_crash_before_terminal_persistence_preserves_possible_effect(self):
        op_id = uuid.uuid4().hex
        request = self.request()
        fingerprint = executor_protocol.request_fingerprint(
            op_id, self.context["context_id"], request
        )
        interrupted = executor_protocol.Executor(
            self.root, fake=self.fake, crash_at="before_terminal_write"
        )

        with self.assertRaises(executor_protocol.CrashInjected):
            interrupted.submit(
                op_id, 1, self.context["context_id"], fingerprint, request
            )

        restarted = executor_protocol.Executor(self.root, fake=self.fake)
        self.assertEqual("UNKNOWN", restarted.get_outcome(op_id, 1)["status"])
        self.assertEqual(1, self.fake.invocation_count(op_id))
        self.assertEqual(1, self.fake.effect_count(op_id))

    def test_crash_after_effect_preserves_unknown_and_effect_count(self):
        op_id = uuid.uuid4().hex
        request = self.request()
        fingerprint = executor_protocol.request_fingerprint(
            op_id, self.context["context_id"], request
        )
        failing_fake = executor_protocol.DeterministicFake(
            self.root, crash_at="after_effect"
        )
        interrupted = executor_protocol.Executor(self.root, fake=failing_fake)

        with self.assertRaises(executor_protocol.CrashInjected):
            interrupted.submit(
                op_id, 1, self.context["context_id"], fingerprint, request
            )
        restarted = executor_protocol.Executor(
            self.root, fake=executor_protocol.DeterministicFake(self.root)
        )

        self.assertEqual("UNKNOWN", restarted.get_outcome(op_id, 1)["status"])
        self.assertEqual(1, restarted.fake.invocation_count(op_id))
        self.assertEqual(1, restarted.fake.effect_count(op_id))
        restarted.submit(
            op_id, 2, self.context["context_id"], fingerprint, request
        )
        self.assertEqual("UNKNOWN", restarted.get_outcome(op_id, 2)["status"])
        self.assertEqual(1, restarted.fake.invocation_count(op_id))
        self.assertEqual(1, restarted.fake.effect_count(op_id))

    def test_process_death_after_effect_releases_locks_and_preserves_unknown(self):
        op_id = uuid.uuid4().hex
        request = self.request()
        fingerprint = executor_protocol.request_fingerprint(
            op_id, self.context["context_id"], request
        )
        source = (
            "import json, os, sys\n"
            "from scripts.executor_protocol import Executor, DeterministicFake\n"
            "root, op_id, context_id, fingerprint, request = sys.argv[1:]\n"
            "fake = DeterministicFake(root, hook=lambda stage, _: "
            "os._exit(23) if stage == 'after_effect' else None)\n"
            "Executor(root, fake=fake).submit("
            "op_id, 1, context_id, fingerprint, json.loads(request))\n"
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                source,
                str(self.root),
                op_id,
                self.context["context_id"],
                fingerprint,
                json.dumps(request),
            ],
            check=False,
            timeout=10,
        )
        restarted = executor_protocol.Executor(
            self.root, fake=executor_protocol.DeterministicFake(self.root)
        )

        self.assertEqual(23, completed.returncode)
        self.assertEqual("UNKNOWN", restarted.get_outcome(op_id, 1)["status"])
        self.assertEqual(1, restarted.fake.invocation_count(op_id))
        self.assertEqual(1, restarted.fake.effect_count(op_id))
        self.assertEqual(
            "AVAILABLE",
            restarted.resume(self.context["context_id"])["status"],
        )

    def test_unknown_operation_stays_unknown_while_context_runs_other_operation(self):
        unknown_id = uuid.uuid4().hex
        unknown_request = self.request()
        unknown_fp = executor_protocol.request_fingerprint(
            unknown_id, self.context["context_id"], unknown_request
        )
        interrupted = executor_protocol.Executor(
            self.root,
            fake=executor_protocol.DeterministicFake(
                self.root, crash_at="after_effect"
            ),
        )
        with self.assertRaises(executor_protocol.CrashInjected):
            interrupted.submit(
                unknown_id,
                1,
                self.context["context_id"],
                unknown_fp,
                unknown_request,
            )

        entered = threading.Event()
        release = threading.Event()
        running_fake = executor_protocol.DeterministicFake(
            self.root,
            hook=lambda stage, _op_id: (
                entered.set(),
                release.wait(2),
            )
            if stage == "before_effect"
            else None,
        )
        running_executor = executor_protocol.Executor(
            self.root, fake=running_fake
        )
        running_id = uuid.uuid4().hex
        running_fp = executor_protocol.request_fingerprint(
            running_id, self.context["context_id"], self.request()
        )
        errors = []
        worker = threading.Thread(
            target=lambda: self._deliver_capturing(
                errors,
                running_executor,
                running_id,
                running_fp,
                self.request(),
            )
        )
        worker.start()
        self.assertTrue(entered.wait(2))
        observed = executor_protocol.Executor(self.root).get_outcome(
            unknown_id, 1
        )
        release.set()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertEqual("UNKNOWN", observed["status"])

    def test_lost_terminal_response_recovers_same_immutable_outcome(self):
        op_id = uuid.uuid4().hex
        request = self.request()
        fingerprint = executor_protocol.request_fingerprint(
            op_id, self.context["context_id"], request
        )
        interrupted = executor_protocol.Executor(
            self.root, fake=self.fake, crash_at="before_response"
        )

        with self.assertRaises(executor_protocol.CrashInjected):
            interrupted.submit(
                op_id, 1, self.context["context_id"], fingerprint, request
            )
        restarted = executor_protocol.Executor(
            self.root, fake=executor_protocol.DeterministicFake(self.root)
        )
        outcome = restarted.get_outcome(op_id, 1)

        self.assertEqual("SUCCESS", outcome["status"])
        self.assertEqual(outcome, restarted.get_outcome(op_id, 99))
        self.assertEqual(1, restarted.fake.invocation_count(op_id))

    def test_concurrent_submit_lookup_observes_running_then_terminal_once(self):
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def pause_before_effect(stage, _op_id):
            if stage == "before_effect":
                entered.set()
                if not release.wait(2):
                    raise AssertionError("timed out waiting to release fake")

        blocking_fake = executor_protocol.DeterministicFake(
            self.root, hook=pause_before_effect
        )
        first = executor_protocol.Executor(self.root, fake=blocking_fake)
        second = executor_protocol.Executor(
            self.root,
            fake=executor_protocol.DeterministicFake(self.root),
        )
        op_id = uuid.uuid4().hex
        request = self.request()
        fingerprint = executor_protocol.request_fingerprint(
            op_id, self.context["context_id"], request
        )

        def deliver():
            try:
                first.submit(
                    op_id,
                    1,
                    self.context["context_id"],
                    fingerprint,
                    request,
                )
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=deliver)
        worker.start()
        self.assertTrue(entered.wait(2))
        self.assertEqual("RUNNING", second.get_outcome(op_id, 1)["status"])
        release.set()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertEqual("SUCCESS", second.get_outcome(op_id, 1)["status"])
        self.assertEqual(1, second.fake.invocation_count(op_id))

    def test_concurrent_same_operation_delivery_invokes_once(self):
        entered = threading.Event()
        release = threading.Event()
        fake = executor_protocol.DeterministicFake(
            self.root,
            hook=lambda stage, _op_id: (
                entered.set(),
                release.wait(2),
            )
            if stage == "before_effect"
            else None,
        )
        first = executor_protocol.Executor(self.root, fake=fake)
        second = executor_protocol.Executor(
            self.root,
            fake=executor_protocol.DeterministicFake(self.root),
        )
        op_id = uuid.uuid4().hex
        request = self.request()
        fingerprint = executor_protocol.request_fingerprint(
            op_id, self.context["context_id"], request
        )
        errors = []
        results = []
        worker = threading.Thread(
            target=lambda: self._deliver_capturing(
                errors, first, op_id, fingerprint, request
            )
        )
        worker.start()
        self.assertTrue(entered.wait(2))
        second_started = threading.Event()

        def redeliver():
            second_started.set()
            try:
                results.append(
                    second.submit(
                        op_id,
                        1,
                        self.context["context_id"],
                        fingerprint,
                        request,
                    )
                )
            except Exception as error:
                errors.append(error)

        redelivery = threading.Thread(target=redeliver)
        redelivery.start()
        self.assertTrue(second_started.wait(2))
        release.set()
        worker.join(2)
        redelivery.join(2)

        self.assertFalse(worker.is_alive())
        self.assertFalse(redelivery.is_alive())
        self.assertEqual([], errors)
        self.assertEqual("ACCEPTED", results[0]["status"])
        self.assertEqual(1, second.fake.invocation_count(op_id))

    def test_concurrent_different_operation_on_busy_context_is_fenced(self):
        entered = threading.Event()
        release = threading.Event()
        blocking_fake = executor_protocol.DeterministicFake(
            self.root,
            hook=lambda stage, _op_id: (
                entered.set(),
                release.wait(2),
            )
            if stage == "before_effect"
            else None,
        )
        first = executor_protocol.Executor(self.root, fake=blocking_fake)
        second_fake = executor_protocol.DeterministicFake(self.root)
        second = executor_protocol.Executor(self.root, fake=second_fake)
        first_id = uuid.uuid4().hex
        second_id = uuid.uuid4().hex
        request = self.request()
        first_fp = executor_protocol.request_fingerprint(
            first_id, self.context["context_id"], request
        )
        second_fp = executor_protocol.request_fingerprint(
            second_id, self.context["context_id"], request
        )
        errors = []

        worker = threading.Thread(
            target=lambda: self._deliver_capturing(
                errors,
                first,
                first_id,
                first_fp,
                request,
            )
        )
        worker.start()
        self.assertTrue(entered.wait(2))
        refused = second.submit(
            second_id,
            1,
            self.context["context_id"],
            second_fp,
            request,
        )
        self.assertEqual("NOT_ACCEPTED", refused["status"])
        self.assertEqual(0, second_fake.invocation_count(second_id))
        release.set()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(
            "NOT_ACCEPTED",
            second.get_outcome(second_id, fence_through=1)["status"],
        )
        eligible = second.submit(
            second_id,
            2,
            self.context["context_id"],
            second_fp,
            request,
        )
        self.assertEqual("ACCEPTED", eligible["status"])
        self.assertEqual(1, second_fake.invocation_count(second_id))

    def _deliver_capturing(self, errors, executor, op_id, fingerprint, request):
        try:
            executor.submit(
                op_id, 1, self.context["context_id"], fingerprint, request
            )
        except Exception as error:
            errors.append(error)


if __name__ == "__main__":
    unittest.main()
