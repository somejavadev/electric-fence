"""Tests for scripts/upstream_watch.sh, run under DRY_RUN against a local git repo."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

if any(shutil.which(tool) is None for tool in ("bash", "git", "jq", "python3")):
    pytest.skip("bash, git, jq and python3 are required", allow_module_level=True)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "upstream_watch.sh"
NOW = "2026-10-24T06:00:00Z"
MARKER = re.compile(r"<!-- upstream-watch:through=([0-9a-f]{40}) -->")


class Core:
    """A throwaway repository standing in for Home Assistant Core `dev`."""

    def __init__(self, path: Path) -> None:
        """Create an empty repository on branch `dev` at path."""
        self.path = path
        path.mkdir(parents=True)
        self.git("init", "-q", "-b", "dev")

    def git(self, *args: str, date: str = NOW) -> str:
        """Run git in the repository with fixed identity and dates."""
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.path),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.test",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.test",
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
        }
        return subprocess.run(
            ["git", "-C", str(self.path), *args],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def commit(self, subject: str, *files: str, date: str) -> str:
        """Commit a change to each file and return the commit's sha."""
        for name in files:
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a") as fh:
                fh.write(f"{subject}\n")
            self.git("add", "--", name)
        self.git("commit", "-q", "-m", subject, date=date)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def core(tmp_path: Path) -> Core:
    """Provide a Core stand-in with one old commit."""
    repo = Core(tmp_path / "core")
    repo.commit(
        "Initial (#1)", "homeassistant/auth/__init__.py", date="2026-10-01T00:00:00Z"
    )
    return repo


def watch(
    core: Core,
    tmp_path: Path,
    last: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the watch in DRY_RUN mode with the given last issue comment."""
    last_file = tmp_path / "last.json"
    last_file.write_text(json.dumps(last))
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env={
            "PATH": os.environ["PATH"],
            "DRY_RUN": "1",
            "UPSTREAM_CORE_DIR": str(core.path),
            "UPSTREAM_LAST_COMMENT": str(last_file),
            "UPSTREAM_NOW": NOW,
            **(env or {}),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_lists_only_watched_changes_in_the_window(core: Core, tmp_path: Path) -> None:
    """Without a marker the window is the last 24 hours; unwatched paths are skipped."""
    core.commit(
        "Old auth change (#103)",
        "homeassistant/auth/models.py",
        date="2026-10-20T04:00:00Z",
    )
    sha = core.commit(
        "Change websocket auth (#101)",
        "homeassistant/components/websocket_api/http.py",
        date="2026-10-24T03:00:00Z",
    )
    core.commit(
        "Tweak a light (#102)",
        "homeassistant/components/light/__init__.py",
        date="2026-10-24T04:00:00Z",
    )
    result = watch(core, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("gh issue comment") == 1
    assert (
        f"[`{sha[:11]}`](https://github.com/home-assistant/core/commit/{sha})"
        " `core#101` `Change websocket auth`"
    ) in result.stdout
    assert "#102" not in result.stdout
    assert "#103" not in result.stdout


def test_config_is_narrowed_to_attributed_modules(core: Core, tmp_path: Path) -> None:
    """Only the config modules Electric Fence attributes are watched."""
    core.commit(
        "Config LLM tool (#201)",
        "homeassistant/components/config/llm.py",
        date="2026-10-24T01:00:00Z",
    )
    core.commit(
        "Config auth (#202)",
        "homeassistant/components/config/auth.py",
        date="2026-10-24T02:00:00Z",
    )
    result = watch(core, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "#202" in result.stdout
    assert "#201" not in result.stdout


def test_does_not_miss_busy_days(core: Core, tmp_path: Path) -> None:
    """Every watched change is listed however many unwatched ones land with it."""
    for i in range(150):
        core.commit(
            f"Noise {i} (#{1000 + i})",
            f"homeassistant/components/x{i}/a.py",
            date="2026-10-24T01:00:00Z",
        )
    core.commit(
        "Login flow change (#999)",
        "homeassistant/data_entry_flow.py",
        date="2026-10-24T01:30:00Z",
    )
    result = watch(core, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "#999" in result.stdout


def test_starts_after_the_marker(core: Core, tmp_path: Path) -> None:
    """The window starts after the commit the last comment covered, not at a time."""
    covered = core.commit(
        "Reported (#301)", "homeassistant/core.py", date="2026-10-22T00:00:00Z"
    )
    core.commit("New (#302)", "homeassistant/core.py", date="2026-10-22T01:00:00Z")
    last = {
        "body": f"...\n<!-- upstream-watch:through={covered} -->\n",
        "created_at": "2026-10-23T23:00:00Z",
    }
    result = watch(core, tmp_path, last)
    assert result.returncode == 0, result.stderr
    assert "#302" in result.stdout
    assert "#301" not in result.stdout
    head = core.git("rev-parse", "HEAD")
    assert MARKER.findall(result.stdout) == [head]


def test_without_marker_starts_at_the_last_comment(core: Core, tmp_path: Path) -> None:
    """Older comments without a marker: everything since that comment is listed."""
    core.commit("Before (#401)", "homeassistant/const.py", date="2026-10-19T00:00:00Z")
    core.commit("After (#402)", "homeassistant/const.py", date="2026-10-21T00:00:00Z")
    result = watch(
        core, tmp_path, {"body": "old", "created_at": "2026-10-20T00:00:00Z"}
    )
    assert result.returncode == 0, result.stderr
    assert "#402" in result.stdout
    assert "#401" not in result.stdout


def test_window_is_capped(core: Core, tmp_path: Path) -> None:
    """A marker or comment far in the past never reaches back more than 30 days."""
    core.commit("Ancient (#501)", "homeassistant/const.py", date="2026-09-01T00:00:00Z")
    core.commit("Recent (#502)", "homeassistant/const.py", date="2026-10-10T00:00:00Z")
    last = {
        "body": "<!-- upstream-watch:through=" + "0" * 40 + " -->",
        "created_at": "2026-08-01T00:00:00Z",
    }
    result = watch(core, tmp_path, last)
    assert result.returncode == 0, result.stderr
    assert "#502" in result.stdout
    assert "#501" not in result.stdout


def test_posts_nothing_when_empty(core: Core, tmp_path: Path) -> None:
    """With nothing new there is no gh call."""
    head = core.git("rev-parse", "HEAD")
    result = watch(
        core,
        tmp_path,
        {"body": f"<!-- upstream-watch:through={head} -->", "created_at": NOW},
    )
    assert result.returncode == 0, result.stderr
    assert "gh issue" not in result.stdout
    assert "No watched" in result.stdout


def test_creates_the_issue_when_missing(core: Core, tmp_path: Path) -> None:
    """Without an open watch issue one is created, then commented on."""
    core.commit(
        "Auth (#601)", "homeassistant/auth/models.py", date="2026-10-24T01:00:00Z"
    )
    result = watch(core, tmp_path)
    assert "gh issue create" in result.stdout
    assert "gh issue comment" in result.stdout


def test_existing_issue_gets_comment_only(core: Core, tmp_path: Path) -> None:
    """An existing watch issue is commented on, not recreated."""
    core.commit(
        "Auth (#602)", "homeassistant/auth/models.py", date="2026-10-24T01:00:00Z"
    )
    result = watch(core, tmp_path, env={"DRY_RUN_EXISTING_ISSUE": "3"})
    assert "gh issue comment 3 --body-file" in result.stdout
    assert "gh issue create" not in result.stdout


def test_commit_without_pr_number_links_the_commit(core: Core, tmp_path: Path) -> None:
    """A subject without `(#n)` gets the commit link and no PR number."""
    sha = core.commit(
        "Direct push", "homeassistant/core.py", date="2026-10-24T01:00:00Z"
    )
    result = watch(core, tmp_path)
    assert (
        f"[`{sha[:11]}`](https://github.com/home-assistant/core/commit/{sha})"
        " `Direct push`"
    ) in result.stdout
    assert "core#" not in result.stdout


def test_never_references_core_prs(core: Core, tmp_path: Path) -> None:
    """No PR link and no live reference, so Core PR timelines get no event.

    Outside code spans the body may hold only commit links: no `/pull/`,
    `/issues/` or `#<n>` that GitHub would turn into a cross-reference.
    """
    core.commit(
        "See home-assistant/core#42 and https://github.com/home-assistant/core/pull/43 (#44)",
        "homeassistant/components/http/ban.py",
        date="2026-10-24T01:00:00Z",
    )
    result = watch(core, tmp_path)
    assert result.returncode == 0, result.stderr
    body = result.stdout.split("DRY_RUN: gh issue comment")[0]
    outside_code = re.sub(r"`[^`\n]*`", "", body)
    assert "/pull/" not in outside_code
    assert "/issues/" not in outside_code
    assert not re.search(r"#\d", outside_code), outside_code


def test_neutralises_subjects(core: Core, tmp_path: Path) -> None:
    """Mentions, issue refs and backticks in a subject cannot ping or break out."""
    sha = core.commit(
        "fix @octocat #99 `x` (#5)",
        "homeassistant/config_entries.py",
        date="2026-10-24T01:00:00Z",
    )
    result = watch(core, tmp_path)
    assert result.returncode == 0, result.stderr
    assert (
        f"[`{sha[:11]}`](https://github.com/home-assistant/core/commit/{sha})"
        " `core#5` `fix @octocat #99 'x'`"
    ) in result.stdout
    assert "`x`" not in result.stdout


def test_neutralises_backticks_in_file_names(core: Core, tmp_path: Path) -> None:
    """A backtick in a changed file name cannot end the code span."""
    core.commit("t (#6)", "homeassistant/auth/a`b.py", date="2026-10-24T01:00:00Z")
    result = watch(core, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "a`b.py" not in result.stdout
    assert "auth/a'b.py" in result.stdout


def test_long_file_lists_are_shortened(core: Core, tmp_path: Path) -> None:
    """More than eight watched files collapse into an `and N more` suffix."""
    files = [f"homeassistant/components/http/f{i}.py" for i in range(12)]
    core.commit("Big http change (#7)", *files, date="2026-10-24T01:00:00Z")
    result = watch(core, tmp_path)
    assert result.returncode == 0, result.stderr
    assert " and 4 more" in result.stdout
