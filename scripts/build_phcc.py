#!/usr/bin/env python3
"""Generate a local pytest_homeassistant_custom_component package from a Core checkout.

Standard library only. Mirrors the layout of the PyPI build
pytest-homeassistant-custom-component 0.13.374 (file set, import rewrites,
header docstrings, const.py, ``pytest11`` entry point), but sources the files
from a local Home Assistant Core checkout.

Source layout:
  * Core >= 2026.11 ships its test helpers as an in-tree package at
    ``packages/pytest-home-assistant/pytest_homeassistant/``. Its modules are
    the successors of ``tests/common.py``, ``tests/conftest.py`` (now
    ``plugins.py``), and so on, so they are used when present.
  * Older Core checkouts keep them under ``tests/``; that layout is handled
    as a fallback (``tests/conftest.py`` -> ``plugins.py``).

Usage:
  build_phcc.py --core /path/to/core --out /path/to/outdir
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil
import sys

PKG = "pytest_homeassistant_custom_component"
DIST = "pytest-homeassistant-custom-component"
VERSION_PREFIX = "0.13.999"
HEADER_NOTE = (
    "This file is originally from homeassistant/core and modified by "
    "pytest-homeassistant-custom-component."
)

# Files in the PyPI 0.13.374 wheel (relative to the package root), mapped to
# their location under Core's tests/ in the legacy layout.
PHCC_0_13_374_FILES = [
    "__init__.py",
    "asyncio_legacy.py",
    "common.py",
    "components/__init__.py",
    "components/common.py",
    "components/diagnostics/__init__.py",
    "components/recorder/__init__.py",
    "components/recorder/common.py",
    "components/recorder/db_schema_0.py",
    "const.py",  # generated from homeassistant/const.py
    "ignore_uncaught_exceptions.py",
    "patch_json.py",
    "patch_recorder.py",
    "patch_time.py",
    "plugins.py",  # tests/conftest.py in the legacy layout
    "syrupy.py",
    "test_util/__init__.py",
    "test_util/aiohttp.py",
    "testing_config/__init__.py",
    "testing_config/custom_components/__init__.py",
    "testing_config/custom_components/test_constant_deprecation/__init__.py",
    "typing.py",
]

# Requires-Dist names of the 0.13.374 wheel (homeassistant itself excluded,
# it is installed from the checkout). Versions are resolved from Core.
PHCC_REQUIRES = [
    "coverage",
    "freezegun",
    "license-expression",
    "mock-open",
    "pydantic",
    "PyGithub",
    "pipdeptree",
    "pytest-asyncio",
    "pytest-aiohttp",
    "pytest-codspeed",
    "pytest-cov",
    "pytest-freezer",
    "pytest-github-actions-annotate-failures",
    "pytest-socket",
    "pytest-timeout",
    "pytest-unordered",
    "pytest-picked",
    "pytest-xdist",
    "pytest",
    "requests",
    "requests-mock",
    "respx",
    "syrupy",
    "tqdm",
    "unidiff",
    "aiohasupervisor",
    "SQLAlchemy",
    "paho-mqtt",
    "numpy",
]

PKG_SUBDIR = Path("packages/pytest-home-assistant/pytest_homeassistant")
DOCSTRING_RE = re.compile(r'\A(?P<q>"""|\'\'\')(?P<body>.*?)(?P=q)', re.DOTALL)


def norm(name: str) -> str:
    """Normalize a distribution name (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def read_pins(*files: Path) -> dict[str, str]:
    """Return {normalized name: 'Name==version'} from requirement files, first wins."""
    pins: dict[str, str] = {}
    for file in files:
        if not file.is_file():
            continue
        for line in file.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?==(\S+)", line)
            if m:
                pins.setdefault(norm(m.group(1)), f"{m.group(1)}=={m.group(3)}")
    return pins


def add_header(text: str) -> str:
    """Rewrite the module docstring the way phcc does."""
    m = DOCSTRING_RE.match(text)
    if not m:
        return f'"""\n{HEADER_NOTE}\n"""\n\n{text}'
    body = m.group("body").strip("\n")
    return f'"""\n{body}\n\n{HEADER_NOTE}\n"""{text[m.end() :]}'


def rewrite(text: str, *, from_tests: bool) -> str:
    """Rewrite imports and dotted names to the phcc package name."""
    text = re.sub(r"\bpytest_homeassistant\b(?!_custom_component)", PKG, text)
    if from_tests:
        text = re.sub(r"^(\s*)from tests\.", rf"\1from {PKG}.", text, flags=re.M)
        text = re.sub(r"^(\s*)from tests import", rf"\1from {PKG} import", text, flags=re.M)
        text = re.sub(r"^(\s*)import tests\.", rf"\1import {PKG}.", text, flags=re.M)
        text = text.replace('"tests.common"', f'"{PKG}.common"')
    return add_header(text)


def core_version(core: Path) -> tuple[str, str]:
    """Return (__version__, const.py snippet) from homeassistant/const.py."""
    const = (core / "homeassistant/const.py").read_text()
    wanted = ("MAJOR_VERSION", "MINOR_VERSION", "PATCH_VERSION", "__short_version__", "__version__")
    lines = [ln for ln in const.splitlines() if ln.split(":", 1)[0].strip() in wanted]
    if len(lines) != len(wanted):
        sys.exit(f"could not find version constants in {core}/homeassistant/const.py")
    ns: dict[str, object] = {"Final": object}
    exec("\n".join(ln.replace(": Final", "") for ln in lines), ns)  # noqa: S102
    snippet = (
        '"""\nConstants used by Home Assistant components.\n\n'
        f'{HEADER_NOTE}\n"""\n'
        "from typing import TYPE_CHECKING, Final\n" + "\n".join(lines) + "\n"
    )
    return str(ns["__version__"]), snippet


def collect_sources(core: Path) -> tuple[dict[str, tuple[Path, bool]], str]:
    """Map package-relative paths to (source file, came from tests/)."""
    tests = core / "tests"
    pkg_src = core / PKG_SUBDIR
    sources: dict[str, tuple[Path, bool]] = {}
    if pkg_src.is_dir():
        layout = f"in-tree package {PKG_SUBDIR}"
        for path in sorted(pkg_src.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            sources[path.relative_to(pkg_src).as_posix()] = (path, False)
        # The in-tree package left mock_real_ip in tests/test_util/__init__.py;
        # phcc 0.13.374 ships it in test_util/__init__.py, so keep that.
        legacy_util = tests / "test_util/__init__.py"
        if legacy_util.is_file() and "def mock_real_ip" in legacy_util.read_text():
            sources["test_util/__init__.py"] = (legacy_util, True)
    else:
        layout = "legacy tests/ directory"
        for rel in PHCC_0_13_374_FILES:
            if rel == "const.py":
                continue
            src = tests / ("conftest.py" if rel == "plugins.py" else rel)
            if src.is_file():
                sources[rel] = (src, True)
    return sources, layout


def write_pyproject(out: Path, version: str, core: Path) -> None:
    """Write a setuptools pyproject.toml for the generated package."""
    pins = read_pins(
        core / "requirements_test.txt",
        core / "requirements_all.txt",
        core / "homeassistant/package_constraints.txt",
    )
    deps = [pins.get(norm(n), n) for n in PHCC_REQUIRES]
    deps.append(f"homeassistant=={core_version(core)[0]}")
    dep_lines = "\n".join(f'  "{d}",' for d in deps)
    (out / "pyproject.toml").write_text(
        f"""[build-system]
requires = ["setuptools>=78.0"]
build-backend = "setuptools.build_meta"

[project]
name = "{DIST}"
version = "{version}"
description = "Local build of Home Assistant Core test helpers for custom integrations."
requires-python = ">=3.14.2"
license = "Apache-2.0"
dependencies = [
{dep_lines}
]

[project.entry-points.pytest11]
homeassistant = "{PKG}.plugins"

[tool.setuptools.packages.find]
include = ["{PKG}*"]

[tool.setuptools.package-data]
"*" = ["*"]
"""
    )
    (out / "requirements_phcc.txt").write_text(
        "# Pins for phcc 0.13.374 Requires-Dist, resolved from Core "
        "requirements_test.txt / requirements_all.txt / package_constraints.txt\n"
        + "\n".join(d for d in deps if not d.startswith("homeassistant=="))
        + "\n"
    )


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--core", required=True, type=Path, help="Core checkout")
    parser.add_argument("--out", required=True, type=Path, help="output project dir")
    args = parser.parse_args()
    core: Path = args.core.resolve()
    out: Path = args.out.resolve()
    if not (core / "homeassistant/const.py").is_file():
        sys.exit(f"{core} is not a Home Assistant Core checkout")

    ha_version, const_py = core_version(core)
    version = f"{VERSION_PREFIX}+core{ha_version}"
    sources, layout = collect_sources(core)

    if out.exists():
        shutil.rmtree(out)
    pkg_dir = out / PKG
    pkg_dir.mkdir(parents=True)

    for rel, (src, from_tests) in sorted(sources.items()):
        dest = pkg_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rewrite(src.read_text(), from_tests=from_tests))
    (pkg_dir / "const.py").write_text(const_py)
    write_pyproject(out, version, core)

    generated = sorted(p.relative_to(pkg_dir).as_posix() for p in pkg_dir.rglob("*.py"))
    missing = sorted(set(PHCC_0_13_374_FILES) - set(generated))
    extra = sorted(set(generated) - set(PHCC_0_13_374_FILES))
    leftovers = [
        p
        for p in generated
        if re.search(r"\bpytest_homeassistant\b(?!_custom_component)|^\s*from tests\.",
                     (pkg_dir / p).read_text(), re.M)
    ]
    print(f"source layout : {layout}")
    print(f"core version  : {ha_version}")
    print(f"package       : {DIST} {version} -> {out}")
    print(f"files         : {len(generated)}")
    print(f"not in source (vs 0.13.374): {missing or 'none'}")
    print(f"added (vs 0.13.374)        : {extra or 'none'}")
    if leftovers:
        sys.exit(f"unrewritten imports in: {leftovers}")


if __name__ == "__main__":
    main()
