import sqlite3
from dataclasses import dataclass
from pathlib import Path

from concord import application as concord
from concord.application.config import CONCORD_TARGET, ConfigManager


@dataclass(frozen=True)
class TargetCompletion:
    name: str
    local_path: Path

    @property
    def item(self) -> tuple[str, str]:
        return self.name, str(self.local_path)


def _manifest_targets() -> list[TargetCompletion]:
    config = ConfigManager().load()
    return [
        TargetCompletion(target.name, Path.home() / target.relative_path)
        for target in config.targets
    ]


def _database_targets() -> list[TargetCompletion]:
    database_path = concord.database_file
    if not database_path.is_file():
        return []
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT name, local_path FROM targets ORDER BY name"
        ).fetchall()
    return [TargetCompletion(name, Path(local_path)) for name, local_path in rows]


def registered_targets() -> list[TargetCompletion]:
    """Read completion data without initializing or modifying Concord."""
    try:
        targets = _manifest_targets()
    except Exception:
        try:
            targets = _database_targets()
        except Exception:
            return []
    return sorted(targets, key=lambda target: target.name)


def _complete(
    incomplete: str,
    *,
    include_concord: bool = True,
    resources: tuple[TargetCompletion, ...] = (),
) -> list[tuple[str, str]]:
    targets = [*resources, *registered_targets()]
    return [
        target.item
        for target in targets
        if target.name.startswith(incomplete)
        and (include_concord or target.name != CONCORD_TARGET)
    ]


def complete_targets(incomplete: str) -> list[tuple[str, str]]:
    return _complete(incomplete)


def complete_removable_targets(incomplete: str) -> list[tuple[str, str]]:
    return _complete(incomplete, include_concord=False)


def complete_editables(incomplete: str) -> list[tuple[str, str]]:
    targets = registered_targets()
    if not targets:
        return []
    try:
        ignore_path = ConfigManager().load().repository_path / ".gitignore"
    except Exception:
        ignore_path = Path(".gitignore")
    resources = (TargetCompletion("ignore", ignore_path),)
    return [
        target.item
        for target in (*resources, *targets)
        if target.name.startswith(incomplete)
    ]
