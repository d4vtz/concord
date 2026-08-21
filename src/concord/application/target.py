from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone
from enum import Enum
import uuid
from concord.application.file import File
import sqlite3
from concord import application as concord
from concord.application.repository import RepositoryManager
from concord.application.config import ConfigManager


class TargetType(Enum):
    FILE = "file"
    DIRECTORY = "directory"


config = ConfigManager().load()


class Target:
    def __init__(self, local_path: Path) -> None:
        self.repo = RepositoryManager()
        self.local_path = local_path.expanduser().resolve()
        self.created_at = datetime.now(timezone.utc)
        self.type = self._type
        self.id = str(uuid.uuid4())
        self.name = self.local_path.name
        self.repository_path = (
            config.repository_path
            / self.name
            / self.local_path.relative_to(Path.home())
        )

    @property
    def _type(self) -> TargetType:
        if self.local_path.is_dir():
            return TargetType.DIRECTORY
        else:
            return TargetType.FILE

    def get_files(self) -> list[Path]:
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


class Database:
    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(concord.database_file)

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS targets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    repository_path TEXT NOT NULL,
                    type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                )
                """)


class TargetManager:
    def __init__(self) -> None:
        self.database = Database()

    def save(self, target: Target) -> None:
        self.database.initialize()
        with self.database.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO targets (
                    id,
                    name,
                    local_path,
                    repositoy_path.
                    type,
                    created_at,
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    target.id,
                    target.name,
                    str(target.local_path),
                    str(target.repository_path),
                    target.type.value,
                    target.created_at.isoformat(),
                ),
            )

    def replicate_target(self, target: Target) -> None:
        pass

    def add(self, local_path: Path) -> None:
        target = Target(local_path)
        self.save(target)
        self.replicate_target(target)
