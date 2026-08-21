from concord.application.database import Database
from concord.application.target import Target
from pathlib import Path


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
                    type,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    target.id,
                    target.name,
                    str(target.local_path),
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
