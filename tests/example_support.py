"""Shared document example loader for standalone and discovered test runs."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from example_blocks import load_example  # noqa: E402
