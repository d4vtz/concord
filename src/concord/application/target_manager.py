import filecmp
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from concord import application as concord
from concord.application.config import (CONCORD_TARGET, ConfigManager,
                                        TargetConfig, TargetPathConfig)
from concord.application.database import Database
from concord.application.repository import RepositoryManager
from concord.application.target import Target, TargetPath, TargetType


@dataclass(frozen=True)
class TargetStatus:
    name: str
    state: str
    changed_paths: int = 0
    total_paths: int = 1


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
        self.manifest_backup: Path | None = None
        if concord.config_file.exists():
            config, self.manifest_backup = self.config_manager.migrate()
            self._repair_index(config)
            if self.manifest_backup:
                self._persist_manifest()

    def _destination(self, target: Target, path: TargetPath) -> Path:
        return self.repository.target_path(target.name) / path.relative_path

    def _copy(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination, follow_symlinks=False)

    def _stage_target(self, target: Target) -> Path:
        missing = [path.local_path for path in target.paths if not os.path.lexists(path.local_path)]
        if missing:
            raise FileNotFoundError(
                f"No existe una ruta local de '{target.name}': {missing[0]}"
            )
        root = self.repository.target_path(target.name)
        temporary = root.with_name(f".{root.name}.tmp")
        if temporary.exists():
            shutil.rmtree(temporary)
        for path in target.paths:
            self._copy(path.local_path, temporary / path.relative_path)
        return temporary

    def _stage_existing_copy(self, target: Target) -> Path:
        root = self.repository.target_path(target.name)
        if not root.is_dir():
            raise FileNotFoundError(
                f"No existe la copia del target '{target.name}'; ejecute concord sync {target.name}."
            )
        temporary = root.with_name(f".{root.name}.tmp")
        if temporary.exists():
            shutil.rmtree(temporary)
        shutil.copytree(root, temporary, symlinks=True)
        return temporary

    def _install_staged_target(self, target: Target, temporary: Path) -> None:
        self._install_staged_targets([(target, temporary)])

    def _install_staged_targets(self, staged: list[tuple[Target, Path]]) -> None:
        installed: list[tuple[Path, Path]] = []
        try:
            for target, temporary in staged:
                root = self.repository.target_path(target.name)
                backup = root.with_name(f".{root.name}.bak")
                if backup.exists():
                    shutil.rmtree(backup)
                if root.exists():
                    root.rename(backup)
                try:
                    temporary.rename(root)
                except Exception:
                    if backup.exists():
                        backup.rename(root)
                    raise
                installed.append((root, backup))
        except Exception:
            for root, backup in reversed(installed):
                if root.exists():
                    shutil.rmtree(root)
                if backup.exists():
                    backup.rename(root)
            for _, temporary in staged:
                if temporary.exists():
                    shutil.rmtree(temporary)
            raise
        for _, backup in installed:
            if backup.exists():
                shutil.rmtree(backup)

    def _write_target(self, target: Target) -> None:
        self._install_staged_target(target, self._stage_target(target))

    def _insert_target(self, connection, target: Target) -> None:
        connection.execute(
            "INSERT INTO targets (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (target.id, target.name, target.created_at.isoformat(), target.updated_at.isoformat()),
        )
        connection.executemany(
            "INSERT INTO target_paths (id, target_id, local_path, type, position) VALUES (?, ?, ?, ?, ?)",
            [
                (path.id, target.id, str(path.local_path), path.type.value, position)
                for position, path in enumerate(target.paths)
            ],
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
            paths=[TargetPathConfig(path.relative_path, path.type.value) for path in target.paths],
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

    def _repair_index(self, config) -> None:
        with self.database.connect() as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM targets")}
        if CONCORD_TARGET not in names:
            target = Target(concord.config_dir, CONCORD_TARGET)
            with self.database.connect() as connection:
                self._insert_target(connection, target)
            names.add(CONCORD_TARGET)
        if len(names) == 1 and len(config.targets) > 1:
            self.import_manifest(replace=True)
        elif not config.targets or any(
            target.created_at is None or target.updated_at is None for target in config.targets
        ):
            self._persist_manifest()

    def _target_from_row(self, row, paths) -> Target:
        return Target(
            name=row[1],
            paths=[
                TargetPath(Path(item[2]), path_id=item[0], target_type=TargetType(item[3]))
                for item in paths
            ],
            target_id=row[0],
            created_at=datetime.fromisoformat(row[2]),
            updated_at=datetime.fromisoformat(row[3]),
        )

    def get(self, name: str) -> Target:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id, name, created_at, updated_at FROM targets WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No existe un target llamado '{name}'.")
            paths = connection.execute(
                "SELECT id, target_id, local_path, type, position FROM target_paths WHERE target_id = ? ORDER BY position",
                (row[0],),
            ).fetchall()
        return self._target_from_row(row, paths)

    def list(self) -> list[Target]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, name, created_at, updated_at FROM targets ORDER BY CASE WHEN name = 'concord' THEN 0 ELSE 1 END, name"
            ).fetchall()
            targets = []
            for row in rows:
                paths = connection.execute(
                    "SELECT id, target_id, local_path, type, position FROM target_paths WHERE target_id = ? ORDER BY position",
                    (row[0],),
                ).fetchall()
                targets.append(self._target_from_row(row, paths))
        return targets

    def _check_overlap(self, candidate: Path) -> None:
        candidate = candidate.expanduser().resolve()
        for target in self.list():
            for existing in target.paths:
                if (
                    candidate == existing.local_path
                    or candidate.is_relative_to(existing.local_path)
                    or existing.local_path.is_relative_to(candidate)
                ):
                    raise ValueError(
                        f"La ruta se solapa con '{existing.local_path}', registrada en '{target.name}'."
                    )

    def add(self, local_path: Path, name: str | None = None) -> Target:
        target = Target(local_path, name)
        if target.name in self.RESERVED_NAMES:
            raise ValueError(f"'{target.name}' es un nombre reservado por Concord. Use otro nombre.")
        try:
            self.get(target.name)
        except KeyError:
            pass
        else:
            raise ValueError(
                f"El target '{target.name}' ya existe; use concord add-path {target.name} <ruta>."
            )
        self._check_overlap(target.local_path)
        self._write_target(target)
        try:
            with self.database.connect() as connection:
                self._insert_target(connection, target)
            self._persist_manifest()
        except Exception:
            with self.database.connect() as connection:
                connection.execute("DELETE FROM targets WHERE id = ?", (target.id,))
            self.repository.remove(target.name)
            raise
        return target

    def add_path(self, name: str, local_path: Path) -> Target:
        target = self.get(name)
        new_path = TargetPath(local_path)
        self._check_overlap(new_path.local_path)
        updated = Target(
            name=target.name,
            paths=[*target.paths, new_path],
            target_id=target.id,
            created_at=target.created_at,
            updated_at=target.updated_at,
        )
        temporary = self._stage_existing_copy(target)
        self._copy(new_path.local_path, temporary / new_path.relative_path)
        self._install_staged_target(updated, temporary)
        updated.touch()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO target_paths VALUES (?, ?, ?, ?, ?)",
                (new_path.id, target.id, str(new_path.local_path), new_path.type.value, len(target.paths)),
            )
            connection.execute(
                "UPDATE targets SET updated_at = ? WHERE id = ?",
                (updated.updated_at.isoformat(), target.id),
            )
        self._persist_manifest()
        return self.get(name)

    def remove_path(self, name: str, local_path: Path) -> Target:
        target = self.get(name)
        requested = local_path.expanduser().resolve()
        selected = next((path for path in target.paths if path.local_path == requested), None)
        if selected is None:
            raise KeyError(f"La ruta '{requested}' no pertenece al target '{name}'.")
        if len(target.paths) == 1:
            raise ValueError(f"'{name}' solo tiene una ruta; use concord remove {name}.")
        retained = [path for path in target.paths if path.id != selected.id]
        updated = Target(
            name=target.name,
            paths=retained,
            target_id=target.id,
            created_at=target.created_at,
            updated_at=target.updated_at,
        )
        temporary = self._stage_existing_copy(target)
        stored = temporary / selected.relative_path
        if os.path.lexists(stored):
            self._remove_local(stored)
        parent = stored.parent
        while parent != temporary and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
        self._install_staged_target(updated, temporary)
        updated.touch()
        with self.database.connect() as connection:
            connection.execute("DELETE FROM target_paths WHERE id = ?", (selected.id,))
            for position, path in enumerate(retained):
                connection.execute(
                    "UPDATE target_paths SET position = ? WHERE id = ?", (position, path.id)
                )
            connection.execute(
                "UPDATE targets SET updated_at = ? WHERE id = ?",
                (updated.updated_at.isoformat(), target.id),
            )
        self._persist_manifest()
        return self.get(name)

    def sync(self, name: str | None = None) -> list[Target]:
        targets = [self.get(name)] if name else self.list()
        for target in targets:
            for path in target.paths:
                if not os.path.lexists(path.local_path):
                    raise FileNotFoundError(
                        f"No existe una ruta local de '{target.name}': {path.local_path}"
                    )
        changed = [target for target in targets if not self._target_diff(target).clean]
        staged = []
        try:
            for target in changed:
                staged.append((target, self._stage_target(target)))
        except Exception:
            for _, temporary in staged:
                if temporary.exists():
                    shutil.rmtree(temporary)
            raise
        self._install_staged_targets(staged)
        for target, _ in staged:
            target.touch()
            self._update_timestamp(target)
        if changed:
            self._persist_manifest()
        return changed

    def _restore_targets(self, targets: list[Target], *, force: bool) -> list[Target]:
        pairs = [(target, path, self._destination(target, path)) for target in targets for path in target.paths]
        for target, path, source in pairs:
            if not os.path.lexists(source):
                raise FileNotFoundError(f"No existe la copia de '{path.local_path}' en '{target.name}'.")
            if os.path.lexists(path.local_path) and not force:
                raise FileExistsError(f"'{path.local_path}' ya existe; use --force para reemplazar todo el target.")
        staged = []
        try:
            for _, path, source in pairs:
                temporary = path.local_path.with_name(f".{path.local_path.name}.concord.tmp")
                if os.path.lexists(temporary):
                    self._remove_local(temporary)
                self._copy(source, temporary)
                staged.append((path.local_path, temporary))
        except Exception:
            for _, temporary in staged:
                if os.path.lexists(temporary):
                    self._remove_local(temporary)
            raise
        installed: list[tuple[Path, Path]] = []
        try:
            for destination, temporary in staged:
                backup = destination.with_name(f".{destination.name}.concord.bak")
                if os.path.lexists(backup):
                    self._remove_local(backup)
                if os.path.lexists(destination):
                    destination.rename(backup)
                try:
                    temporary.rename(destination)
                except Exception:
                    if os.path.lexists(backup):
                        backup.rename(destination)
                    raise
                installed.append((destination, backup))
        except Exception:
            for destination, backup in reversed(installed):
                if os.path.lexists(destination):
                    self._remove_local(destination)
                if os.path.lexists(backup):
                    backup.rename(destination)
            for _, temporary in staged:
                if os.path.lexists(temporary):
                    self._remove_local(temporary)
            raise
        for _, backup in installed:
            if os.path.lexists(backup):
                self._remove_local(backup)
        return targets

    def _remove_local(self, path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    def restore(self, name: str, *, force: bool = False) -> Target:
        return self._restore_targets([self.get(name)], force=force)[0]

    def restore_all(self, *, force: bool = False) -> list[Target]:
        targets = [target for target in self.list() if target.name != CONCORD_TARGET]
        return self._restore_targets(targets, force=force)

    def remove(self, name: str, *, keep_repository: bool = False) -> None:
        if name == CONCORD_TARGET:
            raise ValueError("El target 'concord' es reservado y no puede eliminarse.")
        target = self.get(name)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM targets WHERE id = ?", (target.id,))
        if not keep_repository:
            self.repository.remove(name)
        self._persist_manifest()

    def import_manifest(self, *, replace: bool = False) -> list[Target]:
        config = self.config_manager.load()
        if not config.targets:
            raise ValueError("El manifiesto no contiene targets para importar.")
        targets = []
        all_paths: list[Path] = []
        for item in config.targets:
            paths = []
            for path_item in item.paths:
                relative = path_item.relative_path
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError(f"Ruta no segura en el manifiesto: {relative}")
                absolute = (Path.home() / relative).resolve()
                if any(
                    absolute == existing
                    or absolute.is_relative_to(existing)
                    or existing.is_relative_to(absolute)
                    for existing in all_paths
                ):
                    raise ValueError(f"Ruta solapada en el manifiesto: {relative}")
                all_paths.append(absolute)
                paths.append(TargetPath(absolute, target_type=TargetType(path_item.type)))
            targets.append(
                Target(
                    name=item.name,
                    paths=paths,
                    created_at=item.created_at or datetime.now(UTC),
                    updated_at=item.updated_at or item.created_at or datetime.now(UTC),
                )
            )
        with self.database.connect() as connection:
            existing = connection.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
            if existing and not replace:
                raise ValueError("La base de datos ya contiene targets; use --replace para reconstruirla.")
            connection.execute("DELETE FROM targets")
            for target in targets:
                self._insert_target(connection, target)
        return targets

    def _path_state(self, target: Target, path: TargetPath) -> str:
        copy = self._destination(target, path)
        if not os.path.lexists(path.local_path):
            return "missing"
        if not os.path.lexists(copy):
            return "untracked"
        return "clean" if self._path_diff(target, path).clean else "modified"

    def status(self) -> list[TargetStatus]:
        priority = {"clean": 0, "modified": 1, "untracked": 2, "missing": 3}
        result = []
        for target in self.list():
            states = [self._path_state(target, path) for path in target.paths]
            state = max(states, key=priority.get)
            result.append(
                TargetStatus(target.name, state, sum(item != "clean" for item in states), len(states))
            )
        return result

    def diff(self, name: str | None = None) -> list[TargetDiff]:
        targets = [self.get(name)] if name else self.list()
        return [self._target_diff(target) for target in targets]

    def preview_sync(self, name: str | None = None) -> list[TargetDiff]:
        return self.diff(name)

    def preview_restore(self, name: str | None = None) -> list[TargetDiff]:
        targets = [self.get(name)] if name else [t for t in self.list() if t.name != CONCORD_TARGET]
        return [self._target_diff(target, reverse=True) for target in targets]

    def _path_diff(self, target: Target, path: TargetPath, *, reverse: bool = False) -> TargetDiff:
        local = self._snapshot(path.local_path)
        stored = self._snapshot(self._destination(target, path))
        entries = []
        for relative in sorted(local.keys() | stored.keys(), key=str):
            display = path.relative_path if relative == Path(".") else path.relative_path / relative
            if relative not in stored:
                state = "added"
            elif relative not in local:
                state = "deleted"
            elif not self._paths_equal(local[relative], stored[relative]):
                state = "modified"
            else:
                continue
            if reverse:
                state = {"added": "deleted", "deleted": "added"}.get(state, state)
            entries.append(DiffEntry(state, display))
        return TargetDiff(target.name, entries)

    def _target_diff(self, target: Target, *, reverse: bool = False) -> TargetDiff:
        entries = [
            entry
            for path in target.paths
            for entry in self._path_diff(target, path, reverse=reverse).entries
        ]
        return TargetDiff(target.name, entries)

    def _snapshot(self, root: Path) -> dict[Path, Path]:
        if not os.path.lexists(root):
            return {}
        if root.is_symlink() or root.is_file():
            return {Path("."): root}
        entries = {}
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
