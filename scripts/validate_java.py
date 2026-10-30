"""Compile extracted standard-library examples; no Spring/DB integration claim.

Run from any directory: python3 scripts/validate_java.py
Requires javac/java 17+; generated sources/classes are automatically cleaned up.
"""

from pathlib import Path
import re
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
REFERENCES = ROOT / "production-code/references"


def snippet(filename, marker):
    blocks = re.findall(r"```java\n(.*?)\n```", (REFERENCES / filename).read_text(encoding="utf-8"), re.S)
    matches = [block for block in blocks if marker in block]
    if len(matches) != 1:
        raise AssertionError(f"Expected one {marker!r} block in {filename}, got {len(matches)}")
    return matches[0]


def main():
    if not shutil.which("javac") or not shutil.which("java"):
        raise SystemExit("Java checks NOT RUN: javac and java 17+ are required.")
    stripes = snippet("juc-locks.md", "final class LocalStripes")
    stripes = stripes.removesuffix("}") + snippet("juc-locks.md", "void withKeys(") + "\n}"
    replacements = {
        "/*__STRIPES__*/": stripes.replace("final class LocalStripes", "static final class LocalStripes", 1),
        "/*__TRANSFER__*/": snippet("juc-locks.md", "void transfer("),
        "/*__SNAPSHOT__*/": snippet("juc-locks.md", "record Rules("),
        "/*__PROBE__*/": snippet("java.md", "CompletableFuture<HttpResponse<Void>> probe("),
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
