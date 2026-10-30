"""Ensure repository validation does not silently ignore new checks/references."""

import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('_skill_validation_helpers', ROOT / 'scripts/validate.py')
VALIDATOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VALIDATOR
SPEC.loader.exec_module(VALIDATOR)


class HarnessTests(unittest.TestCase):
    def test_longer_outer_fence_keeps_inner_fences_as_content(self):
        text = '````text\n```python\nx = 1\n```\n````\n'
        self.assertEqual(list(VALIDATOR.fenced_blocks(text)),
                         [('text', '```python\nx = 1\n```')])

    def test_tilde_and_python_alias_are_extracted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'example.md'
            path.write_text('~~~py\nx = 2\n~~~\n```python3\ny = 3\n```\n')
            self.assertEqual(VALIDATOR.blocks(path), ['x = 2', 'y = 3'])

    def test_unclosed_fence_fails(self):
        with self.assertRaises(ValueError):
            list(VALIDATOR.fenced_blocks('```python\nx = 1\n'))

    def test_invalid_local_reference_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'SKILL.md'
            for target in ('missing.md', '../outside.md'):
                with self.subTest(target=target):
                    path.write_text(f'[reference]({target})')
                    with self.assertRaises(ValueError):
                        list(VALIDATOR.local_links(path, root))

    def test_new_test_file_is_discovered_and_failure_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test_newly_added_check.py'
            path.write_text('import unittest\nclass Added(unittest.TestCase):\n'
                            '    def test_exposed_failure(self):\n'
                            '        self.fail("expected detection")\n')
            suite = VALIDATOR.discover_tests(Path(directory))
            self.assertEqual(suite.countTestCases(), 1)
            result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
            self.assertFalse(result.wasSuccessful())
            self.assertEqual(len(result.failures), 1)
            self.assertIn('expected detection', result.failures[0][1])


if __name__ == '__main__':
    unittest.main()
