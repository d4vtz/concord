import filecmp
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from concord import application as concord
from concord.application.config import (CONCORD_TARGET, ConfigManager,
                                        TargetConfig)
from concord.application.database import Database
from concord.application.repository import RepositoryManager
from concord.application.target import Target, TargetType


@dataclass(frozen=True)
class TargetStatus:
    name: str
    state: str


@dataclass(frozen=True)
class DiffEntry:
    state: str
    relative_path: Path


@dataclass(frozen=True)
class TargetDiff:
    name: str
    entries: list[DiffEntry]

    @property
    def clean(self) -> bool:
        return not self.entries


class TargetManager:
    RESERVED_NAMES = {"ignore", "manifest", "config"}

    def __init__(
        self,
        database: Database | None = None,
        repository: RepositoryManager | None = None,
        config_manager: ConfigManager | None = None,
    ) -> None:
        self.database = database or Database()
        self.repository = repository or RepositoryManager()
        self.config_manager = config_manager or ConfigManager()
        self.database.initialize()
        if concord.config_file.exists():
            self._migrate_legacy_installation()

    def _destination(self, target: Target) -> Path:
        return self.repository.target_path(target.name) / target.local_path.relative_to(Path.home())

    def _write_target(self, target: Target) -> None:
        if not target.local_path.exists():
            raise FileNotFoundError(f"No existe la ruta local de '{target.name}': {target.local_path}")
        root = self.repository.target_path(target.name)
        temporary = root.with_name(f".{root.name}.tmp")
        if temporary.exists():
            shutil.rmtree(temporary)
        destination = temporary / target.local_path.relative_to(Path.home())
        destination.parent.mkdir(parents=True, exist_ok=True)
        if target.type is TargetType.DIRECTORY:
            shutil.copytree(target.local_path, destination, symlinks=True)
        else:
            shutil.copy2(target.local_path, destination, follow_symlinks=False)
        if root.exists():
            shutil.rmtree(root)
        temporary.rename(root)

    def _save_row(self, target: Target) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO targets (id, name, local_path, type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    target.id,
                    target.name,
                    str(target.local_path),
                    target.type.value,
                    target.created_at.isoformat(),
                    target.updated_at.isoformat(),
                ),
            )

    def _update_timestamp(self, target: Target) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE targets SET updated_at = ? WHERE id = ?",
                (target.updated_at.isoformat(), target.id),
            )

    def _manifest_target(self, target: Target) -> TargetConfig:
        return TargetConfig(
            name=target.name,
            relative_path=target.local_path.relative_to(Path.home()),
            type=target.type.value,
            created_at=target.created_at,
            updated_at=target.updated_at,
        )

    def _persist_manifest(self) -> None:
        config = self.config_manager.load()
        try:
            concord_target = self.get(CONCORD_TARGET)
        except KeyError:
            concord_target = None
        if concord_target is not None:
            concord_target.touch()
            self._update_timestamp(concord_target)
        config.targets = [self._manifest_target(target) for target in self.list()]
        config.targets.sort(key=lambda item: (item.name != CONCORD_TARGET, item.name))
        self.config_manager.save(config)
        if concord_target is not None:
            self._write_target(concord_target)

    def _migrate_legacy_installation(self) -> None:
        config = self.config_manager.load()
        with self.database.connect() as connection:
            rows = connection.execute("SELECT name FROM targets").fetchall()
        names = {row[0] for row in rows}
        if CONCORD_TARGET not in names:
            self._save_row(Target(concord.config_dir, CONCORD_TARGET))
            names.add(CONCORD_TARGET)
        if not config.targets or any(
            target.created_at is None or target.updated_at is None
            for target in config.targets
        ):
            self._persist_manifest()
        elif len(names) == 1 and len(config.targets) > 1:
            self.import_manifest(replace=True)

    def get(self, name: str) -> Target:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id, name, local_path, type, created_at, updated_at FROM targets WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            raise KeyError(f"No existe un target llamado '{name}'.")
        return Target(
            Path(row[2]),
            row[1],
            target_id=row[0],
            target_type=TargetType(row[3]),
            created_at=datetime.fromisoformat(row[4]),
            updated_at=datetime.fromisoformat(row[5]),
        )

    def list(self) -> list[Target]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, name, local_path, type, created_at, updated_at FROM targets ORDER BY CASE WHEN name = 'concord' THEN 0 ELSE 1 END, name"
            ).fetchall()
        return [
            Target(
                Path(row[2]),
                row[1],
                target_id=row[0],
                target_type=TargetType(row[3]),
                created_at=datetime.fromisoformat(row[4]),
                updated_at=datetime.fromisoformat(row[5]),
            )
            for row in rows
        ]

    def add(self, local_path: Path, name: str | None = None) -> Target:
        target = Target(local_path, name)
        if target.name in self.RESERVED_NAMES:
            raise ValueError(
                f"'{target.name}' es un nombre reservado por Concord. Use otro nombre."
            )
        with self.database.connect() as connection:
            duplicate = connection.execute(
                "SELECT name FROM targets WHERE name = ? OR local_path = ? LIMIT 1",
                (target.name, str(target.local_path)),
            ).fetchone()
        if duplicate:
            raise ValueError(f"El target ya está registrado como '{duplicate[0]}'.")
        self._write_target(target)
        try:
            self._save_row(target)
            self._persist_manifest()
        except Exception:
            with self.database.connect() as connection:
                connection.execute("DELETE FROM targets WHERE id = ?", (target.id,))
            self.repository.remove(target.name)
            raise
        return target

    def sync(self, name: str | None = None) -> list[Target]:
        targets = [self.get(name)] if name else self.list()
        changed_targets = [target for target in targets if not self._target_diff(target).clean]
        if not changed_targets:
            return []
        for target in changed_targets:
            self._write_target(target)
            target.touch()
            self._update_timestamp(target)
        self._persist_manifest()
        return changed_targets

    def restore(self, name: str, *, force: bool = False) -> Target:
        target = self.get(name)
        source = self._destination(target)
        if not source.exists() and not source.is_symlink():
            raise FileNotFoundError(f"No existe la copia del target '{name}'.")
        if target.local_path.exists() and not force:
            raise FileExistsError(f"'{target.local_path}' ya existe; use --force para reemplazarlo.")
        if target.local_path.exists() or target.local_path.is_symlink():
            if target.local_path.is_dir() and not target.local_path.is_symlink():
                shutil.rmtree(target.local_path)
            else:
                target.local_path.unlink()
        target.local_path.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target.local_path, symlinks=True)
        else:
            shutil.copy2(source, target.local_path, follow_symlinks=False)
        return target

    def restore_all(self, *, force: bool = False) -> list[Target]:
        targets = [target for target in self.list() if target.name != CONCORD_TARGET]
        return [self.restore(target.name, force=force) for target in targets]

    def remove(self, name: str, *, keep_repository: bool = False) -> None:
        if name == CONCORD_TARGET:
            raise ValueError("El target 'concord' es reservado y no puede eliminarse.")
        target = self.get(name)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM files WHERE target_id = ?", (target.id,))
            connection.execute("DELETE FROM targets WHERE id = ?", (target.id,))
        if not keep_repository:
            self.repository.remove(name)
        self._persist_manifest()

    def import_manifest(self, *, replace: bool = False) -> list[Target]:
        config = self.config_manager.load()
        if not config.targets:
            raise ValueError("El manifiesto no contiene targets para importar.")
        targets: list[Target] = []
        for item in config.targets:
            if item.relative_path.is_absolute() or ".." in item.relative_path.parts:
                raise ValueError(f"Ruta no segura en el manifiesto: {item.relative_path}")
            targets.append(
                Target(
                    Path.home() / item.relative_path,
                    item.name,
                    target_type=TargetType(item.type),
                    created_at=item.created_at or datetime.now(UTC),
                    updated_at=item.updated_at or item.created_at or datetime.now(UTC),
                )
            )
        with self.database.connect() as connection:
            existing = connection.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
            if existing and not replace:
                raise ValueError("La base de datos ya contiene targets; use --replace para reconstruirla.")
            connection.execute("DELETE FROM files")
            connection.execute("DELETE FROM targets")
            connection.executemany(
                "INSERT INTO targets (id, name, local_path, type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        target.id,
                        target.name,
                        str(target.local_path),
                        target.type.value,
                        target.created_at.isoformat(),
                        target.updated_at.isoformat(),
                    )
                    for target in targets
                ],
            )
        return targets

    def status(self) -> list[TargetStatus]:
        result = []
        for target in self.list():
            copy = self._destination(target)
            if not target.local_path.exists():
                state = "missing"
            elif not copy.exists():
                state = "untracked"
            elif target.type is TargetType.FILE:
                state = "clean" if filecmp.cmp(target.local_path, copy, shallow=False) else "modified"
            else:
                state = "clean" if self._directories_equal(filecmp.dircmp(target.local_path, copy)) else "modified"
            result.append(TargetStatus(target.name, state))
        return result

    def diff(self, name: str | None = None) -> list[TargetDiff]:
        targets = [self.get(name)] if name else self.list()
        return [self._target_diff(target) for target in targets]

    def preview_sync(self, name: str | None = None) -> list[TargetDiff]:
        return self.diff(name)

    def preview_restore(self, name: str | None = None) -> list[TargetDiff]:
        targets = (
            [self.get(name)]
            if name
            else [target for target in self.list() if target.name != CONCORD_TARGET]
        )
        return [self._target_diff(target, reverse=True) for target in targets]

    def _target_diff(self, target: Target, *, reverse: bool = False) -> TargetDiff:
        local = self._snapshot(target.local_path)
        stored = self._snapshot(self._destination(target))
        entries: list[DiffEntry] = []
        display_root = target.local_path.relative_to(Path.home())
        for relative_path in sorted(local.keys() | stored.keys(), key=str):
            display_path = display_root if relative_path == Path(".") else display_root / relative_path
            if relative_path not in stored:
                state = "added"
            elif relative_path not in local:
                state = "deleted"
            elif not self._paths_equal(local[relative_path], stored[relative_path]):
                state = "modified"
            else:
                continue
            if reverse:
                state = {"added": "deleted", "deleted": "added"}.get(state, state)
            entries.append(DiffEntry(state=state, relative_path=display_path))
        return TargetDiff(name=target.name, entries=entries)

    def _snapshot(self, root: Path) -> dict[Path, Path]:
        if not os.path.lexists(root):
            return {}
        if root.is_symlink() or root.is_file():
            return {Path("."): root}
        entries: dict[Path, Path] = {}
        for path in root.rglob("*"):
            if path.is_symlink() or path.is_file():
                entries[path.relative_to(root)] = path
            elif path.is_dir() and not any(path.iterdir()):
                entries[path.relative_to(root)] = path
        if not entries:
            entries[Path(".")] = root
        return entries

    def _paths_equal(self, left: Path, right: Path) -> bool:
        if left.is_symlink() or right.is_symlink():
            return left.is_symlink() and right.is_symlink() and left.readlink() == right.readlink()
        if left.is_dir() or right.is_dir():
            return left.is_dir() and right.is_dir()
        return left.is_file() and right.is_file() and filecmp.cmp(left, right, shallow=False)

    def _directories_equal(self, comparison: filecmp.dircmp) -> bool:
        if comparison.left_only or comparison.right_only or comparison.diff_files or comparison.funny_files:
            return False
        return all(self._directories_equal(child) for child in comparison.subdirs.values())
