from rich.console import Console
from rich.panel import Panel

from concord import application as concord
from concord.application.config import ConfigManager
from concord.application.database import Database
from concord.application.repository import RepositoryManager


class Initializer:
    def __init__(self) -> None:
        self.config_manager = ConfigManager()

    def initialize(self) -> None:
        if concord.is_initialized():
            self._already_initialized()
            return

        config = self.config_manager.request_configuration()
        repository_manager = RepositoryManager(config.repository_path)
        repository_manager.create(path=config.repository_path)
        self.config_manager.save(config)
        Database().initialize()

    def _already_initialized(self) -> None:
        console = Console()
        console.print(
            Panel(
                "Concord ya está inicializado.\n\n"
                f"Configuración: {concord.config_file}",
                title="CONCORD",
                expand=False,
            )
        )

        console.print(
            "\nUse [bold]concord --help[/bold] para consultar los comandos disponibles."
        )
