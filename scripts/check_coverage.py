"""Check coverage per path, after `pytest --cov`.

Fails when any file in `collectors/`, `attribution.py` or `contracts.py` is
below 100%, or the total is below 95%. Run from the repository root:

    pytest --cov --cov-report=term-missing && python scripts/check_coverage.py
"""

import io
from pathlib import Path
import sys

import coverage

PACKAGE = Path("custom_components/electric_fence")
# Every file under these must be fully covered.
STRICT = (PACKAGE / "collectors", PACKAGE / "attribution.py", PACKAGE / "contracts.py")
TOTAL_MIN = 95.0


def _strict_files() -> list[Path]:
    files: list[Path] = []
    for path in STRICT:
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return files


def main() -> int:
    """Return 0 when every minimum is met, else 1 (with the reasons)."""
    cov = coverage.Coverage()
    cov.load()
    measured = {Path(f).resolve() for f in cov.get_data().measured_files()}
    failures: list[str] = []
    for path in _strict_files():
        if path.resolve() not in measured:
            if path.read_text().strip():
                failures.append(f"{path}: not measured")
            continue
        _, statements, _, missing, lines = cov.analysis2(str(path.resolve()))
        if missing:
            covered = 100.0 * (len(statements) - len(missing)) / len(statements)
            failures.append(f"{path}: {covered:.1f}% < 100% (missing {lines})")
    total = cov.report(file=io.StringIO())
    if total < TOTAL_MIN:
        failures.append(f"total: {total:.2f}% < {TOTAL_MIN}%")
    for failure in failures:
        sys.stderr.write(f"coverage: {failure}\n")
    if failures:
        return 1
    sys.stdout.write(
        f"coverage: total {total:.2f}%, 100% for collectors/, attribution.py "
        "and contracts.py\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
