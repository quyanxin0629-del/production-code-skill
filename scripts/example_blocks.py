"""Read fenced examples by explicit IDs, independent of prose or function names.

An executable example uses ``<!-- example: stable-kebab-id -->`` immediately
before its fence (blank lines are allowed). Untagged illustrative fences are
still parsed for Markdown/AST checks. This module never executes a snippet.
"""

from dataclasses import dataclass
from pathlib import Path
import re


_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_TAG = re.compile(r" {0,3}<!-- example: ([a-z][a-z0-9]*(?:-[a-z0-9]+)*) -->[ \t]*")
_OPEN = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_ALIASES = {"py": "python", "python3": "python"}


@dataclass(frozen=True)
class CodeBlock:
    language: str
    body: str
    line: int
    example_id: str | None = None


def parse_blocks(content: str) -> list[CodeBlock]:
    """Parse top-level fences and fail closed on broken/ambiguous ID bindings."""
    result = []
    seen = set()
    pending = None
    opened = None
    body = []
    for number, line in enumerate(content.splitlines(), 1):
        if opened is not None:
            character, length, language, start, example_id = opened
            if re.fullmatch(r" {0,3}" + re.escape(character) + "{" + str(length) + r",}[ \t]*", line):
                result.append(CodeBlock(language, "\n".join(body), start, example_id))
                opened = None
            else:
                body.append(line)
            continue

        tag = _TAG.fullmatch(line)
        if tag:
            example_id = tag[1]
            if pending is not None:
                raise ValueError(f"example {pending!r} has no code fence before line {number}")
            if example_id in seen:
                raise ValueError(f"duplicate example ID {example_id!r} at line {number}")
            seen.add(example_id)
            pending = example_id
            continue
        if line.lstrip().startswith("<!-- example:"):
            raise ValueError(f"malformed example ID at line {number}")
        match = _OPEN.fullmatch(line)
        if match:
            fence, info = match.groups()
            if fence[0] == "`" and "`" in info:
                raise ValueError(f"backtick in code fence info at line {number}")
            words = info.strip().split(maxsplit=1)
            language = words[0].lower() if words else ""
            language = _ALIASES.get(language, language)
            opened = (fence[0], len(fence), language, number + 1, pending)
            pending = None
            body = []
        elif pending is not None and line.strip():
            raise ValueError(f"example {pending!r} must immediately precede a code fence (line {number})")
    if opened is not None:
        raise ValueError(f"unclosed Markdown code fence starting at line {opened[3] - 1}")
    if pending is not None:
        raise ValueError(f"example {pending!r} has no code fence")
    return result


def fenced_blocks(content: str):
    """Compatibility view for AST checks: normalized language and body."""
    return ((block.language, block.body) for block in parse_blocks(content))


def load_example(path: Path, example_id: str, language: str) -> str:
    """Load exactly the tagged example; missing IDs/language drift are errors."""
    if not _ID.fullmatch(example_id):
        raise ValueError(f"invalid example ID: {example_id!r}")
    blocks = parse_blocks(path.read_text(encoding="utf-8"))
    matches = [block for block in blocks if block.example_id == example_id]
    if len(matches) != 1:
        raise ValueError(f"expected one example {example_id!r} in {path.name}, got {len(matches)}")
    block = matches[0]
    expected = _ALIASES.get(language.lower(), language.lower())
    if block.language != expected:
        raise ValueError(f"example {example_id!r} in {path.name}: expected {expected}, got {block.language}")
    return block.body
