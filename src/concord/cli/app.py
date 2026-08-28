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
from rich.table import Table

from concord import application as concord
from concord.application.config import CONCORD_TARGET, ConfigManager, GitConfig
from concord.application.doctor import Doctor
from concord.application.git import GitCommit, GitManager
from concord.application.initializer import Initializer
from concord.application.target_manager import TargetManager
from concord.cli.completion import (complete_editables,
                                    complete_removable_targets,
                                    complete_targets)
from concord.cli.ui import console, details, execute, heading, success, warning

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="[bold #88C0D0]Concord[/] — gestiona y respalda tus dotfiles.",
)
repo_app = typer.Typer(no_args_is_help=True, help="Administra el repositorio Git de Concord.")
remote_app = typer.Typer(no_args_is_help=False, help="Consulta o configura el remoto Git.")
app.add_typer(repo_app, name="repo")
repo_app.add_typer(remote_app, name="remote")


def manager() -> TargetManager:
    if not concord.is_initialized():
        raise ValueError("Concord todavía no está inicializado.")
    return TargetManager()


def format_date(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


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
    message: str | None = typer.Option(None, "--message", "-m", help="Mensaje del commit."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Acepta el mensaje predeterminado."),
    no_commit: bool = typer.Option(False, "--no-commit", help="No crea el commit automático."),
    push: bool | None = typer.Option(None, "--push/--no-push", help="Sobrescribe auto_push para esta operación."),
) -> None:
    """Registra un archivo o directorio y crea su primera copia."""
    heading("NUEVO TARGET", "Registrando una configuración en Concord")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    target = execute(
        lambda: target_manager.add(path, name=name),
        hint="Usa una ruta existente dentro de HOME y un nombre que no esté registrado.",
    )
    destination = target_manager.repository.target_path(target.name) / target.local_path.relative_to(Path.home())
    details(
        [
            ("Nombre", target.name),
            ("Tipo", "directorio" if target.local_path.is_dir() else "archivo"),
            ("Origen", str(target.local_path)),
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


@app.command("list")
def list_targets() -> None:
    """Muestra los targets registrados y sus rutas."""
    heading("TARGETS", "Configuraciones administradas por Concord")
    target_manager = execute(manager, hint="Ejecuta primero: concord init")
    targets = target_manager.list()
    if not targets:
        warning("No hay targets registrados.")
        console.print("  [concord.muted]Agrega uno con:[/] concord add <ruta>")
        return
    table = Table(box=box.ROUNDED, border_style="#4C566A", header_style="bold #88C0D0")
    table.add_column("Nombre", style="bold #D8DEE9")
    table.add_column("Tipo")
    table.add_column("Ruta local", style="concord.path")
    table.add_column("Creado", style="concord.muted", no_wrap=True)
    table.add_column("Actualizado", style="concord.muted", no_wrap=True)
    for target in targets:
        table.add_row(
            target.name,
            "directorio" if target.local_path.is_dir() else "archivo",
            str(target.local_path),
            format_date(target.created_at),
            format_date(target.updated_at),
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
        heading("EDITAR TARGET", "Abriendo la configuración local sin sincronizarla")
        result = execute(lambda: open_in_editor(target.local_path))
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
) -> None:
    """Muestra los cambios que sync aplicaría al repositorio."""
    heading("DIFERENCIAS", "Vista previa de HOME → repositorio")
    differences = execute(
        lambda: manager().diff(name),
        hint="Consulta los nombres disponibles con: concord list",
    )
    render_differences(differences, command="concord sync" + (f" {name}" if name else ""))


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
        differences = execute(
            lambda: manager().preview_sync(name),
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
    targets = execute(lambda: manager().sync(name), hint="Consulta los nombres disponibles con: concord list")
    for target in targets:
        console.print(f"[concord.success]✓[/] {target.name}  [concord.muted]← {target.local_path}[/]")
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
            console.print(f"[concord.success]✓[/] {target.name}  [concord.muted]→ {target.local_path}[/]")
        success(f"Se restauraron {len(targets)} target(s).")
        return
    target = execute(
        lambda: target_manager.restore(name, force=force),
        hint="Si la ruta local ya existe y quieres reemplazarla, agrega --force.",
    )
    details([("Target", target.name), ("Destino", str(target.local_path))], title="Restauración completada")
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
) -> None:
    """Reconstruye Concord desde un repositorio remoto existente."""
    heading("BOOTSTRAP", "Recuperando Concord desde un repositorio remoto")
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
    targets = execute(lambda: TargetManager().import_manifest(replace=True))
    success(f"Se importaron {len(targets)} target(s) desde el manifiesto.")
    should_restore = restore_files
    if should_restore is None and sys.stdin.isatty():
        should_restore = bool(questionary.confirm("¿Restaurar ahora todos los targets?", default=True).ask())
    if should_restore:
        restored = execute(
            lambda: TargetManager().restore_all(),
            hint="Usa concord restore --all --force si existen configuraciones locales.",
        )
        success(f"Se restauraron {len(restored)} target(s).")
    else:
        warning("Los targets fueron importados pero todavía no se restauraron.")
        console.print("  [concord.muted]Cuando estés listo:[/] concord restore --all")
    render_git_status(GitManager(destination).status(remote=config.git.remote))


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
        lambda: manager().import_manifest(replace=replace),
        hint="Usa --replace para reemplazar el índice local actual.",
    )
    for target in targets:
        console.print(
            f"[concord.success]✓[/] {target.name}  "
            f"[concord.muted]→ {target.local_path.relative_to(Path.home())}[/]"
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
            ("Archivo local", f"conservado en {target.local_path}"),
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
