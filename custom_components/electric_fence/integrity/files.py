"""Hash the files of an integration folder, without following symlinks."""

import _imp
from dataclasses import dataclass, field
import hashlib
import importlib.util
import marshal
import os
from pathlib import Path
import sys
from types import CodeType

_CHUNK = 64 * 1024
_PYC_HEADER = 16
# The `.opt-N` levels CPython writes; Home Assistant may run with -O or -OO.
_OPT_LEVELS = {"": 0, "1": 1, "2": 2}


@dataclass
class TreeHashes:
    """SHA-256 of each regular file, plus what could not be hashed.

    `bytecode` lists `__pycache__` files that Python would load in place of
    their source but that were not compiled from it.
    """

    files: dict[str, str]
    symlinks: list[str]
    unreadable: list[str]
    bytecode: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FileReport:
    """Differences between an official copy and the installed files."""

    changed: list[str]
    missing: list[str]
    added: list[str]
    unreadable: list[str]
    symlinks: list[str]

    @property
    def ok(self) -> bool:
        """Return True when nothing differs."""
        return not (
            self.changed
            or self.missing
            or self.added
            or self.unreadable
            or self.symlinks
        )


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:  # noqa: PTH123
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def is_cache(name: str) -> bool:
    """Return True for a bytecode cache folder name.

    A `.pyc` outside `__pycache__` is not a cache: Python imports it on its
    own when there is no source, so it is hashed like any file.
    """
    return name == "__pycache__"


def skipped(rel: str, prefixes: tuple[str, ...], ignore: frozenset[str]) -> bool:
    """Return True when a relative path is ignored."""
    return rel in ignore or rel.startswith(prefixes)


def _pyc_source(name: str) -> tuple[str, int] | None:
    """Return (source name, optimisation level) of a cache file, or None.

    None when this interpreter would never load it. The name is
    `<module>.<cache tag>[.opt-N].pyc`.
    """
    if not name.endswith(".pyc"):
        return None
    parts = name.removesuffix(".pyc").split(".")
    opt = ""
    if len(parts) > 2 and parts[-1].startswith("opt-"):
        opt = parts.pop().removeprefix("opt-")
    if len(parts) < 2 or opt not in _OPT_LEVELS:
        return None
    tag = parts.pop()
    if tag != sys.implementation.cache_tag:
        return None
    return ".".join(parts) + ".py", _OPT_LEVELS[opt]


def _would_load(data: bytes, source: bytes, stat: os.stat_result) -> bool:
    """Return whether CPython would run this bytecode instead of compiling the source.

    Mirrors `SourceLoader.get_code`: on a header it rejects, Python falls back
    to the source, so such a file is harmless.
    """
    if len(data) < _PYC_HEADER or data[:4] != importlib.util.MAGIC_NUMBER:
        return False
    flags = int.from_bytes(data[4:8], "little")
    if flags & ~0b11:
        return False
    if not flags & 0b1:
        return data[8:12] == (int(stat.st_mtime) & 0xFFFFFFFF).to_bytes(
            4, "little"
        ) and data[12:16] == (stat.st_size & 0xFFFFFFFF).to_bytes(4, "little")
    mode = getattr(_imp, "check_hash_based_pycs", "default")
    if mode == "never" or not (flags & 0b10 or mode == "always"):
        return True  # loaded without looking at the source
    return data[8:16] == importlib.util.source_hash(source)


def _bytecode_matches(pyc: str, source: str, optimize: int) -> bool:
    """Return True unless Python would load `pyc` and it isn't `source` compiled.

    Code objects compare their bytecode, constants, names and nested code,
    not their file name.
    """
    try:
        with open(source, "rb") as handle:  # noqa: PTH123
            source_bytes = handle.read()
        stat = os.stat(source)  # noqa: PTH116
        with open(pyc, "rb") as handle:  # noqa: PTH123
            data = handle.read()
    except OSError:
        return True  # the source is reported unreadable; Python can't load either
    if not _would_load(data, source_bytes, stat):
        return True
    try:
        loaded = marshal.loads(data[_PYC_HEADER:])
        expected = compile(
            source_bytes, source, "exec", dont_inherit=True, optimize=optimize
        )
    except ValueError, EOFError, TypeError, SyntaxError:
        return False
    return isinstance(loaded, CodeType) and loaded == expected


def hash_tree(root: Path, ignore: frozenset[str]) -> TreeHashes:
    """Hash every file under `root` (blocking; run in the executor).

    `ignore` holds relative POSIX paths, and directory prefixes ending in "/".
    """
    if os.path.islink(root):  # noqa: PTH114
        return TreeHashes({}, ["."], [])
    result = TreeHashes({}, [], [])
    prefixes = tuple(item for item in ignore if item.endswith("/"))
    base = str(root)

    def scan(directory: str) -> list[os.DirEntry[str]] | None:
        try:
            with os.scandir(directory) as it:
                return sorted(it, key=lambda e: e.name)
        except OSError:
            return None

    def check_cache(directory: str, rel: str, source_dir: str, source_rel: str) -> None:
        """Report bytecode in `__pycache__` that doesn't match its source."""
        if (entries := scan(directory)) is None:
            result.unreadable.append(rel.rstrip("/"))
            return
        for entry in entries:
            rel_path = f"{rel}{entry.name}"
            if skipped(rel_path, prefixes, ignore):
                continue
            if entry.is_symlink():
                result.symlinks.append(rel_path)
                continue
            found = _pyc_source(entry.name)
            if found is None or not entry.is_file(follow_symlinks=False):
                continue
            name, optimize = found
            source = os.path.join(source_dir, name)  # noqa: PTH118
            if skipped(f"{source_rel}{name}", prefixes, ignore) or not os.path.isfile(  # noqa: PTH113
                source
            ):
                continue
            if not _bytecode_matches(entry.path, source, optimize):
                result.bytecode.append(rel_path)

    def walk(directory: str, rel: str) -> None:
        if (entries := scan(directory)) is None:
            result.unreadable.append(rel.rstrip("/") or ".")
            return
        for entry in entries:
            rel_path = f"{rel}{entry.name}"
            if entry.is_symlink():
                if not skipped(rel_path, prefixes, ignore):
                    result.symlinks.append(rel_path)
            elif entry.is_dir(follow_symlinks=False):
                if skipped(rel_path + "/", prefixes, ignore):
                    continue
                if is_cache(entry.name):
                    check_cache(entry.path, rel_path + "/", directory, rel)
                else:
                    walk(entry.path, rel_path + "/")
            elif entry.is_file(follow_symlinks=False):
                if skipped(rel_path, prefixes, ignore):
                    continue
                try:
                    result.files[rel_path] = _hash_file(entry.path)
                except OSError:
                    result.unreadable.append(rel_path)

    walk(base, "")
    result.symlinks.sort()
    result.unreadable.sort()
    result.bytecode.sort()
    return result


def compare(expected: dict[str, str], actual: TreeHashes) -> FileReport:
    """Compare official hashes with the installed tree."""
    unreadable = set(actual.unreadable)
    changed = [
        name
        for name, digest in actual.files.items()
        if name in expected and expected[name] != digest
    ]
    changed += actual.bytecode
    missing = [
        name for name in expected if name not in actual.files and name not in unreadable
    ]
    added = [name for name in actual.files if name not in expected]
    added += actual.symlinks
    return FileReport(
        changed=sorted(changed),
        missing=sorted(missing),
        added=sorted(added),
        unreadable=sorted(unreadable),
        symlinks=sorted(actual.symlinks),
    )
