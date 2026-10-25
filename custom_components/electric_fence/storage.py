"""SQLite storage for login attempts and settings changes.

Synchronous: call it from the executor only.
"""

from collections.abc import Sequence
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

SCHEMA_VERSION = 1

# A `LoginFilter.result` for failed and blocked attempts together.
FAILED_OR_BLOCKED: Final = "failed_or_blocked"
_MAX_LIMIT = 500

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
    count INTEGER NOT NULL DEFAULT 1
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

_LOGIN_COLUMNS = (
    "id, ts, result, reason, username_typed, user_exists, user_id, provider, ip, "
    "country_code, hostname, source, client_id, client_name, user_agent, url, "
    "mfa_module, confirmed, count"
)
_CHANGE_COLUMNS = (
    "id, ts, kind, action, target_id, target_name, details_json, "
    "actor_user_id, actor_name, actor_ip"
)


class StoreSchemaTooNewError(Exception):
    """The database was written by a newer Electric Fence."""

    def __init__(self, version: int) -> None:
        """Initialise with the database's schema version."""
        super().__init__(f"database schema {version} is newer than {SCHEMA_VERSION}")
        self.version = version


def _check_version(db: sqlite3.Connection) -> None:
    """Raise StoreSchemaTooNewError for a schema newer than SCHEMA_VERSION."""
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise StoreSchemaTooNewError(version)


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


@dataclass(frozen=True)
class ChangeFilter:
    """Filter for settings change queries."""

    kind: ChangeKind | None = None
    actor_user_id: str | None = None
    include_system: bool = False
    since: datetime | None = None
    until: datetime | None = None


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
    )


def _change_values(c: SettingsChange) -> tuple[Any, ...]:
    return (
        _to_text(c.ts),
        str(c.kind),
        str(c.action),
        c.target_id,
        c.target_name,
        json.dumps(c.details, sort_keys=True),
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
                _check_version(db)
                db.execute("PRAGMA journal_mode = WAL")
                db.executescript(_SCHEMA)
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
                    "confirmed, count) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        _time_clauses(where, params, f.since, f.until)
        if before_id is not None:
            where.append("(ts, id) < (SELECT ts, id FROM login_attempts WHERE id = ?)")
            params.append(before_id)
        sql = f"SELECT {_LOGIN_COLUMNS} FROM login_attempts"  # noqa: S608
        if where:
            sql += " WHERE " + " AND ".join(where)
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
        if not f.include_system:
            where.append("actor_user_id IS NOT NULL")
        _time_clauses(where, params, f.since, f.until)
        if before_id is not None:
            where.append(
                "(ts, id) < (SELECT ts, id FROM settings_changes WHERE id = ?)"
            )
            params.append(before_id)
        sql = f"SELECT {_CHANGE_COLUMNS} FROM settings_changes"  # noqa: S608
        if where:
            sql += " WHERE " + " AND ".join(where)
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
                    updates.append((json.dumps(details, sort_keys=True), row_id))
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
            for table in ("login_attempts", "settings_changes"):
                total += db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            deleted = 0
            db.execute("BEGIN")
            try:
                for table in ("login_attempts", "settings_changes"):
                    deleted += db.execute(
                        f"DELETE FROM {table} WHERE ts < ?",  # noqa: S608
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
