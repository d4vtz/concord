import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import questionary
import tomli_w
from questionary import ValidationError, Validator

from concord import application as concord

MANIFEST_VERSION = 1
CONCORD_TARGET = "concord"


@dataclass(frozen=True)
class TargetConfig:
    name: str
    relative_path: Path
    type: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Config:
    repository_path: Path
    version: int = MANIFEST_VERSION
    targets: list[TargetConfig] = field(default_factory=list)

    @classmethod
    def defaults(cls) -> "Config":
        return cls(repository_path=concord.default_repository_dir)


class RepositoryPathValidator(Validator):
    def validate(self, document) -> None:
        path = Path(document.text).expanduser()
        if path.exists() and not path.is_dir():
            raise ValidationError(message="El path debe ser un directorio.")


class ConfigManager:
    def _portable_path(self, path: Path) -> str:
        path = path.expanduser().resolve()
        try:
            return str(Path("~") / path.relative_to(Path.home().resolve()))
        except ValueError:
            return str(path)

    def save(self, config: Config) -> None:
        concord.config_dir.mkdir(parents=True, exist_ok=True)
        settings = {
            "version": config.version,
            "repository_path": self._portable_path(config.repository_path),
            "targets": [
                {
                    "name": target.name,
                    "relative_path": target.relative_path.as_posix(),
                    "type": target.type,
                    **(
                        {"created_at": target.created_at.isoformat()}
                        if target.created_at
                        else {}
                    ),
                    **(
                        {"updated_at": target.updated_at.isoformat()}
                        if target.updated_at
                        else {}
                    ),
                }
                for target in config.targets
            ],
        }
        temporary = concord.config_file.with_suffix(".toml.tmp")
        with temporary.open("wb") as file:
            tomli_w.dump(settings, file)
        temporary.replace(concord.config_file)

    def load(self, path: Path | None = None) -> Config:
        source = path or concord.config_file
        with source.open("rb") as file:
            settings = tomllib.load(file)
        version = settings.get("version", MANIFEST_VERSION)
        if version != MANIFEST_VERSION:
            raise ValueError(f"Versión de manifiesto no compatible: {version}.")
        targets = [
            TargetConfig(
                name=item["name"],
                relative_path=Path(item["relative_path"]),
                type=item["type"],
                created_at=(datetime.fromisoformat(item["created_at"]) if item.get("created_at") else None),
                updated_at=(datetime.fromisoformat(item["updated_at"]) if item.get("updated_at") else None),
            )
            for item in settings.get("targets", [])
        ]
        return Config(
            repository_path=Path(settings["repository_path"]).expanduser(),
            version=version,
            targets=targets,
        )

    def repository_manifest_path(self, repository_path: Path) -> Path:
        relative_config = concord.config_file.relative_to(Path.home())
        return repository_path / CONCORD_TARGET / relative_config

    def load_from_repository(self, repository_path: Path) -> Config:
        config = self.load(self.repository_manifest_path(repository_path))
        config.repository_path = repository_path.expanduser().resolve()
        return config

    def register(self, config: Config, target: TargetConfig) -> None:
        config.targets = [item for item in config.targets if item.name != target.name]
        config.targets.append(target)
        config.targets.sort(key=lambda item: (item.name != CONCORD_TARGET, item.name))

    def unregister(self, config: Config, name: str) -> None:
        config.targets = [target for target in config.targets if target.name != name]

    def request_repository_path(self) -> Path:
        repository_path = questionary.text(
            "Directorio del repositorio: ",
            default=Config.defaults().repository_path.as_posix(),
            validate=RepositoryPathValidator,
        ).ask()
        if repository_path is None:
            raise KeyboardInterrupt
        return Path(repository_path).expanduser().resolve()

    def request_configuration(self) -> Config:
        return Config(repository_path=self.request_repository_path())
