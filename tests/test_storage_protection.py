"""Tests for the protection tables in the SQLite storage."""

from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

import pytest

from custom_components.electric_fence.models import (
    COUNTED_FAILURES,
    LoginAttempt,
    LoginReason,
    LoginResult,
    Source,
    UserExists,
)
from custom_components.electric_fence.storage import (
    SCHEMA_VERSION,
    FenceStore,
    LoginFilter,
    read_active_ban_ips,
)

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
IP = "198.51.100.7"

V1_SCHEMA = """
CREATE TABLE login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    result TEXT NOT NULL,
    reason TEXT NOT NULL,
    username_typed TEXT,
    user_exists TEXT NOT NULL,
    user_id TEXT,
    provider TEXT,
    ip TEXT,
    country_code TEXT,
    hostname TEXT,
    source TEXT,
    client_id TEXT,
    client_name TEXT,
    user_agent TEXT,
    url TEXT,
    mfa_module TEXT,
    confirmed INTEGER NOT NULL DEFAULT 1,
    count INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE settings_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT,
    target_name TEXT,
    details_json TEXT NOT NULL,
    actor_user_id TEXT,
    actor_name TEXT,
    actor_ip TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@pytest.fixture
def store(tmp_path: Path) -> Iterator[FenceStore]:
    """Return an open store."""
    s = FenceStore(tmp_path / "fence.db")
    s.open()
    yield s
    s.close()


def add_ban(
    store: FenceStore, banned_at: datetime = NOW, ip: str = IP, number: int = 1
) -> int:
    """Add a ban."""
    return store.add_ban(ip, banned_at, None, 5, number, "NL", "host.example")


def test_counted_failures() -> None:
    """Only credential failures count."""
    assert (
        frozenset(
            {
                LoginReason.NO_SUCH_USER,
                LoginReason.WRONG_PASSWORD,
                LoginReason.INVALID_CREDENTIALS,
                LoginReason.WRONG_2FA_CODE,
                LoginReason.THROTTLED,
            }
        )
        == COUNTED_FAILURES
    )


def test_migrates_v1(tmp_path: Path) -> None:
    """A v1 database gains the new column and tables and seeds countries."""
    path = tmp_path / "fence.db"
    con = sqlite3.connect(path)
    con.executescript(V1_SCHEMA)
    con.execute(
        "INSERT INTO login_attempts (ts, result, reason, user_exists, user_id, "
        "country_code) VALUES (?, 'success', 'ok', 'yes', 'u1', 'NL')",
        (NOW.isoformat(timespec="microseconds"),),
    )
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()

    store = FenceStore(path)
    store.open()
    with closing(sqlite3.connect(path)) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 3
    rows = store.query_logins(LoginFilter())
    assert [r.new_country for r in rows] == [False]
    assert store.note_country("u1", "NL") is False
    assert store.note_country("u1", "DE") is True
    store.close()


def _v1_db(path: Path, extra: str = "") -> None:
    with closing(sqlite3.connect(path)) as con:
        con.executescript(V1_SCHEMA + extra)
        con.execute("PRAGMA user_version = 1")
        con.commit()


def test_migrates_v1_with_column_already_added(tmp_path: Path) -> None:
    """A v1 database left with the new column (crashed migration) still opens."""
    path = tmp_path / "fence.db"
    _v1_db(
        path,
        "ALTER TABLE login_attempts ADD COLUMN new_country INTEGER NOT NULL DEFAULT 0;",
    )
    store = FenceStore(path)
    store.open()
    store.close()
    with closing(sqlite3.connect(path)) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 3


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    """A failing migration leaves the v1 database intact and openable."""
    path = tmp_path / "fence.db"
    _v1_db(path, "CREATE TABLE bans (id INTEGER);")
    with pytest.raises(sqlite3.Error):
        FenceStore(path).open()
    with closing(sqlite3.connect(path)) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = [r[1] for r in check.execute("PRAGMA table_info(login_attempts)")]
        assert "new_country" not in columns
        check.execute("DROP TABLE bans")
        check.commit()
    store = FenceStore(path)
    store.open()
    store.close()


def test_reopen_after_migration_keeps_countries(tmp_path: Path) -> None:
    """Opening twice does not duplicate or lose seeded countries."""
    path = tmp_path / "fence.db"
    store = FenceStore(path)
    store.open()
    store.note_country("u1", "NL")
    store.close()
    store.open()
    assert store.known_countries("u1") == ["NL"]
    store.close()


def test_schema_version() -> None:
    """The schema version is 3."""
    assert SCHEMA_VERSION == 3


def test_note_country_first_is_not_new(store: FenceStore) -> None:
    """The first country is never new; a second different one is."""
    assert store.note_country("u2", "FR") is False
    assert store.note_country("u2", "FR") is False
    assert store.note_country("u2", "BE") is True
    assert store.note_country("u2", "BE") is False
    assert store.known_countries("u2") == ["BE", "FR"]


def test_forget_and_clear_countries(store: FenceStore) -> None:
    """Countries can be forgotten per user or cleared altogether."""
    store.note_country("a", "NL")
    store.note_country("a", "DE")
    store.note_country("b", "FR")
    store.forget_user_countries("a")
    assert store.known_countries("a") == []
    assert store.clear_countries() == 1
    assert store.known_countries("b") == []


def test_new_country_login_roundtrip(store: FenceStore) -> None:
    """The new_country flag is stored and filterable."""
    base = LoginAttempt(
        ts=NOW,
        result=LoginResult.SUCCESS,
        reason=LoginReason.OK,
        username_typed="a",
        user_exists=UserExists.YES,
        user_id="u",
        provider=None,
        ip=IP,
        source=Source.REMOTE,
        url=None,
        user_agent=None,
        client_id=None,
    )
    flagged = LoginAttempt(**{**_fields(base), "new_country": True})
    store.insert([base, flagged])
    rows = store.query_logins(LoginFilter(new_country=True))
    assert [r.new_country for r in rows] == [True]
    assert len(store.query_logins(LoginFilter())) == 2


def _fields(a: LoginAttempt) -> dict[str, object]:
    return {
        name: getattr(a, name)
        for name in LoginAttempt.__slots__
        if name != "credentials"
    }


def test_purge_keeps_active_bans_and_countries(store: FenceStore) -> None:
    """Purge deletes only ended bans and attacks, never countries."""
    old = NOW - timedelta(days=100)
    active = add_ban(store, old)
    ended = add_ban(store, old, ip="198.51.100.8")
    store.end_ban(ended, old + timedelta(hours=1), "expired", None, None)
    attack = store.add_attack("alice", old)
    store.end_attack(attack, old + timedelta(hours=1), "quiet", None, None)
    store.add_attack("bob", old)
    store.note_country("u1", "NL")
    assert store.purge_older_than(NOW - timedelta(days=90)) == 2
    assert [b.id for b in store.active_bans()] == [active]
    assert len(store.query_attacks(active=True)) == 1
    assert store.query_attacks(active=False) == []
    assert store.known_countries("u1") == ["NL"]


def test_bans_roundtrip(store: FenceStore) -> None:
    """Add, list and end a ban."""
    ban_id = store.add_ban(IP, NOW, NOW + timedelta(hours=24), 5, 1, "NL", "h")
    [row] = store.active_bans()
    assert (row.id, row.ip, row.failures, row.ban_number) == (ban_id, IP, 5, 1)
    assert row.expires_at == NOW + timedelta(hours=24)
    assert (row.country_code, row.hostname, row.ended_at) == ("NL", "h", None)
    store.end_ban(ban_id, NOW + timedelta(hours=1), "unbanned", "u1", "Ann")
    assert store.active_bans() == []
    [ended] = store.query_bans(active=False)
    assert ended.end_reason == "unbanned"
    assert ended.ended_by_name == "Ann"
    assert ended.ended_by_user_id == "u1"
    assert store.query_bans(active=True) == []


def test_query_bans_paging(store: FenceStore) -> None:
    """Bans page newest first by id."""
    ids = [add_ban(store, NOW + timedelta(minutes=i)) for i in range(3)]
    first = store.query_bans(active=True, limit=2)
    assert [b.id for b in first] == [ids[2], ids[1]]
    rest = store.query_bans(active=True, before_id=ids[1])
    assert [b.id for b in rest] == [ids[0]]


def test_count_bans_since(store: FenceStore) -> None:
    """Bans are counted from a point in time."""
    add_ban(store, NOW - timedelta(days=2))
    add_ban(store, NOW)
    assert store.count_bans_since(NOW - timedelta(days=1)) == 1


def test_count_ip_bans_since(store: FenceStore) -> None:
    """Bans are counted per IP."""
    add_ban(store, NOW - timedelta(days=40))
    add_ban(store, NOW - timedelta(days=5))
    add_ban(store, NOW)
    add_ban(store, NOW, ip="198.51.100.9")
    assert store.count_ip_bans_since(IP, NOW - timedelta(days=30)) == 2


def test_attacks_roundtrip(store: FenceStore) -> None:
    """Add, update and end an attack."""
    attack_id = store.add_attack("alice", NOW)
    [active] = store.query_attacks(active=True)
    assert active.id == attack_id
    assert active.countries == []
    store.update_attack(attack_id, 12, 4, ["NL", "DE"])
    store.end_attack(attack_id, NOW + timedelta(minutes=30), "quiet", None, None)
    assert store.query_attacks(active=True) == []
    [row] = store.query_attacks(active=False)
    assert row.countries == ["NL", "DE"]
    assert (row.username, row.failures, row.ip_count) == ("alice", 12, 4)
    assert row.end_reason == "quiet"
    assert row.ended_at == NOW + timedelta(minutes=30)


def test_end_open_attacks(store: FenceStore) -> None:
    """Every open attack ends as quiet; ended ones are untouched."""
    first = store.add_attack("alice", NOW)
    store.add_attack("bob", NOW)
    done = store.add_attack("carol", NOW)
    store.end_attack(done, NOW, "ended_by_user", "u1", "Ann")
    assert store.end_open_attacks(NOW + timedelta(hours=1)) == 2
    assert store.end_open_attacks(NOW + timedelta(hours=2)) == 0
    assert store.query_attacks(active=True) == []
    rows = {r.id: r for r in store.query_attacks(active=False)}
    assert rows[first].end_reason == "quiet"
    assert rows[first].ended_at == NOW + timedelta(hours=1)
    assert rows[done].end_reason == "ended_by_user"
    assert rows[done].ended_at == NOW


def test_last_successes_since(store: FenceStore) -> None:
    """The latest confirmed success per (IP, username); failures are ignored."""
    unconfirmed = _login(NOW - timedelta(hours=1), LoginResult.SUCCESS, "203.0.113.5")
    unconfirmed.confirmed = False
    store.insert(
        [
            _login(NOW - timedelta(hours=3), LoginResult.SUCCESS, "203.0.113.1"),
            _login(NOW - timedelta(hours=1), LoginResult.SUCCESS, "203.0.113.1"),
            _login(NOW - timedelta(hours=2), LoginResult.SUCCESS, "203.0.113.1", "b"),
            _login(NOW - timedelta(hours=2), LoginResult.SUCCESS, "203.0.113.2"),
            _login(NOW - timedelta(hours=1), LoginResult.FAILED, "203.0.113.3"),
            _login(NOW - timedelta(days=3), LoginResult.SUCCESS, "203.0.113.4"),
            unconfirmed,
        ]
    )
    assert sorted(store.last_successes_since(NOW - timedelta(days=1))) == [
        ("203.0.113.1", "a", NOW - timedelta(hours=1)),
        ("203.0.113.1", "b", NOW - timedelta(hours=2)),
        ("203.0.113.2", "a", NOW - timedelta(hours=2)),
    ]


def _login(ts: datetime, result: LoginResult, ip: str, user: str = "a") -> LoginAttempt:
    return LoginAttempt(
        ts=ts,
        result=result,
        reason=LoginReason.OK
        if result is LoginResult.SUCCESS
        else LoginReason.WRONG_PASSWORD,
        username_typed=user,
        user_exists=UserExists.YES,
        user_id="u",
        provider=None,
        ip=ip,
        source=Source.REMOTE,
        url=None,
        user_agent=None,
        client_id=None,
    )


def test_read_active_ban_ips(tmp_path: Path) -> None:
    """Active bans are read from a closed database without changing it."""
    path = tmp_path / "fence.db"
    assert read_active_ban_ips(path) == []
    s = FenceStore(path)
    s.open()
    add_ban(s)
    ended = add_ban(s, ip="198.51.100.8")
    s.end_ban(ended, NOW, "expired", None, None)
    s.close()
    before = path.read_bytes()
    assert read_active_ban_ips(path) == [IP]
    assert path.read_bytes() == before


def test_read_active_ban_ips_without_bans_table(tmp_path: Path) -> None:
    """A database from before bans existed has none."""
    path = tmp_path / "fence.db"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(V1_SCHEMA)
    assert read_active_ban_ips(path) == []
