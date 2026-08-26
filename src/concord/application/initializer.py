from datetime import UTC, datetime
from pathlib import Path

from concord import application as concord
from concord.application.config import (CONCORD_TARGET, Config, ConfigManager,
                                        GitConfig, TargetConfig)
from concord.application.database import Database
from concord.application.git import GitManager
from concord.application.repository import RepositoryManager
from concord.application.target_manager import TargetManager


class Initializer:
    def __init__(self) -> None:
        self.config_manager = ConfigManager()

    def initialize(
        self,
        repository_path: Path | None = None,
        *,
        git_config: GitConfig | None = None,
        git_identity: tuple[str, str] | None = None,
        commit_message: str = "concord: initialize repository",
    ) -> bool:
        if concord.is_initialized():
            return False

        if repository_path is None:
            config = self.config_manager.request_configuration()
            manifest = self.config_manager.repository_manifest_path(config.repository_path)
            if manifest.exists():
                config = self.config_manager.load_from_repository(config.repository_path)
        else:
            repository_path = repository_path.expanduser().resolve()
            manifest = self.config_manager.repository_manifest_path(repository_path)
            config = (
                self.config_manager.load_from_repository(repository_path)
                if manifest.exists()
                else Config(repository_path=repository_path)
            )
            config.repository_path = repository_path

        if git_config is not None:
            config.git = git_config
        if config.git.enabled and not GitManager.available():
            config.git.enabled = False

        repository = RepositoryManager(config.repository_path)
        repository.create(config.repository_path)
        now = datetime.now(UTC)
        self.config_manager.register(
            config,
            TargetConfig(
                name=CONCORD_TARGET,
                relative_path=concord.config_dir.relative_to(Path.home()),
                type="directory",
                created_at=now,
                updated_at=now,
            ),
        )
        self.config_manager.save(config)

        database = Database()
        database.initialize()
        manager = TargetManager(database, repository, self.config_manager)
        manager.import_manifest(replace=True)
        manager.sync(CONCORD_TARGET)
        if config.git.enabled:
            git = GitManager(config.repository_path)
            git.initialize()
            git.ensure_gitignore()
            if git_identity is not None:
                git.set_identity(*git_identity)
            if config.git.auto_commit and all(git.identity()):
                git.commit([Path(".")], commit_message)
        return True
