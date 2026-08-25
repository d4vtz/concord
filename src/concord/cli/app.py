from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from concord import application as concord
from concord.application.initializer import Initializer
from concord.application.target_manager import TargetManager

app = typer.Typer(no_args_is_help=True, help="Gestiona y respalda tus dotfiles.")
console = Console()


def manager() -> TargetManager:
    if not concord.is_initialized():
        raise typer.BadParameter("Concord no está inicializado. Ejecute: concord init")
    return TargetManager()


@app.command()
def init() -> None:
    """Inicializa la configuración, el repositorio y la base de datos."""
    Initializer().initialize()


@app.command()
def add(path: Path, name: str | None = typer.Option(None, "--name", "-n", help="Nombre único del target.")) -> None:
    """Registra un archivo o directorio y crea su primera copia."""
    target = manager().add(path, name=name)
    console.print(f"[green]Agregado:[/green] {target.name} → {target.local_path}")


@app.command("list")
def list_targets() -> None:
    """Muestra los targets registrados."""
    table = Table("Nombre", "Tipo", "Ruta local")
    for target in manager().list():
        table.add_row(target.name, target.type.value, str(target.local_path))
    console.print(table)


@app.command()
def status() -> None:
    """Compara los archivos locales con el repositorio."""
    colors = {"clean": "green", "modified": "yellow", "missing": "red", "untracked": "red"}
    table = Table("Target", "Estado")
    for item in manager().status():
        table.add_row(item.name, f"[{colors[item.state]}]{item.state}[/]")
    console.print(table)


@app.command()
def sync(name: str | None = typer.Argument(None)) -> None:
    """Actualiza uno o todos los targets desde HOME al repositorio."""
    targets = manager().sync(name)
    console.print(f"[green]Sincronizados:[/green] {len(targets)}")


@app.command()
def restore(name: str, force: bool = typer.Option(False, "--force", "-f", help="Reemplaza el archivo local existente.")) -> None:
    """Restaura un target del repositorio a HOME."""
    target = manager().restore(name, force=force)
    console.print(f"[green]Restaurado:[/green] {target.local_path}")


@app.command()
def remove(name: str, keep_repository: bool = typer.Option(False, "--keep-repository", help="Conserva la copia del repositorio.")) -> None:
    """Deja de gestionar un target; no borra el archivo local."""
    manager().remove(name, keep_repository=keep_repository)
    console.print(f"[green]Eliminado:[/green] {name}")
