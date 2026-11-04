"""Tests for the CI helper scripts, run under DRY_RUN with fixtures."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

if shutil.which("bash") is None or shutil.which("jq") is None:
    pytest.skip("bash and jq are required", allow_module_level=True)

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"


def run(
    script: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a script with bash from the repository root."""
    full_env = {"PATH": os.environ["PATH"], "DRY_RUN": "1", **(env or {})}
    return subprocess.run(
        ["bash", str(SCRIPTS / script), *args],
        cwd=ROOT,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def resolve(cell: str, tags: list[str], tmp_path: Path) -> str:
    """Resolve a cell against a fixed tag list."""
    tag_file = tmp_path / "tags.txt"
    tag_file.write_text("\n".join(tags) + "\n")
    result = run("ci_resolve_ref.sh", cell, env={"CORE_TAGS_FILE": str(tag_file)})
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


BEFORE = ["2026.9.0", "2026.10.2", "2026.10.3", "2026.11.0b2", "2026.11.0b10"]
AFTER = [*BEFORE, "2026.11.0"]


def test_resolve_before_release(tmp_path: Path) -> None:
    """Before the release: min is rc, beta is b10 (not b2), stable is 2026.10.3."""
    assert resolve("min", BEFORE, tmp_path) == "rc"
    assert resolve("stable", BEFORE, tmp_path) == "2026.10.3"
    assert resolve("beta", BEFORE, tmp_path) == "2026.11.0b10"
    assert resolve("dev", BEFORE, tmp_path) == "dev"


def test_resolve_after_release(tmp_path: Path) -> None:
    """After the release: min and stable are the tag, beta falls back to rc."""
    assert resolve("min", AFTER, tmp_path) == "2026.11.0"
    assert resolve("stable", AFTER, tmp_path) == "2026.11.0"
    assert resolve("beta", AFTER, tmp_path) == "rc"


def test_resolve_unknown_cell(tmp_path: Path) -> None:
    """An unknown cell is an error."""
    tag_file = tmp_path / "tags.txt"
    tag_file.write_text("2026.10.3\n")
    result = run("ci_resolve_ref.sh", "nope", env={"CORE_TAGS_FILE": str(tag_file)})
    assert result.returncode != 0


def test_report_creates_issue() -> None:
    """With no open issue, the failing test is named in a create call."""
    result = run(
        "report_upstream_break.sh", "dev", "abc123", "tests/fixtures/junit-fail.xml"
    )
    assert result.returncode == 0, result.stderr
    assert "gh issue create" in result.stdout
    assert "gh issue comment" not in result.stdout
    assert "Upstream\\ break:\\ dev" in result.stdout
    assert (
        "tests/contracts/test_settings_contracts.py::test_admin_ws_commands_unchanged"
        in (result.stdout)
    )
    assert "tests/test_hooks.py::TestHooks::test_wrapper_installed" in result.stdout
    assert "test_collector_never_raises" not in result.stdout
    assert "abc123" in result.stdout


def test_report_comments_on_existing_issue() -> None:
    """With an open issue, the report is a comment."""
    result = run(
        "report_upstream_break.sh",
        "beta",
        "2026.11.0b4",
        "tests/fixtures/junit-fail.xml",
        env={"DRY_RUN_EXISTING_ISSUE": "7"},
    )
    assert result.returncode == 0, result.stderr
    assert "gh issue comment 7" in result.stdout
    assert "gh issue create" not in result.stdout


def test_report_truncates_long_lists(tmp_path: Path) -> None:
    """500 failures are cut to 100 plus a count, and the body stays small."""
    cases = "".join(
        f'<testcase classname="tests.test_models" name="test_{"x" * 200}_{i}">'
        '<failure message="boom">t</failure></testcase>'
        for i in range(500)
    )
    junit = tmp_path / "junit.xml"
    junit.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
    result = run("report_upstream_break.sh", "dev", "abc123", str(junit))
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("::test_") == 100
    assert "and 400 more" in result.stdout
    assert len(result.stdout) < 60_000


def test_report_hard_caps_body(tmp_path: Path) -> None:
    """Even 100 very long node IDs cannot push the body past the cap."""
    cases = "".join(
        f'<testcase classname="tests.test_models" name="test_{"y" * 3000}_{i}">'
        '<failure message="boom">t</failure></testcase>'
        for i in range(100)
    )
    junit = tmp_path / "junit.xml"
    junit.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
    result = run("report_upstream_break.sh", "dev", "abc123", str(junit))
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.encode()) < 61_000
    assert "truncated" in result.stdout


def test_install_dry_run_prints_steps() -> None:
    """The install script prints every step and never clones for real."""
    result = run(
        "ci_install_ha.sh", "2026.11.0", env={"RUNNER_TEMP": "/nonexistent-ci-tmp"}
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert (
        "git clone --depth 1 --branch 2026.11.0 https://github.com/home-assistant/core"
        in out
    )
    assert "/nonexistent-ci-tmp/ha-core" in out
    assert "build_phcc.py" in out
    assert "requirements_phcc.txt" in out
    assert "--no-deps /nonexistent-ci-tmp/phcc" in out
    assert "requirements_test.txt" in out
    assert (
        out.index("build_phcc.py")
        < out.index("--no-deps")
        < out.index("requirements_test.txt")
    )


@pytest.fixture
def fake_gh(tmp_path: Path) -> dict[str, str]:
    """Provide a fake `gh` on PATH that saves the file given to --body-file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    saved = tmp_path / "body.out"
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$GH_CALLS"\n'
        'if [[ "$1 $2" == "issue list" ]]; then echo "[]"; exit 0; fi\n'
        "while (($#)); do\n"
        '  if [[ "$1" == "--body-file" ]]; then cp "$2" "$GH_BODY_OUT"; fi\n'
        '  if [[ "$1" == "--body" ]]; then echo "--body used" >&2; exit 9; fi\n'
        "  shift\n"
        "done\n"
    )
    gh.chmod(0o755)
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_BODY_OUT": str(saved),
        "GH_CALLS": str(tmp_path / "calls.txt"),
    }


@pytest.mark.parametrize("locale", ["C.UTF-8", "C"])
def test_report_body_is_valid_utf8_and_byte_capped(
    tmp_path: Path, fake_gh: dict[str, str], locale: str
) -> None:
    """Multibyte test names cannot make the body invalid or oversized."""
    cases = "".join(
        f'<testcase classname="tests.test_models" name="test_{"ü€😀" * 100}_{i}">'
        '<failure message="boom">t</failure></testcase>'
        for i in range(150)
    )
    junit = tmp_path / "junit.xml"
    junit.write_text(
        f"<testsuites><testsuite>{cases}</testsuite></testsuites>", encoding="utf-8"
    )
    env = {**fake_gh, "DRY_RUN": "", "LC_ALL": locale}
    full_env = {"HOME": str(tmp_path), **env}
    result = subprocess.run(
        ["bash", str(SCRIPTS / "report_upstream_break.sh"), "dev", "abc", str(junit)],
        cwd=ROOT,
        env=full_env,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    body = Path(fake_gh["GH_BODY_OUT"]).read_bytes()
    text = body.decode("utf-8", errors="strict")
    assert len(body) <= 60_000
    assert "ü€😀" in text
    assert "truncated" in text
    assert "--body-file" in Path(fake_gh["GH_CALLS"]).read_text()


@pytest.mark.parametrize(
    ("core_version", "supported"),
    [
        pytest.param("2026.10.4", False, id="older-release"),
        pytest.param("2025.12.1", False, id="older-year"),
        pytest.param("2026.11.0", True, id="minimum"),
        pytest.param("2026.11.0b3", True, id="minimum-beta"),
        pytest.param("2026.11.0.dev0", True, id="minimum-dev"),
        pytest.param("2026.12.1", True, id="newer"),
    ],
)
def test_supported_core(core_version: str, supported: bool) -> None:
    """A Core older than the integration's minimum is skipped, not failed."""
    result = run("ci_supported_core.sh", core_version)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("true" if supported else "false")


def test_supported_core_reads_the_integration_minimum() -> None:
    """The minimum comes from const.py, so it is never duplicated."""
    const = (ROOT / "custom_components/electric_fence/const.py").read_text()
    result = run("ci_supported_core.sh", "--print-minimum")
    assert result.returncode == 0, result.stderr
    assert f'MIN_HA_VERSION = "{result.stdout.strip()}"' in const


CORE_CONST = """\
MAJOR_VERSION: Final = 2026
MINOR_VERSION: Final = 11
PATCH_VERSION: Final = "0b4"
__short_version__: Final = f"{MAJOR_VERSION}.{MINOR_VERSION}"
__version__: Final = f"{__short_version__}.{PATCH_VERSION}"
"""
HELPERS = (
    "__init__.py",
    "common.py",
    "plugins.py",
    "typing.py",
    "test_util/aiohttp.py",
)


def fake_core(root: Path, helpers_dir: str, helpers: tuple[str, ...]) -> Path:
    """Write a Core checkout holding `helpers` under `helpers_dir`."""
    (root / "homeassistant").mkdir(parents=True)
    (root / "homeassistant/const.py").write_text(CORE_CONST)
    # Core's own tests package, as of 2026.11.0b4: importing it from the
    # generated package is what crashed pytest at startup.
    (root / "tests").mkdir()
    (root / "tests/__init__.py").write_text("from pytest_homeassistant import _paths\n")
    for name in helpers:
        path = root / helpers_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('"""A helper."""\n')
    return root


def build_phcc(core: Path, out: Path) -> subprocess.CompletedProcess[str]:
    """Run the plugin generator on a Core checkout."""
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "build_phcc.py"),
            "--core",
            str(core),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@pytest.mark.parametrize("package", ["pytest-home-assistant", "pytest-homeassistant"])
def test_build_phcc_finds_the_in_tree_package(package: str, tmp_path: Path) -> None:
    """Core's helper package is used under its old and its current directory."""
    core = fake_core(
        tmp_path / "core", f"packages/{package}/pytest_homeassistant", HELPERS
    )
    result = build_phcc(core, tmp_path / "out")
    assert result.returncode == 0, result.stderr
    assert "in-tree package" in result.stdout
    built = tmp_path / "out/pytest_homeassistant_custom_component"
    assert (built / "common.py").is_file()
    assert "_paths" not in (built / "__init__.py").read_text()


def test_build_phcc_reads_the_legacy_layout(tmp_path: Path) -> None:
    """A Core with its helpers under `tests/` still builds."""
    helpers = tuple("conftest.py" if name == "plugins.py" else name for name in HELPERS)
    core = fake_core(tmp_path / "core", "tests", helpers)
    result = build_phcc(core, tmp_path / "out")
    assert result.returncode == 0, result.stderr
    assert "legacy tests/ directory" in result.stdout


def test_build_phcc_refuses_an_unknown_layout(tmp_path: Path) -> None:
    """Helpers that moved again fail the build, not pytest's startup."""
    core = fake_core(tmp_path / "core", "somewhere/new", HELPERS)
    result = build_phcc(core, tmp_path / "out")
    assert result.returncode != 0
    assert "common.py" in result.stderr
