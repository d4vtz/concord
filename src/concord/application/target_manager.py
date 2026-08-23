from concord.application.database import Database
from concord.application.target import Target, TargetType
from concord.application.repository import RepositoryManager
from pathlib import Path
import shutil
from rich.console import Console
from rich.panel import Panel


class TargetManager:
    def __init__(
        self,
        database: Database | None = None,
        repository: RepositoryManager | None = None,
    ) -> None:
        if database is None:
            database = Database()
        if repository is None:
            repository = RepositoryManager()
        self.database = database
        self.repository = repository
        self.database.initialize()

    def save(self, target: Target) -> None:
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
        target_path = self.repository.repository_path / target.name
        self.repository.create(target_path)
        print(target_path)

        if target.type is TargetType.FILE:
            destination = target_path / target.local_path.name
            print(destination)
            shutil.copy2(
                target.local_path,
                destination,
                follow_symlinks=False,
            )

            return

        for file in target.get_files():
            destination = target_path / file.relative_path
            print(file.relative_path)
            destination.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            shutil.copy2(
                file.local_path,
                destination,
                follow_symlinks=False,
            )

    def add(self, local_path: Path) -> Target | None:
        try:
            target = Target(local_path)
            self.replicate_target(target)
            self.save(target)
            return target
        except ValueError:
            console = Console()
            console.print(
                Panel(
                    "Usar un path dentro de home.",
                    title="CONCORD",
                    expand=False,
                )
            )
