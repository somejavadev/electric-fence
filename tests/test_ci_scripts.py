"""Tests for the CI helper scripts, run under DRY_RUN with fixtures."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

if shutil.which("bash") is None or shutil.which("jq") is None:
    pytest.skip("bash and jq are required", allow_module_level=True)

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"
PULLS = FIXTURES / "upstream-pulls.json"
NOW = "2026-10-24T06:00:00Z"


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


def watch(env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the watch against the pulls fixture."""
    return run(
        "upstream_watch.sh",
        env={"UPSTREAM_FIXTURE": str(PULLS), "UPSTREAM_NOW": NOW, **(env or {})},
    )


def test_watch_lists_only_watched_recent_merged_prs() -> None:
    """Only #101 is merged recently and touches a watched path."""
    result = watch()
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("gh issue comment") == 1
    assert "#101" in result.stdout
    for number in ("#102", "#103", "#104"):
        assert number not in result.stdout


def test_watch_posts_nothing_when_empty() -> None:
    """With nothing in the window there is no gh call."""
    result = watch({"UPSTREAM_NOW": "2026-11-30T06:00:00Z"})
    assert result.returncode == 0, result.stderr
    assert "gh issue" not in result.stdout
    assert "No watched" in result.stdout


def test_watch_existing_issue_gets_comment_only() -> None:
    """An existing watch issue is commented on, not recreated."""
    result = watch({"DRY_RUN_EXISTING_ISSUE": "3"})
    assert "gh issue comment 3" in result.stdout
    assert "gh issue create" not in result.stdout


def test_watch_fails_when_files_api_fails() -> None:
    """A failing pulls/{n}/files call aborts with a non-zero exit and no comment."""
    result = watch({"UPSTREAM_FAIL_FILES": "101"})
    assert result.returncode != 0
    assert "gh issue comment" not in result.stdout


def test_watch_neutralises_titles(tmp_path: Path) -> None:
    """Mentions, issue refs and backticks in a title cannot ping or break out."""
    fixture = tmp_path / "pulls.json"
    fixture.write_text(
        '{"pulls": [{"number": 5, "title": "fix @octocat #99 `x`\\\\n",'
        ' "html_url": "https://example.test/5", "merged_at": "2026-10-24T03:00:00Z"}],'
        ' "files": {"5": ["homeassistant/config_entries.py"]}}'
    )
    result = watch({"UPSTREAM_FIXTURE": str(fixture)})
    assert result.returncode == 0, result.stderr
    assert "[#5](https://example.test/5) `fix @octocat #99 " in result.stdout
    assert "`x`" not in result.stdout
    assert "config_entries.py" in result.stdout


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


def test_watch_neutralises_backticks_in_file_names(tmp_path: Path) -> None:
    """A backtick in a changed file name cannot end the code span."""
    fixture = tmp_path / "pulls.json"
    fixture.write_text(
        '{"pulls": [{"number": 6, "title": "t", "html_url": "https://example.test/6",'
        ' "merged_at": "2026-10-24T03:00:00Z"}],'
        ' "files": {"6": ["homeassistant/auth/a`b.py"]}}'
    )
    result = watch({"UPSTREAM_FIXTURE": str(fixture)})
    assert result.returncode == 0, result.stderr
    assert "a`b.py" not in result.stdout
    assert "auth/a" in result.stdout
