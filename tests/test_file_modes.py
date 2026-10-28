"""Files with logins and addresses are readable by their owner only."""

from collections.abc import Iterator
import os
from pathlib import Path
import stat

import pytest

from custom_components.electric_fence.geo.base import _make_temp as make_temp
from custom_components.electric_fence.storage import FenceStore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


@pytest.fixture(autouse=True)
def loose_umask() -> Iterator[None]:
    """Run with the common 022 umask, which would leave files world-readable."""
    old = os.umask(0o022)
    yield
    os.umask(old)


def mode(path: Path) -> int:
    """Return the permission bits."""
    return stat.S_IMODE(path.stat().st_mode)


def test_new_database_is_private(tmp_path: Path) -> None:
    """A new database and its WAL files are 0600."""
    path = tmp_path / "electric_fence.db"
    store = FenceStore(path)
    store.open()
    try:
        store.save_app_repositories({"u"})
        assert mode(path) == 0o600
        for suffix in ("-wal", "-shm"):
            side = Path(f"{path}{suffix}")
            assert side.exists()
            assert mode(side) == 0o600
    finally:
        store.close()


def test_existing_database_is_tightened(tmp_path: Path) -> None:
    """A database left world-readable by an older version is fixed on open."""
    path = tmp_path / "electric_fence.db"
    store = FenceStore(path)
    store.open()
    store.close()
    wal = Path(f"{path}-wal")
    wal.touch()
    path.chmod(0o644)
    wal.chmod(0o644)
    store = FenceStore(path)
    store.open()
    try:
        assert mode(path) == 0o600
        assert mode(wal) == 0o600
    finally:
        store.close()


def test_mode_change_failure_is_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file system that refuses chmod doesn't stop the database opening."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError

    monkeypatch.setattr(Path, "chmod", refuse)
    store = FenceStore(tmp_path / "electric_fence.db")
    store.open()
    store.close()


def test_geoip_directory_is_private(tmp_path: Path) -> None:
    """The GeoIP directory is 0700, also when it already existed, and files 0600."""
    directory = tmp_path / "geoip"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    tmp = make_temp(directory, ".x-")
    assert mode(directory) == 0o700
    assert mode(tmp) == 0o600
