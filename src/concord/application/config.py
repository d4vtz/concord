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
CONCORD_VERSION = "2.8.0"
PROFILE_MINIMUM_VERSION = "2.3.1"
DEPENDENCY_MINIMUM_VERSION = "2.5.0"
CONCORD_TARGET = "concord"


@dataclass(frozen=True)
class TargetPathConfig:
    relative_path: Path
    type: str
    id: str | None = None


@dataclass(frozen=True)
class DependencyConfig:
    package: str
    manager: str
    optional: bool = False


@dataclass(frozen=True)
class TargetConfig:
    name: str
    paths: list[TargetPathConfig]
    id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    dependencies: list[DependencyConfig] = field(default_factory=list)


@dataclass(frozen=True)
class ManifestReference:
    id: str
    name: str


@dataclass(frozen=True)
class ProfileConfig:
    id: str
    name: str
    description: str = ""
    includes: list[ManifestReference] = field(default_factory=list)
    targets: list[ManifestReference] = field(default_factory=list)
    excludes: list[ManifestReference] = field(default_factory=list)


@dataclass(frozen=True)
class SuggestedActivationConfig:
    primary: ManifestReference
    complements: list[ManifestReference] = field(default_factory=list)


@dataclass(frozen=True)
class SecretGroupConfig:
    id: str
    recipient: str
    master_wrapper: str
    recovery_wrapper: str


@dataclass(frozen=True)
class SecretConfig:
    id: str
    target: ManifestReference
    target_path_id: str
    relative_file: Path
    kind: str
    mode: int
    names: list[str] = field(default_factory=list)


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
    profiles: list[ProfileConfig] = field(default_factory=list)
    suggested_activation: SuggestedActivationConfig | None = None
    minimum_concord_version: str | None = None
    secret_group: SecretGroupConfig | None = None
    secrets: list[SecretConfig] = field(default_factory=list)
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
    def _version_tuple(self, version: str) -> tuple[int, ...]:
        try:
            return tuple(int(part) for part in version.split("."))
        except ValueError as error:
            raise ValueError(f"Versión de Concord no válida: {version}.") from error

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
            **(
                {"minimum_concord_version": config.minimum_concord_version}
                if config.minimum_concord_version
                else {}
            ),
            "repository_path": self._portable_path(config.repository_path),
            "git": {
                "enabled": config.git.enabled,
                "auto_commit": config.git.auto_commit,
                "auto_push": config.git.auto_push,
                "remote": config.git.remote,
            },
            "targets": [
                {
                    **({"id": target.id} if target.id else {}),
                    "name": target.name,
                    "paths": [
                        {
                            "relative_path": path.relative_path.as_posix(),
                            "type": path.type,
                            **({"id": path.id} if path.id else {}),
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
                    **(
                        {
                            "dependencies": [
                                {
                                    "package": dependency.package,
                                    "manager": dependency.manager,
                                    "optional": dependency.optional,
                                }
                                for dependency in target.dependencies
                            ]
                        }
                        if target.dependencies
                        else {}
                    ),
                }
                for target in config.targets
            ],
            "profiles": [
                {
                    "id": profile.id,
                    "name": profile.name,
                    "description": profile.description,
                    "includes": [
                        {"id": reference.id, "name": reference.name}
                        for reference in profile.includes
                    ],
                    "targets": [
                        {"id": reference.id, "name": reference.name}
                        for reference in profile.targets
                    ],
                    "excludes": [
                        {"id": reference.id, "name": reference.name}
                        for reference in profile.excludes
                    ],
                }
                for profile in config.profiles
            ],
            **(
                {
                    "secret_group": {
                        "id": config.secret_group.id,
                        "recipient": config.secret_group.recipient,
                        "master_wrapper": config.secret_group.master_wrapper,
                        "recovery_wrapper": config.secret_group.recovery_wrapper,
                    },
                    "secrets": [
                        {
                            "id": secret.id,
                            "target": {"id": secret.target.id, "name": secret.target.name},
                            "target_path_id": secret.target_path_id,
                            "relative_file": secret.relative_file.as_posix(),
                            "kind": secret.kind,
                            "mode": secret.mode,
                            "names": secret.names,
                        }
                        for secret in config.secrets
                    ],
                }
                if config.secret_group
                else {}
            ),
            **(
                {
                    "suggested_activation": {
                        "primary": {
                            "id": config.suggested_activation.primary.id,
                            "name": config.suggested_activation.primary.name,
                        },
                        "complements": [
                            {"id": reference.id, "name": reference.name}
                            for reference in config.suggested_activation.complements
                        ],
                    }
                }
                if config.suggested_activation
                else {}
            ),
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
        minimum = settings.get("minimum_concord_version")
        if minimum and self._version_tuple(minimum) > self._version_tuple(CONCORD_VERSION):
            raise ValueError(
                f"Este manifiesto requiere Concord {minimum} o posterior; "
                f"la versión actual es {CONCORD_VERSION}."
            )
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
                            id=path.get("id"),
                        )
                        for path in path_items
                    ],
                    id=item.get("id"),
                    created_at=(datetime.fromisoformat(item["created_at"]) if item.get("created_at") else None),
                    updated_at=(datetime.fromisoformat(item["updated_at"]) if item.get("updated_at") else None),
                    dependencies=[
                        DependencyConfig(
                            package=dependency["package"],
                            manager=dependency["manager"],
                            optional=bool(dependency.get("optional", False)),
                        )
                        for dependency in item.get("dependencies", [])
                    ],
                )
            )
        profiles = []
        for item in settings.get("profiles", []):
            reference = lambda value: ManifestReference(value["id"], value["name"])
            profiles.append(
                ProfileConfig(
                    id=item["id"],
                    name=item["name"],
                    description=item.get("description", ""),
                    includes=[reference(value) for value in item.get("includes", [])],
                    targets=[reference(value) for value in item.get("targets", [])],
                    excludes=[reference(value) for value in item.get("excludes", [])],
                )
            )
        suggested = settings.get("suggested_activation")
        suggested_activation = None
        if suggested:
            suggested_activation = SuggestedActivationConfig(
                primary=ManifestReference(
                    suggested["primary"]["id"], suggested["primary"]["name"]
                ),
                complements=[
                    ManifestReference(value["id"], value["name"])
                    for value in suggested.get("complements", [])
                ],
            )
        group_data = settings.get("secret_group")
        secret_group = SecretGroupConfig(**group_data) if group_data else None
        secrets = []
        for item in settings.get("secrets", []):
            target = item["target"]
            relative_file = Path(item["relative_file"])
            if relative_file.is_absolute() or ".." in relative_file.parts:
                raise ValueError(f"Ruta de secreto no segura: {relative_file}")
            secrets.append(
                SecretConfig(
                    id=item["id"],
                    target=ManifestReference(target["id"], target["name"]),
                    target_path_id=item["target_path_id"],
                    relative_file=relative_file,
                    kind=item["kind"],
                    mode=int(item["mode"]),
                    names=list(item.get("names", [])),
                )
            )
        return Config(
            repository_path=Path(settings["repository_path"]).expanduser(),
            version=MANIFEST_VERSION,
            targets=targets,
            profiles=profiles,
            suggested_activation=suggested_activation,
            minimum_concord_version=minimum,
            secret_group=secret_group,
            secrets=secrets,
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
