import shutil
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import questionary
import tomli_w
from questionary import ValidationError, Validator

from concord import application as concord

MANIFEST_VERSION = 2
CONCORD_TARGET = "concord"


@dataclass(frozen=True)
class TargetPathConfig:
    relative_path: Path
    type: str


@dataclass(frozen=True)
class TargetConfig:
    name: str
    paths: list[TargetPathConfig]
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class GitConfig:
    enabled: bool = True
    auto_commit: bool = True
    auto_push: bool = False
    remote: str = "origin"


@dataclass
class Config:
    repository_path: Path
    version: int = MANIFEST_VERSION
    targets: list[TargetConfig] = field(default_factory=list)
    git: GitConfig = field(default_factory=GitConfig)
    source_version: int = MANIFEST_VERSION

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
            "git": {
                "enabled": config.git.enabled,
                "auto_commit": config.git.auto_commit,
                "auto_push": config.git.auto_push,
                "remote": config.git.remote,
            },
            "targets": [
                {
                    "name": target.name,
                    "paths": [
                        {
                            "relative_path": path.relative_path.as_posix(),
                            "type": path.type,
                        }
                        for path in target.paths
                    ],
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
        version = settings.get("version", 1)
        if version not in {1, MANIFEST_VERSION}:
            raise ValueError(f"Versión de manifiesto no compatible: {version}.")
        targets = []
        for item in settings.get("targets", []):
            path_items = (
                item.get("paths", [])
                if version == MANIFEST_VERSION
                else [{"relative_path": item["relative_path"], "type": item["type"]}]
            )
            targets.append(
                TargetConfig(
                    name=item["name"],
                    paths=[
                        TargetPathConfig(
                            relative_path=Path(path["relative_path"]),
                            type=path["type"],
                        )
                        for path in path_items
                    ],
                    created_at=(datetime.fromisoformat(item["created_at"]) if item.get("created_at") else None),
                    updated_at=(datetime.fromisoformat(item["updated_at"]) if item.get("updated_at") else None),
                )
            )
        return Config(
            repository_path=Path(settings["repository_path"]).expanduser(),
            version=MANIFEST_VERSION,
            targets=targets,
            git=GitConfig(**settings.get("git", {})),
            source_version=version,
        )

    def migrate(self) -> tuple[Config, Path | None]:
        config = self.load()
        if config.source_version == MANIFEST_VERSION:
            return config, None
        backup_dir = concord.data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = backup_dir / f"concord-v1-{timestamp}.toml"
        shutil.copy2(concord.config_file, backup)
        self.save(config)
        return config, backup

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
