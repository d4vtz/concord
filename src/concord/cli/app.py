import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import questionary
import typer
from rich import box
from rich.syntax import Syntax
from rich.table import Table
from rich.tree import Tree

from concord import application as concord
from concord.application.config import CONCORD_TARGET, ConfigManager, GitConfig
from concord.application.dependencies import (AUR_HELPERS, MANAGERS,
                                              DependencyInstallError,
                                              DependencyManager,
                                              DependencyStatus,
                                              ResolvedDependency)
from concord.application.doctor import Doctor
from concord.application.git import GitCommit, GitManager
from concord.application.initializer import Initializer
from concord.application.profile_manager import Activation, ProfileManager
from concord.application.reset import ResetManager
from concord.application.target_manager import TargetManager
from concord.cli.completion import (complete_editables,
                                    complete_removable_targets,
                                    complete_target_paths, complete_targets)
from concord.cli.ui import console, details, execute, heading, success, warning

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="[bold #88C0D0]Concord[/] — gestiona y respalda tus dotfiles.",
)
repo_app = typer.Typer(no_args_is_help=True, help="Administra el repositorio Git de Concord.")
remote_app = typer.Typer(no_args_is_help=False, help="Consulta o configura el remoto Git.")
profile_app = typer.Typer(no_args_is_help=True, help="Crea, compone y activa perfiles de targets.")
deps_app = typer.Typer(no_args_is_help=True, help="Administra paquetes requeridos por targets.")
profile_deps_app = typer.Typer(
    no_args_is_help=True, help="Consulta e instala paquetes agregados de un perfil."
)
app.add_typer(repo_app, name="repo")
app.add_typer(profile_app, name="profile")
app.add_typer(deps_app, name="deps")
repo_app.add_typer(remote_app, name="remote")
profile_app.add_typer(profile_deps_app, name="deps")


def manager(*, check_manifest: bool = True) -> TargetManager:
    if not concord.is_initialized():
        raise ValueError("Concord todavía no está inicializado.")
    target_manager = TargetManager()
    if check_manifest and (
        target_manager.profile_manifest_changed
        or target_manager.dependency_manifest_changed
    ):
        if not sys.stdin.isatty():
            raise ValueError(
                "El manifiesto contiene cambios externos en perfiles o dependencias. "
                "Revísalos y ejecuta concord import --replace."
            )
        if not questionary.confirm(
            "El manifiesto contiene perfiles o dependencias diferentes. "
            "¿Reconstruir el índice local?",
            default=False,
        ).ask():
            raise ValueError(
                "Importación cancelada; no se modificó SQLite ni el manifiesto."
            )
        target_manager.import_manifest(replace=True)
        success("Los perfiles del manifiesto reemplazaron las definiciones locales.")
    if target_manager.manifest_backup:
        config = ConfigManager().load()
        if config.git.enabled and config.git.auto_commit:
            git = GitManager(config.repository_path)
            if not git.initialized:
                git.initialize()
            if all(git.identity()):
                commit = git.commit(git_paths(CONCORD_TARGET), "concord: migrate manifest v2")
                if commit and config.git.auto_push:
                    try:
                        push_with_secret_check(git, config.git.remote)
                    except (FileNotFoundError, ValueError) as error:
                        warning(str(error))
                        warning("La migración quedó confirmada localmente; reintenta con concord repo push.")
            else:
                warning("La migración quedó pendiente de commit porque Git no tiene identidad.")
        success(
            "Manifiesto migrado a la versión 2.",
            hint=f"Respaldo: {target_manager.manifest_backup}",
        )
    return target_manager


def profiles(target_manager: TargetManager | None = None) -> ProfileManager:
    target_manager = target_manager or manager()
    return ProfileManager(target_manager.database, target_manager.config_manager)


def dependencies(target_manager: TargetManager | None = None) -> DependencyManager:
    target_manager = target_manager or manager()
    return DependencyManager(target_manager.database, target_manager.config_manager)


def render_activation(profile_manager: ProfileManager) -> None:
    activation = profile_manager.activation()
    if activation is None:
        details([("Perfiles", "ninguno — se usan todos los targets")], title="Selección activa")
        return
    details(
        [
            ("Principal", activation.primary),
            ("Complementos", ", ".join(activation.complements) or "ninguno"),
        ],
        title="Selección activa",
    )
    resolution = profile_manager.resolve_active()
    if resolution:
        for message in resolution.warnings:
            warning(message)


def maybe_offer_suggestion(profile_manager: ProfileManager) -> None:
    if not sys.stdin.isatty() or not profile_manager.should_offer_suggestion():
        return
    suggestion = profile_manager.suggestion()
    if suggestion is None:
        return
    label = suggestion.primary
    if suggestion.complements:
        label += f" + {', '.join(suggestion.complements)}"
    if questionary.confirm(
        f"El manifiesto sugiere activar {label}. ¿Deseas adoptarlo?", default=True
    ).ask():
        profile_manager.activate(suggestion.primary, suggestion.complements)
        success("Se adoptó la activación sugerida para este equipo.")
    else:
        profile_manager.decline_suggestion()


def persist_profile_manifest(target_manager: TargetManager, message: str) -> None:
    changed = target_manager.sync(CONCORD_TARGET)
    if changed:
        finalize_git(
            git_paths(CONCORD_TARGET),
            message,
            GitOptions(message=None, yes=False, commit=True, push=None),
        )


def format_date(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def relative_to_home(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(Path.home().resolve())
    except ValueError as error:
        raise ValueError("La ruta debe estar dentro de HOME.") from error


def format_home_path(path: Path) -> str:
    return str(Path("~") / relative_to_home(path))


@dataclass(frozen=True)
class GitOptions:
    message: str | None
    yes: bool
    commit: bool
    push: bool | None


def request_text(message: str, default: str = "") -> str:
    answer = questionary.text(message, default=default).ask()
    if answer is None:
        raise KeyboardInterrupt
    return answer.strip()


def request_checkbox(
    message: str,
    choices: list[str],
    *,
    checked: list[str] | None = None,
) -> list[str]:
    """Solicita varias opciones sin invocar Questionary con una lista vacía."""
    if not choices:
        return []
    selected = set(checked or [])
    answer = questionary.checkbox(
        message,
        choices=[
            questionary.Choice(item, checked=item in selected) for item in choices
        ],
    ).ask()
    if answer is None:
        raise KeyboardInterrupt
    return answer


def request_order(choices: list[str]) -> list[str]:
    """Permite ordenar una selección eligiendo cada capa sucesivamente."""
    remaining = list(choices)
    ordered: list[str] = []
    while len(remaining) > 1:
        selected = questionary.select(
            f"Siguiente complemento ({len(ordered) + 1}/{len(choices)}):",
            choices=remaining,
        ).ask()
        if selected is None:
            raise KeyboardInterrupt
        ordered.append(selected)
        remaining.remove(selected)
    return [*ordered, *remaining]


def request_select(message: str, choices: list[str]) -> str:
    if not choices:
        raise ValueError("No hay opciones disponibles.")
    answer = questionary.select(message, choices=choices).ask()
    if answer is None:
        raise KeyboardInterrupt
    return answer


def choose_dependency_target(target_manager: TargetManager) -> str:
    available = [
        target.name for target in target_manager.list() if target.name != CONCORD_TARGET
    ]
    if not available:
        raise ValueError("No hay targets disponibles para declarar dependencias.")
    if not sys.stdin.isatty():
        raise ValueError("Indique el target en modo no interactivo.")
    return request_select("Target:", available)


def choose_dependency_profile(profile_manager: ProfileManager) -> str:
    available = [profile.name for profile in profile_manager.list()]
    if not available:
        raise ValueError("No hay perfiles disponibles.")
    if not sys.stdin.isatty():
        raise ValueError("Indique el perfil en modo no interactivo.")
    return request_select("Perfil:", available)


def resolve_aur_helper(
    dependency_manager: DependencyManager, *, persist: bool = True
) -> str:
    configured = dependency_manager.configured_aur_helper()
    available = dependency_manager.available_aur_helpers()
    if configured in available:
        return configured
    if not available:
        raise FileNotFoundError(
            "No se encontró paru ni yay. Instale uno de estos helpers AUR y repita la operación."
        )
    if not sys.stdin.isatty():
        if configured:
            raise FileNotFoundError(
                f"El helper configurado '{configured}' no está disponible. "
                "Ejecute concord deps helper en una terminal."
            )
        if len(available) == 1:
            if persist:
                dependency_manager.set_aur_helper(available[0])
            return available[0]
        raise ValueError("Configure el helper AUR con concord deps helper paru|yay.")
    selected = available[0] if len(available) == 1 else request_select(
        "Helper AUR:", available
    )
    if configured and persist and not questionary.confirm(
        f"'{configured}' no está disponible. ¿Cambiar la preferencia local a '{selected}'?",
        default=True,
    ).ask():
        raise ValueError("No se cambió el helper AUR configurado.")
    if persist:
        dependency_manager.set_aur_helper(selected)
    return selected


def render_dependencies(
    dependencies_to_render: list[ResolvedDependency],
    *,
    statuses: list[DependencyStatus] | None = None,
    title: str = "Dependencias",
) -> None:
    status_by_package = {
        status.dependency.package: status.installed for status in statuses or []
    }
    table = Table(box=box.ROUNDED, border_style="#4C566A", title=title)
    table.add_column("Gestor", style="concord.accent", no_wrap=True)
    table.add_column("Tipo", no_wrap=True)
    table.add_column("Paquete", style="concord.path")
    table.add_column("Targets", style="concord.muted")
    if statuses is not None:
        table.add_column("Estado", no_wrap=True)
    for dependency in dependencies_to_render:
        row = [
            dependency.manager,
            "Opcional" if dependency.optional else "Obligatoria",
            dependency.package,
            ", ".join(dependency.targets),
        ]
        if statuses is not None:
            row.append(
                "[concord.success]Instalado[/]"
                if status_by_package[dependency.package]
                else (
                    "[concord.warning]Faltante opcional[/]"
                    if dependency.optional
                    else "[concord.error]Faltante[/]"
                )
            )
        table.add_row(*row)
    console.print(table)


def dependency_scope(
    target_manager: TargetManager,
    *,
    target: str | None = None,
    profile: str | None = None,
) -> tuple[str, list[ResolvedDependency], DependencyManager]:
    dependency_manager = dependencies(target_manager)
    if profile is not None:
        name = profile or choose_dependency_profile(profiles(target_manager))
        return name, dependency_manager.for_profile(name), dependency_manager
    name = target or choose_dependency_target(target_manager)
    return name, dependency_manager.for_target(name), dependency_manager


def dependency_list_command(*, target: str | None = None, profile: str | None = None) -> None:
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    name, declared, _ = execute(
        lambda: dependency_scope(target_manager, target=target, profile=profile)
    )
    if not declared:
        warning(f"'{name}' no declara dependencias de paquetes.")
        return
    render_dependencies(declared, title=f"Dependencias de {name}")
    details(
        [
            ("Obligatorias", str(sum(not item.optional for item in declared))),
            ("Opcionales", str(sum(item.optional for item in declared))),
        ],
        title="Resumen",
    )


def dependency_check_command(*, target: str | None = None, profile: str | None = None) -> None:
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    name, declared, dependency_manager = execute(
        lambda: dependency_scope(target_manager, target=target, profile=profile)
    )
    if not declared:
        success(f"'{name}' no declara dependencias de paquetes.")
        return
    statuses = execute(lambda: dependency_manager.status(declared))
    render_dependencies(declared, statuses=statuses, title=f"Comprobación de {name}")
    missing_required = [
        status for status in statuses
        if not status.installed and not status.dependency.optional
    ]
    missing_optional = [
        status for status in statuses
        if not status.installed and status.dependency.optional
    ]
    details(
        [
            ("Instaladas", str(sum(status.installed for status in statuses))),
            ("Faltantes obligatorias", str(len(missing_required))),
            ("Faltantes opcionales", str(len(missing_optional))),
        ],
        title="Resumen",
    )
    if missing_required:
        raise typer.Exit(1)
    success("Todas las dependencias obligatorias están instaladas.")


def dependency_install_command(
    *,
    target: str | None = None,
    profile: str | None = None,
    include_optional: bool = False,
    dry_run: bool = False,
    yes: bool = False,
) -> None:
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    name, declared, dependency_manager = execute(
        lambda: dependency_scope(target_manager, target=target, profile=profile)
    )
    if not declared:
        success(f"'{name}' no declara dependencias de paquetes.")
        return
    statuses = execute(lambda: dependency_manager.status(declared))
    missing_required = [
        status.dependency for status in statuses
        if not status.installed and not status.dependency.optional
    ]
    missing_optional = [
        status.dependency for status in statuses
        if not status.installed and status.dependency.optional
    ]
    selected_optional: list[ResolvedDependency] = []
    if include_optional:
        selected_optional = missing_optional
    elif missing_optional and sys.stdin.isatty() and not yes:
        selected_names = request_checkbox(
            "Dependencias opcionales que deseas instalar:",
            [dependency.package for dependency in missing_optional],
        )
        selected_optional = [
            dependency for dependency in missing_optional
            if dependency.package in selected_names
        ]
    selected = [*missing_required, *selected_optional]
    if not selected:
        success("No hay dependencias seleccionadas pendientes de instalación.")
        return
    aur_helper = None
    if any(dependency.manager == "aur" for dependency in selected):
        aur_helper = execute(
            lambda: resolve_aur_helper(dependency_manager, persist=not dry_run)
        )
    commands = execute(
        lambda: dependency_manager.install_commands(
            selected, aur_helper=aur_helper
        )
    )
    render_dependencies(selected, title=f"Plan de instalación para {name}")
    for backend, command, batch in commands:
        details(
            [
                ("Backend", backend),
                ("Paquetes", ", ".join(item.package for item in batch)),
                ("Comando", shlex.join(command)),
            ],
            title="Ejecución prevista",
        )
    if dry_run:
        warning("Simulación completada; no se instaló ningún paquete.")
        return
    if not yes:
        if not sys.stdin.isatty():
            execute(lambda: (_ for _ in ()).throw(
                ValueError("La instalación no interactiva requiere --yes.")
            ))
        if not questionary.confirm("¿Instalar estos paquetes?", default=False).ask():
            warning("Instalación cancelada; no se modificó el sistema.")
            return
    try:
        result = dependency_manager.install(selected)
    except DependencyInstallError as error:
        details(
            [
                ("Instalados", ", ".join(item.package for item in error.installed) or "ninguno"),
                ("Pendientes", ", ".join(item.package for item in error.pending) or "ninguno"),
            ],
            title="Instalación parcial",
        )
        execute(lambda: (_ for _ in ()).throw(error))
        return
    success(f"Se instalaron {len(result.installed)} dependencias.")


@deps_app.command("add")
def deps_add(
    target: str | None = typer.Argument(None, autocompletion=complete_targets),
    packages: list[str] | None = typer.Argument(None),
    package_manager: str | None = typer.Option(None, "--manager", "-m"),
    optional: bool | None = typer.Option(None, "--optional/--required"),
    skip_validation: bool = typer.Option(
        False, "--skip-validation", help="Guarda los nombres sin consultar su origen."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirma reclasificaciones."),
) -> None:
    """Declara paquetes necesarios para un target."""
    heading("AGREGAR DEPENDENCIAS", "Asociando paquetes a una configuración")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    dependency_manager = dependencies(target_manager)
    if target is None:
        target = execute(lambda: choose_dependency_target(target_manager))
    if package_manager is None:
        if not sys.stdin.isatty():
            execute(lambda: (_ for _ in ()).throw(
                ValueError("Indique --manager pacman|aur en modo no interactivo.")
            ))
        package_manager = request_select("Origen de los paquetes:", list(MANAGERS))
    if optional is None:
        if not sys.stdin.isatty():
            execute(lambda: (_ for _ in ()).throw(
                ValueError("Indique --required o --optional en modo no interactivo.")
            ))
        category = request_select("Categoría:", ["Obligatorias", "Opcionales"])
        optional = category == "Opcionales"
    selected_packages = list(packages or [])
    if not selected_packages:
        if not sys.stdin.isatty():
            execute(lambda: (_ for _ in ()).throw(
                ValueError("Indique al menos un paquete en modo no interactivo.")
            ))
        selected_packages = shlex.split(request_text("Paquetes separados por espacios:"))
    normalized_manager = package_manager.strip().lower()
    if normalized_manager == "aur" and not skip_validation:
        execute(lambda: resolve_aur_helper(dependency_manager))
    reclassifications = []
    for package in selected_packages:
        current = execute(lambda package=package: dependency_manager.find(target, package))
        if current and current.manager == normalized_manager and current.optional != optional:
            reclassifications.append(current.package)
    reclassify = False
    if reclassifications:
        if yes:
            reclassify = True
        elif not sys.stdin.isatty():
            execute(lambda: (_ for _ in ()).throw(
                ValueError("La reclasificación requiere --yes en modo no interactivo.")
            ))
        else:
            destination = "opcionales" if optional else "obligatorias"
            reclassify = bool(questionary.confirm(
                f"¿Mover {', '.join(reclassifications)} a dependencias {destination}?",
                default=False,
            ).ask())
            if not reclassify:
                warning("No se modificaron las dependencias.")
                return
    added = execute(
        lambda: dependency_manager.add(
            target,
            normalized_manager,
            selected_packages,
            optional=bool(optional),
            validate=not skip_validation,
            reclassify=reclassify,
        )
    )
    execute(
        lambda: persist_profile_manifest(
            target_manager, f"concord: update dependencies for {target}"
        )
    )
    success(
        f"Se registraron {len(added)} dependencias para '{target}'.",
        hint=f"Compruébalas con: concord deps check {target}",
    )


@deps_app.command("remove")
def deps_remove(
    target: str | None = typer.Argument(None, autocompletion=complete_targets),
    packages: list[str] | None = typer.Argument(None),
) -> None:
    """Retira declaraciones sin desinstalar paquetes."""
    heading("RETIRAR DEPENDENCIAS", "Actualizando el manifiesto sin tocar el sistema")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    dependency_manager = dependencies(target_manager)
    if target is None:
        target = execute(lambda: choose_dependency_target(target_manager))
    selected_packages = list(packages or [])
    if not selected_packages:
        declared = execute(lambda: dependency_manager.for_target(target))
        if not declared:
            warning(f"'{target}' no declara dependencias.")
            return
        if not sys.stdin.isatty():
            execute(lambda: (_ for _ in ()).throw(
                ValueError("Indique los paquetes que desea retirar.")
            ))
        selected_packages = request_checkbox(
            "Dependencias que deseas retirar:",
            [dependency.package for dependency in declared],
        )
        if not selected_packages:
            warning("No se seleccionaron dependencias.")
            return
    removed = execute(lambda: dependency_manager.remove(target, selected_packages))
    execute(
        lambda: persist_profile_manifest(
            target_manager, f"concord: update dependencies for {target}"
        )
    )
    success(
        f"Se retiraron {len(removed)} declaraciones; no se desinstaló ningún paquete."
    )


@deps_app.command("list")
def deps_list(
    target: str | None = typer.Argument(None, autocompletion=complete_targets),
) -> None:
    """Lista las dependencias declaradas por un target."""
    heading("DEPENDENCIAS", "Paquetes declarados por target")
    dependency_list_command(target=target)


@deps_app.command("check")
def deps_check(
    target: str | None = typer.Argument(None, autocompletion=complete_targets),
) -> None:
    """Comprueba qué dependencias de un target están instaladas."""
    heading("COMPROBAR DEPENDENCIAS", "Consultando el sistema sin modificarlo")
    dependency_check_command(target=target)


@deps_app.command("install")
def deps_install(
    target: str | None = typer.Argument(None, autocompletion=complete_targets),
    include_optional: bool = typer.Option(False, "--include-optional"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Instala únicamente los paquetes faltantes de un target."""
    heading("INSTALAR DEPENDENCIAS", "Preparando un plan seguro por backend")
    dependency_install_command(
        target=target,
        include_optional=include_optional,
        dry_run=dry_run,
        yes=yes,
    )


@deps_app.command("helper")
def deps_helper(
    helper: str | None = typer.Argument(None),
) -> None:
    """Consulta o selecciona el helper AUR local."""
    heading("HELPER AUR", "Configuración local de paru o yay")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    dependency_manager = dependencies(target_manager)
    if helper is None:
        helper = execute(lambda: resolve_aur_helper(dependency_manager))
    else:
        execute(lambda: dependency_manager.set_aur_helper(helper))
    success(f"El helper AUR preferido en esta máquina es '{helper}'.")


@profile_deps_app.command("list")
def profile_deps_list(name: str | None = typer.Argument(None)) -> None:
    """Lista las dependencias agregadas de un perfil."""
    heading("DEPENDENCIAS DEL PERFIL", "Expandiendo composición y exclusiones")
    if name is None:
        target_manager = execute(manager, hint="Ejecuta primero: concord init")
        name = execute(lambda: choose_dependency_profile(profiles(target_manager)))
    dependency_list_command(profile=name)


@profile_deps_app.command("check")
def profile_deps_check(name: str | None = typer.Argument(None)) -> None:
    """Comprueba las dependencias agregadas de un perfil."""
    heading("COMPROBAR PERFIL", "Consultando paquetes de todos sus targets efectivos")
    if name is None:
        target_manager = execute(manager, hint="Ejecuta primero: concord init")
        name = execute(lambda: choose_dependency_profile(profiles(target_manager)))
    dependency_check_command(profile=name)


@profile_deps_app.command("install")
def profile_deps_install(
    name: str | None = typer.Argument(None),
    include_optional: bool = typer.Option(False, "--include-optional"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Instala las dependencias agregadas de un perfil."""
    heading("INSTALAR PERFIL", "Preparando paquetes de sus targets efectivos")
    if name is None:
        target_manager = execute(manager, hint="Ejecuta primero: concord init")
        name = execute(lambda: choose_dependency_profile(profiles(target_manager)))
    dependency_install_command(
        profile=name,
        include_optional=include_optional,
        dry_run=dry_run,
        yes=yes,
    )


def request_commit_message(default: str, options: GitOptions) -> str | None:
    if options.message:
        return options.message
    if options.yes or not sys.stdin.isatty():
        return default
    answer = questionary.text("Mensaje del commit:", default=default).ask()
    return answer.strip() if answer is not None else None


def git_paths(*names: str) -> list[Path]:
    return sorted({Path(name) for name in (*names, "concord")}, key=str)


def sync_commit_message(target_names: list[str]) -> str:
    if not target_names:
        raise ValueError("No hay targets modificados para crear el mensaje del commit.")
    if len(target_names) == 1:
        return f"{target_names[0]}: sync target"
    return "concord: sync all targets"


def editor_command() -> list[str]:
    configured = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if configured:
        command = shlex.split(configured)
        if not command:
            raise ValueError("La variable del editor está vacía.")
        if shutil.which(command[0]) is None:
            raise FileNotFoundError(f"No se encontró el editor configurado: {command[0]}")
        return command
    for candidate in ("nvim", "vim", "vi", "nano"):
        if shutil.which(candidate):
            return [candidate]
    raise FileNotFoundError(
        "No se encontró un editor. Define VISUAL o EDITOR, por ejemplo: export EDITOR=nvim"
    )


def open_in_editor(path: Path) -> int:
    path = path.expanduser().resolve()
    cwd = path if path.is_dir() else path.parent
    argument = "." if path.is_dir() else path.name
    return subprocess.run([*editor_command(), argument], cwd=cwd, check=False).returncode


def finalize_git(
    paths: list[Path],
    default_message: str,
    options: GitOptions,
) -> GitCommit | None:
    config = ConfigManager().load()
    if not config.git.enabled or not config.git.auto_commit or not options.commit:
        warning("Los cambios quedaron en el repositorio sin commit.")
        return None
    git = GitManager(config.repository_path)
    if not git.initialized:
        git.initialize()
    if not all(git.identity()):
        warning("Los cambios quedaron sin commit porque Git no tiene identidad configurada.")
        console.print("  [concord.muted]Configúrala con: concord repo init[/]")
        return None
    if not git.changed(paths):
        console.print("[concord.muted]Git: no hay cambios nuevos para confirmar.[/]")
        return None
    message = request_commit_message(default_message, options)
    if message is None:
        warning("Commit cancelado; los cambios permanecen pendientes en Git.")
        return None
    commit = git.commit(paths, message)
    if commit:
        success(f"Commit creado: {commit.sha}  {commit.message}")
    should_push = config.git.auto_push if options.push is None else options.push
    if commit and should_push:
        push_with_secret_check(git, config.git.remote, assume_yes=options.yes)
    return commit


def push_with_secret_check(
    git: GitManager,
    remote: str,
    *,
    assume_yes: bool = False,
) -> None:
    first_push = git.status(remote=remote).upstream is None
    sensitive = git.sensitive_files() if first_push else []
    if sensitive:
        warning("Se detectaron archivos que podrían contener secretos:")
        for path in sensitive:
            console.print(f"  [concord.path]{path.as_posix()}[/]")
        if not sys.stdin.isatty():
            raise ValueError("Push bloqueado: revise los archivos sensibles y ejecute concord repo push.")
        if not assume_yes and not questionary.confirm("¿Deseas continuar con el push?", default=False).ask():
            warning("Push cancelado; el commit permanece guardado localmente.")
            return
    git.push(remote)
    success(f"Cambios enviados a {remote}/{git.status(remote=remote).branch}.")


def git_command_options(
    message: str | None,
    yes: bool,
    no_commit: bool,
    push: bool | None,
) -> GitOptions:
    return GitOptions(message=message, yes=yes, commit=not no_commit, push=push)


def render_differences(differences, *, command: str) -> None:
    changed = 0
    totals = {"added": 0, "modified": 0, "deleted": 0}
    labels = {
        "added": ("+ Agregado", "#A3BE8C"),
        "modified": ("● Modificado", "#EBCB8B"),
        "deleted": ("− Eliminado", "#BF616A"),
    }
    for target_diff in differences:
        if target_diff.clean:
            console.print(
                f"[concord.success]✓[/] [bold]{target_diff.name}[/]  "
                "[concord.muted]sin cambios[/]"
            )
            continue
        changed += 1
        table = Table(
            box=box.ROUNDED,
            border_style="#4C566A",
            header_style="bold #88C0D0",
            title=target_diff.name,
        )
        table.add_column("Cambio", no_wrap=True)
        table.add_column("Ruta relativa a HOME", style="concord.path")
        for entry in target_diff.entries:
            label, color = labels[entry.state]
            totals[entry.state] += 1
            table.add_row(f"[{color}]{label}[/]", entry.relative_path.as_posix())
        console.print(table)
    total_changes = sum(totals.values())
    if total_changes == 0:
        success("No hay cambios que aplicar.")
        return
    details(
        [
            ("Targets con cambios", str(changed)),
            ("Agregados", str(totals["added"])),
            ("Modificados", str(totals["modified"])),
            ("Eliminados", str(totals["deleted"])),
        ],
        title="Resumen",
    )
    warning("Esta es una simulación; no se modificó ningún archivo ni metadato.")
    console.print(f"  [concord.muted]Para aplicar los cambios:[/] {command}")


def render_content_differences(name: str, differences, *, path: Path | None = None) -> None:
    if not differences:
        scope = f" en '{path}'" if path is not None else ""
        success(f"'{name}' no tiene cambios locales{scope}.")
        return
    labels = {
        "added": ("+ Agregado", "#A3BE8C"),
        "modified": ("● Modificado", "#EBCB8B"),
        "deleted": ("− Eliminado", "#BF616A"),
    }
    for difference in differences:
        label, color = labels[difference.state]
        console.print(
            f"\n[{color}]{label}[/]  [bold concord.path]{format_home_path(Path.home() / difference.relative_path)}[/]"
        )
        if difference.kind == "text" and difference.content:
            console.print(Syntax(difference.content.rstrip("\n"), "diff", theme="nord", word_wrap=False))
        elif difference.detail:
            console.print(difference.detail, style="concord.muted", markup=False)
    details(
        [
            ("Target", name),
            ("Archivos con cambios", str(len(differences))),
        ],
        title="Resumen",
    )
    warning("Comparación de solo lectura; no se modificó ningún archivo ni metadato.")


@app.command()
def init(
    repository: Path | None = typer.Option(
        None,
        "--repository",
        "-r",
        help="Repositorio existente o ruta donde se creará uno nuevo.",
    ),
) -> None:
    """Prepara la configuración, el repositorio y la base de datos."""
    heading("INICIALIZACIÓN", "Preparando el espacio de trabajo de tus dotfiles")
    if concord.is_initialized():
        warning("Concord ya estaba inicializado; no se modificó la configuración.")
        config = ConfigManager().load()
        details(
            [
                ("Configuración", str(concord.config_file)),
                ("Base de datos", str(concord.database_file)),
                ("Repositorio", str(config.repository_path)),
            ],
            title="Rutas activas",
        )
        return

    interactive = sys.stdin.isatty()
    if repository is None:
        repository = ConfigManager().request_repository_path()
    repository = repository.expanduser().resolve()
    git_config = GitConfig(enabled=True, auto_commit=True, auto_push=False)
    create_remote = False
    github_name = "dotfiles"
    github_private = True
    identity: tuple[str, str] | None = None
    commit_message = "concord: initialize repository"

    if interactive:
        enabled = bool(questionary.confirm("¿Inicializar Git?", default=True).ask())
        auto_commit = enabled and bool(
            questionary.confirm("¿Crear commits automáticos?", default=True).ask()
        )
        auto_push = auto_commit and bool(
            questionary.confirm("¿Enviar commits automáticamente?", default=True).ask()
        )
        git_config = GitConfig(enabled=enabled, auto_commit=auto_commit, auto_push=auto_push)
        if enabled:
            name_result = subprocess.run(
                ["git", "config", "--global", "user.name"], text=True, capture_output=True, check=False
            ) if shutil.which("git") else None
            email_result = subprocess.run(
                ["git", "config", "--global", "user.email"], text=True, capture_output=True, check=False
            ) if shutil.which("git") else None
            git_name = name_result.stdout.strip() if name_result and name_result.returncode == 0 else ""
            git_email = email_result.stdout.strip() if email_result and email_result.returncode == 0 else ""
            if not git_name:
                git_name = request_text("Nombre para los commits:")
            if not git_email:
                git_email = request_text("Correo para los commits:")
            identity = (git_name, git_email)
            if auto_commit:
                commit_message = request_text("Mensaje del commit inicial:", commit_message)
            create_remote = bool(
                questionary.confirm("¿Crear ahora el repositorio remoto en GitHub?", default=True).ask()
            )
            if create_remote:
                github_name = request_text("Nombre del repositorio de GitHub:", "dotfiles")
                visibility = questionary.select(
                    "Visibilidad del repositorio:", choices=["Privado", "Público"], default="Privado"
                ).ask()
                if visibility is None:
                    raise KeyboardInterrupt
                github_private = visibility == "Privado"

    requested_git = git_config.enabled
    execute(
        lambda: Initializer().initialize(
            repository,
            git_config=git_config,
            git_identity=identity,
            commit_message=commit_message,
        )
    )
    config = ConfigManager().load()
    git = GitManager(config.repository_path)
    if requested_git and not config.git.enabled:
        warning("Git no está instalado; Concord continuará administrando archivos sin commits.")
    if config.git.enabled:
        if not all(git.identity()):
            warning("Git no tiene identidad; los cambios iniciales quedaron sin commit.")
            console.print("  [concord.muted]Configúrala con concord repo init.[/]")
        if create_remote:
            try:
                url = git.create_github_repository(github_name, private=github_private)
                success(f"Repositorio remoto creado: {url}")
            except (FileNotFoundError, ValueError) as error:
                warning(str(error))
                console.print("  [concord.muted]La instalación local está completa; conecta después con concord repo init.[/]")
        if config.git.auto_push and git.has_remote(config.git.remote):
            try:
                push_with_secret_check(git, config.git.remote)
            except (FileNotFoundError, ValueError) as error:
                warning(str(error))
                console.print("  [concord.muted]El commit local se conservó. Reintenta con: concord repo push[/]")
        elif config.git.auto_push:
            warning("auto_push está activo, pero todavía no existe un remoto configurado.")
            console.print("  [concord.muted]Conéctalo con: concord repo remote set <URL>[/]")
    details(
        [
            ("Configuración", str(concord.config_file)),
            ("Base de datos", str(concord.database_file)),
            ("Repositorio", str(config.repository_path)),
        ],
        title="Rutas activas",
    )
    success("Concord está listo para usarse.", hint="Agrega tu primer target con: concord add <ruta>")


@app.command()
def add(
    path: Path,
    name: str | None = typer.Option(None, "--name", "-n", help="Nombre único del target."),
    profile: list[str] | None = typer.Option(
        None, "--profile", help="Perfil al que se asignará; puede repetirse."
    ),
    message: str | None = typer.Option(None, "--message", "-m", help="Mensaje del commit."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Acepta el mensaje predeterminado."),
    no_commit: bool = typer.Option(False, "--no-commit", help="No crea el commit automático."),
    push: bool | None = typer.Option(None, "--push/--no-push", help="Sobrescribe auto_push para esta operación."),
) -> None:
    """Registra un archivo o directorio y crea su primera copia."""
    heading("NUEVO TARGET", "Registrando una configuración en Concord")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    profile_manager = profiles(target_manager)
    maybe_offer_suggestion(profile_manager)
    selected_profiles = list(profile or [])
    active = execute(profile_manager.activation)
    if not selected_profiles and active and sys.stdin.isatty():
        choices = [active.primary, *active.complements]
        selected_profiles = request_checkbox(
            "¿A qué perfiles activos deseas agregar el target?", choices
        )
    selected_profile_models = [
        execute(lambda profile_name=profile_name: profile_manager.get(profile_name))
        for profile_name in selected_profiles
    ]
    target = execute(
        lambda: target_manager.add(path, name=name),
        hint="Usa una ruta existente dentro de HOME y un nombre que no esté registrado.",
    )
    for current in selected_profile_models:
        execute(
            lambda current=current: profile_manager.update(
                current.name, targets=[*current.targets, target.name]
            )
        )
    if selected_profiles:
        execute(lambda: target_manager.sync(CONCORD_TARGET))
    target_path = target.paths[0]
    destination = target_manager.repository.target_path(target.name) / target_path.relative_path
    details(
        [
            ("Nombre", target.name),
            ("Tipo", "directorio" if target_path.type.value == "directory" else "archivo"),
            ("Origen", str(target_path.local_path)),
            ("Copia", str(destination)),
            ("Archivos", str(len(target.get_files()))),
            ("Creado", format_date(target.created_at)),
            ("Actualizado", format_date(target.updated_at)),
        ],
        title="Target registrado",
    )
    execute(
        lambda: finalize_git(
            git_paths(target.name),
            f"concord: add {target.name}",
            git_command_options(message, yes, no_commit, push),
        ),
        hint="Los archivos se conservaron. Revisa Git con: concord repo status",
    )
    success(f"'{target.name}' fue agregado correctamente.", hint=f"Comprueba su estado con: concord status")


@app.command("add-path")
def add_path(
    name: str = typer.Argument(..., autocompletion=complete_targets),
    path: Path = typer.Argument(..., help="Nueva ruta local del target."),
    message: str | None = typer.Option(None, "--message", "-m"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    no_commit: bool = typer.Option(False, "--no-commit"),
    push: bool | None = typer.Option(None, "--push/--no-push"),
) -> None:
    """Agrega una ruta a un target existente."""
    heading("AGREGAR RUTA", "Ampliando una configuración existente")
    target = execute(
        lambda: manager().add_path(name, path),
        hint="La ruta debe existir, estar dentro de HOME y no solaparse con otra.",
    )
    added = execute(lambda: relative_to_home(path)).as_posix()
    execute(
        lambda: finalize_git(
            git_paths(name),
            f"{name}: add {added}",
            git_command_options(message, yes, no_commit, push),
        )
    )
    success(f"Se agregó '{added}' a '{name}'.", hint=f"El target contiene {len(target.paths)} rutas.")


@app.command("remove-path")
def remove_path(
    name: str = typer.Argument(..., autocompletion=complete_removable_targets),
    path: Path = typer.Argument(
        ...,
        help="Ruta registrada que dejará de administrarse.",
        autocompletion=complete_target_paths,
    ),
    message: str | None = typer.Option(None, "--message", "-m"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    no_commit: bool = typer.Option(False, "--no-commit"),
    push: bool | None = typer.Option(None, "--push/--no-push"),
) -> None:
    """Retira una ruta de un target sin borrarla de HOME."""
    heading("RETIRAR RUTA", "Dejando de administrar una parte del target")
    relative = execute(lambda: relative_to_home(path)).as_posix()
    target = execute(
        lambda: manager().remove_path(name, path),
        hint=f"Si es la única ruta, elimina el target completo con: concord remove {name}",
    )
    execute(
        lambda: finalize_git(
            git_paths(name),
            f"{name}: remove {relative}",
            git_command_options(message, yes, no_commit, push),
        )
    )
    success(
        f"'{relative}' permanece en HOME, pero ya no pertenece a '{name}'.",
        hint=f"El target conserva {len(target.paths)} rutas.",
    )


@app.command("list")
def list_targets(
    all_targets: bool = typer.Option(False, "--all", help="Muestra también targets fuera de los perfiles activos."),
) -> None:
    """Muestra los targets registrados y sus rutas."""
    heading("TARGETS", "Configuraciones administradas por Concord")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    profile_manager = profiles(target_manager)
    maybe_offer_suggestion(profile_manager)
    if profile_manager.activation() is not None:
        render_activation(profile_manager)
    targets = target_manager.list() if all_targets else target_manager.selected()
    if not targets:
        warning("No hay targets registrados.")
        console.print("  [concord.muted]Agrega uno con:[/] concord add <ruta>")
        return
    table = Table(box=box.ROUNDED, border_style="#4C566A", header_style="bold #88C0D0")
    table.add_column("Nombre", style="bold #D8DEE9")
    table.add_column("Rutas locales", style="concord.path")
    table.add_column("Creado", style="concord.muted", no_wrap=True)
    table.add_column("Actualizado", style="concord.muted", no_wrap=True)
    for index, target in enumerate(targets):
        table.add_row(
            target.name,
            "\n".join(format_home_path(path.local_path) for path in target.paths),
            format_date(target.created_at),
            format_date(target.updated_at),
            end_section=index < len(targets) - 1,
        )
    console.print(table)
    console.print(f"[concord.muted]Total:[/] {len(targets)} target(s)")


@app.command()
def edit(
    name: str = typer.Argument(
        ...,
        help="Target o recurso especial que se abrirá.",
        autocompletion=complete_editables,
    ),
    no_push: bool = typer.Option(
        False,
        "--no-push",
        help="Conserva localmente el commit generado al editar ignore.",
    ),
    path: Path | None = typer.Option(
        None,
        "--path",
        help="Ruta registrada que se abrirá.",
        autocompletion=complete_target_paths,
    ),
) -> None:
    """Abre un target local o edita un recurso especial de Concord."""
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    if name != "ignore":
        if name in TargetManager.RESERVED_NAMES:
            execute(
                lambda: (_ for _ in ()).throw(
                    ValueError(f"El recurso especial '{name}' todavía no está implementado.")
                )
            )
        target = execute(
            lambda: target_manager.get(name),
            hint="Consulta los nombres disponibles con: concord list",
        )
        selected = None
        if path is not None:
            requested = path.expanduser().resolve()
            selected = next((item for item in target.paths if item.local_path == requested), None)
            if selected is None:
                execute(lambda: (_ for _ in ()).throw(ValueError(f"La ruta no pertenece a '{name}'.")))
        elif len(target.paths) == 1:
            selected = target.paths[0]
        elif sys.stdin.isatty():
            answer = questionary.select(
                "Ruta que se abrirá:", choices=[str(item.local_path) for item in target.paths]
            ).ask()
            if answer is None:
                raise KeyboardInterrupt
            selected = next(item for item in target.paths if str(item.local_path) == answer)
        else:
            execute(
                lambda: (_ for _ in ()).throw(
                    ValueError(f"'{name}' contiene varias rutas; use --path <ruta>.")
                )
            )
        heading("EDITAR TARGET", "Abriendo la configuración local sin sincronizarla")
        result = execute(lambda: open_in_editor(selected.local_path))
        if result:
            raise typer.Exit(result)
        success(
            f"Se cerró el editor de '{name}'.",
            hint=f"Cuando termines de probar los cambios: concord sync {name}",
        )
        return

    heading("EDITAR IGNORE", "Actualizando las exclusiones del repositorio")
    config = ConfigManager().load()
    if not config.git.enabled:
        execute(lambda: (_ for _ in ()).throw(ValueError("Git está desactivado en Concord.")))
    git = GitManager(config.repository_path)
    if not git.initialized:
        execute(lambda: (_ for _ in ()).throw(ValueError("El repositorio Git no está inicializado.")))
    if git.changed():
        execute(
            lambda: (_ for _ in ()).throw(
                ValueError("El repositorio tiene cambios pendientes; confírmalos o descártalos antes de editar ignore.")
            ),
            hint="Revisa el estado con: concord repo status",
        )
    ignore_path = config.repository_path / ".gitignore"
    before = ignore_path.read_bytes() if ignore_path.exists() else None
    ignore_path.touch(exist_ok=True)
    result = execute(lambda: open_in_editor(ignore_path))
    if result:
        raise typer.Exit(result)
    after = ignore_path.read_bytes()
    if before == after:
        success(".gitignore no cambió; no se creó ningún commit.")
        return
    ignored = execute(git.tracked_ignored_files)
    if ignored:
        warning("Estos archivos dejarán de estar rastreados por Git:")
        for path in ignored:
            console.print(f"  [concord.path]{path.as_posix()}[/]")
    execute(lambda: git.stage_ignore_update(ignored))
    commit = execute(lambda: git.commit_staged("concord: update ignore rules"))
    if commit is None:
        success("No hubo cambios que confirmar.")
        return
    success(f"Commit creado: {commit.sha}  {commit.message}")
    if no_push:
        warning("El commit se conservó localmente por solicitud del usuario.")
        return
    execute(
        lambda: git.push(config.git.remote),
        hint="El commit local se conservó. Reintenta con: concord repo push",
    )
    success(f"Cambios enviados a {config.git.remote}/{git.status(remote=config.git.remote).branch}.")


def render_git_status(git_status) -> None:
    if not git_status.initialized:
        warning("El repositorio de Concord todavía no está inicializado con Git.")
        return
    divergence = "sin upstream"
    if git_status.ahead is not None and git_status.behind is not None:
        divergence = f"{git_status.ahead} por publicar · {git_status.behind} por descargar"
    details(
        [
            ("Estado", "limpio" if git_status.clean else "cambios pendientes"),
            ("Rama", git_status.branch or "sin rama"),
            ("Remoto", git_status.remote or "no configurado"),
            ("Seguimiento", git_status.upstream or "no configurado"),
            ("Divergencia", divergence),
            ("Commit", git_status.commit or "sin commits"),
            ("Último mensaje", git_status.message or "sin commits"),
        ],
        title="Repositorio Git",
    )


@app.command()
def doctor(
    fetch: bool = typer.Option(False, "--fetch", help="Comprueba también el estado remoto."),
    strict: bool = typer.Option(False, "--strict", help="Trata las advertencias como errores."),
    timings: bool = typer.Option(
        False,
        "--timings",
        help="Muestra el tiempo empleado por cada bloque del diagnóstico.",
    ),
) -> None:
    """Diagnostica la instalación de Concord sin modificarla."""
    heading("DIAGNÓSTICO", "Comprobando que Concord está listo para trabajar")
    report = Doctor().run(fetch=fetch)
    labels = {
        "pass": ("✓ Correcto", "#A3BE8C"),
        "warning": ("! Advertencia", "#EBCB8B"),
        "failure": ("× Error", "#BF616A"),
    }
    sections = list(dict.fromkeys(check.section for check in report.checks))
    for section in sections:
        table = Table(
            box=box.ROUNDED,
            border_style="#4C566A",
            header_style="bold #88C0D0",
            title=section,
        )
        table.add_column("Estado", no_wrap=True)
        table.add_column("Comprobación", style="bold #D8DEE9", no_wrap=True)
        table.add_column("Resultado", style="concord.path")
        for check in (item for item in report.checks if item.section == section):
            label, color = labels[check.state]
            result = check.message
            if check.hint:
                result += f"\n[concord.muted]Sugerencia: {check.hint}[/]"
            table.add_row(f"[{color}]{label}[/]", check.name, result)
        console.print(table)
    details(
        [
            ("Correctas", str(report.passed)),
            ("Advertencias", str(report.warnings)),
            ("Errores", str(report.failures)),
        ],
        title="Resumen del diagnóstico",
    )
    if timings:
        details(
            [
                *((timing.name, f"{timing.seconds:.3f} s") for timing in report.timings),
                ("Total", f"{report.elapsed:.3f} s"),
            ],
            title="Tiempos",
        )
    if report.failures:
        warning("Concord necesita correcciones antes de probar el flujo completo.")
        raise typer.Exit(1)
    if strict and report.warnings:
        warning("El diagnóstico estricto encontró advertencias.")
        raise typer.Exit(1)
    if report.warnings:
        success("Concord funciona, pero conviene revisar las advertencias.")
    else:
        success("Concord está listo para probarse.")


@app.command()
def status(
    fetch: bool = typer.Option(False, "--fetch", help="Actualiza la información del remoto."),
) -> None:
    """Compara los archivos locales con sus copias del repositorio."""
    heading("ESTADO", "Comparando HOME con el repositorio de Concord")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    profile_manager = profiles(target_manager)
    maybe_offer_suggestion(profile_manager)
    render_activation(profile_manager)
    items = target_manager.status()
    if not items:
        warning("No hay targets que comprobar.")
        return
    labels = {
        "clean": ("✓ Sin cambios", "#A3BE8C"),
        "modified": ("● Modificado", "#EBCB8B"),
        "missing": ("× Falta local", "#BF616A"),
        "untracked": ("× Falta copia", "#BF616A"),
    }
    table = Table(box=box.ROUNDED, border_style="#4C566A", header_style="bold #88C0D0")
    table.add_column("Target", style="bold #D8DEE9")
    table.add_column("Estado")
    table.add_column("Acción sugerida", style="concord.muted")
    hints = {"clean": "Ninguna", "modified": "concord sync <target>", "missing": "concord restore <target>", "untracked": "concord sync <target>"}
    for item in items:
        label, color = labels[item.state]
        table.add_row(item.name, f"[{color}]{label}[/]", hints[item.state])
    console.print(table)
    clean = sum(item.state == "clean" for item in items)
    success(f"Comprobación terminada: {clean}/{len(items)} target(s) sin cambios.")
    config = ConfigManager().load()
    if config.git.enabled:
        git_status = execute(
            lambda: GitManager(config.repository_path).status(fetch=fetch, remote=config.git.remote),
            hint="Comprueba Git con: concord repo status",
        )
        render_git_status(git_status)


@app.command("diff")
def diff_targets(
    name: str | None = typer.Argument(
        None,
        help="Target concreto; omítelo para comparar todos.",
        autocompletion=complete_targets,
    ),
    path: Path | None = typer.Option(
        None,
        "--path",
        "-p",
        help="Limita la comparación a una ruta del target.",
        autocompletion=complete_target_paths,
    ),
    context: int = typer.Option(
        3,
        "--context",
        "-C",
        min=0,
        help="Líneas de contexto del diff unificado.",
    ),
) -> None:
    """Compara HOME con el repositorio sin modificar archivos."""
    heading("DIFERENCIAS", "Vista previa de HOME → repositorio")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    profile_manager = profiles(target_manager)
    maybe_offer_suggestion(profile_manager)
    render_activation(profile_manager)
    if path is not None and name is None:
        execute(
            lambda: (_ for _ in ()).throw(
                ValueError("--path requiere indicar primero un target.")
            )
        )
    if name is not None:
        differences = execute(
            lambda: target_manager.content_diff(name, path=path, context=context),
            hint="La ruta debe pertenecer al target seleccionado.",
        )
        render_content_differences(name, differences, path=path)
        return
    differences = execute(
        lambda: target_manager.diff(),
        hint="Consulta los nombres disponibles con: concord list",
    )
    render_differences(differences, command="concord sync")


@app.command()
def sync(
    name: str | None = typer.Argument(
        None,
        help="Target concreto; omítelo para sincronizar todos.",
        autocompletion=complete_targets,
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Simula la operación sin modificar archivos."),
    message: str | None = typer.Option(None, "--message", "-m", help="Mensaje del commit."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Acepta el mensaje predeterminado."),
    no_commit: bool = typer.Option(False, "--no-commit", help="No crea el commit automático."),
    push: bool | None = typer.Option(None, "--push/--no-push", help="Sobrescribe auto_push para esta operación."),
) -> None:
    """Actualiza uno o todos los targets desde HOME al repositorio."""
    if dry_run:
        heading("SIMULACIÓN DE SINCRONIZACIÓN", "Vista previa de HOME → repositorio")
        target_manager = execute(manager, hint="Ejecuta primero: concord init")
        profile_manager = profiles(target_manager)
        maybe_offer_suggestion(profile_manager)
        render_activation(profile_manager)
        differences = execute(
            lambda: target_manager.preview_sync(name),
            hint="Consulta los nombres disponibles con: concord list",
        )
        render_differences(
            differences,
            command="concord sync" + (f" {name}" if name else ""),
        )
        config = ConfigManager().load()
        changed_names = [target_diff.name for target_diff in differences if not target_diff.clean]
        if (
            changed_names
            and config.git.enabled
            and config.git.auto_commit
            and not no_commit
        ):
            will_push = config.git.auto_push if push is None else push
            details(
                [
                    ("Commit", message or sync_commit_message(changed_names)),
                    ("Push", f"sí, a {config.git.remote}" if will_push else "no"),
                ],
                title="Git (simulación)",
            )
        return
    heading("SINCRONIZACIÓN", "Copiando cambios locales al repositorio")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    profile_manager = profiles(target_manager)
    maybe_offer_suggestion(profile_manager)
    render_activation(profile_manager)
    targets = execute(lambda: target_manager.sync(name), hint="Consulta los nombres disponibles con: concord list")
    for target in targets:
        console.print(f"[concord.success]✓[/] {target.name}  [concord.muted]← {len(target.paths)} ruta(s)[/]")
    if targets:
        target_names = [target.name for target in targets]
        default_message = sync_commit_message(target_names)
        execute(
            lambda: finalize_git(
                git_paths(*target_names),
                default_message,
                git_command_options(message, yes, no_commit, push),
            ),
            hint="El commit local se conservó. Si el remoto avanzó, ejecuta concord repo pull y después concord repo push.",
        )
    success(f"Se sincronizaron {len(targets)} target(s).", hint="Verifica el resultado con: concord status")


@app.command()
def restore(
    name: str | None = typer.Argument(
        None,
        help="Target que se restaurará.",
        autocompletion=complete_targets,
    ),
    all_targets: bool = typer.Option(False, "--all", "-a", help="Restaura todos los targets del manifiesto."),
    force: bool = typer.Option(False, "--force", "-f", help="Reemplaza la ruta local existente."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Simula la operación sin modificar archivos."),
) -> None:
    """Restaura un target del repositorio a HOME."""
    if (name is None) == (not all_targets):
        execute(lambda: (_ for _ in ()).throw(ValueError("Indique un target o use --all, pero no ambos.")))
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    profile_manager = profiles(target_manager)
    maybe_offer_suggestion(profile_manager)
    render_activation(profile_manager)
    if dry_run:
        heading("SIMULACIÓN DE RESTAURACIÓN", "Vista previa de repositorio → HOME")
        differences = execute(
            lambda: target_manager.preview_restore(None if all_targets else name),
            hint="Consulta los nombres disponibles con: concord list",
        )
        command = "concord restore --all" if all_targets else f"concord restore {name}"
        if force:
            command += " --force"
        render_differences(differences, command=command)
        return
    heading("RESTAURACIÓN", "Recuperando una configuración desde el repositorio")
    if all_targets:
        targets = execute(
            lambda: target_manager.restore_all(force=force),
            hint="Usa --force si deseas reemplazar configuraciones locales existentes.",
        )
        for target in targets:
            console.print(f"[concord.success]✓[/] {target.name}  [concord.muted]→ {len(target.paths)} ruta(s)[/]")
        success(f"Se restauraron {len(targets)} target(s).")
        return
    target = execute(
        lambda: target_manager.restore(name, force=force),
        hint="Si la ruta local ya existe y quieres reemplazarla, agrega --force.",
    )
    details(
        [("Target", target.name), ("Destinos", "\n".join(str(path.local_path) for path in target.paths))],
        title="Restauración completada",
    )
    success(f"'{target.name}' fue restaurado correctamente.")


@app.command()
def bootstrap(
    remote_url: str = typer.Argument(..., help="URL del repositorio remoto de dotfiles."),
    repository: Path | None = typer.Option(None, "--repository", "-r", help="Directorio local del repositorio."),
    restore_files: bool | None = typer.Option(
        None,
        "--restore/--no-restore",
        help="Restaura todos los targets después de importar el manifiesto.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Reemplaza rutas locales existentes al restaurar.",
    ),
) -> None:
    """Reconstruye Concord desde un repositorio remoto existente."""
    heading("BOOTSTRAP", "Recuperando Concord desde un repositorio remoto")
    if force and restore_files is False:
        execute(
            lambda: (_ for _ in ()).throw(
                ValueError("--force no puede combinarse con --no-restore.")
            )
        )
    if concord.is_initialized():
        execute(lambda: (_ for _ in ()).throw(ValueError("Concord ya está inicializado.")))
    if not GitManager.available():
        execute(lambda: (_ for _ in ()).throw(FileNotFoundError("Git no está instalado.")))
    destination = (repository or concord.default_repository_dir).expanduser().resolve()
    if destination.exists() and any(destination.iterdir()):
        execute(
            lambda: (_ for _ in ()).throw(
                FileExistsError(f"El directorio de destino no está vacío: {destination}")
            )
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    clone = subprocess.run(
        ["git", "clone", remote_url, str(destination)],
        text=True,
        capture_output=True,
        check=False,
    )
    if clone.returncode:
        execute(lambda: (_ for _ in ()).throw(ValueError(clone.stderr.strip() or "No fue posible clonar.")))
    config_manager = ConfigManager()
    config = execute(
        lambda: config_manager.load_from_repository(destination),
        hint="El repositorio debe contener el manifiesto administrado por Concord.",
    )
    config.repository_path = destination
    config_manager.save(config)
    target_manager = TargetManager()
    targets = execute(lambda: target_manager.import_manifest(replace=True))
    success(f"Se importaron {len(targets)} target(s) desde el manifiesto.")
    should_restore = restore_files
    if should_restore is None and sys.stdin.isatty():
        should_restore = bool(questionary.confirm("¿Restaurar ahora todos los targets?", default=True).ask())
    if should_restore:
        restore_force = force
        conflicts = target_manager.restore_conflicts()
        if conflicts and not restore_force:
            details(
                [
                    ("Rutas detectadas", str(len(conflicts))),
                    ("Se reemplazarán", "\n".join(format_home_path(path) for path in conflicts)),
                ],
                title="Configuraciones locales existentes",
            )
            if not sys.stdin.isatty():
                execute(
                    lambda: (_ for _ in ()).throw(
                        FileExistsError(
                            "Existen rutas locales que requieren confirmación; "
                            "vuelva a ejecutar bootstrap con --restore --force para reemplazarlas."
                        )
                    )
                )
            restore_force = bool(
                questionary.confirm(
                    "¿Reemplazar estas rutas con las copias del repositorio?",
                    default=False,
                ).ask()
            )
            if not restore_force:
                warning("Restauración cancelada; no se modificaron las rutas locales.")
                console.print("  [concord.muted]Cuando estés listo:[/] concord restore --all --force")
                render_git_status(GitManager(destination).status(remote=config.git.remote))
                return
        restored = execute(
            lambda: target_manager.restore_all(force=restore_force),
            hint=(
                "Si falta una copia en el repositorio, sincroniza ese target desde el equipo original "
                "o elimínalo del manifiesto."
            ),
        )
        success(f"Se restauraron {len(restored)} target(s).")
    else:
        warning("Los targets fueron importados pero todavía no se restauraron.")
        console.print("  [concord.muted]Cuando estés listo:[/] concord restore --all")
    render_git_status(GitManager(destination).status(remote=config.git.remote))


@app.command()
def reset(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Muestra qué eliminaría sin modificar archivos.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirma la eliminación sin interacción.",
    ),
) -> None:
    """Elimina todo el estado local de Concord y conserva los targets de HOME."""
    heading("REINICIAR CONCORD", "Eliminando configuración, datos y repositorio local")
    reset_manager = ResetManager()
    plan = execute(reset_manager.plan)
    if not plan.paths:
        success("No existe estado local de Concord que eliminar.")
        return
    details(
        [("Eliminar", str(path)) for path in plan.paths],
        title="Rutas internas",
    )
    console.print(
        "[concord.muted]Los targets restaurados en HOME y el remoto no se modificarán.[/]"
    )
    if dry_run:
        warning("Esta es una simulación; no se eliminó ningún archivo.")
        return
    if not yes:
        if not sys.stdin.isatty():
            execute(
                lambda: (_ for _ in ()).throw(
                    ValueError("La confirmación requiere una terminal; use --yes.")
                )
            )
        confirmation = questionary.text("Escribe RESET para continuar:").ask()
        if confirmation != "RESET":
            warning("Reset cancelado; no se eliminó ningún archivo.")
            return
    execute(lambda: reset_manager.reset(plan))
    success(
        "Se eliminó todo el estado local de Concord.",
        hint="Puedes reconstruirlo con: concord bootstrap <URL>",
    )


def active_git() -> tuple[GitManager, GitConfig]:
    config = ConfigManager().load()
    return GitManager(config.repository_path), config.git


@repo_app.command("status")
def repo_status(
    fetch: bool = typer.Option(False, "--fetch", help="Consulta el remoto antes de mostrar el estado."),
) -> None:
    """Muestra el estado Git del repositorio de Concord."""
    heading("REPOSITORIO", "Estado del historial y del remoto Git")
    git, settings = execute(active_git, hint="Ejecuta primero: concord init")
    render_git_status(execute(lambda: git.status(fetch=fetch, remote=settings.remote)))


@repo_app.command("log")
def repo_log(
    limit: int = typer.Option(10, "--limit", "-n", min=1, help="Número máximo de commits."),
) -> None:
    """Muestra el historial reciente de Concord."""
    heading("HISTORIAL GIT", "Commits recientes del repositorio")
    git, _ = execute(active_git, hint="Ejecuta primero: concord init")
    commits = execute(lambda: git.log(limit))
    if not commits:
        warning("El repositorio todavía no contiene commits.")
        return
    table = Table(box=box.ROUNDED, border_style="#4C566A", header_style="bold #88C0D0")
    table.add_column("Commit", style="concord.accent", no_wrap=True)
    table.add_column("Fecha", style="concord.muted", no_wrap=True)
    table.add_column("Mensaje", style="concord.path")
    for sha, date, message in commits:
        table.add_row(sha, date, message)
    console.print(table)


@repo_app.command("diff")
def repo_diff(
    staged: bool = typer.Option(False, "--staged", help="Muestra solamente cambios preparados."),
) -> None:
    """Muestra las diferencias Git del repositorio."""
    heading("DIFERENCIAS GIT", "Cambios dentro del repositorio de Concord")
    git, _ = execute(active_git, hint="Ejecuta primero: concord init")
    output = execute(lambda: git.diff(staged=staged))
    if output:
        console.print(output, markup=False)
    else:
        success("No hay diferencias que mostrar.")


@repo_app.command("commit")
def repo_commit(
    message: str | None = typer.Option(None, "--message", "-m", help="Mensaje del commit."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Usa el mensaje predeterminado."),
    push: bool | None = typer.Option(None, "--push/--no-push", help="Sobrescribe auto_push."),
) -> None:
    """Crea manualmente un commit con todos los cambios pendientes."""
    heading("COMMIT MANUAL", "Confirmando cambios pendientes del repositorio")
    git, settings = execute(active_git, hint="Ejecuta primero: concord init")
    if not git.changed():
        success("No hay cambios pendientes.")
        return
    commit_message = request_commit_message(
        "concord: update repository", GitOptions(message, yes, True, push)
    )
    if commit_message is None:
        warning("Commit cancelado; los cambios permanecen pendientes.")
        return
    commit = execute(lambda: git.commit([Path(".")], commit_message))
    if commit:
        success(f"Commit creado: {commit.sha}  {commit.message}")
    should_push = settings.auto_push if push is None else push
    if commit and should_push:
        execute(
            lambda: push_with_secret_check(git, settings.remote, assume_yes=yes),
            hint="El commit se conservó. Reintenta con: concord repo push",
        )


@repo_app.command("push")
def repo_push(
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirma archivos sensibles sin preguntar."),
) -> None:
    """Envía los commits locales al remoto configurado."""
    heading("PUSH", "Publicando commits del repositorio")
    git, settings = execute(active_git, hint="Ejecuta primero: concord init")
    execute(
        lambda: push_with_secret_check(git, settings.remote, assume_yes=yes),
        hint="El historial local no fue modificado.",
    )


@repo_app.command("pull")
def repo_pull() -> None:
    """Descarga cambios usando exclusivamente fast-forward."""
    heading("PULL", "Actualizando el repositorio de forma segura")
    git, settings = execute(active_git, hint="Ejecuta primero: concord init")
    execute(
        lambda: git.pull(settings.remote),
        hint="Concord no realizará merges ni rebases automáticos.",
    )
    success("Repositorio actualizado mediante fast-forward.")


@repo_app.command("init")
def repo_init() -> None:
    """Inicializa o repara la integración Git del repositorio."""
    heading("INICIALIZAR GIT", "Preparando el repositorio de Concord")
    config_manager = ConfigManager()
    config = execute(config_manager.load, hint="Ejecuta primero: concord init")
    if not config.git.enabled:
        config.git.enabled = True
        config_manager.save(config)
        TargetManager().sync(CONCORD_TARGET)
    git, settings = execute(active_git, hint="Ejecuta primero: concord init")
    created = execute(lambda: git.initialize())
    git.ensure_gitignore()
    name, email = git.identity()
    if not name:
        name = request_text("Nombre para los commits:")
    if not email:
        email = request_text("Correo para los commits:")
    git.set_identity(name, email)
    if git.changed():
        message = request_commit_message(
            "concord: initialize repository", GitOptions(None, False, True, None)
        )
        if message:
            commit = execute(lambda: git.commit([Path(".")], message))
            if commit:
                success(f"Commit creado: {commit.sha}  {commit.message}")
    if not git.has_remote(settings.remote) and sys.stdin.isatty():
        if questionary.confirm("¿Crear el repositorio remoto en GitHub?", default=True).ask():
            repo_name = request_text("Nombre del repositorio de GitHub:", "dotfiles")
            visibility = questionary.select(
                "Visibilidad:", choices=["Privado", "Público"], default="Privado"
            ).ask()
            execute(lambda: git.create_github_repository(repo_name, private=visibility != "Público"))
            if settings.auto_push:
                execute(
                    lambda: push_with_secret_check(git, settings.remote),
                    hint="El commit local se conservó. Reintenta con: concord repo push",
                )
    success("Integración Git preparada." if created else "La integración Git ya estaba inicializada.")


@remote_app.callback(invoke_without_command=True)
def repo_remote(ctx: typer.Context) -> None:
    """Muestra el remoto Git configurado."""
    if ctx.invoked_subcommand is not None:
        return
    git, settings = execute(active_git, hint="Ejecuta primero: concord init")
    url = git.remote_url(settings.remote)
    details([("Nombre", settings.remote), ("URL", url or "no configurado")], title="Remoto Git")


@remote_app.command("set")
def repo_remote_set(url: str, name: str = typer.Option("origin", "--name")) -> None:
    """Crea o reemplaza el remoto configurado."""
    git, _ = execute(active_git, hint="Ejecuta primero: concord init")
    execute(lambda: git.set_remote(url, name))
    success(f"Remoto '{name}' configurado: {url}")


@remote_app.command("remove")
def repo_remote_remove(name: str = typer.Option("origin", "--name")) -> None:
    """Elimina un remoto Git."""
    git, _ = execute(active_git, hint="Ejecuta primero: concord init")
    execute(lambda: git.remove_remote(name))
    success(f"Remoto '{name}' eliminado.")


@app.command("import")
def import_targets(
    replace: bool = typer.Option(False, "--replace", help="Reconstruye una base de datos que ya contiene targets."),
) -> None:
    """Reconstruye SQLite usando los targets declarados en concord.toml."""
    heading("IMPORTAR MANIFIESTO", "Reconstruyendo el índice local desde concord.toml")
    targets = execute(
        lambda: manager(check_manifest=False).import_manifest(replace=replace),
        hint="Usa --replace para reemplazar el índice local actual.",
    )
    for target in targets:
        console.print(
            f"[concord.success]✓[/] {target.name}  "
            f"[concord.muted]→ {len(target.paths)} ruta(s)[/]"
        )
    success(f"Se importaron {len(targets)} target(s).", hint="Restaura tus archivos con: concord restore --all")


@app.command()
def remove(
    name: str = typer.Argument(..., autocompletion=complete_removable_targets),
    keep_repository: bool = typer.Option(False, "--keep-repository", help="Conserva la copia del repositorio."),
    message: str | None = typer.Option(None, "--message", "-m", help="Mensaje del commit."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Acepta el mensaje predeterminado."),
    no_commit: bool = typer.Option(False, "--no-commit", help="No crea el commit automático."),
    push: bool | None = typer.Option(None, "--push/--no-push", help="Sobrescribe auto_push para esta operación."),
) -> None:
    """Deja de gestionar un target sin borrar su archivo local."""
    heading("ELIMINAR TARGET", "Quitando una configuración del registro de Concord")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    target = execute(lambda: target_manager.get(name), hint="Consulta los nombres disponibles con: concord list")
    execute(lambda: target_manager.remove(name, keep_repository=keep_repository))
    details(
        [
            ("Target", name),
            ("Rutas locales", f"{len(target.paths)} conservada(s)"),
            ("Copia", "conservada" if keep_repository else "eliminada"),
        ],
        title="Resultado",
    )
    execute(
        lambda: finalize_git(
            git_paths(*(() if keep_repository else (name,))),
            f"concord: remove {name}",
            git_command_options(message, yes, no_commit, push),
        ),
        hint="La eliminación se conservó. Revisa Git con: concord repo status",
    )
    success(f"Concord dejó de administrar '{name}'.")


def profile_tree(profile_manager: ProfileManager, name: str, *, root: Tree | None = None) -> Tree:
    profile = profile_manager.get(name)
    tree = root or Tree(f"[bold #D8DEE9]{profile.name}[/]")
    if profile.includes:
        includes = tree.add("[concord.accent]Incluye[/]")
        for included in profile.includes:
            branch = includes.add(f"[bold]{included}[/]")
            profile_tree(profile_manager, included, root=branch)
    if profile.targets:
        targets = tree.add("[concord.success]Targets[/]")
        for target in profile.targets:
            targets.add(target)
    if profile.excludes:
        excludes = tree.add("[concord.warning]Excluye[/]")
        for target in profile.excludes:
            excludes.add(target)
    return tree


def choose_profile_activation(
    profile_manager: ProfileManager, *, confirmation: str = "¿Activar esta combinación?"
) -> Activation | None:
    available = [profile.name for profile in profile_manager.list()]
    if not available:
        raise ValueError("No hay perfiles; cree uno con: concord profile create <nombre>.")
    primary = questionary.select("Perfil principal:", choices=available).ask()
    if primary is None:
        raise KeyboardInterrupt
    complements = request_checkbox(
        "Complementos:",
        [name for name in available if name != primary],
    )
    complements = request_order(complements)
    resolution = profile_manager.resolve_activation(primary, complements)
    details(
        [
            ("Principal", primary),
            ("Complementos (en orden)", "\n".join(complements) or "—"),
            ("Targets efectivos", "\n".join(resolution.target_names) or "vacío"),
            ("Exclusiones aplicadas", "\n".join(resolution.applied_exclusions) or "—"),
        ],
        title="Vista previa",
    )
    if resolution.warnings:
        warning("Advertencias de resolución:\n- " + "\n- ".join(resolution.warnings))
    if not resolution.target_names:
        warning("La combinación es válida, pero su resultado está vacío.")
    if not questionary.confirm(confirmation, default=True).ask():
        warning("Operación cancelada; no se modificó la activación.")
        return None
    return resolution.activation


@profile_app.command("create")
def profile_create(
    name: str,
    description: str = typer.Option("", "--description", "-d"),
) -> None:
    """Crea un perfil vacío."""
    heading("NUEVO PERFIL", "Creando una selección reutilizable de targets")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    profile_manager = profiles(target_manager)
    profile = execute(lambda: profile_manager.create(name, description=description))
    execute(lambda: persist_profile_manifest(target_manager, f"concord: create profile {profile.name}"))
    details(
        [
            ("Nombre", profile.name),
            ("Descripción", profile.description or "—"),
            ("Targets", "0"),
        ],
        title="Perfil creado",
    )
    success(f"Se creó el perfil vacío '{profile.name}'.", hint=f"Edítalo con: concord profile edit {profile.name}")


@profile_app.command("edit")
def profile_edit(
    name: str,
    description: str | None = typer.Option(None, "--description", "-d"),
    includes: list[str] | None = typer.Option(None, "--include"),
    targets: list[str] | None = typer.Option(None, "--target"),
    excludes: list[str] | None = typer.Option(None, "--exclude"),
) -> None:
    """Edita toda la composición de un perfil."""
    heading("EDITAR PERFIL", "Modificando composición, exclusiones y metadatos")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    profile_manager = profiles(target_manager)
    current = execute(lambda: profile_manager.get(name))
    interactive = all(value is None for value in (description, includes, targets, excludes))
    if interactive:
        description = request_text("Descripción:", current.description)
        available_profiles = [item.name for item in profile_manager.list() if item.id != current.id]
        includes = request_checkbox(
            "Perfiles incluidos:",
            available_profiles,
            checked=current.includes,
        )
        available_targets = [
            target.name for target in target_manager.list()
            if target.name != CONCORD_TARGET
        ]
        targets = request_checkbox(
            "Targets directos:",
            available_targets,
            checked=current.targets,
        )
        excludes = request_checkbox(
            "Targets excluidos:",
            available_targets,
            checked=current.excludes,
        )
        if not questionary.confirm("¿Guardar todos los cambios?", default=True).ask():
            warning("Edición cancelada; no se modificó el perfil.")
            return
    updated = execute(
        lambda: profile_manager.update(
            current.name,
            description=description,
            includes=includes,
            targets=targets,
            excludes=excludes,
        )
    )
    execute(lambda: persist_profile_manifest(target_manager, f"concord: edit profile {updated.name}"))
    success(f"Se actualizó el perfil '{updated.name}'.")


@profile_app.command("rename")
def profile_rename(name: str, new_name: str) -> None:
    """Cambia el nombre sin romper referencias."""
    heading("RENOMBRAR PERFIL", "Conservando referencias mediante UUID")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    renamed = execute(lambda: profiles(target_manager).rename(name, new_name))
    execute(lambda: persist_profile_manifest(target_manager, f"concord: rename profile {name} to {renamed.name}"))
    success(f"El perfil ahora se llama '{renamed.name}'.")


@profile_app.command("delete")
def profile_delete(
    name: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Elimina sin pedir confirmación."),
) -> None:
    """Elimina un perfil y limpia sus referencias."""
    heading("ELIMINAR PERFIL", "Retirando una composición de targets")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    profile_manager = profiles(target_manager)
    profile = execute(lambda: profile_manager.get(name))
    if not yes and sys.stdin.isatty():
        if not questionary.confirm(f"¿Eliminar el perfil '{profile.name}'?", default=False).ask():
            warning("Eliminación cancelada.")
            return
    execute(lambda: profile_manager.delete(profile.name))
    execute(lambda: persist_profile_manifest(target_manager, f"concord: delete profile {profile.name}"))
    success(f"Se eliminó el perfil '{profile.name}' y sus referencias.")


@profile_app.command("list")
def profile_list() -> None:
    """Muestra perfiles, composición y estado de activación."""
    heading("PERFILES", "Composiciones disponibles y targets efectivos")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    profile_manager = profiles(target_manager)
    available = profile_manager.list()
    if not available:
        warning("No hay perfiles definidos.")
        return
    active = execute(profile_manager.activation)
    table = Table(box=box.SIMPLE_HEAVY, expand=True)
    table.add_column("Nombre", style="bold #88C0D0")
    table.add_column("Descripción")
    table.add_column("Composición", style="concord.muted")
    table.add_column("Estado", no_wrap=True)
    for profile in available:
        state = "—"
        if active and profile.name == active.primary:
            state = "[concord.success]Principal[/]"
        elif active and profile.name in active.complements:
            position = active.complements.index(profile.name) + 1
            state = f"[concord.accent]Complemento {position}[/]"
        composition = (
            f"{len(profile.includes)} incluidos · {len(profile.targets)} targets · "
            f"{len(profile.excludes)} exclusiones"
        )
        table.add_row(profile.name, profile.description or "—", composition, state)
    console.print(table)


@profile_app.command("show")
def profile_show(name: str) -> None:
    """Muestra el detalle de un perfil."""
    heading("DETALLE DEL PERFIL", "Composición directa y resultado expandido")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    profile_manager = profiles(target_manager)
    profile = execute(lambda: profile_manager.get(name))
    details(
        [
            ("UUID", profile.id),
            ("Nombre", profile.name),
            ("Descripción", profile.description or "—"),
        ],
        title="Perfil",
    )
    console.print(profile_tree(profile_manager, profile.name))
    resolution = execute(lambda: profile_manager.resolve(profile.name))
    details(
        [("Targets efectivos", "\n".join(resolution.target_names) or "vacío")],
        title="Resultado expandido",
    )
    for message in resolution.warnings:
        warning(message)


@profile_app.command("activate")
def profile_activate(
    primary: str | None = typer.Option(None, "--primary", help="Perfil principal."),
    complements: list[str] | None = typer.Option(None, "--with", help="Complemento; puede repetirse."),
) -> None:
    """Activa un perfil principal y complementos ordenados."""
    heading("ACTIVAR PERFILES", "Seleccionando los targets efectivos de este equipo")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    profile_manager = profiles(target_manager)
    if primary is None:
        chosen = execute(lambda: choose_profile_activation(profile_manager))
        if chosen is None:
            return
        activation = execute(
            lambda: profile_manager.activate(chosen.primary, chosen.complements)
        )
    else:
        activation = execute(lambda: profile_manager.activate(primary, complements))
    render_activation(profile_manager)
    resolution = execute(profile_manager.resolve_active)
    if resolution and not resolution.target_names:
        warning("La activación es válida, pero su resultado está vacío.")
    success(f"Se activó el perfil principal '{activation.primary}'.")


@profile_app.command("deactivate")
def profile_deactivate(
    name: str | None = typer.Argument(None),
    all_profiles: bool = typer.Option(False, "--all", help="Desactiva la selección completa."),
    replace_with: str | None = typer.Option(None, "--replace-with", help="Nuevo perfil principal."),
) -> None:
    """Desactiva un perfil o vuelve al modo global."""
    heading("DESACTIVAR PERFILES", "Actualizando la selección local")
    profile_manager = profiles(execute(manager, hint="Ejecuta primero: concord init"))
    if all_profiles:
        if name is not None:
            execute(lambda: (_ for _ in ()).throw(ValueError("No combine un nombre con --all.")))
        profile_manager.deactivate_all()
        success("Se desactivaron todos los perfiles; Concord usará todos los targets.")
        return
    if name is None:
        execute(lambda: (_ for _ in ()).throw(ValueError("Indique un perfil o use --all.")))
    active = execute(lambda: profile_manager.deactivate(name, replace_with=replace_with))
    if active:
        render_activation(profile_manager)
    success(f"Se desactivó el perfil '{name}'.")


@profile_app.command("suggest")
def profile_suggest(
    primary: str | None = typer.Option(None, "--primary"),
    complements: list[str] | None = typer.Option(None, "--with"),
) -> None:
    """Guarda en el manifiesto una activación recomendada."""
    heading("SUGERIR ACTIVACIÓN", "Definiendo la combinación recomendada para otros equipos")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    profile_manager = profiles(target_manager)
    if primary is None:
        chosen = execute(
            lambda: choose_profile_activation(
                profile_manager, confirmation="¿Guardar esta combinación como sugerencia?"
            )
        )
        if chosen is None:
            return
        activation = execute(lambda: profile_manager.suggest(chosen.primary, chosen.complements))
    else:
        activation = execute(lambda: profile_manager.suggest(primary, complements))
    execute(lambda: persist_profile_manifest(target_manager, "concord: update suggested profiles"))
    success(
        f"Se sugirió '{activation.primary}'"
        + (f" con {', '.join(activation.complements)}." if activation.complements else ".")
    )


@profile_app.command("validate")
def profile_validate() -> None:
    """Comprueba referencias, ciclos y la activación actual."""
    heading("VALIDAR PERFILES", "Comprobando composición e integridad")
    profile_manager = profiles(execute(manager, hint="Ejecuta primero: concord init"))
    warnings = execute(profile_manager.validate)
    if warnings:
        for message in warnings:
            warning(message)
        success("Los perfiles son válidos con advertencias.")
    else:
        success("Todos los perfiles y la activación son válidos.")
