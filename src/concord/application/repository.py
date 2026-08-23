from pathlib import Path
from concord.application.config import ConfigManager


class RepositoryManager:
    def __init__(self, repository_path: Path | None = None) -> None:
        if repository_path is None:
            repository_path = ConfigManager().load().repository_path
        self.repository_path = repository_path

    def create(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
