from concord import application as concord
from concord.application.config import ConfigManager
from concord.application.database import Database
from concord.application.repository import RepositoryManager


class Initializer:
    def __init__(self) -> None:
        self.config_manager = ConfigManager()

    def initialize(self) -> bool:
        if concord.is_initialized():
            return False

        config = self.config_manager.request_configuration()
        repository_manager = RepositoryManager(config.repository_path)
        repository_manager.create(path=config.repository_path)
        self.config_manager.save(config)
        Database().initialize()
        return True
