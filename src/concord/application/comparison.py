import filecmp
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SnapshotEntry:
    path: Path
    kind: str
    size: int = 0
    mtime_ns: int = 0
    link_target: str | None = None


def paths_equal(left: Path, right: Path) -> bool:
    if left.is_symlink() or right.is_symlink():
        return left.is_symlink() and right.is_symlink() and left.readlink() == right.readlink()
    if left.is_file() or right.is_file():
        return left.is_file() and right.is_file() and _files_equal(left, right)
    if not left.is_dir() or not right.is_dir():
        return False
    left_entries = directory_snapshot(left)
    right_entries = directory_snapshot(right)
    if left_entries.keys() != right_entries.keys():
        return False
    return all(
        _entries_equal(left_entries[relative], right_entries[relative])
        for relative in left_entries
    )


def directory_snapshot(root: Path) -> dict[Path, SnapshotEntry]:
    entries: dict[Path, SnapshotEntry] = {}
    pending = [(root, Path())]
    while pending:
        directory, parent = pending.pop()
        with os.scandir(directory) as iterator:
            for item in iterator:
                relative = parent / item.name
                path = Path(item.path)
                if item.is_symlink():
                    entries[relative] = SnapshotEntry(
                        path,
                        "symlink",
                        link_target=os.readlink(item.path),
                    )
                elif item.is_dir(follow_symlinks=False):
                    entries[relative] = SnapshotEntry(path, "directory")
                    pending.append((path, relative))
                elif item.is_file(follow_symlinks=False):
                    stat = item.stat(follow_symlinks=False)
                    entries[relative] = SnapshotEntry(
                        path,
                        "file",
                        size=stat.st_size,
                        mtime_ns=stat.st_mtime_ns,
                    )
                else:
                    entries[relative] = SnapshotEntry(path, "other")
    return entries


def _entries_equal(left: SnapshotEntry, right: SnapshotEntry) -> bool:
    if left.kind != right.kind:
        return False
    if left.kind == "symlink":
        return left.link_target == right.link_target
    if left.kind == "directory":
        return True
    if left.kind != "file":
        return False
    if left.size == right.size and left.mtime_ns == right.mtime_ns:
        return True
    if left.size != right.size:
        return False
    return filecmp.cmp(left.path, right.path, shallow=False)


def _files_equal(left: Path, right: Path) -> bool:
    left_stat = left.stat()
    right_stat = right.stat()
    if left_stat.st_size == right_stat.st_size and left_stat.st_mtime_ns == right_stat.st_mtime_ns:
        return True
    if left_stat.st_size != right_stat.st_size:
        return False
    return filecmp.cmp(left, right, shallow=False)
