from pathlib import Path

import typer

from concord.application.initializer import Initializer
from concord.application.target_manager import TargetManager

app = typer.Typer()
commands = typer.Typer()

app.add_typer(commands)


@commands.command()
def init():
    """Initialize Concord."""

    initializer = Initializer()
    initializer.initialize()


@commands.command()
def add(
    path,
    name: str | None = typer.Option(
        None, "--name", "-n", help="Nombre del target dentro del repositorio."
    ),
):
    """Add target Concord."""

    targetmanager = TargetManager()
    targetmanager.add(Path(path), name=name)
