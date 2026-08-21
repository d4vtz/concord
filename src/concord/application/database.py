from concord import application as concord
import sqlite3


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
                    type TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """)
