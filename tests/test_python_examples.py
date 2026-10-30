"""Semantic checks against the exact Python examples shipped in the Skill."""

import asyncio
import inspect
from pathlib import Path
import re
import unittest

from pydantic import ValidationError


REFERENCE = (
    Path(__file__).resolve().parents[1]
    / "production-code/references/python.md"
)


def example(marker, **ports):
    blocks = re.findall(r"```python\n(.*?)\n```", REFERENCE.read_text(encoding="utf-8"), re.S)
    matches = [block for block in blocks if marker in block]
    if len(matches) != 1:
        raise AssertionError(f"expected one Python example containing {marker!r}")
    namespace = dict(ports)
    exec(compile(matches[0], str(REFERENCE), "exec"), namespace)
    return namespace


class TimedTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_execution_and_preserves_metadata_and_return_identity(self):
        namespace = example("def timed(emit)")
        ticks = iter((100, 350))
        namespace["perf_counter_ns"] = lambda: next(ticks)
        events = []
        entered = asyncio.Event()
        release = asyncio.Event()
        result = object()

        async def operation(value, *, enabled=True):
            """The operation contract."""
            entered.set()
            await release.wait()
            self.assertEqual(value, 3)
            self.assertTrue(enabled)
            return result

        wrapped = namespace["timed"](lambda *event: events.append(event))(operation)
        self.assertEqual(inspect.signature(wrapped), inspect.signature(operation))
        self.assertEqual(wrapped.__name__, operation.__name__)
        self.assertEqual(wrapped.__doc__, operation.__doc__)
        self.assertIs(wrapped.__wrapped__, operation)
        task = asyncio.create_task(wrapped(3, enabled=True))
        try:
            async with asyncio.timeout(2):
                await entered.wait()
            self.assertEqual(events, [])
            release.set()
            self.assertIs(await task, result)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(
            events, [(f"{operation.__module__}.{operation.__qualname__}", "ok", 250)]
        )

    async def test_metric_failure_does_not_replace_success(self):
        timed = example("def timed(emit)")["timed"]
        emitted = []
        result = object()

        def failed_emit(*event):
            emitted.append(event)
            raise RuntimeError("metrics unavailable")

        async def successful():
            return result

        self.assertIs(await timed(failed_emit)(successful)(), result)
        self.assertEqual([event[1] for event in emitted], ["ok"])

    async def test_metric_failure_does_not_replace_business_exception(self):
        timed = example("def timed(emit)")["timed"]
        emitted = []
        failure = LookupError("business failure")

        def failed_emit(*event):
            emitted.append(event)
            raise RuntimeError("metrics unavailable")

        async def failing():
            raise failure

        with self.assertRaises(LookupError) as raised:
            await timed(failed_emit)(failing)()
        self.assertIs(raised.exception, failure)
        self.assertEqual([event[1] for event in emitted], ["error"])

    async def test_metric_failure_does_not_replace_cancellation(self):
        timed = example("def timed(emit)")["timed"]
        entered = asyncio.Event()
        cleaned = asyncio.Event()
        emitted = []

        def failed_emit(*event):
            emitted.append(event)
            raise RuntimeError("metrics unavailable")

        @timed(failed_emit)
        async def operation():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        task = asyncio.create_task(operation())
        try:
            async with asyncio.timeout(2):
                await entered.wait()
            task.cancel("caller stopped")
            with self.assertRaises(asyncio.CancelledError) as raised:
                await task
            self.assertEqual(raised.exception.args, ("caller stopped",))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(cleaned.is_set())
        self.assertEqual([event[1] for event in emitted], ["cancelled"])

    async def test_repeated_metric_failures_remain_visible_with_bounded_diagnostics(self):
        namespace = example("def timed(emit)")

        def failed_emit(*event):
            raise RuntimeError("metrics unavailable")

        @namespace["timed"](failed_emit)
        async def operation():
            return "business result"

        diagnostics = namespace["timing_diagnostics"]
        self.assertEqual(diagnostics(), {"emit_failures": 0})
        for _ in range(3):
            self.assertEqual(await operation(), "business result")
        snapshot = diagnostics()
        self.assertEqual(snapshot, {"emit_failures": 3})
        snapshot["emit_failures"] = 0
        self.assertEqual(diagnostics(), {"emit_failures": 3})

        # Exercise saturation without running billions of failed emissions.
        limit = namespace["_EMIT_FAILURE_LIMIT"]
        namespace["_timing_emit_failures"] = limit - 1
        await operation()
        await operation()
        self.assertEqual(diagnostics(), {"emit_failures": limit})


class ModelTests(unittest.TestCase):
    def test_quantity_is_strict_bounded_and_cannot_supply_tenant(self):
        model = example("class CreateItem(BaseModel)")["CreateItem"]
        for value in (1, 999):
            self.assertEqual(model.model_validate({"quantity": value}).quantity, value)
        for value in (True, False, 0, 1000, 1.0, "1", None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                model.model_validate({"quantity": value})
        with self.assertRaises(ValidationError):
            model.model_validate({"quantity": 1, "tenant_id": 9})


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.namespace = example("def parse_columns")

    def test_cached_values_are_immutable_and_entry_count_is_bounded(self):
        parse = self.namespace["parse_columns"]
        original = (" a ", "b")
        self.assertEqual(parse(original), ("a", "b"))
        self.assertIs(parse(original), parse(original))
        self.assertEqual(original, (" a ", "b"))
        cached = self.namespace.get("_parse_columns", parse)
        for index in range(200):
            parse((str(index),))
        self.assertEqual(cached.cache_info().currsize, 128)

    def test_oversized_or_invalid_keys_are_rejected_before_cache_lookup(self):
        parse = self.namespace["parse_columns"]
        cached = self.namespace.get("_parse_columns", parse)
        before = cached.cache_info()
        for columns in (("a",) * 65, ("a" * 129,), ["a"], (True,)):
            with self.subTest(columns=columns), self.assertRaises(ValueError):
                parse(columns)
        self.assertEqual(cached.cache_info(), before)
        self.assertEqual(parse(("a" * 128,) * 64), ("a" * 128,) * 64)


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_calls_restore_parent_context_after_failure_and_cancel(self):
        namespace = example("async def dispatch")
        rid = namespace["request_id"]
        dispatch = namespace["dispatch"]
        failure = LookupError("expected")

        async def inner():
            self.assertEqual(rid.get(), "inner")
            raise failure

        async def outer():
            self.assertEqual(rid.get(), "outer")
            with self.assertRaises(LookupError) as raised:
                await dispatch("inner", inner)
            self.assertIs(raised.exception, failure)
            self.assertEqual(rid.get(), "outer")
            raise asyncio.CancelledError("cancelled")

        token = rid.set("caller")
        try:
            with self.assertRaises(asyncio.CancelledError):
                await dispatch("outer", outer)
            self.assertEqual(rid.get(), "caller")
        finally:
            rid.reset(token)

    async def test_concurrent_calls_are_isolated(self):
        namespace = example("async def dispatch")
        rid = namespace["request_id"]
        dispatch = namespace["dispatch"]
        entered = 0
        ready = asyncio.Event()

        async def handle():
            nonlocal entered
            before = rid.get()
            entered += 1
            if entered == 2:
                ready.set()
            await ready.wait()
            self.assertEqual(rid.get(), before)
            return before

        async with asyncio.timeout(2):
            self.assertEqual(
                await asyncio.gather(dispatch("alpha", handle), dispatch("beta", handle)),
                ["alpha", "beta"],
            )
        self.assertEqual(rid.get(), "")


class ResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_is_open_during_writes_and_closed_on_every_exit(self):
        copy_body = example("async def copy_body")["copy_body"]
        for mode in ("success", "status", "read", "write", "cancel"):
            with self.subTest(mode=mode):
                opened = False
                closed = False
                writes = []
                failure = LookupError(mode)

                class Response:
                    async def __aenter__(self):
                        nonlocal opened
                        opened = True
                        return self

                    async def __aexit__(self, *exc):
                        nonlocal opened, closed
                        opened = False
                        closed = True

                    def raise_for_status(self):
                        if mode == "status":
                            raise failure

                    async def aiter_bytes(self, *, chunk_size):
                        self_outer.assertEqual(chunk_size, 64 * 1024)
                        if mode == "read":
                            raise failure
                        for chunk in (b"a", b"b"):
                            self_outer.assertTrue(opened)
                            yield chunk

                class Client:
                    def stream(self, method, url):
                        self_outer.assertEqual((method, url), ("GET", "https://example.test"))
                        return Response()

                class Sink:
                    async def write(self, chunk):
                        self_outer.assertTrue(opened)
                        if mode == "write":
                            raise failure
                        if mode == "cancel":
                            raise asyncio.CancelledError("caller stopped")
                        writes.append(chunk)
                        await asyncio.sleep(0)

                self_outer = self
                if mode == "success":
                    await copy_body(Client(), "https://example.test", Sink())
                    self.assertEqual(writes, [b"a", b"b"])
                elif mode == "cancel":
                    with self.assertRaises(asyncio.CancelledError):
                        await copy_body(Client(), "https://example.test", Sink())
                else:
                    with self.assertRaises(LookupError) as raised:
                        await copy_body(Client(), "https://example.test", Sink())
                    self.assertIs(raised.exception, failure)
                self.assertFalse(opened)
                self.assertTrue(closed)


if __name__ == "__main__":
    unittest.main()
