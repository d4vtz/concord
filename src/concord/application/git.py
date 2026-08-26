import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitStatus:
    initialized: bool
    clean: bool
    branch: str | None = None
    commit: str | None = None
    message: str | None = None
    remote: str | None = None
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None


@dataclass(frozen=True)
class GitCommit:
    sha: str
    message: str


class GitManager:
    DEFAULT_GITIGNORE = "*.db\n*.sqlite\n*.sqlite3\n__pycache__/\n.DS_Store\n"
    SENSITIVE_PATTERNS = (
        ".env",
        "*.pem",
        "id_rsa",
        "id_ed25519",
        "credentials*",
        "secrets*",
        "token*",
    )

    def __init__(self, repository_path: Path) -> None:
        self.repository_path = repository_path.expanduser().resolve()

    @staticmethod
    def available() -> bool:
        return shutil.which("git") is not None

    @staticmethod
    def github_available() -> bool:
        return shutil.which("gh") is not None

    def _run(
        self,
        *arguments: str,
        check: bool = True,
        capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if not self.available():
            raise FileNotFoundError("Git no está instalado o no está disponible en PATH.")
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.repository_path,
            text=True,
            capture_output=capture,
            check=False,
        )
        if check and result.returncode:
            message = result.stderr.strip() or result.stdout.strip() or "Git terminó con un error."
            raise ValueError(message)
        return result

    @property
    def initialized(self) -> bool:
        return (self.repository_path / ".git").is_dir()

    def initialize(self, branch: str = "main") -> bool:
        self.repository_path.mkdir(parents=True, exist_ok=True)
        if self.initialized:
            return False
        self._run("init", "-b", branch)
        return True

    def ensure_gitignore(self) -> bool:
        path = self.repository_path / ".gitignore"
        if path.exists():
            return False
        path.write_text(self.DEFAULT_GITIGNORE)
        return True

    def config(self, key: str, value: str | None = None, *, global_: bool = False) -> str | None:
        arguments = ["config"]
        if global_:
            arguments.append("--global")
        arguments.append(key)
        if value is not None:
            arguments.append(value)
        result = self._run(*arguments, check=value is not None)
        return result.stdout.strip() or None

    def identity(self) -> tuple[str | None, str | None]:
        return self.config("user.name"), self.config("user.email")

    def set_identity(self, name: str, email: str) -> None:
        self.config("user.name", name)
        self.config("user.email", email)

    def changed(self, paths: list[Path] | None = None) -> bool:
        arguments = ["status", "--porcelain"]
        if paths:
            arguments.extend(["--", *[path.as_posix() for path in paths]])
        return bool(self._run(*arguments).stdout.strip())

    def commit(self, paths: list[Path], message: str) -> GitCommit | None:
        normalized = [path.as_posix() for path in paths]
        self._run("add", "-A", "--", *normalized)
        staged = self._run("diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            return None
        if staged.returncode != 1:
            raise ValueError(staged.stderr.strip() or "No fue posible comprobar el índice de Git.")
        self._run("commit", "-m", message)
        return GitCommit(self._run("rev-parse", "--short", "HEAD").stdout.strip(), message)

    def status(self, *, fetch: bool = False, remote: str = "origin") -> GitStatus:
        if not self.initialized:
            return GitStatus(initialized=False, clean=True)
        if fetch and self.has_remote(remote):
            self._run("fetch", remote)
        branch = self._run("branch", "--show-current").stdout.strip() or None
        commit_result = self._run("rev-parse", "--short", "HEAD", check=False)
        commit = commit_result.stdout.strip() or None if commit_result.returncode == 0 else None
        message_result = self._run("log", "-1", "--pretty=%s", check=False)
        message = message_result.stdout.strip() or None if message_result.returncode == 0 else None
        remote_url = self.remote_url(remote)
        upstream_result = self._run(
            "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False
        )
        upstream = upstream_result.stdout.strip() or None if upstream_result.returncode == 0 else None
        ahead = behind = None
        if upstream:
            counts = self._run("rev-list", "--left-right", "--count", f"{upstream}...HEAD").stdout.split()
            if len(counts) == 2:
                behind, ahead = map(int, counts)
        return GitStatus(
            initialized=True,
            clean=not self.changed(),
            branch=branch,
            commit=commit,
            message=message,
            remote=remote_url,
            upstream=upstream,
            ahead=ahead,
            behind=behind,
        )

    def has_remote(self, name: str = "origin") -> bool:
        return self._run("remote", "get-url", name, check=False).returncode == 0

    def remote_url(self, name: str = "origin") -> str | None:
        result = self._run("remote", "get-url", name, check=False)
        return result.stdout.strip() or None if result.returncode == 0 else None

    def set_remote(self, url: str, name: str = "origin") -> None:
        action = "set-url" if self.has_remote(name) else "add"
        self._run("remote", action, name, url)

    def remove_remote(self, name: str = "origin") -> None:
        if not self.has_remote(name):
            raise ValueError(f"No existe el remoto '{name}'.")
        self._run("remote", "remove", name)

    def push(self, remote: str = "origin") -> None:
        if not self.has_remote(remote):
            raise ValueError(f"No existe el remoto '{remote}'.")
        branch = self._run("branch", "--show-current").stdout.strip()
        if not branch:
            raise ValueError("No fue posible determinar la rama activa.")
        self._run("push", "--set-upstream", remote, branch)

    def pull(self, remote: str = "origin") -> None:
        if not self.has_remote(remote):
            raise ValueError(f"No existe el remoto '{remote}'.")
        branch = self._run("branch", "--show-current").stdout.strip()
        self._run("pull", "--ff-only", remote, branch)

    def log(self, limit: int = 10) -> list[tuple[str, str, str]]:
        result = self._run(
            "log", f"-{limit}", "--date=short", "--pretty=format:%h%x1f%ad%x1f%s", check=False
        )
        if result.returncode:
            return []
        return [tuple(line.split("\x1f", 2)) for line in result.stdout.splitlines()]

    def diff(self, *, staged: bool = False) -> str:
        arguments = ["diff"]
        if staged:
            arguments.append("--cached")
        return self._run(*arguments).stdout

    def sensitive_files(self, paths: list[Path] | None = None) -> list[Path]:
        candidates = paths or [Path(".")]
        result: set[Path] = set()
        for root in candidates:
            absolute = self.repository_path / root
            if absolute.is_file():
                files = [absolute]
            elif absolute.exists():
                files = [path for path in absolute.rglob("*") if path.is_file()]
            else:
                continue
            for file in files:
                relative = file.relative_to(self.repository_path)
                if any(file.match(pattern) for pattern in self.SENSITIVE_PATTERNS):
                    result.add(relative)
        return sorted(result, key=str)

    def create_github_repository(self, name: str, *, private: bool = True) -> str:
        if not self.github_available():
            raise FileNotFoundError("GitHub CLI (gh) no está instalado.")
        auth = subprocess.run(["gh", "auth", "status"], text=True, capture_output=True, check=False)
        if auth.returncode:
            raise ValueError("GitHub CLI no está autenticado. Ejecuta: gh auth login")
        visibility = "--private" if private else "--public"
        result = subprocess.run(
            ["gh", "repo", "create", name, visibility, "--source", str(self.repository_path), "--remote", "origin"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise ValueError(result.stderr.strip() or "No fue posible crear el repositorio en GitHub.")
        return self.remote_url() or result.stdout.strip()
