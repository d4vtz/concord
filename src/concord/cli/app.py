from datetime import datetime
from pathlib import Path

import typer
from rich import box
from rich.table import Table

from concord import application as concord
from concord.application.config import ConfigManager
from concord.application.initializer import Initializer
from concord.application.target_manager import TargetManager
from concord.cli.ui import console, details, execute, heading, success, warning

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="[bold #88C0D0]Concord[/] — gestiona y respalda tus dotfiles.",
)


def manager() -> TargetManager:
    if not concord.is_initialized():
        raise ValueError("Concord todavía no está inicializado.")
    return TargetManager()


def format_date(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


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
    created = execute(lambda: Initializer().initialize(repository))
    config = ConfigManager().load()
    if not created:
        warning("Concord ya estaba inicializado; no se modificó la configuración.")
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
def status() -> None:
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


@app.command("diff")
def diff_targets(
    name: str | None = typer.Argument(
        None, help="Target concreto; omítelo para comparar todos."
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
    name: str | None = typer.Argument(None, help="Target concreto; omítelo para sincronizar todos."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Simula la operación sin modificar archivos."),
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
        return
    heading("SINCRONIZACIÓN", "Copiando cambios locales al repositorio")
    targets = execute(lambda: manager().sync(name), hint="Consulta los nombres disponibles con: concord list")
    for target in targets:
        console.print(f"[concord.success]✓[/] {target.name}  [concord.muted]← {target.local_path}[/]")
    success(f"Se sincronizaron {len(targets)} target(s).", hint="Verifica el resultado con: concord status")


@app.command()
def restore(
    name: str | None = typer.Argument(None, help="Target que se restaurará."),
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
    name: str,
    keep_repository: bool = typer.Option(False, "--keep-repository", help="Conserva la copia del repositorio."),
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
    success(f"Concord dejó de administrar '{name}'.")
