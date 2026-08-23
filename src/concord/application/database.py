from concord import application as concord
import sqlite3
from pathlib import Path


class Database:
    def __init__(self, database_path: Path | None = None):
        if database_path is None:
            database_path = concord.database_file
        self.database_path = database_path

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path)

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS targets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    type TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """)
