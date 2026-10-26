"""Hash the files of an integration folder, without following symlinks."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path

_CHUNK = 64 * 1024


@dataclass
class TreeHashes:
    """SHA-256 of each regular file, plus what could not be hashed."""

    files: dict[str, str]
    symlinks: list[str]
    unreadable: list[str]


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


def _skipped(rel: str, prefixes: tuple[str, ...], ignore: frozenset[str]) -> bool:
    return rel in ignore or rel.startswith(prefixes)


def hash_tree(root: Path, ignore: frozenset[str]) -> TreeHashes:
    """Hash every file under `root` (blocking; run in the executor).

    `ignore` holds relative POSIX paths, and directory prefixes ending in "/".
    """
    if os.path.islink(root):  # noqa: PTH114
        return TreeHashes({}, ["."], [])
    result = TreeHashes({}, [], [])
    prefixes = tuple(item for item in ignore if item.endswith("/"))
    base = str(root)

    def walk(directory: str, rel: str) -> None:
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            result.unreadable.append(rel.rstrip("/") or ".")
            return
        for entry in entries:
            rel_path = f"{rel}{entry.name}"
            if entry.is_symlink():
                if not _skipped(rel_path, prefixes, ignore):
                    result.symlinks.append(rel_path)
            elif entry.is_dir(follow_symlinks=False):
                if entry.name != "__pycache__" and not _skipped(
                    rel_path + "/", prefixes, ignore
                ):
                    walk(entry.path, rel_path + "/")
            elif entry.is_file(follow_symlinks=False):
                if entry.name.endswith(".pyc") or _skipped(rel_path, prefixes, ignore):
                    continue
                try:
                    result.files[rel_path] = _hash_file(entry.path)
                except OSError:
                    result.unreadable.append(rel_path)

    walk(base, "")
    result.symlinks.sort()
    result.unreadable.sort()
    return result


def compare(expected: dict[str, str], actual: TreeHashes) -> FileReport:
    """Compare official hashes with the installed tree."""
    unreadable = set(actual.unreadable)
    changed = [
        name
        for name, digest in actual.files.items()
        if name in expected and expected[name] != digest
    ]
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
