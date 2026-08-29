import filecmp
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from concord import application as concord
from concord.application.config import CONCORD_TARGET, Config, ConfigManager
from concord.application.git import GitManager


@dataclass(frozen=True)
class DoctorCheck:
    section: str
    name: str
    state: str
    message: str
    hint: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    checks: list[DoctorCheck]

    @property
    def failures(self) -> int:
        return sum(check.state == "failure" for check in self.checks)

    @property
    def warnings(self) -> int:
        return sum(check.state == "warning" for check in self.checks)

    @property
    def passed(self) -> int:
        return sum(check.state == "pass" for check in self.checks)

    @property
    def healthy(self) -> bool:
        return self.failures == 0


class Doctor:
    def __init__(self, config_manager: ConfigManager | None = None) -> None:
        self.config_manager = config_manager or ConfigManager()

    def run(self, *, fetch: bool = False) -> DoctorReport:
        checks: list[DoctorCheck] = []
        config = self._configuration_checks(checks)
        if config is None:
            return DoctorReport(checks)
        self._database_checks(config, checks)
        self._profile_checks(config, checks)
        self._target_checks(config, checks)
        self._git_checks(config, checks, fetch=fetch)
        return DoctorReport(checks)

    def _add(
        self,
        checks: list[DoctorCheck],
        section: str,
        name: str,
        state: str,
        message: str,
        hint: str | None = None,
    ) -> None:
        checks.append(DoctorCheck(section, name, state, message, hint))

    def _configuration_checks(self, checks: list[DoctorCheck]) -> Config | None:
        if not concord.config_file.is_file():
            self._add(
                checks,
                "Concord",
                "Configuración",
                "failure",
                f"No existe {concord.config_file}.",
                "Ejecuta: concord init",
            )
            return None
        try:
            config = self.config_manager.load()
        except (OSError, KeyError, TypeError, ValueError) as error:
            self._add(
                checks,
                "Concord",
                "Manifiesto",
                "failure",
                f"No es válido: {error}",
                "Corrige concord.toml o recupéralo desde el repositorio.",
            )
            return None
        self._add(checks, "Concord", "Manifiesto", "pass", "Formato y versión válidos.")
        if config.repository_path.is_dir():
            self._add(
                checks,
                "Concord",
                "Repositorio",
                "pass",
                str(config.repository_path),
            )
        else:
            self._add(
                checks,
                "Concord",
                "Repositorio",
                "failure",
                f"No existe {config.repository_path}.",
                "Comprueba repository_path o ejecuta concord bootstrap.",
            )
        names = [target.name for target in config.targets]
        if len(names) != len(set(names)):
            self._add(
                checks,
                "Concord",
                "Targets del manifiesto",
                "failure",
                "Hay nombres duplicados.",
                "Elimina los targets duplicados de concord.toml.",
            )
        elif CONCORD_TARGET not in names:
            self._add(
                checks,
                "Concord",
                "Target concord",
                "failure",
                "No está declarado en el manifiesto.",
                "Recupera el manifiesto o vuelve a inicializar Concord.",
            )
        else:
            self._add(
                checks,
                "Concord",
                "Targets del manifiesto",
                "pass",
                f"{len(config.targets)} target(s), incluido concord.",
            )
        unsafe = [
            target.name
            for target in config.targets
            if any(
                path.relative_path.is_absolute() or ".." in path.relative_path.parts
                for path in target.paths
            )
        ]
        paths = [
            (target.name, path.relative_path)
            for target in config.targets
            for path in target.paths
        ]
        overlaps = []
        for index, (name, path) in enumerate(paths):
            for other_name, other in paths[index + 1 :]:
                if path == other or path.is_relative_to(other) or other.is_relative_to(path):
                    overlaps.append(f"{name}:{path} ↔ {other_name}:{other}")
        if unsafe:
            self._add(
                checks,
                "Seguridad",
                "Rutas del manifiesto",
                "failure",
                f"Rutas no seguras en: {', '.join(unsafe)}.",
                "Usa únicamente rutas relativas a HOME.",
            )
        else:
            self._add(
                checks,
                "Seguridad",
                "Rutas del manifiesto",
                "pass",
                "Todas son relativas a HOME.",
            )
        if overlaps:
            self._add(
                checks,
                "Seguridad",
                "Solapamiento de rutas",
                "failure",
                "; ".join(overlaps),
                "Cada ruta debe pertenecer exclusivamente a un target.",
            )
        else:
            self._add(
                checks,
                "Seguridad",
                "Solapamiento de rutas",
                "pass",
                "No hay rutas iguales ni anidadas.",
            )
        return config

    def _database_checks(self, config: Config, checks: list[DoctorCheck]) -> None:
        if not concord.database_file.is_file():
            self._add(
                checks,
                "Concord",
                "Base de datos",
                "failure",
                f"No existe {concord.database_file}.",
                "Reconstrúyela con: concord import",
            )
            return
        try:
            uri = f"file:{concord.database_file}?mode=ro"
            with sqlite3.connect(uri, uri=True) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                rows = connection.execute("SELECT name FROM targets").fetchall()
                path_rows = connection.execute(
                    """
                    SELECT targets.name, target_paths.local_path
                    FROM targets
                    JOIN target_paths ON target_paths.target_id = targets.id
                    """
                ).fetchall()
        except sqlite3.Error as error:
            self._add(
                checks,
                "Concord",
                "Base de datos",
                "failure",
                f"SQLite no pudo leerla: {error}",
                "Reconstrúyela con: concord import --replace",
            )
            return
        if integrity != "ok":
            self._add(
                checks,
                "Concord",
                "Integridad SQLite",
                "failure",
                integrity,
                "Reconstrúyela con: concord import --replace",
            )
            return
        self._add(checks, "Concord", "Integridad SQLite", "pass", "Base de datos íntegra.")
        database_names = {row[0] for row in rows}
        manifest_names = {target.name for target in config.targets}
        database_paths = {(name, Path(path)) for name, path in path_rows}
        manifest_paths = {
            (target.name, (Path.home() / path.relative_path).resolve())
            for target in config.targets
            for path in target.paths
        }
        if database_names == manifest_names and database_paths == manifest_paths:
            self._add(
                checks,
                "Concord",
                "Índice local",
                "pass",
                "SQLite coincide con el manifiesto.",
            )
        else:
            self._add(
                checks,
                "Concord",
                "Índice local",
                "failure",
                "SQLite y concord.toml contienen targets diferentes.",
                "Ejecuta: concord import --replace",
            )

    def _target_checks(self, config: Config, checks: list[DoctorCheck]) -> None:
        missing_local: list[str] = []
        missing_copy: list[str] = []
        modified: list[str] = []
        for target in config.targets:
            for path in target.paths:
                local = Path.home() / path.relative_path
                stored = config.repository_path / target.name / path.relative_path
                label = f"{target.name}:{path.relative_path}"
                local_exists = os.path.lexists(local)
                stored_exists = os.path.lexists(stored)
                if not local_exists:
                    missing_local.append(label)
                if not stored_exists:
                    missing_copy.append(label)
                if local_exists and stored_exists and not self._paths_equal(local, stored):
                    modified.append(label)
        if missing_copy:
            self._add(
                checks, "Targets", "Copias del repositorio", "failure",
                f"Faltan: {', '.join(missing_copy)}.", "Sincroniza los targets indicados."
            )
        else:
            self._add(
                checks, "Targets", "Copias del repositorio", "pass",
                "Todos los targets tienen una copia."
            )
        if missing_local:
            self._add(
                checks, "Targets", "Rutas locales", "warning",
                f"No existen: {', '.join(missing_local)}.",
                "Revísalas con concord status o usa concord restore."
            )
        else:
            self._add(
                checks, "Targets", "Rutas locales", "pass",
                "Todos los targets existen en HOME."
            )
        if modified:
            self._add(
                checks, "Targets", "Sincronización", "warning",
                f"Cambios locales en: {', '.join(modified)}.", "Revísalos con: concord diff"
            )
        elif not missing_copy:
            self._add(
                checks, "Targets", "Sincronización", "pass",
                "HOME y el repositorio coinciden."
            )

    def _profile_checks(self, config: Config, checks: list[DoctorCheck]) -> None:
        if not concord.database_file.is_file():
            return
        try:
            uri = f"file:{concord.database_file}?mode=ro"
            with sqlite3.connect(uri, uri=True) as connection:
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if "profiles" not in tables:
                    self._add(
                        checks, "Perfiles", "Esquema SQLite", "failure",
                        "La base de datos todavía no contiene las tablas de perfiles.",
                        "Ejecuta cualquier comando de Concord 2.3.1 para actualizarla."
                    )
                    return
                rows = connection.execute("SELECT id, name FROM profiles").fetchall()
                includes = connection.execute(
                    "SELECT profile_id, included_profile_id FROM profile_includes"
                ).fetchall()
                active_primary = connection.execute(
                    "SELECT primary_profile_id FROM profile_activation WHERE singleton = 1"
                ).fetchone()
                active_complements = connection.execute(
                    "SELECT profile_id FROM profile_activation_complements"
                ).fetchall()
        except sqlite3.Error as error:
            self._add(checks, "Perfiles", "Integridad", "failure", f"No pudieron comprobarse: {error}")
            return

        if set(rows) != {(profile.id, profile.name) for profile in config.profiles}:
            self._add(
                checks, "Perfiles", "Índice local", "failure",
                "SQLite y concord.toml contienen perfiles diferentes.",
                "Ejecuta: concord import --replace"
            )
        else:
            self._add(
                checks, "Perfiles", "Índice local", "pass",
                f"{len(rows)} perfil(es) coinciden con el manifiesto."
            )

        graph: dict[str, list[str]] = {}
        for profile_id, included_id in includes:
            graph.setdefault(profile_id, []).append(included_id)
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(profile_id: str) -> None:
            if profile_id in visiting:
                raise ValueError("Se detectó un ciclo entre perfiles.")
            if profile_id in visited:
                return
            visiting.add(profile_id)
            for included_id in graph.get(profile_id, []):
                visit(included_id)
            visiting.remove(profile_id)
            visited.add(profile_id)

        try:
            for profile_id, _ in rows:
                visit(profile_id)
        except ValueError as error:
            self._add(checks, "Perfiles", "Composición", "failure", str(error))
        else:
            self._add(
                checks, "Perfiles", "Composición", "pass",
                "No hay ciclos ni referencias estructurales rotas."
            )

        ids = {profile_id for profile_id, _ in rows}
        active_ids = ([active_primary[0]] if active_primary else []) + [
            profile_id for (profile_id,) in active_complements
        ]
        if any(profile_id not in ids for profile_id in active_ids):
            self._add(
                checks, "Perfiles", "Activación local", "failure",
                "La activación referencia perfiles inexistentes.",
                "Ejecuta: concord profile activate o concord profile deactivate --all"
            )
        elif active_primary:
            self._add(
                checks, "Perfiles", "Activación local", "pass",
                "La activación local es válida."
            )
        else:
            self._add(
                checks, "Perfiles", "Activación local", "pass",
                "No hay perfiles activos; se usarán todos los targets."
            )

    def _git_checks(self, config: Config, checks: list[DoctorCheck], *, fetch: bool) -> None:
        if not config.git.enabled:
            self._add(
                checks,
                "Git",
                "Integración",
                "warning",
                "Está desactivada en concord.toml.",
                "Actívala con: concord repo init",
            )
            return
        if not GitManager.available():
            self._add(
                checks,
                "Git",
                "Ejecutable",
                "failure",
                "Git no está disponible en PATH.",
                "Instala Git y ejecuta concord repo init.",
            )
            return
        self._add(checks, "Git", "Ejecutable", "pass", "Git está disponible.")
        git = GitManager(config.repository_path)
        if not git.initialized:
            self._add(
                checks,
                "Git",
                "Repositorio",
                "failure",
                "No está inicializado.",
                "Ejecuta: concord repo init",
            )
            return
        self._add(checks, "Git", "Repositorio", "pass", "Repositorio Git válido.")
        name, email = git.identity()
        if name and email:
            self._add(checks, "Git", "Identidad", "pass", f"{name} <{email}>")
        else:
            self._add(
                checks,
                "Git",
                "Identidad",
                "failure",
                "Falta user.name o user.email.",
                "Ejecuta: concord repo init",
            )
        try:
            status = git.status(fetch=fetch, remote=config.git.remote)
        except (FileNotFoundError, ValueError) as error:
            self._add(
                checks,
                "Git",
                "Conectividad remota",
                "failure" if fetch else "warning",
                str(error),
                "Comprueba la conexión y ejecuta concord repo status --fetch.",
            )
            status = git.status(fetch=False, remote=config.git.remote)
        if status.branch:
            self._add(checks, "Git", "Rama", "pass", status.branch)
        else:
            self._add(checks, "Git", "Rama", "failure", "No hay una rama activa.")
        if status.clean:
            self._add(checks, "Git", "Estado", "pass", "No hay cambios pendientes.")
        else:
            self._add(
                checks,
                "Git",
                "Estado",
                "warning",
                "Hay cambios sin confirmar.",
                "Revísalos con: concord repo status",
            )
        if status.remote:
            self._add(checks, "Git", "Remoto", "pass", status.remote)
        else:
            self._add(
                checks,
                "Git",
                "Remoto",
                "warning",
                f"No existe el remoto '{config.git.remote}'.",
                "Configúralo con: concord repo remote set <URL>",
            )
        if status.remote and not status.upstream:
            self._add(
                checks,
                "Git",
                "Upstream",
                "warning",
                "La rama todavía no tiene seguimiento remoto.",
                "Ejecuta: concord repo push",
            )
        elif status.upstream:
            divergence = f"{status.ahead} por publicar, {status.behind} por descargar"
            state = "warning" if status.ahead or status.behind else "pass"
            hint = "Ejecuta concord repo pull o concord repo push." if state == "warning" else None
            self._add(checks, "Git", "Upstream", state, f"{status.upstream}: {divergence}.", hint)
        sensitive = git.sensitive_files()
        if sensitive:
            preview = ", ".join(path.as_posix() for path in sensitive[:5])
            suffix = "…" if len(sensitive) > 5 else ""
            self._add(
                checks,
                "Seguridad",
                "Archivos sensibles",
                "warning",
                f"Detectados: {preview}{suffix}",
                "Revísalos antes de ejecutar concord repo push.",
            )
        else:
            self._add(
                checks,
                "Seguridad",
                "Archivos sensibles",
                "pass",
                "No se detectaron nombres sospechosos.",
            )
        if GitManager.github_available():
            state = "pass"
            message = "GitHub CLI está disponible."
            if fetch:
                auth = subprocess.run(
                    ["gh", "auth", "status"], text=True, capture_output=True, check=False
                )
                if auth.returncode:
                    state = "warning"
                    message = "GitHub CLI no está autenticado."
            self._add(
                checks,
                "GitHub",
                "GitHub CLI",
                state,
                message,
                "Ejecuta: gh auth login" if state == "warning" else None,
            )
        else:
            self._add(
                checks,
                "GitHub",
                "GitHub CLI",
                "warning",
                "gh no está instalado; no se podrán crear remotos automáticamente.",
            )

    def _paths_equal(self, left: Path, right: Path) -> bool:
        if left.is_symlink() or right.is_symlink():
            return left.is_symlink() and right.is_symlink() and left.readlink() == right.readlink()
        if left.is_file() or right.is_file():
            return left.is_file() and right.is_file() and filecmp.cmp(left, right, shallow=False)
        comparison = filecmp.dircmp(left, right)
        if comparison.left_only or comparison.right_only or comparison.diff_files or comparison.funny_files:
            return False
        return all(self._directories_equal(child) for child in comparison.subdirs.values())

    def _directories_equal(self, comparison: filecmp.dircmp) -> bool:
        if comparison.left_only or comparison.right_only or comparison.diff_files or comparison.funny_files:
            return False
        return all(self._directories_equal(child) for child in comparison.subdirs.values())
