import uuid
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from concord.application.file import File


class TargetType(Enum):
    FILE = "file"
    DIRECTORY = "directory"


class Target:
    def __init__(self, local_path: Path, name: str | None = None, *, target_id: str | None = None, created_at: datetime | None = None, updated_at: datetime | None = None, target_type: TargetType | None = None) -> None:
        self.local_path = local_path.expanduser().resolve()
        try:
            self.local_path.relative_to(Path.home().resolve())
        except ValueError as error:
            raise ValueError("El target debe estar dentro de HOME.") from error
        self.created_at = created_at or datetime.now(UTC)
        self.updated_at = updated_at or self.created_at
        self.type = target_type or self._type
        self.id = target_id or str(uuid.uuid4())
        self.name = name or self._default_name
        if not self.name or self.name in {".", ".."} or "/" in self.name or "\\" in self.name:
            raise ValueError("El nombre del target no es válido.")

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)

    @property
    def _default_name(self) -> str:
        name = self.local_path.name

        if name.startswith("."):
            return f"dot_{name[1:]}"

        return name

    @property
    def _type(self) -> TargetType:
        if not self.local_path.exists():
            raise FileNotFoundError(self.local_path)
        elif self.local_path.is_dir():
            return TargetType.DIRECTORY
        else:
            return TargetType.FILE

    def get_files(self) -> list[File]:
        if self.type is TargetType.FILE:
            return [File(self.id, self.local_path, self.local_path)]
        files = []
        for path in self.local_path.rglob("*"):
            if path.is_file():
                files.append(
                    File(
                        target_id=self.id,
                        local_path=path,
                        target_path=self.local_path,
                    )
                )
        return files
