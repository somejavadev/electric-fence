"""Tests for the SQLite storage."""
# ruff: noqa: SLF001

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from custom_components.electric_fence.models import (
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
    SCHEMA_VERSION,
    ChangeFilter,
    FenceStore,
    LoginFilter,
)

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)


def login(
    ts: datetime = NOW,
    *,
    result: LoginResult = LoginResult.FAILED,
    username: str | None = "alice",
    user_id: str | None = None,
    ip: str | None = "203.0.113.7",
    country_code: str | None = "NL",
    count: int = 1,
) -> LoginAttempt:
    """Build a login attempt."""
    return LoginAttempt(
        ts=ts,
        result=result,
        reason=LoginReason.OK
        if result is LoginResult.SUCCESS
        else LoginReason.WRONG_PASSWORD,
        username_typed=username,
        user_exists=UserExists.YES if user_id else UserExists.NO,
        user_id=user_id,
        provider="homeassistant",
        ip=ip,
        source=Source.REMOTE,
        url="/auth/login_flow",
        user_agent="UA",
        client_id="https://example.com/",
        country_code=country_code,
        count=count,
        flow_id="secret-flow",
    )


def change(
    ts: datetime = NOW,
    *,
    kind: ChangeKind = ChangeKind.USER,
    actor: str | None = "u1",
) -> SettingsChange:
    """Build a settings change."""
    return SettingsChange(
        ts=ts,
        kind=kind,
        action=ChangeAction.UPDATED,
        target_id="t1",
        target_name="Target",
        details={"b": 2, "a": 1},
        actor_user_id=actor,
        actor_name="Actor" if actor else None,
        actor_ip="10.0.0.1" if actor else None,
    )


@pytest.fixture
def store(tmp_path: Path):
    """Return an open store."""
    s = FenceStore(tmp_path / "fence.db")
    s.open()
    yield s
    s.close()


def test_insert_and_query_logins_newest_first(store: FenceStore) -> None:
    """Rows come back newest first with typed fields."""
    store.insert(
        [
            login(NOW - timedelta(minutes=2), username="one"),
            login(NOW - timedelta(minutes=1), username="two", user_id="u1"),
            login(NOW, username="three"),
        ]
    )
    rows = store.query_logins(LoginFilter())
    assert [r.username_typed for r in rows] == ["three", "two", "one"]
    row = rows[1]
    assert row.ts == NOW - timedelta(minutes=1)
    assert row.ts.tzinfo is not None
    assert row.result is LoginResult.FAILED
    assert row.reason is LoginReason.WRONG_PASSWORD
    assert row.user_exists is UserExists.YES
    assert row.source is Source.REMOTE
    assert row.user_id == "u1"
    assert row.confirmed is True
    assert row.count == 1


def test_non_utc_timestamps_are_normalised(store: FenceStore) -> None:
    """Offsets and zero microseconds don't break ordering or comparison."""
    plus2 = timezone(timedelta(hours=2))
    store.insert(
        [
            login(datetime(2026, 10, 24, 14, 0, 0, 1, tzinfo=plus2), username="b"),
            login(datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC), username="a"),
        ]
    )
    rows = store.query_logins(LoginFilter(since=NOW))
    assert {r.username_typed for r in rows} == {"a", "b"}
    assert store.query_logins(LoginFilter(since=NOW + timedelta(microseconds=2))) == []
    assert all(r.ts.utcoffset() == timedelta(0) for r in rows)


def test_naive_timestamp_rejected(store: FenceStore) -> None:
    """Naive datetimes are a programming error."""
    with pytest.raises(ValueError, match="timezone"):
        store.insert([login(datetime(2026, 1, 1))])
    assert store.stats().logins == 0


def test_query_logins_filters(store: FenceStore) -> None:
    """Each filter field narrows the result."""
    store.insert(
        [
            login(
                NOW - timedelta(days=3),
                result=LoginResult.SUCCESS,
                username="Alice",
                user_id="u1",
                ip="198.51.100.1",
                country_code="DE",
            ),
            login(NOW - timedelta(days=2), username="bob_100%", ip="203.0.113.7"),
            login(NOW - timedelta(days=1), username="bobX100Y", ip="203.0.113.8"),
        ]
    )

    def names(f: LoginFilter) -> list[str | None]:
        return [r.username_typed for r in store.query_logins(f)]

    assert names(LoginFilter(result=LoginResult.SUCCESS)) == ["Alice"]
    assert names(LoginFilter(user_id="u1")) == ["Alice"]
    assert names(LoginFilter(username_contains="ALIC")) == ["Alice"]
    assert names(LoginFilter(ip="203.0.113.8")) == ["bobX100Y"]
    assert names(LoginFilter(country_code="DE")) == ["Alice"]
    assert names(LoginFilter(since=NOW - timedelta(days=2))) == ["bobX100Y", "bob_100%"]
    assert names(LoginFilter(until=NOW - timedelta(days=2))) == ["bob_100%", "Alice"]
    # LIKE wildcards in user input are literal.
    assert names(LoginFilter(username_contains="_100%")) == ["bob_100%"]
    assert names(LoginFilter(username_contains="%")) == ["bob_100%"]
    assert names(LoginFilter(username_contains="b_b")) == []
    assert names(LoginFilter(username_contains="\\")) == []


def test_keyset_paging_returns_next_page(store: FenceStore) -> None:
    """before_id continues after the last row of the previous page."""
    store.insert(
        [login(NOW + timedelta(seconds=i), username=f"u{i}") for i in range(5)]
    )
    page1 = store.query_logins(LoginFilter(), limit=2)
    page2 = store.query_logins(LoginFilter(), limit=2, before_id=page1[-1].id)
    page3 = store.query_logins(LoginFilter(), limit=2, before_id=page2[-1].id)
    assert [r.username_typed for r in page1] == ["u4", "u3"]
    assert [r.username_typed for r in page2] == ["u2", "u1"]
    assert [r.username_typed for r in page3] == ["u0"]
    assert store.query_logins(LoginFilter(), limit=2, before_id=page3[-1].id) == []


def test_changes_hide_system_by_default(store: FenceStore) -> None:
    """System changes (no actor) are hidden unless asked for."""
    store.insert(
        [
            change(NOW - timedelta(minutes=2), actor=None),
            change(NOW - timedelta(minutes=1), actor="u1", kind=ChangeKind.CORE_CONFIG),
            change(NOW, actor="u2"),
        ]
    )
    default = store.query_changes(ChangeFilter())
    assert [r.actor_user_id for r in default] == ["u2", "u1"]
    assert default[0].details == {"a": 1, "b": 2}
    assert default[0].kind is ChangeKind.USER
    assert default[0].action is ChangeAction.UPDATED
    assert default[0].ts == NOW
    assert len(store.query_changes(ChangeFilter(include_system=True))) == 3
    assert [
        r.actor_user_id
        for r in store.query_changes(ChangeFilter(kind=ChangeKind.CORE_CONFIG))
    ] == ["u1"]
    assert [
        r.actor_user_id for r in store.query_changes(ChangeFilter(actor_user_id="u2"))
    ] == ["u2"]
    assert len(store.query_changes(ChangeFilter(since=NOW))) == 1
    assert (
        len(
            store.query_changes(
                ChangeFilter(include_system=True, until=NOW - timedelta(minutes=1))
            )
        )
        == 2
    )
    page = store.query_changes(ChangeFilter(), limit=1)
    assert [
        r.actor_user_id
        for r in store.query_changes(ChangeFilter(), limit=1, before_id=page[0].id)
    ] == ["u1"]


def test_count_failed_since_sums_count(store: FenceStore) -> None:
    """Merged rows count as their count; only failures since the cutoff."""
    store.insert(
        [
            login(NOW, count=5),
            login(NOW, count=1),
            login(NOW, result=LoginResult.SUCCESS, count=1),
            login(NOW, result=LoginResult.BLOCKED, count=1),
            login(NOW - timedelta(hours=2), count=10),
        ]
    )
    assert store.count_failed_since(NOW - timedelta(hours=1)) == 6
    assert store.count_failed_since(NOW + timedelta(hours=1)) == 0


def test_last_success_per_user(store: FenceStore) -> None:
    """The newest success per user id is returned."""
    store.insert(
        [
            login(
                NOW - timedelta(days=2),
                result=LoginResult.SUCCESS,
                user_id="u1",
                ip="1.1.1.1",
            ),
            login(
                NOW - timedelta(days=1),
                result=LoginResult.SUCCESS,
                user_id="u1",
                ip="2.2.2.2",
            ),
            login(NOW, result=LoginResult.SUCCESS, user_id="u2", ip="3.3.3.3"),
            login(NOW, result=LoginResult.FAILED, user_id="u3"),
            login(NOW, result=LoginResult.SUCCESS, user_id=None),
        ]
    )
    result = store.last_success_per_user()
    assert set(result) == {"u1", "u2"}
    assert result["u1"].ip == "2.2.2.2"
    assert result["u2"].ip == "3.3.3.3"


def test_purge_older_than_deletes_only_old_rows(store: FenceStore) -> None:
    """Rows older than the cutoff go; newer ones stay."""
    store.insert(
        [
            login(NOW - timedelta(days=91)),
            login(NOW - timedelta(days=1)),
            change(NOW - timedelta(days=1)),
            change(NOW - timedelta(days=1)),
        ]
    )
    assert store.purge_older_than(NOW - timedelta(days=90)) == 1
    assert store.stats().logins == 1
    assert store.stats().changes == 2


def test_purge_counts_both_tables_and_vacuums(store: FenceStore) -> None:
    """Deleting most rows works (VACUUM path) across both tables."""
    store.insert(
        [login(NOW - timedelta(days=100)) for _ in range(5)]
        + [change(NOW - timedelta(days=100)) for _ in range(3)]
        + [login(NOW)]
    )
    assert store.purge_older_than(NOW - timedelta(days=90)) == 8
    stats = store.stats()
    assert (stats.logins, stats.changes) == (1, 0)
    assert stats.size_bytes > 0


def test_reopen_keeps_data_and_schema_version(tmp_path: Path) -> None:
    """Data and schema version survive close and reopen."""
    path = tmp_path / "fence.db"
    first = FenceStore(path)
    first.open()
    first.insert([login()])
    first.close()

    second = FenceStore(path)
    second.open()
    assert len(second.query_logins(LoginFilter())) == 1
    second.close()

    con = sqlite3.connect(path)
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1 == SCHEMA_VERSION
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert (
            con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[
                0
            ]
            == "1"
        )
    finally:
        con.close()


def test_credentials_never_persisted(store: FenceStore, tmp_path: Path) -> None:
    """No credentials or flow id column exists."""
    store.insert([login()])
    store.close()
    con = sqlite3.connect(tmp_path / "fence.db")
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(login_attempts)")}
    finally:
        con.close()
    assert "credentials" not in cols
    assert "flow_id" not in cols
    assert "count" in cols
    store.open()


def test_paging_orders_by_ts_not_id(store: FenceStore) -> None:
    """Rows inserted out of ts order page by (ts, id) without gaps or repeats."""
    order = [3, 0, 4, 1, 2, 2]  # minutes; last two share a ts
    store.insert(
        [
            login(NOW + timedelta(minutes=m), username=f"n{i}")
            for i, m in enumerate(order)
        ]
    )
    seen: list[str | None] = []
    before = None
    while page := store.query_logins(LoginFilter(), limit=2, before_id=before):
        seen += [r.username_typed for r in page]
        before = page[-1].id
    # ts desc, then id desc for the tie (n5 and n4 share a ts).
    assert seen == ["n2", "n0", "n5", "n4", "n3", "n1"]


def test_changes_paging_orders_by_ts(store: FenceStore) -> None:
    """Changes page by (ts, id) too."""
    for m in (2, 0, 3, 1):
        store.insert([change(NOW + timedelta(minutes=m), actor=f"m{m}")])
    seen = []
    before = None
    while page := store.query_changes(ChangeFilter(), limit=3, before_id=before):
        seen += [r.actor_user_id for r in page]
        before = page[-1].id
    assert seen == ["m3", "m2", "m1", "m0"]


class _Proxy:
    """Connection wrapper that fails chosen statements."""

    def __init__(self, db: sqlite3.Connection, fail_on: str) -> None:
        self._db = db
        self._fail_on = fail_on

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
        if sql == self._fail_on:
            raise sqlite3.OperationalError("boom")
        return self._db.execute(sql, *args)  # type: ignore[arg-type]

    def __getattr__(self, name: str):
        return getattr(self._db, name)


def test_failed_commit_rolls_back_and_store_recovers(store: FenceStore) -> None:
    """A failing COMMIT leaves no open transaction."""
    real = store._db
    store._db = _Proxy(real, "COMMIT")  # type: ignore[assignment]
    with pytest.raises(sqlite3.OperationalError, match="boom"):
        store.insert([login(username="lost")])
    store._db = real
    store.insert([login(username="kept")])
    assert [r.username_typed for r in store.query_logins(LoginFilter())] == ["kept"]


def test_failed_commit_in_purge_rolls_back(store: FenceStore) -> None:
    """Purge with a failing COMMIT raises, then works again."""
    store.insert([login(NOW - timedelta(days=100))])
    real = store._db
    store._db = _Proxy(real, "COMMIT")  # type: ignore[assignment]
    with pytest.raises(sqlite3.OperationalError):
        store.purge_older_than(NOW - timedelta(days=90))
    store._db = real
    assert store.stats().logins == 1
    assert store.purge_older_than(NOW - timedelta(days=90)) == 1


def test_limit_is_clamped(store: FenceStore) -> None:
    """Zero, negative and huge limits are clamped to 1..500."""
    store.insert([login(NOW + timedelta(seconds=i)) for i in range(3)])
    assert len(store.query_logins(LoginFilter(), limit=0)) == 1
    assert len(store.query_logins(LoginFilter(), limit=-5)) == 1
    assert len(store.query_logins(LoginFilter(), limit=10**9)) == 3
    store.insert([change()])
    assert len(store.query_changes(ChangeFilter(), limit=0)) == 1


def test_purge_survives_vacuum_failure(store: FenceStore) -> None:
    """A VACUUM error after commit doesn't hide the deleted count."""
    store.insert([login(NOW - timedelta(days=100))])
    store._db = _Proxy(store._db, "VACUUM")  # type: ignore[arg-type,assignment]
    assert store.purge_older_than(NOW - timedelta(days=90)) == 1
    assert store.stats().logins == 0


def test_last_success_ignores_unconfirmed(store: FenceStore) -> None:
    """Only confirmed successes count."""
    ok = login(
        NOW - timedelta(days=1), result=LoginResult.SUCCESS, user_id="u1", ip="1.1.1.1"
    )
    pending = replace(
        login(NOW, result=LoginResult.SUCCESS, user_id="u1", ip="2.2.2.2"),
        confirmed=False,
    )
    only_pending = replace(
        login(NOW, result=LoginResult.SUCCESS, user_id="u2"), confirmed=False
    )
    store.insert([ok, pending, only_pending])
    result = store.last_success_per_user()
    assert set(result) == {"u1"}
    assert result["u1"].ip == "1.1.1.1"


def test_count_includes_blocked_only_on_request(tmp_path: Path) -> None:
    """`count_failed_since` counts blocked rows only with `include_blocked`."""
    store = FenceStore(tmp_path / "c.db")
    store.open()
    try:
        token = {"reason": LoginReason.INVALID_TOKEN}
        store.insert(
            [
                login(NOW, count=2),
                replace(
                    login(NOW, result=LoginResult.BLOCKED, count=3),
                    reason=LoginReason.BLOCKED_INACTIVE,
                ),
                replace(login(NOW, count=4), **token),
                replace(login(NOW - timedelta(hours=2), count=9), **token),
            ]
        )
        since = NOW - timedelta(minutes=1)
        assert store.count_failed_since(since) == 6
        assert store.count_failed_since(since, include_blocked=True) == 9
        assert store.count_token_failures_since("203.0.113.7", since) == 4
        assert store.count_token_failures_since("1.1.1.1", since) == 0
    finally:
        store.close()


def test_failed_buckets_since_lists_rows_ascending(store: FenceStore) -> None:
    """Failed and blocked rows come back as (ts, count), oldest first."""
    store.insert(
        [
            login(NOW, count=3),
            login(NOW - timedelta(hours=2), count=2),
            login(NOW - timedelta(hours=30), count=7),
            login(NOW, result=LoginResult.SUCCESS),
            login(NOW - timedelta(hours=1), result=LoginResult.BLOCKED),
        ]
    )
    since = NOW - timedelta(hours=24)
    assert store.failed_buckets_since(since) == [
        (NOW - timedelta(hours=2), 2),
        (NOW - timedelta(hours=1), 1),
        (NOW, 3),
    ]
    assert store.failed_buckets_since(since, include_blocked=False) == [
        (NOW - timedelta(hours=2), 2),
        (NOW, 3),
    ]
