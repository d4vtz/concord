import filecmp
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from concord.application.database import Database
from concord.application.repository import RepositoryManager
from concord.application.target import Target, TargetType


@dataclass(frozen=True)
class TargetStatus:
    name: str
    state: str


class TargetManager:
    def __init__(self, database: Database | None = None, repository: RepositoryManager | None = None) -> None:
        self.database = database or Database()
        self.repository = repository or RepositoryManager()
        self.database.initialize()

    def _destination(self, target: Target) -> Path:
        return self.repository.target_path(target.name) / target.local_path.relative_to(Path.home())

    def _write_target(self, target: Target) -> None:
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

    def save(self, target: Target) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO targets (id, name, local_path, type, created_at) VALUES (?, ?, ?, ?, ?)",
                (target.id, target.name, str(target.local_path), target.type.value, target.created_at.isoformat()),
            )
            connection.executemany(
                "INSERT INTO files (target_id, relative_path) VALUES (?, ?)",
                [(target.id, str(file.local_path.relative_to(target.local_path.parent if target.type is TargetType.FILE else target.local_path))) for file in target.get_files()],
            )

    def get(self, name: str) -> Target:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id, name, local_path, type, created_at FROM targets WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            raise KeyError(f"No existe un target llamado '{name}'.")
        return Target(Path(row[2]), row[1], target_id=row[0], target_type=TargetType(row[3]), created_at=datetime.fromisoformat(row[4]))

    def list(self) -> list[Target]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT id, name, local_path, type, created_at FROM targets ORDER BY name").fetchall()
        return [Target(Path(row[2]), row[1], target_id=row[0], target_type=TargetType(row[3]), created_at=datetime.fromisoformat(row[4])) for row in rows]

    def add(self, local_path: Path, name: str | None = None) -> Target:
        target = Target(local_path, name)
        with self.database.connect() as connection:
            duplicate = connection.execute(
                "SELECT name FROM targets WHERE name = ? OR local_path = ? LIMIT 1", (target.name, str(target.local_path))
            ).fetchone()
        if duplicate:
            raise ValueError(f"El target ya está registrado como '{duplicate[0]}'.")
        self._write_target(target)
        try:
            self.save(target)
        except Exception:
            self.repository.remove(target.name)
            raise
        return target

    def sync(self, name: str | None = None) -> list[Target]:
        targets = [self.get(name)] if name else self.list()
        for target in targets:
            self._write_target(target)
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

    def remove(self, name: str, *, keep_repository: bool = False) -> None:
        target = self.get(name)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM files WHERE target_id = ?", (target.id,))
            connection.execute("DELETE FROM targets WHERE id = ?", (target.id,))
        if not keep_repository:
            self.repository.remove(name)

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
