import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from concord import application as concord
from concord.application.config import ConfigManager


@dataclass(frozen=True)
class ResetPlan:
    paths: list[Path]


class ResetManager:
    def __init__(self, config_manager: ConfigManager | None = None) -> None:
        self.config_manager = config_manager or ConfigManager()

    def plan(self) -> ResetPlan:
        candidates = [concord.config_dir, concord.data_dir]
        repository: Path | None = None
        if concord.config_file.is_file():
            try:
                repository = self.config_manager.load().repository_path
                candidates.append(repository)
            except (OSError, KeyError, TypeError, ValueError):
                pass
        normalized = self._normalize(candidates)
        for path in normalized:
            self._validate(path)
        if repository is not None:
            self._validate_repository(repository.expanduser().resolve())
        return ResetPlan([path for path in normalized if os.path.lexists(path)])

    def reset(self, plan: ResetPlan) -> None:
        for path in sorted(plan.paths, key=lambda item: len(item.parts), reverse=True):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif os.path.lexists(path):
                path.unlink()

    def _normalize(self, candidates: list[Path]) -> list[Path]:
        unique = sorted(
            {path.expanduser().resolve() for path in candidates},
            key=lambda item: len(item.parts),
        )
        result: list[Path] = []
        for path in unique:
            if any(path.is_relative_to(parent) for parent in result):
                continue
            result.append(path)
        return result

    def _validate(self, path: Path) -> None:
        home = Path.home().resolve()
        protected = {
            home,
            (home / ".config").resolve(),
            (home / ".local").resolve(),
            (home / ".local/share").resolve(),
        }
        if path in protected or not path.is_relative_to(home):
            raise ValueError(f"Ruta insegura para reset: {path}")

    def _validate_repository(self, repository: Path) -> None:
        data_dir = concord.data_dir.expanduser().resolve()
        if not os.path.lexists(repository) or repository.is_relative_to(data_dir):
            return
        manifest = repository / "concord/.config/concord/concord.toml"
        if not manifest.is_file():
            raise ValueError(
                f"El repositorio configurado no parece pertenecer a Concord: {repository}"
            )
