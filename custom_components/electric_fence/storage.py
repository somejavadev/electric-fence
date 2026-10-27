"""SQLite storage for login attempts and settings changes.

Synchronous: call it from the executor only.
"""

from collections.abc import Iterator, Sequence
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import json
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any, Final, Literal

from .models import (
    OUTSIDE_ACTOR,
    ChangeAction,
    ChangeKind,
    LoginAttempt,
    LoginReason,
    LoginResult,
    Record,
    SettingsChange,
    Source,
    UserExists,
)

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 3

# A `LoginFilter.result` for failed and blocked attempts together.
FAILED_OR_BLOCKED: Final = "failed_or_blocked"
_MAX_LIMIT = 500
_AND = " AND "

_VACUUM_FRACTION = 0.2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS login_attempts (
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
    count INTEGER NOT NULL DEFAULT 1,
    new_country INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_login_ts ON login_attempts (ts);
CREATE INDEX IF NOT EXISTS idx_login_result_ts ON login_attempts (result, ts);
CREATE INDEX IF NOT EXISTS idx_login_user_ts ON login_attempts (user_id, ts);
CREATE INDEX IF NOT EXISTS idx_login_ip ON login_attempts (ip);
CREATE TABLE IF NOT EXISTS settings_changes (
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
CREATE INDEX IF NOT EXISTS idx_change_ts ON settings_changes (ts);
CREATE INDEX IF NOT EXISTS idx_change_kind_ts ON settings_changes (kind, ts);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_PROTECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS bans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT NOT NULL,
    banned_at TEXT NOT NULL,
    expires_at TEXT,
    failures INTEGER NOT NULL,
    ban_number INTEGER NOT NULL,
    country_code TEXT,
    hostname TEXT,
    ended_at TEXT,
    end_reason TEXT,
    ended_by_user_id TEXT,
    ended_by_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_ban_ip_at ON bans (ip, banned_at);
CREATE INDEX IF NOT EXISTS idx_ban_ended ON bans (ended_at);
CREATE TABLE IF NOT EXISTS attacks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    failures INTEGER NOT NULL DEFAULT 0,
    ip_count INTEGER NOT NULL DEFAULT 0,
    countries_json TEXT NOT NULL DEFAULT '[]',
    end_reason TEXT,
    ended_by_user_id TEXT,
    ended_by_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_attack_ended ON attacks (ended_at);
CREATE TABLE IF NOT EXISTS known_countries (
    user_id TEXT NOT NULL,
    country_code TEXT NOT NULL,
    PRIMARY KEY (user_id, country_code)
);
"""

_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_references (
    repository TEXT NOT NULL,
    ref TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (repository, ref, path)
);
CREATE TABLE IF NOT EXISTS file_baselines (
    domain TEXT NOT NULL,
    version TEXT,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (domain, path)
);
CREATE TABLE IF NOT EXISTS integration_findings (
    domain TEXT NOT NULL,
    check_name TEXT NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (domain, check_name)
);
CREATE TABLE IF NOT EXISTS app_snapshots (
    slug TEXT PRIMARY KEY,
    name TEXT,
    version TEXT,
    state TEXT,
    options_hash TEXT,
    settings_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_repositories (
    url TEXT PRIMARY KEY
);
"""

_SEED_COUNTRIES = (
    "INSERT OR IGNORE INTO known_countries (user_id, country_code) "
    "SELECT DISTINCT user_id, country_code FROM login_attempts "
    "WHERE result = 'success' AND user_id IS NOT NULL AND country_code IS NOT NULL;"
)

_ADD_NEW_COUNTRY = (
    "ALTER TABLE login_attempts ADD COLUMN new_country INTEGER NOT NULL DEFAULT 0;"
)


def _migration_v1(db: sqlite3.Connection) -> str:
    """Return the v1 to v2 script; user_version is set inside its transaction."""
    columns = {r[1] for r in db.execute("PRAGMA table_info(login_attempts)")}
    return (
        "BEGIN;"
        + ("" if "new_country" in columns else _ADD_NEW_COUNTRY)
        + _PROTECTION_SCHEMA
        + _SEED_COUNTRIES
        + "PRAGMA user_version = 2;"
        + "COMMIT;"
    )


def _migration_v2() -> str:
    """Return the v2 to v3 script; user_version is set inside its transaction."""
    return (
        "BEGIN;"
        + _AUDIT_SCHEMA
        + f"PRAGMA user_version = {int(SCHEMA_VERSION)};"
        + "COMMIT;"
    )


_LOGIN_COLUMNS = (
    "id, ts, result, reason, username_typed, user_exists, user_id, provider, ip, "
    "country_code, hostname, source, client_id, client_name, user_agent, url, "
    "mfa_module, confirmed, count, new_country"
)
_CHANGE_COLUMNS = (
    "id, ts, kind, action, target_id, target_name, details_json, "
    "actor_user_id, actor_name, actor_ip"
)


_LOGIN_SEARCH = (
    "username_typed",
    "ip",
    "hostname",
    "client_name",
    "client_id",
    "user_agent",
)
_CHANGE_SEARCH = ("target_id", "target_name", "actor_name", "actor_ip", "details_json")


class StoreSchemaTooNewError(Exception):
    """The database was written by a newer Electric Fence."""

    def __init__(self, version: int) -> None:
        """Initialise with the database's schema version."""
        super().__init__(f"database schema {version} is newer than {SCHEMA_VERSION}")
        self.version = version


def _check_version(db: sqlite3.Connection) -> int:
    """Return the schema version; raise StoreSchemaTooNewError if too new."""
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise StoreSchemaTooNewError(version)
    return version


def _to_text(ts: datetime) -> str:
    """Return a uniform UTC text form: fixed width, always with microseconds."""
    if ts.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return ts.astimezone(UTC).isoformat(timespec="microseconds")


def _from_text(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(frozen=True)
class LoginFilter:
    """Filter for login queries."""

    result: LoginResult | Literal["failed_or_blocked"] | None = None
    user_id: str | None = None
    username_contains: str | None = None
    ip: str | None = None
    country_code: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    new_country: bool | None = None
    search: str | None = None


@dataclass(frozen=True)
class ChangeFilter:
    """Filter for settings change queries."""

    kind: ChangeKind | None = None
    actor_user_id: str | None = None
    include_system: bool = False
    since: datetime | None = None
    until: datetime | None = None
    action: ChangeAction | None = None
    ip: str | None = None
    search: str | None = None


@dataclass(frozen=True)
class AppSnapshot:
    """The last seen state of one app."""

    slug: str
    name: str
    version: str | None
    state: str | None
    options_hash: str | None
    settings: dict[str, Any]


@dataclass(frozen=True)
class LoginRow:
    """A stored login attempt."""

    id: int
    ts: datetime
    result: LoginResult
    reason: LoginReason
    username_typed: str | None
    user_exists: UserExists
    user_id: str | None
    provider: str | None
    ip: str | None
    country_code: str | None
    hostname: str | None
    source: Source | None
    client_id: str | None
    client_name: str | None
    user_agent: str | None
    url: str | None
    mfa_module: str | None
    confirmed: bool
    count: int
    new_country: bool


@dataclass(frozen=True)
class BanRow:
    """A stored IP ban."""

    id: int
    ip: str
    banned_at: datetime
    expires_at: datetime | None
    failures: int
    ban_number: int
    country_code: str | None
    hostname: str | None
    ended_at: datetime | None
    end_reason: str | None
    ended_by_user_id: str | None
    ended_by_name: str | None


@dataclass(frozen=True)
class AttackRow:
    """A stored username attack."""

    id: int
    username: str
    started_at: datetime
    ended_at: datetime | None
    failures: int
    ip_count: int
    countries: list[str]
    end_reason: str | None
    ended_by_user_id: str | None
    ended_by_name: str | None


@dataclass(frozen=True)
class ChangeRow:
    """A stored settings change."""

    id: int
    ts: datetime
    kind: ChangeKind | str
    action: ChangeAction | str
    target_id: str | None
    target_name: str | None
    details: dict[str, Any]
    actor_user_id: str | None
    actor_name: str | None
    actor_ip: str | None


@dataclass(frozen=True)
class StoreStats:
    """Row counts and file size."""

    logins: int
    changes: int
    size_bytes: int


def _login_row(r: Sequence[Any]) -> LoginRow:
    return LoginRow(
        id=r[0],
        ts=_from_text(r[1]),
        result=LoginResult(r[2]),
        reason=LoginReason(r[3]),
        username_typed=r[4],
        user_exists=UserExists(r[5]),
        user_id=r[6],
        provider=r[7],
        ip=r[8],
        country_code=r[9],
        hostname=r[10],
        source=Source(r[11]) if r[11] is not None else None,
        client_id=r[12],
        client_name=r[13],
        user_agent=r[14],
        url=r[15],
        mfa_module=r[16],
        confirmed=bool(r[17]),
        count=r[18],
        new_country=bool(r[19]),
    )


def _tolerant[E: StrEnum](enum: type[E], raw: str) -> E | str:
    """Return the enum member, or the raw text for a name this version lacks."""
    try:
        return enum(raw)
    except ValueError:
        return raw


def _change_row(r: Sequence[Any]) -> ChangeRow:
    return ChangeRow(
        id=r[0],
        ts=_from_text(r[1]),
        kind=_tolerant(ChangeKind, r[2]),
        action=_tolerant(ChangeAction, r[3]),
        target_id=r[4],
        target_name=r[5],
        details=json.loads(r[6]),
        actor_user_id=r[7],
        actor_name=r[8],
        actor_ip=r[9],
    )


_BAN_COLUMNS = (
    "id, ip, banned_at, expires_at, failures, ban_number, country_code, hostname, "
    "ended_at, end_reason, ended_by_user_id, ended_by_name"
)
_ATTACK_COLUMNS = (
    "id, username, started_at, ended_at, failures, ip_count, countries_json, "
    "end_reason, ended_by_user_id, ended_by_name"
)


def _optional_from_text(text: str | None) -> datetime | None:
    return _from_text(text) if text is not None else None


def _ban_row(r: Sequence[Any]) -> BanRow:
    return BanRow(
        id=r[0],
        ip=r[1],
        banned_at=_from_text(r[2]),
        expires_at=_optional_from_text(r[3]),
        failures=r[4],
        ban_number=r[5],
        country_code=r[6],
        hostname=r[7],
        ended_at=_optional_from_text(r[8]),
        end_reason=r[9],
        ended_by_user_id=r[10],
        ended_by_name=r[11],
    )


def _attack_row(r: Sequence[Any]) -> AttackRow:
    return AttackRow(
        id=r[0],
        username=r[1],
        started_at=_from_text(r[2]),
        ended_at=_optional_from_text(r[3]),
        failures=r[4],
        ip_count=r[5],
        countries=[str(c) for c in json.loads(r[6])],
        end_reason=r[7],
        ended_by_user_id=r[8],
        ended_by_name=r[9],
    )


def _login_values(a: LoginAttempt) -> tuple[Any, ...]:
    return (
        _to_text(a.ts),
        str(a.result),
        str(a.reason),
        a.username_typed,
        str(a.user_exists),
        a.user_id,
        a.provider,
        a.ip,
        a.country_code,
        a.hostname,
        str(a.source) if a.source is not None else None,
        a.client_id,
        a.client_name,
        a.user_agent,
        a.url,
        a.mfa_module,
        1 if a.confirmed else 0,
        a.count,
        1 if a.new_country else 0,
    )


def _change_values(c: SettingsChange) -> tuple[Any, ...]:
    return (
        _to_text(c.ts),
        str(c.kind),
        str(c.action),
        c.target_id,
        c.target_name,
        json.dumps(c.details, sort_keys=True, ensure_ascii=False),
        c.actor_user_id,
        c.actor_name,
        c.actor_ip,
    )


def _time_clauses(
    where: list[str], params: list[Any], since: datetime | None, until: datetime | None
) -> None:
    if since is not None:
        where.append("ts >= ?")
        params.append(_to_text(since))
    if until is not None:
        where.append("ts <= ?")
        params.append(_to_text(until))


def _search_clause(
    where: list[str], params: list[Any], search: str | None, columns: Sequence[str]
) -> None:
    if not search:
        return
    pattern = f"%{_like_escape(search)}%"
    where.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in columns) + ")")
    params.extend([pattern] * len(columns))


def read_active_ban_ips(path: Path) -> list[str]:
    """Return the addresses of the active bans in a closed database.

    Opened read-only, so nothing is created or migrated; [] without a
    database or a bans table.
    """
    if not path.is_file():
        return []
    db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'bans'"
        ).fetchone():
            return []
        rows = db.execute("SELECT ip FROM bans WHERE ended_at IS NULL").fetchall()
    finally:
        db.close()
    return [str(r[0]) for r in rows]


# Marks a baseline of zero files, so its version row still exists
_EMPTY_BASELINE = ""


class FenceStore:
    """SQLite repository. Synchronous and thread-safe via one lock."""

    def __init__(self, path: Path) -> None:
        """Initialise without opening the database."""
        self._path = path
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("FenceStore is not open")
        return self._db

    def open(self) -> None:
        """Open the database and create the schema.

        Raises StoreSchemaTooNewError, before changing anything, when the
        database has a newer schema version than this code knows.
        """
        with self._lock:
            if self._db is not None:
                return
            # Autocommit mode: transactions are explicit (VACUUM needs none).
            db = sqlite3.connect(
                self._path, check_same_thread=False, isolation_level=None
            )
            try:
                version = _check_version(db)
                db.execute("PRAGMA journal_mode = WAL")
                if version == 1:
                    try:
                        db.executescript(_migration_v1(db))
                    except BaseException:
                        with contextlib.suppress(sqlite3.Error):
                            db.execute("ROLLBACK")
                        raise
                if version == 2:
                    try:
                        db.executescript(_migration_v2())
                    except BaseException:
                        with contextlib.suppress(sqlite3.Error):
                            db.execute("ROLLBACK")
                        raise
                db.executescript(_SCHEMA + _PROTECTION_SCHEMA + _AUDIT_SCHEMA)
                db.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
                db.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
            except Exception:
                db.close()
                raise
            self._db = db

    def close(self) -> None:
        """Close the database. Safe to call twice."""
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def insert(self, records: Sequence[Record]) -> None:
        """Insert records in one transaction (all or nothing)."""
        with self._lock:
            db = self._conn()
            # Convert first so a bad record fails before any write.
            logins: list[tuple[Any, ...]] = []
            changes: list[tuple[Any, ...]] = []
            for rec in records:
                if isinstance(rec, LoginAttempt):
                    logins.append(_login_values(rec))
                else:
                    changes.append(_change_values(rec))
            db.execute("BEGIN")
            try:
                db.executemany(
                    "INSERT INTO login_attempts (ts, result, reason, username_typed, "
                    "user_exists, user_id, provider, ip, country_code, hostname, "
                    "source, client_id, client_name, user_agent, url, mfa_module, "
                    "confirmed, count, new_country) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    logins,
                )
                db.executemany(
                    "INSERT INTO settings_changes (ts, kind, action, target_id, "
                    "target_name, details_json, actor_user_id, actor_name, actor_ip) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    changes,
                )
                db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    db.execute("ROLLBACK")
                raise

    def query_logins(
        self, f: LoginFilter, *, limit: int = 50, before_id: int | None = None
    ) -> list[LoginRow]:
        """Return login attempts, newest first (ts, id), with keyset paging."""
        where: list[str] = []
        params: list[Any] = []
        if f.result == FAILED_OR_BLOCKED:
            where.append("result IN (?, ?)")
            params.extend((str(LoginResult.FAILED), str(LoginResult.BLOCKED)))
        elif f.result is not None:
            where.append("result = ?")
            params.append(str(f.result))
        if f.user_id is not None:
            where.append("user_id = ?")
            params.append(f.user_id)
        if f.username_contains:
            where.append("username_typed LIKE ? ESCAPE '\\'")
            params.append(f"%{_like_escape(f.username_contains)}%")
        if f.ip is not None:
            where.append("ip = ?")
            params.append(f.ip)
        if f.country_code is not None:
            where.append("country_code = ?")
            params.append(f.country_code)
        if f.new_country:
            where.append("new_country = 1")
        _search_clause(where, params, f.search, _LOGIN_SEARCH)
        _time_clauses(where, params, f.since, f.until)
        if before_id is not None:
            where.append("(ts, id) < (SELECT ts, id FROM login_attempts WHERE id = ?)")
            params.append(before_id)
        sql = f"SELECT {_LOGIN_COLUMNS} FROM login_attempts"  # noqa: S608
        if where:
            sql += " WHERE " + _AND.join(where)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(1, min(limit, _MAX_LIMIT)))
        with self._lock:
            rows = self._conn().execute(sql, params).fetchall()
        return [_login_row(r) for r in rows]

    def query_changes(
        self, f: ChangeFilter, *, limit: int = 50, before_id: int | None = None
    ) -> list[ChangeRow]:
        """Return settings changes, newest first (ts, id), with keyset paging."""
        where: list[str] = []
        params: list[Any] = []
        if f.kind is not None:
            where.append("kind = ?")
            params.append(str(f.kind))
        if f.actor_user_id is not None:
            where.append("actor_user_id = ?")
            params.append(f.actor_user_id)
        if f.action is not None:
            where.append("action = ?")
            params.append(str(f.action))
        if f.ip is not None:
            where.append("actor_ip = ?")
            params.append(f.ip)
        _search_clause(where, params, f.search, _CHANGE_SEARCH)
        if not f.include_system:
            # System only: changes made outside Home Assistant stay shown.
            where.append("(actor_user_id IS NOT NULL OR actor_name = ?)")
            params.append(OUTSIDE_ACTOR)
        _time_clauses(where, params, f.since, f.until)
        if before_id is not None:
            where.append(
                "(ts, id) < (SELECT ts, id FROM settings_changes WHERE id = ?)"
            )
            params.append(before_id)
        sql = f"SELECT {_CHANGE_COLUMNS} FROM settings_changes"  # noqa: S608
        if where:
            sql += " WHERE " + _AND.join(where)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(1, min(limit, _MAX_LIMIT)))
        with self._lock:
            rows = self._conn().execute(sql, params).fetchall()
        return [_change_row(r) for r in rows]

    def count_failed_since(
        self, since: datetime, *, include_blocked: bool = False
    ) -> int:
        """Return the number of failed attempts since a time (summing merged rows).

        With `include_blocked`, blocked attempts count too.
        """
        results = [str(LoginResult.FAILED)]
        if include_blocked:
            results.append(str(LoginResult.BLOCKED))
        marks = ",".join("?" * len(results))
        with self._lock:
            row = (
                self._conn()
                .execute(
                    "SELECT COALESCE(SUM(count), 0) FROM login_attempts "  # noqa: S608
                    f"WHERE result IN ({marks}) AND ts >= ?",
                    (*results, _to_text(since)),
                )
                .fetchone()
            )
        return int(row[0])

    def failed_buckets_since(
        self, since: datetime, *, include_blocked: bool = True
    ) -> list[tuple[datetime, int]]:
        """Return (ts, count) of failed rows since a time, oldest first."""
        results = [str(LoginResult.FAILED)]
        if include_blocked:
            results.append(str(LoginResult.BLOCKED))
        marks = ",".join("?" * len(results))
        with self._lock:
            rows = (
                self._conn()
                .execute(
                    "SELECT ts, count FROM login_attempts "  # noqa: S608
                    f"WHERE result IN ({marks}) AND ts >= ? ORDER BY ts, id",
                    (*results, _to_text(since)),
                )
                .fetchall()
            )
        return [(_from_text(r[0]), int(r[1])) for r in rows]

    def count_token_failures_since(self, ip: str, since: datetime) -> int:
        """Return the invalid token attempts from an IP since a time."""
        with self._lock:
            row = (
                self._conn()
                .execute(
                    "SELECT COALESCE(SUM(count), 0) FROM login_attempts "
                    "WHERE result = ? AND reason = ? AND ip = ? AND ts >= ?",
                    (
                        str(LoginResult.FAILED),
                        str(LoginReason.INVALID_TOKEN),
                        ip,
                        _to_text(since),
                    ),
                )
                .fetchone()
            )
        return int(row[0])

    def last_success_per_user(self) -> dict[str, LoginRow]:
        """Return each user's most recent successful login."""
        sql = (
            f"SELECT {_LOGIN_COLUMNS} FROM ("  # noqa: S608
            f"SELECT {_LOGIN_COLUMNS}, ROW_NUMBER() OVER ("
            "PARTITION BY user_id ORDER BY ts DESC, id DESC) AS rn "
            "FROM login_attempts WHERE result = ? AND confirmed = 1 AND user_id IS NOT NULL"
            ") WHERE rn = 1"
        )
        with self._lock:
            rows = self._conn().execute(sql, (str(LoginResult.SUCCESS),)).fetchall()
        result: dict[str, LoginRow] = {}
        for r in rows:
            row = _login_row(r)
            if row.user_id is not None:
                result[row.user_id] = row
        return result

    def strip_change_values(self) -> int:
        """Remove the `values` key from every stored change; return rows updated."""
        with self._lock:
            db = self._conn()
            rows = db.execute(
                "SELECT id, details_json FROM settings_changes "
                "WHERE details_json LIKE '%\"values\"%'"
            ).fetchall()
            updates: list[tuple[str, int]] = []
            for row_id, raw in rows:
                try:
                    details = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(details, dict) and "values" in details:
                    del details["values"]
                    updates.append(
                        (
                            json.dumps(details, sort_keys=True, ensure_ascii=False),
                            row_id,
                        )
                    )
            if not updates:
                return 0
            db.execute("BEGIN")
            try:
                db.executemany(
                    "UPDATE settings_changes SET details_json = ? WHERE id = ?",
                    updates,
                )
                db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    db.execute("ROLLBACK")
                raise
            return len(updates)

    def purge_older_than(self, cutoff: datetime) -> int:
        """Delete rows older than the cutoff; VACUUM if over 20% were removed."""
        text = _to_text(cutoff)
        with self._lock:
            db = self._conn()
            total = 0
            for table in ("login_attempts", "settings_changes", "bans", "attacks"):
                total += db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            deleted = 0
            db.execute("BEGIN")
            try:
                for table in ("login_attempts", "settings_changes"):
                    deleted += db.execute(
                        f"DELETE FROM {table} WHERE ts < ?",  # noqa: S608
                        (text,),
                    ).rowcount
                for table in ("bans", "attacks"):
                    deleted += db.execute(
                        f"DELETE FROM {table} WHERE ended_at < ?",  # noqa: S608
                        (text,),
                    ).rowcount
                db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    db.execute("ROLLBACK")
                raise
            if deleted and deleted > total * _VACUUM_FRACTION:
                try:
                    db.execute("VACUUM")
                except sqlite3.Error:
                    _LOGGER.debug("VACUUM failed after purge")
        return deleted

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Hold the lock and run the block in one transaction (all or nothing)."""
        with self._lock:
            db = self._conn()
            db.execute("BEGIN")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    db.execute("ROLLBACK")
                raise

    def _write(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        """Run one statement in its own transaction and return the cursor."""
        with self._lock:
            db = self._conn()
            db.execute("BEGIN")
            try:
                cursor = db.execute(sql, params)
                db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    db.execute("ROLLBACK")
                raise
            return cursor

    def _read(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        with self._lock:
            return self._conn().execute(sql, params).fetchall()

    def reference_hashes(self, repository: str, ref: str) -> dict[str, str] | None:
        """Return the cached official hashes of a repository at a ref, if any."""
        rows = self._read(
            "SELECT path, sha256 FROM file_references WHERE repository = ? AND ref = ?",
            (repository, ref),
        )
        return dict(rows) if rows else None

    def save_reference(self, repository: str, ref: str, hashes: dict[str, str]) -> None:
        """Cache official hashes, replacing any earlier ref of the repository."""
        with self._transaction() as db:
            db.execute(
                "DELETE FROM file_references WHERE repository = ?", (repository,)
            )
            db.executemany(
                "INSERT INTO file_references (repository, ref, path, sha256) "
                "VALUES (?, ?, ?, ?)",
                [(repository, ref, p, h) for p, h in hashes.items()],
            )

    def baseline(self, domain: str) -> tuple[str | None, dict[str, str]]:
        """Return the baseline version and file hashes of an integration."""
        rows = self._read(
            "SELECT version, path, sha256 FROM file_baselines WHERE domain = ?",
            (domain,),
        )
        if not rows:
            return None, {}
        return rows[0][0], {
            path: sha for _, path, sha in rows if path != _EMPTY_BASELINE
        }

    def save_baseline(
        self, domain: str, version: str | None, hashes: dict[str, str]
    ) -> None:
        """Replace the baseline of an integration."""
        with self._transaction() as db:
            db.execute("DELETE FROM file_baselines WHERE domain = ?", (domain,))
            db.executemany(
                "INSERT INTO file_baselines (domain, version, path, sha256) "
                "VALUES (?, ?, ?, ?)",
                [(domain, version, p, h) for p, h in hashes.items()]
                or [(domain, version, _EMPTY_BASELINE, "")],
            )

    def finding(self, domain: str, check_name: str) -> dict[str, Any] | None:
        """Return the stored state of one check, if any."""
        rows = self._read(
            "SELECT state_json FROM integration_findings "
            "WHERE domain = ? AND check_name = ?",
            (domain, check_name),
        )
        return json.loads(rows[0][0]) if rows else None

    def save_finding(
        self, domain: str, check_name: str, state: dict[str, Any], at: datetime
    ) -> None:
        """Store the state of one check."""
        self._write(
            "INSERT OR REPLACE INTO integration_findings "
            "(domain, check_name, state_json, updated_at) VALUES (?, ?, ?, ?)",
            (
                domain,
                check_name,
                json.dumps(state, sort_keys=True, ensure_ascii=False),
                _to_text(at),
            ),
        )

    def drop_findings(self, keep_domains: set[str]) -> None:
        """Delete the findings of integrations that are no longer installed."""
        with self._transaction() as db:
            marks = ", ".join("?" * len(keep_domains))
            db.execute(
                f"DELETE FROM integration_findings WHERE domain NOT IN ({marks})",  # noqa: S608
                sorted(keep_domains),
            )

    def app_snapshot(self) -> dict[str, AppSnapshot]:
        """Return the last seen state of every app, by slug."""
        rows = self._read(
            "SELECT slug, name, version, state, options_hash, settings_json "
            "FROM app_snapshots"
        )
        return {
            r[0]: AppSnapshot(r[0], r[1], r[2], r[3], r[4], json.loads(r[5]))
            for r in rows
        }

    def save_app_snapshot(self, snaps: dict[str, AppSnapshot], at: datetime) -> None:
        """Replace the stored app states."""
        text = _to_text(at)
        with self._transaction() as db:
            db.execute("DELETE FROM app_snapshots")
            db.executemany(
                "INSERT INTO app_snapshots (slug, name, version, state, "
                "options_hash, settings_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        s.slug,
                        s.name,
                        s.version,
                        s.state,
                        s.options_hash,
                        json.dumps(s.settings, sort_keys=True, ensure_ascii=False),
                        text,
                    )
                    for s in snaps.values()
                ],
            )

    def app_repositories(self) -> set[str]:
        """Return the known app repository URLs."""
        return {r[0] for r in self._read("SELECT url FROM app_repositories")}

    def save_app_repositories(self, urls: set[str]) -> None:
        """Replace the known app repository URLs."""
        with self._transaction() as db:
            db.execute("DELETE FROM app_repositories")
            db.executemany(
                "INSERT INTO app_repositories (url) VALUES (?)",
                [(u,) for u in sorted(urls)],
            )

    def add_ban(
        self,
        ip: str,
        banned_at: datetime,
        expires_at: datetime | None,
        failures: int,
        ban_number: int,
        country_code: str | None,
        hostname: str | None,
    ) -> int:
        """Record a ban and return its id."""
        cursor = self._write(
            "INSERT INTO bans (ip, banned_at, expires_at, failures, ban_number, "
            "country_code, hostname) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ip,
                _to_text(banned_at),
                _to_text(expires_at) if expires_at is not None else None,
                failures,
                ban_number,
                country_code,
                hostname,
            ),
        )
        return int(cursor.lastrowid or 0)

    def end_ban(
        self,
        ban_id: int,
        ended_at: datetime,
        end_reason: Literal["expired", "unbanned", "removed"],
        by_user_id: str | None,
        by_name: str | None,
    ) -> None:
        """Mark a ban as ended."""
        self._write(
            "UPDATE bans SET ended_at = ?, end_reason = ?, ended_by_user_id = ?, "
            "ended_by_name = ? WHERE id = ?",
            (_to_text(ended_at), end_reason, by_user_id, by_name, ban_id),
        )

    def active_bans(self) -> list[BanRow]:
        """Return the bans that have not ended, oldest first."""
        rows = self._read(
            f"SELECT {_BAN_COLUMNS} FROM bans "  # noqa: S608
            "WHERE ended_at IS NULL ORDER BY banned_at, id"
        )
        return [_ban_row(r) for r in rows]

    def query_bans(
        self, *, active: bool, limit: int = 50, before_id: int | None = None
    ) -> list[BanRow]:
        """Return active or ended bans, newest first, with keyset paging."""
        where = ["ended_at IS NULL" if active else "ended_at IS NOT NULL"]
        params: list[Any] = []
        if before_id is not None:
            where.append("id < ?")
            params.append(before_id)
        params.append(max(1, min(limit, _MAX_LIMIT)))
        rows = self._read(
            f"SELECT {_BAN_COLUMNS} FROM bans WHERE "  # noqa: S608
            + _AND.join(where)
            + " ORDER BY id DESC LIMIT ?",
            params,
        )
        return [_ban_row(r) for r in rows]

    def count_bans_since(self, since: datetime) -> int:
        """Return the number of bans made since a time."""
        row = self._read(
            "SELECT COUNT(*) FROM bans WHERE banned_at >= ?", (_to_text(since),)
        )[0]
        return int(row[0])

    def count_ip_bans_since(self, ip: str, since: datetime) -> int:
        """Return the number of bans of an IP since a time."""
        row = self._read(
            "SELECT COUNT(*) FROM bans WHERE ip = ? AND banned_at >= ?",
            (ip, _to_text(since)),
        )[0]
        return int(row[0])

    def add_attack(self, username: str, started_at: datetime) -> int:
        """Record the start of a username attack and return its id."""
        cursor = self._write(
            "INSERT INTO attacks (username, started_at) VALUES (?, ?)",
            (username, _to_text(started_at)),
        )
        return int(cursor.lastrowid or 0)

    def update_attack(
        self, attack_id: int, failures: int, ip_count: int, countries: list[str]
    ) -> None:
        """Update the running totals of an attack."""
        self._write(
            "UPDATE attacks SET failures = ?, ip_count = ?, countries_json = ? "
            "WHERE id = ?",
            (failures, ip_count, json.dumps(countries), attack_id),
        )

    def end_attack(
        self,
        attack_id: int,
        ended_at: datetime,
        end_reason: Literal["quiet", "ended_by_user"],
        by_user_id: str | None,
        by_name: str | None,
    ) -> None:
        """Mark an attack as ended."""
        self._write(
            "UPDATE attacks SET ended_at = ?, end_reason = ?, ended_by_user_id = ?, "
            "ended_by_name = ? WHERE id = ?",
            (_to_text(ended_at), end_reason, by_user_id, by_name, attack_id),
        )

    def end_open_attacks(self, ended_at: datetime) -> int:
        """End every attack still open as quiet; return how many."""
        cursor = self._write(
            "UPDATE attacks SET ended_at = ?, end_reason = 'quiet' "
            "WHERE ended_at IS NULL",
            (_to_text(ended_at),),
        )
        return cursor.rowcount

    def query_attacks(self, *, active: bool, limit: int = 50) -> list[AttackRow]:
        """Return active or ended attacks, newest first."""
        condition = "ended_at IS NULL" if active else "ended_at IS NOT NULL"
        rows = self._read(
            f"SELECT {_ATTACK_COLUMNS} FROM attacks "  # noqa: S608
            f"WHERE {condition} ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, _MAX_LIMIT)),),
        )
        return [_attack_row(r) for r in rows]

    def note_country(self, user_id: str, country_code: str) -> bool:
        """Record a country for a user; True when it is new to a known user."""
        with self._lock:
            db = self._conn()
            db.execute("BEGIN")
            try:
                known = db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(country_code = ?), 0) "
                    "FROM known_countries WHERE user_id = ?",
                    (country_code, user_id),
                ).fetchone()
                db.execute(
                    "INSERT OR IGNORE INTO known_countries (user_id, country_code) "
                    "VALUES (?, ?)",
                    (user_id, country_code),
                )
                db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    db.execute("ROLLBACK")
                raise
        return bool(known[0]) and not known[1]

    def forget_user_countries(self, user_id: str) -> None:
        """Forget every known country of a user."""
        self._write("DELETE FROM known_countries WHERE user_id = ?", (user_id,))

    def clear_countries(self) -> int:
        """Forget every known country and return how many rows were removed."""
        return self._write("DELETE FROM known_countries").rowcount

    def known_countries(self, user_id: str) -> list[str]:
        """Return the known countries of a user, sorted."""
        rows = self._read(
            "SELECT country_code FROM known_countries WHERE user_id = ? "
            "ORDER BY country_code",
            (user_id,),
        )
        return [r[0] for r in rows]

    def last_successes_since(
        self, since: datetime
    ) -> list[tuple[str, str | None, datetime]]:
        """Return (IP, typed username, latest time) of confirmed successes since a time."""
        rows = self._read(
            "SELECT ip, username_typed, MAX(ts) FROM login_attempts "
            "WHERE result = ? AND confirmed = 1 AND ip IS NOT NULL AND ts >= ? "
            "GROUP BY ip, username_typed",
            (str(LoginResult.SUCCESS), _to_text(since)),
        )
        return [(r[0], r[1], _from_text(r[2])) for r in rows]

    def stats(self) -> StoreStats:
        """Return row counts and the database size on disk."""
        with self._lock:
            db = self._conn()
            logins = db.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0]
            changes = db.execute("SELECT COUNT(*) FROM settings_changes").fetchone()[0]
        size = 0
        for suffix in ("", "-wal"):
            with contextlib.suppress(OSError):
                size += Path(f"{self._path}{suffix}").stat().st_size
        return StoreStats(logins=logins, changes=changes, size_bytes=size)
