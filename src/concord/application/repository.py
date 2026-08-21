from pathlib import Path


class RepositoryManager:

    def create(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
