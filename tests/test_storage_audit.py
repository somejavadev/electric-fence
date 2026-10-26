"""Tests for the v0.3 audit tables and filters in the SQLite storage."""

from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

import pytest

from custom_components.electric_fence.models import (
    OUTSIDE_ACTOR,
    ChangeAction,
    ChangeKind,
    LoginAttempt,
    LoginReason,
    LoginResult,
    SettingsChange,
    Source,
    UserExists,
)
from custom_components.electric_fence.storage import (
    _PROTECTION_SCHEMA,
    _SCHEMA,
    SCHEMA_VERSION,
    AppSnapshot,
    ChangeFilter,
    FenceStore,
    LoginFilter,
)

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[FenceStore]:
    """Return an open store."""
    s = FenceStore(tmp_path / "fence.db")
    s.open()
    yield s
    s.close()


def change(
    *,
    kind: ChangeKind = ChangeKind.USER,
    action: ChangeAction = ChangeAction.UPDATED,
    name: str | None = "Target",
    ip: str | None = "10.0.0.1",
    details: dict[str, str] | None = None,
) -> SettingsChange:
    """Build a settings change."""
    return SettingsChange(
        ts=NOW,
        kind=kind,
        action=action,
        target_id="t1",
        target_name=name,
        details=details or {},
        actor_user_id="u1",
        actor_name="Actor",
        actor_ip=ip,
    )


def test_outside_actor_constant() -> None:
    """The outside marker is the agreed string."""
    assert OUTSIDE_ACTOR == "outside"
    assert ChangeKind.APP == "app"
    assert ChangeAction.FORWARDING_FAILED == "forwarding_failed"


def test_migrates_v2(tmp_path: Path) -> None:
    """A v2 database gains the audit tables and keeps its rows."""
    path = tmp_path / "fence.db"
    with closing(sqlite3.connect(path)) as con:
        con.executescript(_SCHEMA + _PROTECTION_SCHEMA)
        con.execute(
            "INSERT INTO settings_changes (ts, kind, action, details_json) "
            "VALUES (?, 'user', 'updated', '{}')",
            (NOW.isoformat(timespec="microseconds"),),
        )
        con.execute("PRAGMA user_version = 2")
        con.commit()
    store = FenceStore(path)
    store.open()
    with closing(sqlite3.connect(path)) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 3 == SCHEMA_VERSION
    assert len(store.query_changes(ChangeFilter(include_system=True))) == 1
    assert store.baseline("x") == (None, {})
    store.close()


def test_reference_keeps_one_ref(store: FenceStore) -> None:
    """Saving a new ref replaces the earlier one of the same repository."""
    store.save_reference("o/r", "v1", {"a.py": "11"})
    assert store.reference_hashes("o/r", "v1") == {"a.py": "11"}
    store.save_reference("o/r", "v2", {"a.py": "22"})
    assert store.reference_hashes("o/r", "v1") is None
    assert store.reference_hashes("o/r", "v2") == {"a.py": "22"}


def test_baseline_round_trip(store: FenceStore) -> None:
    """A baseline is replaced as a whole."""
    store.save_baseline("x", "1.0", {"a": "1", "b": "2"})
    store.save_baseline("x", "1.1", {"a": "3"})
    assert store.baseline("x") == ("1.1", {"a": "3"})
    store.save_baseline("y", None, {})
    assert store.baseline("y") == (None, {})


def test_findings(store: FenceStore) -> None:
    """Findings are stored per check and dropped for removed domains."""
    assert store.finding("x", "archived") is None
    store.save_finding("x", "archived", {"state": "ok"}, NOW)
    store.save_finding("x", "archived", {"state": "bad"}, NOW)
    store.save_finding("y", "archived", {"n": 1}, NOW)
    assert store.finding("x", "archived") == {"state": "bad"}
    store.drop_findings({"y"})
    assert store.finding("x", "archived") is None
    assert store.finding("y", "archived") == {"n": 1}


def test_app_snapshots_and_repositories(store: FenceStore) -> None:
    """The snapshot table and repository set are replaced whole."""
    a = AppSnapshot("a", "A", "1", "started", "h", {"boot": "auto"})
    b = AppSnapshot("b", "B", None, None, None, {})
    store.save_app_snapshot({"a": a, "b": b}, NOW)
    assert store.app_snapshot() == {"a": a, "b": b}
    store.save_app_snapshot({"b": b}, NOW)
    assert store.app_snapshot() == {"b": b}
    assert store.app_repositories() == set()
    store.save_app_repositories({"u1", "u2"})
    store.save_app_repositories({"u2", "u3"})
    assert store.app_repositories() == {"u2", "u3"}


def test_change_filter_action_ip_search(store: FenceStore) -> None:
    """Action, exact IP and search filters each pick the expected row."""
    store.insert(
        [
            change(
                kind=ChangeKind.APP,
                action=ChangeAction.INSTALLED,
                name="Mosquitto broker",
                ip="198.51.100.20",
            ),
            change(name="Other", ip="198.51.100.2", details={"note": "50%"}),
            change(name="Third", ip="10.0.0.9", details={"note": "500"}),
        ]
    )

    def names(f: ChangeFilter) -> list[str | None]:
        return [r.target_name for r in store.query_changes(f)]

    assert names(ChangeFilter(action=ChangeAction.INSTALLED)) == ["Mosquitto broker"]
    assert names(ChangeFilter(ip="198.51.100.20")) == ["Mosquitto broker"]
    assert names(ChangeFilter(search="mosq")) == ["Mosquitto broker"]
    assert names(ChangeFilter(search="50%")) == ["Other"]


def test_login_filter_search(store: FenceStore) -> None:
    """Search matches the user agent and the IP."""

    def attempt(ip: str, agent: str) -> LoginAttempt:
        return LoginAttempt(
            ts=NOW,
            result=LoginResult.FAILED,
            reason=LoginReason.WRONG_PASSWORD,
            username_typed="alice",
            user_exists=UserExists.NO,
            user_id=None,
            provider=None,
            ip=ip,
            source=Source.REMOTE,
            url=None,
            user_agent=agent,
            client_id=None,
        )

    store.insert([attempt("203.0.113.5", "curl/8.1"), attempt("192.0.2.1", "Firefox")])
    assert [r.user_agent for r in store.query_logins(LoginFilter(search="curl"))] == [
        "curl/8.1"
    ]
    assert [r.ip for r in store.query_logins(LoginFilter(search="203.0.113"))] == [
        "203.0.113.5"
    ]


def test_purge_keeps_audit_tables(store: FenceStore) -> None:
    """Purging old rows never touches the audit tables."""
    store.save_reference("o/r", "v1", {"a": "1"})
    store.save_baseline("x", "1", {"a": "1"})
    store.save_finding("x", "c", {"k": 1}, NOW - timedelta(days=400))
    store.save_app_snapshot({"a": AppSnapshot("a", "A", "1", None, None, {})}, NOW)
    store.save_app_repositories({"u"})
    store.purge_older_than(NOW + timedelta(days=1))
    assert store.reference_hashes("o/r", "v1") == {"a": "1"}
    assert store.baseline("x") == ("1", {"a": "1"})
    assert store.finding("x", "c") == {"k": 1}
    assert set(store.app_snapshot()) == {"a"}
    assert store.app_repositories() == {"u"}
