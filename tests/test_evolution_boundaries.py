"""Behavior checks for the signature and shutdown ports; no DB/broker integration."""
import asyncio
import hmac
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from example_support import ROOT, load_example


def example_namespace(filename, identifier):
    namespace = {}
    source = load_example(ROOT / "production-code/references" / filename,
                          identifier, "python")
    exec(compile(source, f"<{identifier}>", "exec"), namespace)
    return namespace


VALID_TAG = example_namespace("security-boundaries.md", "signature-hmac-good")["valid_webhook_tag"]
SHUTDOWN = example_namespace("data-and-resilience.md", "shutdown-service-good")["shutdown_service"]


class SignatureTests(unittest.TestCase):
    def test_accepts_exact_tag_for_original_body(self):
        body = b'{"event":"paid","amount":120}'
        secret = b"test-only-key"
        self.assertTrue(VALID_TAG(secret, body, hmac.digest(secret, body, "sha256")))

    def test_rejects_changed_body_or_key(self):
        body = b"original"
        tag = hmac.digest(b"test-only-key", body, "sha256")
        self.assertFalse(VALID_TAG(b"test-only-key", body + b"!", tag))
        self.assertFalse(VALID_TAG(b"different-key", body, tag))

    def test_rejects_mismatch_at_either_end(self):
        secret, body = b"test-only-key", b"payload"
        tag = hmac.digest(secret, body, "sha256")
        for index in (0, len(tag) - 1):
            with self.subTest(index=index):
                changed = bytearray(tag)
                changed[index] ^= 1
                self.assertFalse(VALID_TAG(secret, body, bytes(changed)))

    def test_rejects_wrong_public_protocol_length(self):
        secret, body = b"test-only-key", b"payload"
        tag = hmac.digest(secret, body, "sha256")
        for invalid in (b"", tag[:-1], tag + b"\x00"):
            with self.subTest(length=len(invalid)):
                self.assertFalse(VALID_TAG(secret, body, invalid))

    def test_content_comparison_uses_the_cryptographic_primitive(self):
        # Verify the primitive contract, not elapsed time on a noisy test host.
        secret, body = b"test-only-key", b"payload"
        tag = hmac.digest(secret, body, "sha256")
        with patch.object(hmac, "compare_digest", wraps=hmac.compare_digest) as compare:
            self.assertTrue(VALID_TAG(secret, body, tag))
            compare.assert_called_once_with(tag, tag)


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, blocked_stage=None):
        events = []
        entered = asyncio.Event()
        release = asyncio.Event()
        runtime = SimpleNamespace(
            mark_unready=lambda: events.append("unready"),
            reject_new_work=lambda: events.append("reject_new"),
        )

        def stage(name):
            async def perform():
                events.append(name)
                if name == blocked_stage:
                    entered.set()
                    try:
                        await release.wait()
                    finally:
                        events.append(name + ":exit")
            return perform

        for name in ("stop_pulling", "drain_inflight", "drain_queue", "stop_workers",
                     "close_consumer", "close_executor", "close_database", "close_clients"):
            setattr(runtime, name, stage(name))
        return runtime, events, entered

    async def test_success_closes_dependencies_only_after_draining(self):
        runtime, events, _ = self.runtime()
        await SHUTDOWN(runtime, deadline=asyncio.get_running_loop().time() + 1)
        self.assertEqual(events, [
            "unready", "reject_new", "stop_pulling", "drain_inflight", "drain_queue",
            "stop_workers", "close_consumer", "close_executor", "close_database", "close_clients",
        ])

    async def test_shared_deadline_cancels_drain_without_closing_live_dependencies(self):
        runtime, events, _ = self.runtime("drain_queue")
        with self.assertRaises(TimeoutError):
            await SHUTDOWN(runtime, deadline=asyncio.get_running_loop().time() + 0.03)
        self.assertEqual(events, ["unready", "reject_new", "stop_pulling",
                                  "drain_inflight", "drain_queue", "drain_queue:exit"])
        # The caller/supervisor receives failure; durable recovery belongs to the runtime protocol.

    async def test_deadline_does_not_force_kill_a_task_waiting_in_cleanup(self):
        runtime, events, _ = self.runtime()
        cleanup_entered, release_cleanup = asyncio.Event(), asyncio.Event()

        async def drain():
            try:
                await asyncio.Event().wait()
            finally:
                events.append("cleanup_started")
                cleanup_entered.set()
                await release_cleanup.wait()
                events.append("cleanup_finished")

        runtime.drain_inflight = drain
        task = asyncio.create_task(SHUTDOWN(
            runtime, deadline=asyncio.get_running_loop().time() + 0.02))
        try:
            await asyncio.wait_for(cleanup_entered.wait(), timeout=1)
            self.assertFalse(task.done())
            self.assertNotIn("close_database", events)
        finally:
            release_cleanup.set()
            with self.assertRaises(TimeoutError):
                await task
        self.assertEqual(events[-2:], ["cleanup_started", "cleanup_finished"])

    async def test_caller_cancellation_propagates_and_waits_for_drain_cleanup(self):
        runtime, events, entered = self.runtime("drain_inflight")
        task = asyncio.create_task(SHUTDOWN(
            runtime, deadline=asyncio.get_running_loop().time() + 1))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(events, ["unready", "reject_new", "stop_pulling",
                                  "drain_inflight", "drain_inflight:exit"])


if __name__ == "__main__":
    unittest.main()
