import shutil
from pathlib import Path

from concord.application.config import ConfigManager


class RepositoryManager:
    def __init__(self, repository_path: Path | None = None) -> None:
        if repository_path is None:
            repository_path = ConfigManager().load().repository_path
        self.repository_path = repository_path

    def create(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def target_path(self, name: str) -> Path:
        return self.repository_path / name

    def remove(self, name: str) -> None:
        path = self.target_path(name)
        if path.exists():
            shutil.rmtree(path)
