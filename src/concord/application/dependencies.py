import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from concord.application.config import (CONCORD_TARGET, CONCORD_VERSION,
                                        PROFILE_MINIMUM_VERSION, Config,
                                        ConfigManager, DependencyConfig)
from concord.application.database import Database
from concord.application.profile_manager import ProfileManager

PACKAGE_PATTERN = re.compile(r"^[A-Za-z0-9@._+:-]+$")
MANAGERS = ("pacman", "aur")
AUR_HELPERS = ("paru", "yay")


@dataclass(frozen=True)
class Dependency:
    target_id: str
    target_name: str
    package: str
    manager: str
    optional: bool = False


@dataclass(frozen=True)
class ResolvedDependency:
    package: str
    manager: str
    optional: bool
    targets: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DependencyStatus:
    dependency: ResolvedDependency
    installed: bool


@dataclass(frozen=True)
class InstallResult:
    installed: tuple[ResolvedDependency, ...]
    pending: tuple[ResolvedDependency, ...]


class DependencyInstallError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        installed: list[ResolvedDependency],
        pending: list[ResolvedDependency],
    ) -> None:
        super().__init__(message)
        self.installed = tuple(installed)
        self.pending = tuple(pending)


class DependencyManager:
    """Declara, resuelve y ejecuta dependencias de paquetes de Arch Linux."""

    def __init__(
        self,
        database: Database | None = None,
        config_manager: ConfigManager | None = None,
        *,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        self.database = database or Database()
        self.config_manager = config_manager or ConfigManager()
        self.runner = runner or subprocess.run
        self.which = which or shutil.which
        self.database.initialize()

    def _normalize_manager(self, manager: str) -> str:
        normalized = manager.strip().lower()
        if normalized not in MANAGERS:
            raise ValueError("El gestor debe ser 'pacman' o 'aur'.")
        return normalized

    def _normalize_package(self, package: str) -> str:
        normalized = package.strip()
        if not normalized or not PACKAGE_PATTERN.fullmatch(normalized) or normalized.startswith("-"):
            raise ValueError(f"Nombre de paquete no válido: '{package}'.")
        return normalized

    def _target(self, name: str) -> tuple[str, str]:
        normalized = name.strip().lower()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id, name FROM targets WHERE name = ?", (normalized,)
            ).fetchone()
        if row is None:
            raise KeyError(f"No existe un target llamado '{normalized}'.")
        if row[1] == CONCORD_TARGET:
            raise ValueError("El target interno 'concord' no admite dependencias de paquetes.")
        return row[0], row[1]

    def _run_query(self, command: list[str]) -> subprocess.CompletedProcess:
        return self.runner(command, text=True, capture_output=True, check=False)

    def configured_aur_helper(self) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value FROM local_settings WHERE key = 'aur_helper'"
            ).fetchone()
        return row[0] if row else None

    def available_aur_helpers(self) -> list[str]:
        return [helper for helper in AUR_HELPERS if self.which(helper) is not None]

    def set_aur_helper(self, helper: str) -> None:
        normalized = helper.strip().lower()
        if normalized not in AUR_HELPERS:
            raise ValueError("El helper AUR debe ser 'paru' o 'yay'.")
        if self.which(normalized) is None:
            raise FileNotFoundError(f"No se encontró el helper AUR '{normalized}'.")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO local_settings (key, value) VALUES ('aur_helper', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (normalized,),
            )

    def aur_helper(self) -> str:
        configured = self.configured_aur_helper()
        if configured and self.which(configured) is not None:
            return configured
        available = self.available_aur_helpers()
        if len(available) == 1 and configured is None:
            return available[0]
        if not available:
            raise FileNotFoundError(
                "No se encontró paru ni yay. Instale un helper AUR y vuelva a intentarlo."
            )
        if configured:
            raise FileNotFoundError(
                f"El helper AUR configurado '{configured}' no está disponible; "
                f"configure uno de estos: {', '.join(available)}."
            )
        raise ValueError(
            f"Hay varios helpers AUR disponibles ({', '.join(available)}); elija uno."
        )

    def _backend_command(
        self,
        manager: str,
        operation: str,
        packages: list[str],
        *,
        aur_helper: str | None = None,
    ) -> list[str]:
        if manager == "pacman":
            executable = "pacman"
        else:
            executable = aur_helper or self.aur_helper()
        if self.which(executable) is None:
            raise FileNotFoundError(f"No se encontró el backend '{executable}'.")
        if operation == "validate":
            return [executable, "-Si", *packages]
        if operation == "install":
            command = [executable, "-S", "--needed", "--", *packages]
            if manager == "pacman" and os.geteuid() != 0:
                if self.which("sudo") is None:
                    raise FileNotFoundError("pacman requiere privilegios y no se encontró sudo.")
                command.insert(0, "sudo")
            return command
        raise ValueError(f"Operación de backend no válida: {operation}.")

    def validate(self, manager: str, packages: list[str]) -> None:
        normalized_manager = self._normalize_manager(manager)
        normalized_packages = [self._normalize_package(package) for package in packages]
        if not normalized_packages:
            raise ValueError("Indique al menos un paquete.")
        command = self._backend_command(normalized_manager, "validate", normalized_packages)
        for package in normalized_packages:
            result = self._run_query([*command[:-len(normalized_packages)], package])
            if result.returncode != 0:
                raise ValueError(
                    f"El paquete '{package}' no existe en el origen '{normalized_manager}'."
                )

    def _save_manifest(self) -> None:
        config = self.config_manager.load()
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT targets.name, dependencies.package, dependencies.manager,
                       dependencies.optional
                FROM targets JOIN dependencies ON dependencies.target_id = targets.id
                ORDER BY targets.name, dependencies.manager, dependencies.optional,
                         dependencies.position
                """
            ).fetchall()
        grouped: dict[str, list[DependencyConfig]] = {}
        for target_name, package, manager, optional in rows:
            grouped.setdefault(target_name, []).append(
                DependencyConfig(package, manager, bool(optional))
            )
        config.targets = [
            type(target)(
                name=target.name,
                paths=target.paths,
                id=target.id,
                created_at=target.created_at,
                updated_at=target.updated_at,
                dependencies=grouped.get(target.name, []),
            )
            for target in config.targets
        ]
        if any(target.dependencies for target in config.targets):
            config.minimum_concord_version = CONCORD_VERSION
        else:
            config.minimum_concord_version = (
                PROFILE_MINIMUM_VERSION if config.profiles else None
            )
        self.config_manager.save(config)

    def matches_config(self, config: Config) -> bool:
        expected = {
            target.name: sorted(
                (item.package, item.manager, item.optional)
                for item in target.dependencies
            )
            for target in config.targets
        }
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT targets.name, dependencies.package, dependencies.manager,
                       dependencies.optional
                FROM targets LEFT JOIN dependencies
                    ON dependencies.target_id = targets.id
                ORDER BY targets.name, dependencies.package
                """
            ).fetchall()
        actual: dict[str, list[tuple[str, str, bool]]] = {}
        for target_name, package, manager, optional in rows:
            actual.setdefault(target_name, [])
            if package is not None:
                actual[target_name].append((package, manager, bool(optional)))
        return actual == expected

    def find(self, target: str, package: str) -> Dependency | None:
        target_id, target_name = self._target(target)
        normalized_package = self._normalize_package(package)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT manager, optional FROM dependencies
                WHERE target_id = ? AND package = ?
                """,
                (target_id, normalized_package),
            ).fetchone()
        if row is None:
            return None
        return Dependency(target_id, target_name, normalized_package, row[0], bool(row[1]))

    def add(
        self,
        target: str,
        manager: str,
        packages: list[str],
        *,
        optional: bool = False,
        validate: bool = True,
        reclassify: bool = False,
    ) -> list[Dependency]:
        target_id, target_name = self._target(target)
        normalized_manager = self._normalize_manager(manager)
        normalized_packages = list(dict.fromkeys(self._normalize_package(item) for item in packages))
        if not normalized_packages:
            raise ValueError("Indique al menos un paquete.")
        if validate:
            self.validate(normalized_manager, normalized_packages)
        with self.database.connect() as connection:
            placeholders = ",".join("?" for _ in normalized_packages)
            conflicts = connection.execute(
                f"""
                SELECT package, manager FROM dependencies
                WHERE package IN ({placeholders}) AND manager != ?
                """,
                (*normalized_packages, normalized_manager),
            ).fetchall()
            if conflicts:
                package, existing_manager = conflicts[0]
                raise ValueError(
                    f"'{package}' ya está declarado para '{existing_manager}' y no puede "
                    f"declararse también para '{normalized_manager}'."
                )
            existing = {
                package: bool(current_optional)
                for package, current_optional in connection.execute(
                    f"""
                    SELECT package, optional FROM dependencies
                    WHERE target_id = ? AND package IN ({placeholders})
                    """,
                    (target_id, *normalized_packages),
                )
            }
            mismatched = [
                package for package, current_optional in existing.items()
                if current_optional != optional
            ]
            if mismatched and not reclassify:
                raise ValueError(
                    f"'{mismatched[0]}' ya está declarado con otra categoría; "
                    "confirme su reclasificación."
                )
            duplicate = [
                package for package, current_optional in existing.items()
                if current_optional == optional
            ]
            if duplicate:
                raise ValueError(f"'{duplicate[0]}' ya está declarado en '{target_name}'.")
            next_position = connection.execute(
                """
                SELECT COALESCE(MAX(position), -1) + 1 FROM dependencies
                WHERE target_id = ? AND manager = ?
                """,
                (target_id, normalized_manager),
            ).fetchone()[0]
            for package in normalized_packages:
                if package in existing:
                    connection.execute(
                        "UPDATE dependencies SET optional = ? WHERE target_id = ? AND package = ?",
                        (int(optional), target_id, package),
                    )
                else:
                    connection.execute(
                        "INSERT INTO dependencies VALUES (?, ?, ?, ?, ?)",
                        (target_id, package, normalized_manager, int(optional), next_position),
                    )
                    next_position += 1
        self._save_manifest()
        return [
            Dependency(target_id, target_name, package, normalized_manager, optional)
            for package in normalized_packages
        ]

    def remove(self, target: str, packages: list[str]) -> list[Dependency]:
        target_id, target_name = self._target(target)
        normalized_packages = list(dict.fromkeys(self._normalize_package(item) for item in packages))
        if not normalized_packages:
            raise ValueError("Seleccione al menos una dependencia.")
        with self.database.connect() as connection:
            placeholders = ",".join("?" for _ in normalized_packages)
            rows = connection.execute(
                f"""
                SELECT package, manager, optional FROM dependencies
                WHERE target_id = ? AND package IN ({placeholders})
                """,
                (target_id, *normalized_packages),
            ).fetchall()
            found = {row[0] for row in rows}
            missing = [package for package in normalized_packages if package not in found]
            if missing:
                raise KeyError(
                    f"'{missing[0]}' no está declarado en el target '{target_name}'."
                )
            connection.execute(
                f"DELETE FROM dependencies WHERE target_id = ? AND package IN ({placeholders})",
                (target_id, *normalized_packages),
            )
            for manager in MANAGERS:
                remaining = connection.execute(
                    """
                    SELECT package FROM dependencies WHERE target_id = ? AND manager = ?
                    ORDER BY position
                    """,
                    (target_id, manager),
                ).fetchall()
                for position, (package,) in enumerate(remaining):
                    connection.execute(
                        "UPDATE dependencies SET position = ? WHERE target_id = ? AND package = ?",
                        (position, target_id, package),
                    )
        self._save_manifest()
        return [
            Dependency(target_id, target_name, package, manager, bool(optional))
            for package, manager, optional in rows
        ]

    def for_targets(self, target_names: list[str]) -> list[ResolvedDependency]:
        if not target_names:
            return []
        normalized = [name.strip().lower() for name in target_names]
        placeholders = ",".join("?" for _ in normalized)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT targets.name, dependencies.package, dependencies.manager,
                       dependencies.optional
                FROM dependencies JOIN targets ON targets.id = dependencies.target_id
                WHERE targets.name IN ({placeholders})
                ORDER BY dependencies.manager, dependencies.package, targets.name
                """,
                normalized,
            ).fetchall()
        grouped: dict[str, dict[str, object]] = {}
        for target_name, package, manager, optional in rows:
            current = grouped.get(package)
            if current is not None and current["manager"] != manager:
                raise ValueError(
                    f"'{package}' está declarado para pacman y AUR; corrija el conflicto."
                )
            if current is None:
                current = {"manager": manager, "optional": bool(optional), "targets": []}
                grouped[package] = current
            current["optional"] = bool(current["optional"]) and bool(optional)
            current["targets"].append(target_name)
        return [
            ResolvedDependency(
                package=package,
                manager=str(value["manager"]),
                optional=bool(value["optional"]),
                targets=tuple(value["targets"]),
            )
            for package, value in sorted(
                grouped.items(), key=lambda item: (str(item[1]["manager"]), item[0])
            )
        ]

    def for_target(self, target: str) -> list[ResolvedDependency]:
        _, target_name = self._target(target)
        return self.for_targets([target_name])

    def for_profile(self, profile: str) -> list[ResolvedDependency]:
        resolution = ProfileManager(self.database, self.config_manager).resolve(profile)
        return self.for_targets(resolution.target_names)

    def status(self, dependencies: list[ResolvedDependency]) -> list[DependencyStatus]:
        if dependencies and self.which("pacman") is None:
            raise FileNotFoundError("No se encontró pacman; esta versión de dependencias requiere Arch Linux.")
        if not dependencies:
            return []
        query = self._run_query(
            ["pacman", "-T", *[dependency.package for dependency in dependencies]]
        )
        if query.returncode not in {0, 127}:
            detail = query.stderr.strip() if query.stderr else "consulta fallida"
            raise ValueError(f"No se pudo consultar pacman: {detail}.")
        missing = set(query.stdout.splitlines())
        return [
            DependencyStatus(dependency, dependency.package not in missing)
            for dependency in dependencies
        ]

    def install_commands(
        self,
        dependencies: list[ResolvedDependency],
        *,
        aur_helper: str | None = None,
    ) -> list[tuple[str, list[str], list[ResolvedDependency]]]:
        commands = []
        for manager in MANAGERS:
            selected = [dependency for dependency in dependencies if dependency.manager == manager]
            if selected:
                commands.append(
                    (
                        manager,
                        self._backend_command(
                            manager,
                            "install",
                            [dependency.package for dependency in selected],
                            aur_helper=aur_helper,
                        ),
                        selected,
                    )
                )
        return commands

    def install(self, dependencies: list[ResolvedDependency]) -> InstallResult:
        commands = self.install_commands(dependencies)
        installed: list[ResolvedDependency] = []
        for index, (manager, command, selected) in enumerate(commands):
            result = self.runner(command, text=True, check=False)
            if result.returncode != 0:
                pending = [
                    dependency
                    for _, _, batch in commands[index:]
                    for dependency in batch
                ]
                raise DependencyInstallError(
                    f"La instalación con '{manager}' falló con código {result.returncode}.",
                    installed=installed,
                    pending=pending,
                )
            installed.extend(selected)
        return InstallResult(tuple(installed), tuple())
