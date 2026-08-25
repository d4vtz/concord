import filecmp
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


class TargetManager:
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
                "INSERT INTO targets (id, name, local_path, type, created_at) VALUES (?, ?, ?, ?, ?)",
                (target.id, target.name, str(target.local_path), target.type.value, target.created_at.isoformat()),
            )

    def _manifest_target(self, target: Target) -> TargetConfig:
        return TargetConfig(
            name=target.name,
            relative_path=target.local_path.relative_to(Path.home()),
            type=target.type.value,
        )

    def _persist_manifest(self) -> None:
        config = self.config_manager.load()
        config.targets = [self._manifest_target(target) for target in self.list()]
        config.targets.sort(key=lambda item: (item.name != CONCORD_TARGET, item.name))
        self.config_manager.save(config)
        concord_target = next((target for target in self.list() if target.name == CONCORD_TARGET), None)
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
        if not config.targets:
            self._persist_manifest()
        elif len(names) == 1 and len(config.targets) > 1:
            self.import_manifest(replace=True)

    def get(self, name: str) -> Target:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id, name, local_path, type, created_at FROM targets WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            raise KeyError(f"No existe un target llamado '{name}'.")
        return Target(
            Path(row[2]),
            row[1],
            target_id=row[0],
            target_type=TargetType(row[3]),
            created_at=datetime.fromisoformat(row[4]),
        )

    def list(self) -> list[Target]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, name, local_path, type, created_at FROM targets ORDER BY CASE WHEN name = 'concord' THEN 0 ELSE 1 END, name"
            ).fetchall()
        return [
            Target(
                Path(row[2]),
                row[1],
                target_id=row[0],
                target_type=TargetType(row[3]),
                created_at=datetime.fromisoformat(row[4]),
            )
            for row in rows
        ]

    def add(self, local_path: Path, name: str | None = None) -> Target:
        target = Target(local_path, name)
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
        for target in targets:
            self._write_target(target)
        if name != CONCORD_TARGET:
            self._write_target(self.get(CONCORD_TARGET))
        return targets

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
                    created_at=datetime.now(UTC),
                )
            )
        with self.database.connect() as connection:
            existing = connection.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
            if existing and not replace:
                raise ValueError("La base de datos ya contiene targets; use --replace para reconstruirla.")
            connection.execute("DELETE FROM files")
            connection.execute("DELETE FROM targets")
            connection.executemany(
                "INSERT INTO targets (id, name, local_path, type, created_at) VALUES (?, ?, ?, ?, ?)",
                [
                    (target.id, target.name, str(target.local_path), target.type.value, target.created_at.isoformat())
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

    def _directories_equal(self, comparison: filecmp.dircmp) -> bool:
        if comparison.left_only or comparison.right_only or comparison.diff_files or comparison.funny_files:
            return False
        return all(self._directories_equal(child) for child in comparison.subdirs.values())
