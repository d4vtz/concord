from dataclasses import dataclass, asdict
from concord import application as concord
from pathlib import Path
from questionary import Validator, ValidationError
import tomli_w
import tomllib
import questionary
from rich.console import Console
from rich.panel import Panel


@dataclass
class Config:
    repository_path: Path

    @classmethod
    def defaults(cls) -> Config:
        return cls(repository_path=concord.default_repository_dir)


class RepositoryPathValidator(Validator):
    def validate(self, document) -> None:
        path = Path(document.text).expanduser()
        if path.exists() and not path.is_dir():
            raise ValidationError(message="El path debe ser un directorio.")


class ConfigManager:

    def save(self, config: Config) -> None:
        concord.config_dir.mkdir(parents=True, exist_ok=True)
        settings = {
            key: str(value) if isinstance(value, Path) else value
            for key, value in asdict(config).items()
        }
        with concord.config_file.open("wb") as file:
            tomli_w.dump(settings, file)

    def load(self) -> Config:
        with concord.config_file.open("rb") as file:
            config = tomllib.load(file)

        settings = {
            key: Path(value) if "path" in key else value
            for key, value in config.items()
        }
        return Config(**settings)

    def request_repository_path(self) -> Path:
        repository_path = questionary.text(
            "Directorio del repositorio: ",
            default=Config.defaults().repository_path.as_posix(),
            validate=RepositoryPathValidator,
        ).ask()
        if repository_path is None:
            raise KeyboardInterrupt
        return Path(repository_path).expanduser()

    def request_configuration(self) -> Config:
        console = Console()
        console.print(
            Panel(
                "Configuración inicial",
                title="CONCORD",
                expand=False,
            )
        )
        repository_path = self.request_repository_path()
        return Config(repository_path=repository_path)
