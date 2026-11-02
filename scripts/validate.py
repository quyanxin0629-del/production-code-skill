import ast
import asyncio
from contextvars import copy_context
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import sys
import unittest
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import yaml
from example_blocks import fenced_blocks, load_example, parse_blocks

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'production-code'
REFERENCE = SKILL / 'references/defensive-and-observability.md'


def blocks(path):
    return [body for language, body in fenced_blocks(path.read_text(encoding="utf-8"))
            if language in {'python', 'python3', 'py'}]


def local_links(path, root):
    for target in re.findall(r'\]\(([^)]+)\)', path.read_text(encoding="utf-8")):
        parsed = urlsplit(target)
        if parsed.scheme or not parsed.path:
            continue
        resolved = (path.parent / unquote(parsed.path)).resolve()
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            raise ValueError(f'invalid local reference in {path.name}: {target}')
        yield resolved


def discover_tests(path):
    # Use a fresh loader so another discovery root cannot leak cached settings.
    return unittest.TestLoader().discover(str(path), pattern='test_*.py')


def find_block(example_id, path=REFERENCE):
    return load_example(path, example_id, 'python')


NAMESPACE = {}
for example_id in ['money-parse-python', 'money-allocation-python', 'utc-python', 'dst-python']:
    exec(compile(find_block(example_id), str(REFERENCE), 'exec'), NAMESPACE)
JSON_NS = {'bounded_body': '{}', 'response_dto': {}}
exec(compile(find_block('json-boundaries-python'), str(REFERENCE), 'exec'), JSON_NS)
MODEL_NS = {}
exec(compile(find_block('order-model-python'), str(REFERENCE), 'exec'), MODEL_NS)
CONTEXT_NS = {}
exec(compile(find_block('request-middleware-python'), str(REFERENCE), 'exec'), CONTEXT_NS)


class ArtifactTests(unittest.TestCase):
    def test_metadata(self):
        content = (SKILL / 'SKILL.md').read_text(encoding="utf-8")
        metadata = yaml.safe_load(content.split('---', 2)[1])
        self.assertEqual(metadata['name'], 'production-code')
        self.assertTrue(metadata['description'])
        interface = yaml.safe_load((SKILL / 'agents/openai.yaml').read_text(encoding="utf-8"))['interface']
        self.assertIn('$production-code', interface['default_prompt'])
        self.assertGreaterEqual(len(interface['short_description']), 25)
        self.assertLessEqual(len(interface['short_description']), 64)

    def test_markdown_and_local_links(self):
        files = list(SKILL.rglob('*.md'))
        self.assertTrue(files)
        graph = {}
        example_ids = {}
        for file in files:
            with self.subTest(file=file.name):
                content = file.read_text(encoding="utf-8")
                code_blocks = parse_blocks(content)
                for block in code_blocks:
                    if block.example_id is not None:
                        self.assertNotIn(block.example_id, example_ids,
                                         f'duplicate example ID across documents: {block.example_id}')
                        example_ids[block.example_id] = file
                if file.is_relative_to(SKILL / 'references'):
                    code_lines = {number for block in code_blocks
                                  for number, _ in enumerate(block.body.splitlines(), block.line)}
                    prose = '\n'.join('' if number in code_lines else line
                                      for number, line in enumerate(content.splitlines(), 1))
                    # Ignore examples/comments without changing the source used by other checks.
                    prose = re.sub(r'<!--.*?(?:-->|$)',
                                   lambda match: re.sub(r'[^\n]', ' ', match[0]), prose, flags=re.S)
                    headings = [line for line in prose.splitlines() if re.match(r'^#{1,6} ', line)]
                    # A leading H1 is the document title, not a body section.
                    if headings and headings[0].startswith('# '):
                        headings = headings[1:]
                    location = file.relative_to(SKILL).as_posix()
                    self.assertIn('## 目录', headings,
                                  f'{location}: missing standalone ## 目录')
                    self.assertEqual(headings[0], '## 目录',
                                     f'{location}: ## 目录 must precede the first body heading')
                self.assertNotIn('[TODO:', content)
                graph[file.resolve()] = set(local_links(file, SKILL))
        reached = set()
        pending = [(SKILL / 'SKILL.md').resolve()]
        while pending:
            current = pending.pop()
            if current not in reached:
                reached.add(current)
                pending.extend(graph.get(current, set()) - reached)
        self.assertFalse(set(graph) - reached, 'unreachable Skill reference files')

    def test_python_fences_parse(self):
        count = 0
        for file in SKILL.rglob('*.md'):
            for index, block in enumerate(blocks(file), 1):
                with self.subTest(file=file.name, block=index):
                    ast.parse(block, filename=f'{file.name}:{index}')
                count += 1
        self.assertGreater(count, 0)


class DefensiveTests(unittest.TestCase):
    def test_money_exact_and_range(self):
        parse = NAMESPACE['parse_cny']
        self.assertEqual(parse('19.99'), Decimal('19.99'))
        self.assertEqual(parse('0'), Decimal('0.00'))
        self.assertEqual(parse('999999999999.99'), Decimal('999999999999.99'))
        for value in ['NaN', 'Infinity', '1e3', '-1', '01', '1.999', '1000000000000', 19.99, True]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse(value)

    def test_allocation_conservation(self):
        allocate = NAMESPACE['allocate_fen']
        for total in range(51):
            for weights in [(1,), (1, 1, 1), (0, 2, 5), (3, 0, 2, 8)]:
                result = allocate(total, weights)
                self.assertEqual(sum(result), total)
                self.assertTrue(all(value >= 0 for value in result))
        self.assertEqual(allocate(100, (1, 1, 1)), (34, 33, 33))
        for total, weights in [(-1, (1,)), (1, (0, 0)), (True, (1,)), (1, (True,))]:
            with self.assertRaises(ValueError):
                allocate(total, weights)

    def test_utc_requires_offset(self):
        with self.assertRaises(ValueError):
            NAMESPACE['as_utc'](datetime(2024, 1, 1))
        self.assertEqual(NAMESPACE['as_utc'](datetime(2024, 1, 1, tzinfo=timezone.utc)).hour, 0)

    def test_dst_rejects_gap_and_overlap(self):
        resolve = NAMESPACE['local_to_utc_strict']
        zone = ZoneInfo('America/New_York')
        for local in [datetime(2024, 3, 10, 2, 30), datetime(2024, 11, 3, 1, 30)]:
            with self.assertRaises(ValueError):
                resolve(local, zone)
        self.assertEqual(resolve(datetime(2024, 6, 1, 12), zone).hour, 16)
        spring = resolve(datetime(2024, 3, 11), zone) - resolve(datetime(2024, 3, 10), zone)
        autumn = resolve(datetime(2024, 11, 4), zone) - resolve(datetime(2024, 11, 3), zone)
        self.assertEqual(spring.total_seconds(), 23 * 3600)
        self.assertEqual(autumn.total_seconds(), 25 * 3600)

    def test_json_rejects_duplicates_nonfinite_and_overflow(self):
        def parse(raw):
            return json.loads(raw, parse_constant=JSON_NS['reject_constant'],
                              parse_float=JSON_NS['finite_json_float'],
                              object_pairs_hook=JSON_NS['unique_object'])
        for raw in ['NaN', 'Infinity', '1e400', '-1e400', '{"id":1,"id":2}']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse(raw)
        self.assertEqual(parse('{"order_id":"9007199254740993"}')['order_id'], '9007199254740993')
        self.assertEqual(parse('{"x":1.5}')['x'], 1.5)

    def test_validation_shape_and_duplicates(self):
        from pydantic import ValidationError
        model = MODEL_NS['CreateOrder']
        command = model.model_validate_json('{"lines":[{"sku_id":1,"quantity":2}]}')
        self.assertIsInstance(command.lines, tuple)
        invalid = [
            {'lines': []},
            {'lines': [{'sku_id': True, 'quantity': 2}]},
            {'lines': [{'sku_id': 1, 'quantity': '2'}]},
            {'lines': [{'sku_id': 1, 'quantity': 0}]},
            {'lines': [{'sku_id': 1, 'quantity': 2}] * 2},
            {'lines': [{'sku_id': 1, 'quantity': 2}], 'tenant_id': 9},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                model.model_validate(payload)


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_isolation_and_restore(self):
        rid = CONTEXT_NS['request_id']
        middleware_type = CONTEXT_NS['RequestContextMiddleware']
        entered = 0
        ready = asyncio.Event()
        observed = []

        async def app(scope, receive, send):
            nonlocal entered
            before = rid.get()
            entered += 1
            if entered == 2:
                ready.set()
            await ready.wait()
            observed.append((before, rid.get()))

        wrapped = middleware_type(app)
        token = rid.set('outer')
        try:
            await asyncio.gather(*[
                wrapped({'type': 'http', 'headers': [(b'x-request-id', value)]}, None, None)
                for value in (b'alpha', b'beta')
            ])
            self.assertEqual(set(observed), {('alpha', 'alpha'), ('beta', 'beta')})
            self.assertEqual(rid.get(), 'outer')
        finally:
            rid.reset(token)

    async def test_failure_restore_and_invalid_id(self):
        rid = CONTEXT_NS['request_id']
        async def app(scope, receive, send):
            self.assertRegex(rid.get(), r'^[0-9a-f]{32}$')
            raise LookupError('expected')
        wrapped = CONTEXT_NS['RequestContextMiddleware'](app)
        with self.assertRaises(LookupError):
            await wrapped({'type': 'http', 'headers': [(b'x-request-id', b'bad\nvalue')]}, None, None)
        self.assertEqual(rid.get(), '')

    async def test_thread_copy_context(self):
        rid = CONTEXT_NS['request_id']
        token = rid.set('thread-test')
        try:
            context = copy_context()
            actual = await asyncio.get_running_loop().run_in_executor(None, context.run, rid.get)
            self.assertEqual(actual, 'thread-test')
        finally:
            rid.reset(token)


def suite():
    loader = unittest.defaultTestLoader
    result = loader.loadTestsFromModule(sys.modules[__name__])
    result.addTests(discover_tests(ROOT / 'tests'))
    return result


if __name__ == '__main__':
    outcome = unittest.TextTestRunner(verbosity=2).run(suite())
    sys.exit(not outcome.wasSuccessful())
