import uuid
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from concord.application.file import File


class TargetType(Enum):
    FILE = "file"
    DIRECTORY = "directory"


class Target:
    def __init__(self, local_path: Path, name: str | None = None) -> None:
        self.local_path = local_path.expanduser().resolve()
        self.created_at = datetime.now(UTC)
        self.type = self._type
        self.id = str(uuid.uuid4())
        self.name = name or self._default_name

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
