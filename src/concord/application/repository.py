from pathlib import Path
from concord.application.config import ConfigManager


class RepositoryManager:
    def __init__(self) -> None:
        self.repository_path = ConfigManager().load().repository_path

    def create(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
