import uuid
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from concord.application.file import File


class TargetType(Enum):
    FILE = "file"
    DIRECTORY = "directory"


class TargetPath:
    def __init__(
        self,
        local_path: Path,
        *,
        path_id: str | None = None,
        target_type: TargetType | None = None,
    ) -> None:
        self.local_path = local_path.expanduser().resolve()
        try:
            self.local_path.relative_to(Path.home().resolve())
        except ValueError as error:
            raise ValueError("La ruta del target debe estar dentro de HOME.") from error
        self.id = path_id or str(uuid.uuid4())
        self.type = target_type or self._detect_type()

    def _detect_type(self) -> TargetType:
        if not self.local_path.exists() and not self.local_path.is_symlink():
            raise FileNotFoundError(self.local_path)
        return TargetType.DIRECTORY if self.local_path.is_dir() else TargetType.FILE

    @property
    def relative_path(self) -> Path:
        return self.local_path.relative_to(Path.home().resolve())

    def get_files(self) -> list[File]:
        if self.type is TargetType.FILE:
            return [File(self.id, self.local_path, self.local_path)]
        files = []
        for path in self.local_path.rglob("*"):
            if path.is_file() or path.is_symlink():
                files.append(File(self.id, path, self.local_path))
        return files


class Target:
    def __init__(
        self,
        local_path: Path | None = None,
        name: str | None = None,
        *,
        paths: list[TargetPath] | None = None,
        target_id: str | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        target_type: TargetType | None = None,
    ) -> None:
        if paths is None:
            if local_path is None:
                raise ValueError("Un target debe contener al menos una ruta.")
            paths = [TargetPath(local_path, target_type=target_type)]
        if not paths:
            raise ValueError("Un target debe contener al menos una ruta.")
        self.paths = paths
        self.created_at = created_at or datetime.now(UTC)
        self.updated_at = updated_at or self.created_at
        self.id = target_id or str(uuid.uuid4())
        self.name = name or self._default_name
        if not self.name or self.name in {".", ".."} or "/" in self.name or "\\" in self.name:
            raise ValueError("El nombre del target no es válido.")

    @property
    def local_path(self) -> Path:
        return self.paths[0].local_path

    @property
    def type(self) -> TargetType:
        return self.paths[0].type

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)

    @property
    def _default_name(self) -> str:
        name = self.paths[0].local_path.name
        return f"dot_{name[1:]}" if name.startswith(".") else name

    def get_files(self) -> list[File]:
        return [file for path in self.paths for file in path.get_files()]
