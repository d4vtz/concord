import sqlite3
from pathlib import Path

from concord import application as concord


class Database:
    def __init__(self, database_path: Path | None = None):
        if database_path is None:
            database_path = concord.database_file
        self.database_path = database_path

    def connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.database_path)

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS targets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    local_path TEXT NOT NULL,
                    type TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    target_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    PRIMARY KEY (target_id, relative_path),
                    FOREIGN KEY (target_id) REFERENCES targets(id) ON DELETE CASCADE
                )
                """)
