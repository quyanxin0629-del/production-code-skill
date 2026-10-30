import asyncio
import unittest

from pathlib import Path
import re

# Execute the exact bounded-batch example included in the distributed Skill.
_reference = Path(__file__).resolve().parents[1] / 'production-code/references/python.md'
_blocks = re.findall(r'```python\n(.*?)\n```', _reference.read_text(encoding="utf-8"), re.S)
_matches = [block for block in _blocks if 'async def run_jobs' in block]
if len(_matches) != 1:
    raise AssertionError('expected one bounded-batch example')
_namespace = {}
exec(compile(_matches[0], str(_reference), 'exec'), _namespace)
run_jobs = _namespace['run_jobs']


class Source:
    """A caller-owned iterator that exposes consumption and cancellation."""

    def __init__(self, items):
        self.items = iter(items)
        self.produced = 0
        self.close_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            item = next(self.items)
        except StopIteration:
            raise StopAsyncIteration from None
        self.produced += 1
        return item

    async def aclose(self):
        self.close_calls += 1


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.existing_tasks = asyncio.all_tasks()

    async def asyncTearDown(self):
        # These tests retain references to the batch task. Only tasks created
        # by the batch can remain, so check them before the test runner cleans up.
        remaining = asyncio.all_tasks() - self.existing_tasks
        remaining.discard(asyncio.current_task())
        self.assertEqual(remaining, set(), "batch left pending child tasks")

    async def test_success_empty_and_none_items(self):
        for items in ([], [None, 0, False, "", 3]):
            with self.subTest(items=items):
                source = Source(items)
                handled = []

                async def handle(item):
                    handled.append(item)

                result = await run_jobs(source, handle, workers=3)
                self.assertIsNone(result)
                self.assertCountEqual(handled, items)
                self.assertEqual(source.close_calls, 0)

    async def test_long_input_has_bounded_tasks_buffer_and_parallelism(self):
        workers = 3
        source = Source(range(100_000))
        release = asyncio.Event()
        started = asyncio.Event()
        active = 0
        peak_active = 0
        handled = 0

        async def handle(item):
            nonlocal active, peak_active, handled
            active += 1
            peak_active = max(peak_active, active)
            if active == workers:
                started.set()
            try:
                await release.wait()
                handled += 1
            finally:
                active -= 1

        before = asyncio.all_tasks()
        batch = asyncio.create_task(
            run_jobs(source, handle, workers=workers, total_timeout_s=30)
        )
        try:
            async with asyncio.timeout(2):
                await started.wait()
            # All handlers are blocked. Give the producer a scheduling turn
            # to reach its queue-capacity wait, then inspect actual backpressure.
            await asyncio.sleep(0)
            self.assertLessEqual(source.produced, 3 * workers + 1)
            self.assertEqual(len(asyncio.all_tasks() - before), workers + 2)
            release.set()
            await batch
        finally:
            batch.cancel()
            await asyncio.gather(batch, return_exceptions=True)
        self.assertEqual(handled, 100_000)
        self.assertEqual(peak_active, workers)
        self.assertEqual(source.close_calls, 0)

    async def test_handler_failure_cancels_and_awaits_sibling_cleanup(self):
        entered = asyncio.Event()
        cleaned = asyncio.Event()
        source = Source(range(100_000))
        failure = RuntimeError("handler failed")

        async def handle(item):
            if item == 0:
                await entered.wait()
                raise failure
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        with self.assertRaises(RuntimeError) as raised:
            await run_jobs(source, handle, workers=2)
        self.assertIs(raised.exception, failure)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(source.close_calls, 0)

    async def test_source_failure_cancels_and_awaits_handlers(self):
        entered = asyncio.Event()
        cleaned = asyncio.Event()
        failure = LookupError("source failed")

        class FailingSource(Source):
            async def __anext__(self):
                if self.produced == 1:
                    await entered.wait()
                    raise failure
                return await super().__anext__()

        source = FailingSource([1])

        async def handle(item):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        with self.assertRaises(LookupError) as raised:
            await run_jobs(source, handle, workers=2)
        self.assertIs(raised.exception, failure)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(source.close_calls, 0)

    async def test_item_budget_expires_and_waits_for_cleanup(self):
        cleaned = asyncio.Event()

        async def handle(item):
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        async with asyncio.timeout(2):
            with self.assertRaises(TimeoutError):
                await run_jobs(
                    Source([1]), handle, item_timeout_s=0.02, total_timeout_s=1
                )
        self.assertTrue(cleaned.is_set())

    async def test_total_budget_includes_blocked_source_read(self):
        read_cancelled = asyncio.Event()

        class SlowSource(Source):
            async def __anext__(self):
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    read_cancelled.set()

        source = SlowSource([])

        async def handle(item):
            self.fail("source did not yield")

        async with asyncio.timeout(2):
            with self.assertRaises(TimeoutError):
                await run_jobs(source, handle, total_timeout_s=0.02)
        self.assertTrue(read_cancelled.is_set())
        self.assertEqual(source.close_calls, 0)

    async def test_total_budget_includes_queue_wait_and_handler(self):
        cleaned = asyncio.Event()
        source = Source(range(100_000))

        async def handle(item):
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        async with asyncio.timeout(2):
            with self.assertRaises(TimeoutError):
                await run_jobs(
                    source, handle, workers=1,
                    item_timeout_s=1, total_timeout_s=0.02,
                )
        self.assertTrue(cleaned.is_set())
        self.assertLessEqual(source.produced, 4)

    async def test_caller_cancel_cancels_producer_and_handlers(self):
        entered = asyncio.Event()
        cleaned = asyncio.Event()
        source = Source(range(100_000))

        async def handle(item):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        batch = asyncio.create_task(run_jobs(source, handle, workers=1))
        try:
            async with asyncio.timeout(2):
                await entered.wait()
            batch.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await batch
        finally:
            batch.cancel()
            await asyncio.gather(batch, return_exceptions=True)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(source.close_calls, 0)

    async def test_child_cancel_aborts_batch_without_waiting_for_total_budget(self):
        for cancel_source in (False, True):
            with self.subTest(cancel_source=cancel_source):
                class CancelledSource(Source):
                    async def __anext__(self):
                        raise asyncio.CancelledError

                source = CancelledSource([]) if cancel_source else Source(range(100_000))

                async def handle(item):
                    raise asyncio.CancelledError

                async with asyncio.timeout(2):
                    with self.assertRaises(asyncio.CancelledError):
                        await run_jobs(source, handle, total_timeout_s=60)

    async def test_invalid_capacity_and_budgets_do_not_consume_source(self):
        invalid = [
            {"workers": value} for value in (0, -1, True, 1.5, None)
        ]
        invalid.extend(
            {name: value}
            for name in ("item_timeout_s", "total_timeout_s")
            for value in (0, -1, float("nan"), float("inf"), True, None)
        )
        for options in invalid:
            with self.subTest(options=options):
                source = Source([1])

                async def handle(item):
                    self.fail("invalid configuration reached handler")

                with self.assertRaises(ValueError):
                    await run_jobs(source, handle, **options)
                self.assertEqual(source.produced, 0)
                self.assertEqual(source.close_calls, 0)


if __name__ == "__main__":
    unittest.main()
