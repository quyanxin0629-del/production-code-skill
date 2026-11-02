"""Run Node ownership/request-isolation checks against tagged Markdown examples.

Run from any directory: python3 /path/to/repository/scripts/validate_node.py
Requires Node >=20.3.0 (AbortSignal.any); missing prerequisites fail explicitly.
No package installation or browser/React integration is performed.
"""

from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from time import monotonic

from example_blocks import load_example


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "production-code/references/typescript-node-react.md"
EXAMPLES = (
    "node-owned-pair",
    "node-request-local-account",
    "node-request-trace-scope",
)


def main():
    node = shutil.which("node")
    if node is None:
        raise SystemExit("Node checks NOT RUN: Node >=20.3.0 is required.")

    # One wall-clock subprocess budget covers probing and all six test cases.
    deadline = monotonic() + 30

    def remaining():
        budget = deadline - monotonic()
        if budget <= 0:
            raise SystemExit("Node checks failed: 30-second execution budget exhausted.")
        return budget

    try:
        version = subprocess.run(
            [node, "--version"], capture_output=True, text=True,
            encoding="utf-8", check=True, timeout=remaining(),
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"Node checks NOT RUN: could not query Node version: {exc}") from exc
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", version)
    if match is None or tuple(map(int, match.groups())) < (20, 3, 0):
        raise SystemExit(f"Node checks NOT RUN: Node >=20.3.0 is required; found {version!r}.")

    with tempfile.TemporaryDirectory(prefix="production-code-node-") as temporary:
        work = Path(temporary)
        for example_id in EXAMPLES:
            (work / f"{example_id}.mjs").write_text(
                load_example(REFERENCE, example_id, "javascript"), encoding="utf-8",
            )
        harness = work / "ownership.test.mjs"
        harness.write_text(
            (ROOT / "tests/node/ownership.test.mjs").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        print(f"Running extracted ownership/request-isolation examples with Node {version}", flush=True)
        try:
            subprocess.run(
                [node, "--test", str(harness)], cwd=work,
                check=True, timeout=remaining(),
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise SystemExit(f"Node checks failed: {exc}") from exc


if __name__ == "__main__":
    main()
