"""Compile extracted standard-library examples; no Spring/DB integration claim.

Run from any directory: python3 scripts/validate_java.py
Requires javac/java 17+; generated sources/classes are automatically cleaned up.
"""

from pathlib import Path
import shutil
import subprocess
import tempfile

from example_blocks import load_example


ROOT = Path(__file__).resolve().parents[1]
REFERENCES = ROOT / "production-code/references"


def snippet(filename, example_id):
    return load_example(REFERENCES / filename, example_id, "java")


def main():
    if not shutil.which("javac") or not shutil.which("java"):
        raise SystemExit("Java checks NOT RUN: javac and java 17+ are required.")
    stripes = snippet("juc-locks.md", "juc-stripes-java").rstrip()
    if not stripes.endswith("}"):
        raise AssertionError("juc-stripes-java must end with its class closing brace")
    # The document explicitly presents withKeys as an addition to this class.
    # Assemble that extension at the checked closing brace; no signature search.
    stripes = stripes[:-1] + snippet("juc-locks.md", "juc-multikey-java") + "\n}"
    replacements = {
        "/*__STRIPES__*/": stripes,
        "/*__TRANSFER__*/": snippet("juc-locks.md", "juc-transfer-java"),
        "/*__SNAPSHOT__*/": snippet("juc-locks.md", "juc-snapshot-java"),
        "/*__PROBE__*/": snippet("java.md", "http-probe-java"),
    }
    source = (ROOT / "tests/java/MechanismTests.java").read_text(encoding="utf-8")
    for marker, content in replacements.items():
        if source.count(marker) != 1:
            raise AssertionError(f"Missing or duplicate harness marker: {marker}")
        source = source.replace(marker, content)
    with tempfile.TemporaryDirectory(prefix="production-code-java-") as temporary:
        work = Path(temporary)
        target = work / "MechanismTests.java"
        target.write_text(source, encoding="utf-8")
        print("Compiling extracted JUC/HttpClient examples with --release 17", flush=True)
        subprocess.run(["javac", "-encoding", "UTF-8", "--release", "17", "-d", str(work), str(target)], check=True, timeout=30)
        subprocess.run(["java", "-ea", "-cp", str(work), "MechanismTests"], check=True, timeout=30)


if __name__ == "__main__":
    main()
