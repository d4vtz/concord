import sqlite3
import uuid
from pathlib import Path

from concord import application as concord


class Database:
    def __init__(self, database_path: Path | None = None):
        if database_path is None:
            database_path = concord.database_file
        self.database_path = database_path

    def connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'targets'"
            ).fetchone()
            if table:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(targets)").fetchall()
                }
                if "local_path" in columns:
                    self._migrate_v1(connection)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS targets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS target_paths (
                    id TEXT PRIMARY KEY,
                    target_id TEXT NOT NULL,
                    local_path TEXT NOT NULL UNIQUE,
                    type TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    FOREIGN KEY (target_id) REFERENCES targets(id) ON DELETE CASCADE,
                    UNIQUE (target_id, position)
                )
                """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    target_path_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    PRIMARY KEY (target_path_id, relative_path),
                    FOREIGN KEY (target_path_id) REFERENCES target_paths(id) ON DELETE CASCADE
                )
                """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS dependencies (
                    target_id TEXT NOT NULL,
                    package TEXT NOT NULL,
                    manager TEXT NOT NULL CHECK (manager IN ('pacman', 'aur')),
                    optional INTEGER NOT NULL DEFAULT 0 CHECK (optional IN (0, 1)),
                    position INTEGER NOT NULL,
                    PRIMARY KEY (target_id, package),
                    FOREIGN KEY (target_id) REFERENCES targets(id) ON DELETE CASCADE,
                    UNIQUE (target_id, manager, position)
                )
                """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT ''
                )
                """)
            # Las etiquetas se retiraron en 2.3.1; sus datos anteriores se descartan.
            connection.execute("DROP TABLE IF EXISTS profile_tags")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS profile_includes (
                    profile_id TEXT NOT NULL,
                    included_profile_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    PRIMARY KEY (profile_id, included_profile_id),
                    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
                    FOREIGN KEY (included_profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
                    UNIQUE (profile_id, position)
                )
                """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS profile_targets (
                    profile_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    PRIMARY KEY (profile_id, target_id),
                    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
                    FOREIGN KEY (target_id) REFERENCES targets(id) ON DELETE CASCADE,
                    UNIQUE (profile_id, position)
                )
                """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS profile_exclusions (
                    profile_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    PRIMARY KEY (profile_id, target_id),
                    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
                    FOREIGN KEY (target_id) REFERENCES targets(id) ON DELETE CASCADE,
                    UNIQUE (profile_id, position)
                )
                """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS profile_activation (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    primary_profile_id TEXT NOT NULL
                )
                """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS profile_activation_complements (
                    profile_id TEXT PRIMARY KEY,
                    position INTEGER NOT NULL UNIQUE
                )
                """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS profile_suggestion (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    primary_profile_id TEXT NOT NULL,
                    FOREIGN KEY (primary_profile_id) REFERENCES profiles(id) ON DELETE CASCADE
                )
                """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS profile_suggestion_complements (
                    profile_id TEXT PRIMARY KEY,
                    position INTEGER NOT NULL UNIQUE,
                    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
                )
                """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS local_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """)

    def _migrate_v1(self, connection: sqlite3.Connection) -> None:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(targets)").fetchall()
        }
        updated = "COALESCE(updated_at, created_at)" if "updated_at" in columns else "created_at"
        rows = connection.execute(
            f"SELECT id, name, local_path, type, created_at, {updated} FROM targets"
        ).fetchall()
        connection.execute("DROP TABLE IF EXISTS files")
        connection.execute("ALTER TABLE targets RENAME TO targets_v1")
        connection.execute("""
            CREATE TABLE targets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """)
        connection.execute("""
            CREATE TABLE target_paths (
                id TEXT PRIMARY KEY,
                target_id TEXT NOT NULL,
                local_path TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL,
                position INTEGER NOT NULL,
                FOREIGN KEY (target_id) REFERENCES targets(id) ON DELETE CASCADE,
                UNIQUE (target_id, position)
            )
            """)
        for target_id, name, local_path, type_, created_at, updated_at in rows:
            connection.execute(
                "INSERT INTO targets VALUES (?, ?, ?, ?)",
                (target_id, name, created_at, updated_at),
            )
            connection.execute(
                "INSERT INTO target_paths VALUES (?, ?, ?, ?, 0)",
                (str(uuid.uuid4()), target_id, local_path, type_),
            )
        connection.execute("DROP TABLE targets_v1")
