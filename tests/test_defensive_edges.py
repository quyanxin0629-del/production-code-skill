"""Execute the distributed examples at numeric and transaction boundaries.

Lua uses the optional lupa Lua 5.1 runtime and command doubles, not a Redis
server. Transaction doubles test control flow, not database locking/isolation.
"""

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
import json
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
from example_support import load_example

try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None


ROOT = Path(__file__).resolve().parents[1]
DEFENSIVE = ROOT / "production-code/references/defensive-and-observability.md"
DATA = ROOT / "production-code/references/data-and-resilience.md"


def example(path, example_id, initial=None, *, definitions_only=False):
    tree = ast.parse(load_example(path, example_id, "python"))
    if definitions_only:
        tree.body = [node for node in tree.body if isinstance(
            node, (ast.Import, ast.ImportFrom, ast.ClassDef,
                   ast.FunctionDef, ast.AsyncFunctionDef))]
    namespace = dict(initial or {})
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace


class DefensiveEdgeTests(unittest.TestCase):
    def test_money_remains_exact_under_hostile_decimal_context(self):
        parse = example(DEFENSIVE, "money-parse-python")["parse_cny"]
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            self.assertEqual(parse("999999999999.99"), Decimal("999999999999.99"))
            self.assertEqual(context.prec, 2)
        for raw in ("1" * 100_000, "1\n", "１２.３４", "0.001", "-0", None):
            with self.subTest(raw_type=type(raw)), self.assertRaises(ValueError):
                parse(raw)

    def test_allocation_enforces_resource_and_type_boundaries(self):
        namespace = example(DEFENSIVE, "money-allocation-python")
        allocate = namespace["allocate_fen"]
        maximum = namespace["MAX_TOTAL_FEN"]
        weights = (0, namespace["MAX_WEIGHT"], namespace["MAX_WEIGHT"])
        shares = allocate(maximum, weights)
        self.assertEqual(sum(shares), maximum)
        self.assertEqual(shares[0], 0)
        self.assertLessEqual(abs(shares[1] - shares[2]), 1)
        for total, parts in ((maximum + 1, (1,)), (10**100_000, (1,)),
                             (1, (namespace["MAX_WEIGHT"] + 1,)),
                             (1, (1,) * (namespace["MAX_PARTS"] + 1)),
                             (1, [1, 2]), (1, (True,)), (1, (0, 0))):
            with self.subTest(total_bits=total.bit_length()), self.assertRaises(ValueError):
                allocate(total, parts)

    def test_utc_year_edges_have_explicit_rejection(self):
        convert = example(DEFENSIVE, "utc-python")["as_utc"]
        for instant in (datetime.min.replace(tzinfo=timezone(timedelta(hours=14))),
                        datetime.max.replace(tzinfo=timezone(timedelta(hours=-14)))):
            with self.assertRaises(ValueError) as raised:
                convert(instant)
            self.assertIsInstance(raised.exception.__cause__, OverflowError)
        self.assertEqual(convert(datetime.max.replace(tzinfo=timezone.utc)),
                         datetime.max.replace(tzinfo=timezone.utc))

    def test_local_time_year_edge_and_non_hour_dst(self):
        resolve = example(DEFENSIVE, "dst-python")["local_to_utc_strict"]
        with self.assertRaises(ValueError):
            resolve(datetime.max, ZoneInfo("America/New_York"))
        zone = ZoneInfo("Australia/Lord_Howe")
        for local in (datetime(2024, 4, 7, 1, 45), datetime(2024, 10, 6, 2, 15)):
            with self.assertRaises(ValueError):
                resolve(local, zone)
        self.assertEqual(resolve(datetime(2024, 10, 6, 2, 45), zone),
                         datetime(2024, 10, 5, 15, 45, tzinfo=timezone.utc))

    def test_json_nested_duplicates_exponents_and_id_precision(self):
        namespace = example(DEFENSIVE, "json-boundaries-python",
                            {"bounded_body": "{}", "response_dto": {}})

        def parse(raw):
            return json.loads(raw, parse_constant=namespace["reject_constant"],
                              parse_float=namespace["finite_json_float"],
                              object_pairs_hook=namespace["unique_object"])

        for raw in ('{"nested":{"x":1,"x":2}}', '[1e400]', '[-Infinity]',
                    '{"x":1,"\\u0078":2}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse(raw)
        self.assertEqual(parse('9007199254740993'), 9007199254740993)
        self.assertEqual(parse('"9007199254740993"'), "9007199254740993")
        # Underflow is explicitly allowed by this approximate float contract.
        self.assertEqual(parse('1e-400'), 0.0)
        with self.assertRaises(ValueError):
            json.dumps({"x": float("nan")}, allow_nan=False)


class TransientReadFailure(Exception):
    def __init__(self, retry_after_s=None):
        self.retry_after_s = retry_after_s


class RetryBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.namespace = example(DATA, "retry-budget-python",
                                 {"TransientReadFailure": TransientReadFailure})
        self.read = self.namespace["read_with_budget"]

    async def test_invalid_config_and_expired_deadline_do_not_call_operation(self):
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            return "done"

        for attempts in (True, 0, -1, 1.5, "3"):
            with self.subTest(attempts=attempts), self.assertRaises(ValueError):
                await self.read(operation, deadline=asyncio.get_running_loop().time() + 1,
                                gate=asyncio.Semaphore(1), max_attempts=attempts)
        for deadline in (True, float("nan"), float("inf"), -float("inf"), 10**1000):
            with self.subTest(kind=type(deadline)), self.assertRaises(ValueError):
                await self.read(operation, deadline=deadline, gate=asyncio.Semaphore(1))
        for deadline in (-1, asyncio.get_running_loop().time() - 1):
            with self.assertRaises(TimeoutError):
                await self.read(operation, deadline=deadline, gate=asyncio.Semaphore(1))
        self.assertEqual(calls, 0)

    async def test_bad_retry_after_stops_without_a_second_request(self):
        for retry_after in (True, -1, float("nan"), float("inf"), 10**1000):
            calls = 0

            async def operation():
                nonlocal calls
                calls += 1
                raise TransientReadFailure(retry_after)

            with self.subTest(kind=type(retry_after)), self.assertRaises(ValueError):
                await self.read(operation, deadline=asyncio.get_running_loop().time() + 1,
                                gate=asyncio.Semaphore(1))
            self.assertEqual(calls, 1)

    async def test_exact_attempt_limit_and_nontransient_propagation(self):
        calls = 0
        failure = TransientReadFailure()

        async def operation():
            nonlocal calls
            calls += 1
            raise failure

        with patch.object(self.namespace["random"], "uniform", return_value=0):
            with self.assertRaises(TransientReadFailure) as raised:
                await self.read(operation, deadline=asyncio.get_running_loop().time() + 1,
                                gate=asyncio.Semaphore(1), max_attempts=3)
        self.assertIs(raised.exception, failure)
        self.assertEqual(calls, 3)
        permanent = ValueError("bad protocol")

        async def bad_protocol():
            raise permanent

        with self.assertRaises(ValueError) as raised:
            await self.read(bad_protocol, deadline=asyncio.get_running_loop().time() + 1,
                            gate=asyncio.Semaphore(1))
        self.assertIs(raised.exception, permanent)

    async def test_queue_wait_and_retry_after_are_inside_budget(self):
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            raise TransientReadFailure(10)

        for gate, expected_calls in ((asyncio.Semaphore(0), 0),
                                     (asyncio.Semaphore(1), 1)):
            calls = 0
            async with asyncio.timeout(1):
                with self.assertRaises(TimeoutError):
                    await self.read(operation, deadline=asyncio.get_running_loop().time() + .01,
                                    gate=gate)
            self.assertEqual(calls, expected_calls)

    async def test_cancellation_propagates_and_releases_gate(self):
        entered = asyncio.Event()
        gate = asyncio.Semaphore(1)

        async def operation():
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(self.read(
            operation, deadline=asyncio.get_running_loop().time() + 10, gate=gate))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(gate.locked())


class IntegrityError(Exception):
    pass


class ConsumerFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.records = set()
        self.effects = []
        self.events = []
        self.insert_failure = self.effect_failure = self.commit_failure = None
        self.ack_failure = None
        self.message = SimpleNamespace(event_id="evt-1")
        owner = self

        class Transaction:
            async def __aenter__(self):
                self.pending_records = set(owner.records)
                self.pending_effects = list(owner.effects)
                owner.events.append("begin")
                return self

            async def __aexit__(self, exc_type, exc, tb):
                if exc is not None:
                    owner.events.append("rollback")
                    return False
                if owner.commit_failure:
                    owner.events.append("rollback")
                    raise owner.commit_failure
                owner.records = self.pending_records
                owner.effects = self.pending_effects
                owner.events.append("commit")

        async def insert(session, consumer, event_id):
            self.events.append("insert")
            if self.insert_failure:
                raise self.insert_failure
            key = (consumer, event_id)
            if key in session.pending_records:
                raise IntegrityError("inbox_unique")
            session.pending_records.add(key)

        async def effect(session, message):
            self.events.append("effect")
            session.pending_effects.append(message.event_id)
            if self.effect_failure:
                raise self.effect_failure

        async def ack(message):
            self.events.append("ack")
            if self.ack_failure:
                raise self.ack_failure

        namespace = example(DATA, "inbox-consume-python", {
            "session_factory": SimpleNamespace(begin=Transaction),
            "inbox": SimpleNamespace(insert_and_flush=insert),
            "consumer_name": "billing",
            "apply_local_effect": effect,
            "broker": SimpleNamespace(ack=ack),
            "IntegrityError": IntegrityError,
            "is_exact_inbox_duplicate": lambda exc: exc.args == ("inbox_unique",),
        }, definitions_only=True)
        self.consume = namespace["consume"]

    async def test_success_and_replay_apply_once_ack_after_transaction(self):
        await self.consume(self.message)
        await self.consume(self.message)
        self.assertEqual(self.effects, ["evt-1"])
        self.assertEqual(self.events, ["begin", "insert", "effect", "commit", "ack",
                                       "begin", "insert", "rollback", "ack"])

    async def test_integrity_errors_outside_exact_insert_are_not_duplicates(self):
        for stage, error in (("insert_failure", IntegrityError("other_unique")),
                             ("effect_failure", IntegrityError("inbox_unique")),
                             ("commit_failure", IntegrityError("inbox_unique"))):
            self.setUp()
            setattr(self, stage, error)
            with self.subTest(stage=stage), self.assertRaises(IntegrityError) as raised:
                await self.consume(self.message)
            self.assertIs(raised.exception, error)
            self.assertEqual(self.records, set())
            self.assertEqual(self.effects, [])
            self.assertNotIn("ack", self.events)

    async def test_ack_failure_replay_does_not_duplicate_committed_effect(self):
        self.ack_failure = OSError("broker unavailable")
        with self.assertRaises(OSError):
            await self.consume(self.message)
        self.assertEqual(self.effects, ["evt-1"])
        self.ack_failure = None
        await self.consume(self.message)
        self.assertEqual(self.effects, ["evt-1"])

    async def test_cancellation_rolls_back_and_does_not_ack(self):
        self.effect_failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.consume(self.message)
        self.assertEqual(self.records, set())
        self.assertEqual(self.effects, [])
        self.assertNotIn("ack", self.events)


@unittest.skipIf(LuaRuntime is None, "optional lupa.lua51 is needed for Lua execution")
class LuaBoundaryTests(unittest.TestCase):
    def run_stock(self, quantity, stock="10", kind="string", *, keys=("stock",), args=None):
        runtime = LuaRuntime(unpack_returned_tuples=True)
        calls = []
        current = [stock]

        def redis_call(command, key, *values):
            calls.append((command, key, values))
            if command == "TYPE":
                return runtime.table_from({"ok": kind})
            if command == "GET":
                return current[0] if current[0] is not None else False
            if command == "DECRBY":
                # Validate the actual command argument, rather than silently
                # accepting Lua numeric/exponent coercion in the test double.
                amount = values[0]
                if not isinstance(amount, str) or not re.fullmatch(r"[1-9][0-9]*", amount):
                    raise ValueError("DECRBY requires canonical integer text")
                current[0] = str(int(current[0]) - int(amount))
                return int(current[0])
            raise AssertionError(command)

        runtime.globals().redis = runtime.table_from({"call": redis_call})
        runtime.globals().KEYS = runtime.table_from(keys)
        runtime.globals().ARGV = runtime.table_from((quantity,) if args is None else args)
        script = load_example(DATA, "stock-reserve-lua", "lua")
        return runtime.execute(script), current[0], calls

    def test_bad_quantity_is_rejected_before_any_key_access(self):
        for quantity in ("0", "-1", "1.0", "1e0", "01", "+1", " 1", "NaN",
                         "9007199254740992", "9" * 100_000):
            with self.subTest(quantity_length=len(quantity)):
                result, stock, calls = self.run_stock(quantity, kind="list")
                self.assertEqual((result, stock, calls), (-2, "10", []))
        for keys, args in (((), ("1",)), (("stock",), ()),
                           (("stock", "other"), ("1",))):
            result, _, calls = self.run_stock("1", keys=keys, args=args)
            self.assertEqual((result, calls), (-2, []))

    def test_corrupt_or_wrong_type_stock_has_no_mutation(self):
        for stock in ("-1", "1.5", "1e3", "01", "9007199254740992", "NaN", ""):
            with self.subTest(stock=stock):
                result, unchanged, calls = self.run_stock("1", stock)
                self.assertEqual((result, unchanged), (-3, stock))
                self.assertNotIn("DECRBY", [call[0] for call in calls])
        for kind, expected in (("none", -1), ("list", -3)):
            result, _, calls = self.run_stock("1", kind=kind)
            self.assertEqual(result, expected)
            self.assertEqual([call[0] for call in calls], ["TYPE"])

    def test_safe_maximum_and_insufficient_stock(self):
        for quantity, stock, expected in (("1", "9007199254740991", 9007199254740990),
                                         ("9007199254740991", "9007199254740991", 0),
                                         ("7", "10", 3)):
            with self.subTest(quantity=quantity):
                result, remaining, calls = self.run_stock(quantity, stock)
                self.assertEqual((result, remaining), (expected, str(expected)))
                self.assertEqual(calls[-1], ("DECRBY", "stock", (quantity,)))
        result, stock, calls = self.run_stock("11")
        self.assertEqual((result, stock), (-1, "10"))
        self.assertNotIn("DECRBY", [call[0] for call in calls])


if __name__ == "__main__":
    unittest.main()
