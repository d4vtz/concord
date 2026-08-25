from collections.abc import Callable
from typing import TypeVar

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

T = TypeVar("T")

theme = Theme(
    {
        "concord.title": "bold #88C0D0",
        "concord.accent": "#81A1C1",
        "concord.success": "bold #A3BE8C",
        "concord.warning": "bold #EBCB8B",
        "concord.error": "bold #BF616A",
        "concord.muted": "#7B88A1",
        "concord.path": "#D8DEE9",
    }
)
console = Console(theme=theme)


def heading(title: str, subtitle: str) -> None:
    content = Text()
    content.append("CONCORD\n", style="concord.title")
    content.append(subtitle, style="concord.muted")
    console.print(Panel(content, title=f"[concord.accent]{title}[/]", box=box.ROUNDED, border_style="#5E81AC"))


def details(rows: list[tuple[str, str]], *, title: str | None = None) -> None:
    table = Table(box=None, show_header=False, padding=(0, 1), expand=False)
    table.add_column(style="concord.muted", no_wrap=True)
    table.add_column(style="concord.path")
    for label, value in rows:
        table.add_row(label, value)
    console.print(Panel(table, title=title, box=box.ROUNDED, border_style="#4C566A", expand=False))


def success(message: str, *, hint: str | None = None) -> None:
    console.print(f"[concord.success]✓[/] {message}")
    if hint:
        console.print(f"  [concord.muted]Siguiente:[/] {hint}")


def warning(message: str) -> None:
    console.print(f"[concord.warning]![/] {message}")


def abort(error: Exception, *, hint: str | None = None) -> None:
    message = error.args[0] if isinstance(error, KeyError) and error.args else str(error)
    body = Text(str(message), style="concord.error")
    if hint:
        body.append(f"\n\nSugerencia: {hint}", style="concord.muted")
    console.print(Panel(body, title="[concord.error]Error[/]", box=box.ROUNDED, border_style="#BF616A"))
    raise typer.Exit(1)


def execute(action: Callable[[], T], *, hint: str | None = None) -> T:
    try:
        return action()
    except (FileNotFoundError, FileExistsError, KeyError, ValueError) as error:
        abort(error, hint=hint)
