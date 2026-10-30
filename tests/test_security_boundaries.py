"""Port-level checks; these do not replace filesystem/network security integration."""

import asyncio
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from uuid import UUID


REFERENCE = Path(__file__).resolve().parents[1] / 'production-code/references/security-boundaries.md'


def example(marker):
    matches = [block for block in re.findall(r'```python\n(.*?)\n```', REFERENCE.read_text(encoding="utf-8"), re.S)
               if marker in block]
    if len(matches) != 1:
        raise AssertionError(marker)
    namespace = {}
    exec(compile(matches[0], str(REFERENCE), 'exec'), namespace)
    return namespace


class DestinationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_provider_never_starts_network_operation(self):
        calls = []
        client = SimpleNamespace(stream=lambda *args, **kwargs: calls.append(args))
        probe = example('async def probe_provider')['probe_provider']
        for provider in ('unknown', 'https://other.example/path'):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                await probe(client, provider, {'pricing': 'https://configured.example/health'})
        self.assertEqual(calls, [])

    async def test_registered_endpoint_disables_redirects_and_closes_every_exit(self):
        probe = example('async def probe_provider')['probe_provider']
        for status, failure in [(200, None), (302, None), (503, LookupError('upstream')),
                                (200, asyncio.CancelledError('stopped'))]:
            with self.subTest(status=status, failure=type(failure)):
                closed = False
                calls = []

                class Response:
                    status_code = status
                    async def __aenter__(self):
                        return self
                    async def __aexit__(self, *exc):
                        nonlocal closed
                        closed = True
                    def raise_for_status(self):
                        if failure is not None:
                            raise failure

                def stream(*args, **kwargs):
                    calls.append((args, kwargs))
                    return Response()

                client = SimpleNamespace(stream=stream)
                if status == 302:
                    with self.assertRaises(ValueError):
                        await probe(client, 'pricing', {'pricing': 'https://configured.example/health'})
                elif failure is not None:
                    with self.assertRaises(type(failure)) as raised:
                        await probe(client, 'pricing', {'pricing': 'https://configured.example/health'})
                    self.assertIs(raised.exception, failure)
                else:
                    self.assertEqual(await probe(client, 'pricing', {
                        'pricing': 'https://configured.example/health'}), 200)
                self.assertEqual(calls, [(('GET', 'https://configured.example/health'),
                                         {'follow_redirects': False})])
                self.assertTrue(closed)


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_id_never_queries_or_opens(self):
        open_blob = example('async def open_owned_blob')['open_owned_blob']
        async def unexpected(*args):
            self.fail('invalid identifier reached an adapter')
        with self.assertRaises(ValueError):
            await open_blob('../../secrets', SimpleNamespace(tenant_id=7),
                            SimpleNamespace(find_owned_blob=unexpected),
                            SimpleNamespace(open_read=unexpected))

    async def test_ownership_filter_and_server_key_are_preserved(self):
        open_blob = example('async def open_owned_blob')['open_owned_blob']
        identifier = '12345678-1234-5678-1234-567812345678'
        queries, opened = [], []
        resource = object()

        async def find(blob_id, tenant_id):
            queries.append((blob_id, tenant_id))
            return SimpleNamespace(storage_key='server-generated-key') if tenant_id == 7 else None

        async def read(storage_key):
            opened.append(storage_key)
            return resource

        repo, storage = SimpleNamespace(find_owned_blob=find), SimpleNamespace(open_read=read)
        result = await open_blob(identifier, SimpleNamespace(tenant_id=7), repo, storage)
        self.assertIs(result, resource)
        with self.assertRaises(FileNotFoundError):
            await open_blob(identifier, SimpleNamespace(tenant_id=8), repo, storage)
        self.assertEqual(queries, [(UUID(identifier), 7), (UUID(identifier), 8)])
        self.assertEqual(opened, ['server-generated-key'])


if __name__ == '__main__':
    unittest.main()
