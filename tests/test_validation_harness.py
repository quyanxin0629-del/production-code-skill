"""Ensure repository validation does not silently ignore new checks/references."""

import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from example_support import load_example
from example_blocks import parse_blocks


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('_skill_validation_helpers', ROOT / 'scripts/validate.py')
VALIDATOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VALIDATOR
SPEC.loader.exec_module(VALIDATOR)
JAVA_SPEC = importlib.util.spec_from_file_location('_skill_java_helpers', ROOT / 'scripts/validate_java.py')
JAVA = importlib.util.module_from_spec(JAVA_SPEC)
JAVA_SPEC.loader.exec_module(JAVA)


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

    def test_id_survives_prose_reorder_and_function_rename_with_untagged_decoy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'example.md'
            path.write_text('```python\ndef old_name(): return "decoy"\n```\n'
                            '## A renamed heading\n<!-- example: stable-id -->\n\n'
                            '~~~python3 title="renamed example"\ndef new_name(): return 7\n~~~\n',
                            encoding='utf-8')
            namespace = {}
            exec(load_example(path, 'stable-id', 'python'), namespace)
            self.assertEqual(namespace['new_name'](), 7)
            self.assertNotIn('old_name', namespace)

    def test_duplicate_missing_and_wrong_language_fail_instead_of_selecting_a_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'example.md'
            tagged = '<!-- example: chosen -->\n```lua\nreturn 1\n```\n'
            path.write_text(tagged, encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'expected python, got lua'):
                load_example(path, 'chosen', 'python')
            with self.assertRaisesRegex(ValueError, 'got 0'):
                load_example(path, 'missing', 'lua')
            path.write_text(tagged + tagged, encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'duplicate example ID'):
                load_example(path, 'chosen', 'lua')

    def test_detached_dangling_and_malformed_ids_fail_closed(self):
        invalid = [
            '<!-- example: chosen -->\nprose\n```python\nx = 1\n```',
            '<!-- example: chosen -->\n',
            '<!-- example: BAD_ID -->\n```python\nx = 1\n```',
            '<!-- example: first -->\n<!-- example: second -->\n```python\nx = 1\n```',
        ]
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_blocks(content)

    def test_nested_id_is_literal_content_and_crlf_metadata_are_supported(self):
        content = ('<!-- example: outer -->\r\n````text\r\n'
                   '<!-- example: fake -->\r\n```python\r\nx = 1\r\n```\r\n````\r\n'
                   '<!-- example: actual -->\r\n~~~py title=test\r\nx = 2\r\n~~~\r\n')
        parsed = parse_blocks(content)
        self.assertEqual([block.example_id for block in parsed], ['outer', 'actual'])
        self.assertEqual(parsed[1].language, 'python')
        self.assertEqual(parsed[1].body, 'x = 2')
        self.assertEqual(parsed[1].line, 10)

    def test_java_adapter_uses_same_explicit_id_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'renamed.md').write_text('```java\nvoid oldName() {}\n```\n'
                '<!-- example: java-stable -->\n~~~java\nvoid newName() {}\n~~~', encoding='utf-8')
            with patch.object(JAVA, 'REFERENCES', root):
                self.assertEqual(JAVA.snippet('renamed.md', 'java-stable'), 'void newName() {}')
                with self.assertRaises(ValueError):
                    JAVA.snippet('renamed.md', 'old-name')

    def test_invalid_local_reference_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'SKILL.md'
            for target in ('missing.md', '../outside.md'):
                with self.subTest(target=target):
                    path.write_text(f'[reference]({target})')
                    with self.assertRaises(ValueError):
                        list(VALIDATOR.local_links(path, root))

    def test_reference_toc_is_required_before_body_headings(self):
        cases = [
            ('valid', '## 目录\n\n- Rule\n\n## Rule\n', None),
            ('valid_with_example', '```text\n<!--\n## Example heading\n```\n\n'
             '## 目录\n\n- Rule\n\n## Rule\n', None),
            ('missing', '## Rule\n', 'missing standalone ## 目录'),
            ('wrong_level', '### 目录\n\n## Rule\n', 'missing standalone ## 目录'),
            ('fenced_only', '```text\n## 目录\n```\n\n## Rule\n', 'missing standalone ## 目录'),
            ('commented_only', '<!--\n## 目录\n-->\n\n## Rule\n', 'missing standalone ## 目录'),
            ('after_body', '## Rule\n\n## 目录\n', 'must precede the first body heading'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            skill = Path(directory)
            (skill / 'references').mkdir()
            (skill / 'SKILL.md').write_text('# Skill\n\n[Reference](references/example.md)\n',
                                           encoding='utf-8')
            reference = skill / 'references/example.md'
            for name, body, expected_error in cases:
                with self.subTest(case=name):
                    reference.write_text('# Reference\n\n' + body, encoding='utf-8')
                    result = unittest.TestResult()
                    with patch.object(VALIDATOR, 'SKILL', skill):
                        VALIDATOR.ArtifactTests('test_markdown_and_local_links').run(result)
                    self.assertEqual(result.errors, [])
                    if expected_error is None:
                        self.assertTrue(result.wasSuccessful(), result.failures)
                    else:
                        self.assertEqual(len(result.failures), 1)
                        self.assertIn('references/example.md', result.failures[0][1])
                        self.assertIn(expected_error, result.failures[0][1])

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
