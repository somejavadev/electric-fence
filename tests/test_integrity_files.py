"""File hashing and comparison for custom integrations."""

import hashlib
import importlib.util
import marshal
import os
from pathlib import Path
import py_compile
import sys
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
    """Caches Python never loads, ignored files and the persistent directory are not hashed."""
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_bytes(b"x")
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
    assert result.bytecode == []


GENUINE = b"VALUE = 'genuine'\n\n\ndef f():\n    return [i for i in range(3)]\n"
EVIL = b"VALUE = 'MALICIOUS'\n\n\ndef f():\n    return [i for i in range(3)]\n"


def _write_pyc(
    source: Path,
    code_src: bytes,
    *,
    flags: int = 0,
    stamp: tuple[int, int] | None = None,
    opt: str = "",
    body: bytes | None = None,
) -> Path:
    """Write `__pycache__` bytecode for `source`, compiled from `code_src`.

    A timestamp pyc (flags 0) gets `stamp` (mtime, size), by default the real
    source's: that is what makes Python load it. A hash pyc gets the hash of
    the real source.
    """
    pyc = Path(
        importlib.util.cache_from_source(
            str(source), optimization=opt.removeprefix(".opt-")
        )
    )
    pyc.parent.mkdir(exist_ok=True)
    if flags == 0:
        st = source.stat()
        mtime, size = stamp or (int(st.st_mtime), st.st_size)
        extra = (mtime & 0xFFFFFFFF).to_bytes(4, "little") + (
            size & 0xFFFFFFFF
        ).to_bytes(4, "little")
    else:
        extra = importlib.util.source_hash(source.read_bytes())
    optimize = int(opt.removeprefix(".opt-")) if opt else 0
    code = compile(code_src, str(source), "exec", dont_inherit=True, optimize=optimize)
    payload = marshal.dumps(code) if body is None else body
    pyc.write_bytes(
        importlib.util.MAGIC_NUMBER + flags.to_bytes(4, "little") + extra + payload
    )
    return pyc


def _rel(pyc: Path, root: Path) -> str:
    return pyc.relative_to(root).as_posix()


def test_forged_pyc_is_tampered(tmp_path: Path) -> None:
    """Bytecode Python would load in place of an untouched source is reported."""
    src = tmp_path / "sub" / "mod.py"
    src.parent.mkdir()
    src.write_bytes(GENUINE)
    pyc = _write_pyc(src, EVIL)

    result = hash_tree(tmp_path, frozenset())

    assert result.files == {"sub/mod.py": _sha(GENUINE)}
    assert result.bytecode == [_rel(pyc, tmp_path)]
    report = compare({"sub/mod.py": _sha(GENUINE)}, result)
    assert report.changed == [_rel(pyc, tmp_path)]
    assert not report.ok


def test_genuine_pyc_passes(tmp_path: Path) -> None:
    """Bytecode compiled from the source, by Python itself, is fine."""
    src = tmp_path / "mod.py"
    src.write_bytes(GENUINE)
    py_compile.compile(str(src), doraise=True)
    py_compile.compile(str(src), doraise=True, optimize=2)

    result = hash_tree(tmp_path, frozenset())

    assert result.bytecode == []
    assert result.files == {"mod.py": _sha(GENUINE)}


def test_genuine_optimised_pyc_passes(tmp_path: Path) -> None:
    """An `.opt-1` file is compared with the source compiled at that level."""
    src = tmp_path / "mod.py"
    src.write_bytes(b"assert False, 'x'\nVALUE = 1\n")
    _write_pyc(src, src.read_bytes(), opt=".opt-1")
    assert hash_tree(tmp_path, frozenset()).bytecode == []


def test_stale_pyc_is_ignored(tmp_path: Path) -> None:
    """Bytecode whose stamp doesn't match the source is never loaded: not reported."""
    src = tmp_path / "mod.py"
    src.write_bytes(GENUINE)
    _write_pyc(src, EVIL, stamp=(1, 1))
    assert hash_tree(tmp_path, frozenset()).bytecode == []


def test_unchecked_hash_pyc_is_tampered(tmp_path: Path) -> None:
    """An unchecked hash pyc is loaded without looking at the source."""
    src = tmp_path / "mod.py"
    src.write_bytes(GENUINE)
    pyc = _write_pyc(src, EVIL, flags=0b01)
    assert hash_tree(tmp_path, frozenset()).bytecode == [_rel(pyc, tmp_path)]


def test_checked_hash_pyc(tmp_path: Path) -> None:
    """A checked hash pyc is loaded only when its hash matches the source."""
    src = tmp_path / "mod.py"
    src.write_bytes(GENUINE)
    pyc = _write_pyc(src, EVIL, flags=0b11)
    assert hash_tree(tmp_path, frozenset()).bytecode == [_rel(pyc, tmp_path)]

    data = bytearray(pyc.read_bytes())
    data[8:16] = b"\x00" * 8  # wrong hash: Python recompiles instead
    pyc.write_bytes(bytes(data))
    assert hash_tree(tmp_path, frozenset()).bytecode == []


def test_unreadable_bytecode_is_tampered(tmp_path: Path) -> None:
    """Bytecode Python would load that can't be read as code is reported."""
    src = tmp_path / "mod.py"
    src.write_bytes(GENUINE)
    pyc = _write_pyc(src, GENUINE, body=b"\xff\xfe not marshal")
    assert hash_tree(tmp_path, frozenset()).bytecode == [_rel(pyc, tmp_path)]


def test_invalid_header_is_ignored(tmp_path: Path) -> None:
    """Python falls back to the source on unknown flags or another magic number."""
    src = tmp_path / "mod.py"
    src.write_bytes(GENUINE)
    pyc = _write_pyc(src, EVIL, flags=0b100)
    assert hash_tree(tmp_path, frozenset()).bytecode == []

    pyc.write_bytes(b"\x00\x00\r\n" + pyc.read_bytes()[4:])
    assert hash_tree(tmp_path, frozenset()).bytecode == []


def test_other_interpreter_and_orphan_pyc_ignored(tmp_path: Path) -> None:
    """Bytecode for another Python, or without a source, is never loaded."""
    src = tmp_path / "mod.py"
    src.write_bytes(GENUINE)
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "mod.cpython-30.pyc").write_bytes(b"x")
    (cache / f"gone.{sys.implementation.cache_tag}.pyc").write_bytes(b"x")
    result = hash_tree(tmp_path, frozenset())
    assert result.bytecode == []
    assert result.files == {"mod.py": _sha(GENUINE)}


def test_bytecode_of_ignored_source_is_skipped(tmp_path: Path) -> None:
    """When the source is ignored, so is its bytecode."""
    src = tmp_path / "mod.py"
    src.write_bytes(GENUINE)
    _write_pyc(src, EVIL)
    assert hash_tree(tmp_path, frozenset({"mod.py"})).bytecode == []


def test_symlinked_bytecode_is_listed(tmp_path: Path) -> None:
    """A symlink inside `__pycache__` is listed like any other symlink."""
    src = tmp_path / "mod.py"
    src.write_bytes(GENUINE)
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / f"mod.{sys.implementation.cache_tag}.pyc").symlink_to("/etc/hostname")
    result = hash_tree(tmp_path, frozenset())
    assert result.symlinks == [f"__pycache__/mod.{sys.implementation.cache_tag}.pyc"]


def test_other_cache_entries_ignored(tmp_path: Path) -> None:
    """Non-bytecode files in `__pycache__` and ignored cache paths are skipped."""
    src = tmp_path / "mod.py"
    src.write_bytes(GENUINE)
    pyc = _write_pyc(src, EVIL)
    (pyc.parent / "notes.txt").write_bytes(b"x")
    result = hash_tree(tmp_path, frozenset({_rel(pyc, tmp_path)}))
    assert result.bytecode == []
    assert result.files == {"mod.py": _sha(GENUINE)}


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
def test_unlistable_cache_is_unreadable(tmp_path: Path) -> None:
    """A `__pycache__` that can't be listed may still be loaded from: reported."""
    src = tmp_path / "mod.py"
    src.write_bytes(GENUINE)
    pyc = _write_pyc(src, EVIL)
    pyc.parent.chmod(0o311)  # enterable, not listable: Python still imports it
    try:
        result = hash_tree(tmp_path, frozenset())
    finally:
        pyc.parent.chmod(0o755)
    assert result.unreadable == ["__pycache__"]
    assert not compare({"mod.py": _sha(GENUINE)}, result).ok


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
def test_unreadable_pyc_is_not_bytecode(tmp_path: Path) -> None:
    """Bytecode that can't be read can't be loaded by Python either."""
    src = tmp_path / "mod.py"
    src.write_bytes(GENUINE)
    pyc = _write_pyc(src, EVIL)
    pyc.chmod(0)
    try:
        assert hash_tree(tmp_path, frozenset()).bytecode == []
    finally:
        pyc.chmod(0o600)


def test_sourceless_pyc_is_hashed(tmp_path: Path) -> None:
    """A `.pyc` outside `__pycache__` can be imported on its own: it is a file."""
    (tmp_path / "top.pyc").write_bytes(b"x")
    result = hash_tree(tmp_path, frozenset())
    assert result.files == {"top.pyc": _sha(b"x")}
    assert compare({}, result).added == ["top.pyc"]


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
