"""File hashing and comparison for custom integrations."""

import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from custom_components.electric_fence.integrity.files import (
    TreeHashes,
    compare,
    hash_tree,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_hash_tree_skips(tmp_path: Path) -> None:
    """Caches, ignored files and the persistent directory are not hashed."""
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_bytes(b"x")
    (tmp_path / "top.pyc").write_bytes(b"x")
    (tmp_path / "data.json").write_text("{}")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_bytes(b"print(1)")
    (tmp_path / "sub" / "__pycache__").mkdir()
    (tmp_path / "sub" / "__pycache__" / "a.cpython.pyc").write_bytes(b"x")
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / "state.db").write_bytes(b"s")
    (tmp_path / "b.txt").write_bytes(b"b")

    result = hash_tree(tmp_path, frozenset({"data.json", "keep/"}))

    assert result.files == {"sub/a.py": _sha(b"print(1)"), "b.txt": _sha(b"b")}
    assert result.symlinks == []
    assert result.unreadable == []


def test_hash_tree_streams_large_file(tmp_path: Path) -> None:
    """A file bigger than one chunk hashes correctly."""
    data = os.urandom(200_000)
    (tmp_path / "big.bin").write_bytes(data)
    assert hash_tree(tmp_path, frozenset()).files == {"big.bin": _sha(data)}


def test_hash_tree_symlink_and_unreadable(tmp_path: Path) -> None:
    """Symlinks are listed, never followed; unreadable files are listed."""
    (tmp_path / "link").symlink_to("/etc/hostname")
    (tmp_path / "dirlink").symlink_to("/etc")
    (tmp_path / "ok.py").write_bytes(b"ok")
    real_open = open
    opened: list[str] = []

    def spy(file: object, *args: object, **kwargs: object) -> object:
        opened.append(str(file))
        return real_open(file, *args, **kwargs)  # type: ignore[call-overload]

    with patch("builtins.open", spy):
        result = hash_tree(tmp_path, frozenset())

    assert result.symlinks == ["dirlink", "link"]
    assert result.files == {"ok.py": _sha(b"ok")}
    assert not any("hostname" in p or p.endswith("/link") for p in opened)
    assert not any(p.startswith("/etc") for p in opened)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
def test_hash_tree_unreadable(tmp_path: Path) -> None:
    """A file that can't be opened is `unreadable`, not hashed."""
    locked = tmp_path / "locked.py"
    locked.write_bytes(b"x")
    locked.chmod(0)
    try:
        result = hash_tree(tmp_path, frozenset())
    finally:
        locked.chmod(0o600)
    assert result.unreadable == ["locked.py"]
    assert result.files == {}


def test_compare_report() -> None:
    """Changed, missing, added, unreadable and symlinks are reported sorted."""
    expected = {"a": "1", "b": "2", "c": "3", "d": "4"}
    actual = TreeHashes(
        files={"a": "1", "b": "X", "z": "9", "y": "8"},
        symlinks=["s"],
        unreadable=["d"],
    )
    report = compare(expected, actual)
    assert report.changed == ["b"]
    assert report.missing == ["c"]
    assert report.added == ["s", "y", "z"]
    assert report.symlinks == ["s"]
    assert report.unreadable == ["d"]
    assert not report.ok


def test_compare_ok() -> None:
    """Identical trees are ok."""
    assert compare({"a": "1"}, TreeHashes({"a": "1"}, [], [])).ok


def test_hash_tree_root_symlink(tmp_path: Path) -> None:
    """A symlinked root is reported, and nothing behind it is read."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.py").write_bytes(b"x")
    link = tmp_path / "link"
    link.symlink_to(outside)
    result = hash_tree(link, frozenset())
    assert result == TreeHashes({}, ["."], [])
    assert not compare({"f.py": "1"}, result).ok


def test_hash_tree_ignored_symlink_omitted(tmp_path: Path) -> None:
    """An ignored symlink is not listed."""
    (tmp_path / "link").symlink_to("/etc/hostname")
    assert hash_tree(tmp_path, frozenset({"link"})).symlinks == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory")
def test_hash_tree_unreadable_subdir(tmp_path: Path) -> None:
    """An unreadable directory is listed as unreadable."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.py").write_bytes(b"x")
    sub.chmod(0)
    try:
        result = hash_tree(tmp_path, frozenset())
    finally:
        sub.chmod(0o700)
    assert result.unreadable == ["sub"]
