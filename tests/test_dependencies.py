import subprocess
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from concord import application as concord
from concord.application.config import CONCORD_TARGET, Config, ConfigManager
from concord.application.database import Database
from concord.application.dependencies import (DependencyInstallError,
                                              DependencyManager)
from concord.application.doctor import Doctor
from concord.application.git import GitManager
from concord.application.initializer import Initializer
from concord.application.profile_manager import ProfileManager
from concord.application.repository import RepositoryManager
from concord.application.target_manager import TargetManager
from concord.cli.app import app


def configure_concord_environment(
    home: Path, repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = home / ".config/concord"
    data_dir = home / ".local/share/concord"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(concord, "config_dir", config_dir)
    monkeypatch.setattr(concord, "config_file", config_dir / "concord.toml")
    monkeypatch.setattr(concord, "database_file", data_dir / "concord.db")
    monkeypatch.setattr(concord, "default_repository_dir", repository)


@pytest.fixture
def dependency_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    repository = tmp_path / "repository"
    configure_concord_environment(home, repository, monkeypatch)
    config_dir = home / ".config/concord"
    data_dir = home / ".local/share/concord"
    ConfigManager().save(Config(repository_path=repository))
    database = Database(data_dir / "concord.db")
    targets = TargetManager(database, RepositoryManager(repository))

    def add_target(name: str):
        source = home / f".{name}"
        source.write_text(name)
        return targets.add(source, name)

    add_target("nvim")
    add_target("shell")
    config = ConfigManager().load()
    config.git.enabled = False
    ConfigManager().save(config)
    return targets, DependencyManager(database), ProfileManager(database)


def completed(command, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def test_dependencies_are_persisted_and_rebuilt_from_manifest(dependency_environment):
    targets, dependencies, _ = dependency_environment
    dependencies.add(
        "nvim", "pacman", ["neovim", "ripgrep"], validate=False
    )
    dependencies.add("nvim", "aur", ["some-language-server"], optional=True, validate=False)

    config = ConfigManager().load()
    nvim = next(target for target in config.targets if target.name == "nvim")
    assert [(item.package, item.manager, item.optional) for item in nvim.dependencies] == [
        ("some-language-server", "aur", True),
        ("neovim", "pacman", False),
        ("ripgrep", "pacman", False),
    ]
    assert config.minimum_concord_version == "2.5.0"

    targets.import_manifest(replace=True)
    rebuilt = DependencyManager(targets.database).for_target("nvim")
    assert {item.package for item in rebuilt} == {
        "neovim", "ripgrep", "some-language-server"
    }


def test_package_cannot_use_pacman_and_aur_across_targets(dependency_environment):
    _, dependencies, _ = dependency_environment
    dependencies.add("nvim", "pacman", ["fzf"], validate=False)

    with pytest.raises(ValueError, match="pacman"):
        dependencies.add("shell", "aur", ["fzf"], validate=False)


@pytest.mark.parametrize("package", ["--noconfirm", "name;rm", "name with spaces"])
def test_package_names_cannot_become_shell_or_backend_options(
    dependency_environment, package
):
    _, dependencies, _ = dependency_environment

    with pytest.raises(ValueError, match="no válido"):
        dependencies.add("nvim", "pacman", [package], validate=False)


def test_reclassification_requires_confirmation_and_remove_only_changes_declaration(
    dependency_environment,
):
    _, dependencies, _ = dependency_environment
    dependencies.add("nvim", "pacman", ["neovim"], validate=False)

    with pytest.raises(ValueError, match="reclasificación"):
        dependencies.add(
            "nvim", "pacman", ["neovim"], optional=True, validate=False
        )

    dependencies.add(
        "nvim",
        "pacman",
        ["neovim"],
        optional=True,
        validate=False,
        reclassify=True,
    )
    assert dependencies.for_target("nvim")[0].optional

    removed = dependencies.remove("nvim", ["neovim"])
    assert [item.package for item in removed] == ["neovim"]
    assert dependencies.for_target("nvim") == []


def test_profile_aggregation_deduplicates_and_required_wins(dependency_environment):
    _, dependencies, profiles = dependency_environment
    dependencies.add("nvim", "pacman", ["fzf"], optional=True, validate=False)
    dependencies.add("shell", "pacman", ["fzf", "zsh"], validate=False)
    profiles.create("base")
    profiles.update("base", targets=["nvim", "shell"])

    resolved = dependencies.for_profile("base")

    assert [item.package for item in resolved] == ["fzf", "zsh"]
    assert not next(item for item in resolved if item.package == "fzf").optional
    assert next(item for item in resolved if item.package == "fzf").targets == (
        "nvim", "shell"
    )


def test_status_uses_single_pacman_transaction_query(dependency_environment):
    targets, _, _ = dependency_environment
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return completed(command, 127, "ripgrep\n")

    dependencies = DependencyManager(
        targets.database,
        runner=runner,
        which=lambda executable: f"/usr/bin/{executable}" if executable == "pacman" else None,
    )
    dependencies.add("nvim", "pacman", ["neovim", "ripgrep"], validate=False)

    statuses = dependencies.status(dependencies.for_target("nvim"))

    assert calls == [["pacman", "-T", "neovim", "ripgrep"]]
    assert {item.dependency.package: item.installed for item in statuses} == {
        "neovim": True,
        "ripgrep": False,
    }


def test_aur_helper_is_local_and_not_written_to_manifest(dependency_environment):
    targets, _, _ = dependency_environment
    dependencies = DependencyManager(
        targets.database,
        which=lambda executable: "/usr/bin/paru" if executable == "paru" else None,
    )

    dependencies.set_aur_helper("paru")

    assert dependencies.configured_aur_helper() == "paru"
    assert "\naur_helper =" not in concord.config_file.read_text()


def test_install_stops_after_partial_failure(dependency_environment, monkeypatch):
    targets, _, _ = dependency_environment
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return completed(command, 1 if command[0] == "paru" else 0)

    monkeypatch.setattr("concord.application.dependencies.os.geteuid", lambda: 0)
    dependencies = DependencyManager(
        targets.database,
        runner=runner,
        which=lambda executable: f"/usr/bin/{executable}"
        if executable in {"pacman", "paru"}
        else None,
    )
    dependencies.set_aur_helper("paru")
    dependencies.add("nvim", "pacman", ["neovim"], validate=False)
    dependencies.add("nvim", "aur", ["visual-studio-code-bin"], validate=False)
    selected = dependencies.for_target("nvim")

    with pytest.raises(DependencyInstallError) as captured:
        dependencies.install(selected)

    assert [item.package for item in captured.value.installed] == ["neovim"]
    assert [item.package for item in captured.value.pending] == [
        "visual-studio-code-bin"
    ]
    assert len(calls) == 2


def test_only_pacman_uses_sudo_for_installation(dependency_environment, monkeypatch):
    targets, _, _ = dependency_environment
    monkeypatch.setattr("concord.application.dependencies.os.geteuid", lambda: 1000)
    dependencies = DependencyManager(
        targets.database,
        which=lambda executable: f"/usr/bin/{executable}"
        if executable in {"pacman", "paru", "sudo"}
        else None,
    )
    dependencies.set_aur_helper("paru")
    dependencies.add("nvim", "pacman", ["neovim"], validate=False)
    dependencies.add("nvim", "aur", ["visual-studio-code-bin"], validate=False)

    commands = dependencies.install_commands(dependencies.for_target("nvim"))

    assert commands[0][1][:2] == ["sudo", "pacman"]
    assert commands[1][1][0] == "paru"


def test_cli_add_list_check_and_dry_run(dependency_environment, monkeypatch):
    targets, _, _ = dependency_environment
    monkeypatch.setattr("concord.cli.app.manager", lambda: targets)
    monkeypatch.setattr("concord.application.dependencies.os.geteuid", lambda: 1000)

    def fake_run(command, **kwargs):
        if command[:2] == ["pacman", "-T"]:
            return completed(command, 127, "neovim\nripgrep\n")
        raise AssertionError(f"No debía ejecutarse: {command}")

    monkeypatch.setattr("concord.application.dependencies.subprocess.run", fake_run)
    monkeypatch.setattr(
        "concord.application.dependencies.shutil.which",
        lambda executable: f"/usr/bin/{executable}"
        if executable in {"pacman", "sudo"}
        else None,
    )
    runner = CliRunner()

    added = runner.invoke(
        app,
        [
            "deps", "add", "nvim", "neovim", "ripgrep",
            "--manager", "pacman", "--required", "--skip-validation", "--yes",
        ],
    )
    listed = runner.invoke(app, ["deps", "list", "nvim"])
    checked = runner.invoke(app, ["deps", "check", "nvim"])
    preview = runner.invoke(app, ["deps", "install", "nvim", "--dry-run"])

    assert added.exit_code == 0, added.output
    assert listed.exit_code == 0 and "neovim" in listed.output
    assert checked.exit_code == 1 and "Faltantes obligatorias" in checked.output
    assert preview.exit_code == 0, preview.output
    assert "pacman -S --needed -- neovim ripgrep" in preview.output
    assert "Simulación completada" in preview.output


def test_profile_cli_lists_aggregated_dependencies(dependency_environment, monkeypatch):
    targets, dependencies, profiles = dependency_environment
    dependencies.add("nvim", "pacman", ["neovim"], validate=False)
    dependencies.add("shell", "pacman", ["zsh"], validate=False)
    profiles.create("base")
    profiles.update("base", targets=["nvim", "shell"])
    monkeypatch.setattr("concord.cli.app.manager", lambda: targets)

    result = CliRunner().invoke(app, ["profile", "deps", "list", "base"])

    assert result.exit_code == 0, result.output
    assert "neovim" in result.output and "zsh" in result.output


def test_external_dependency_manifest_change_is_detected(dependency_environment):
    targets, dependencies, _ = dependency_environment
    dependencies.add("nvim", "pacman", ["neovim"], validate=False)
    external = ConfigManager().load()
    nvim = next(target for target in external.targets if target.name == "nvim")
    external.targets = [
        type(target)(
            name=target.name,
            paths=target.paths,
            id=target.id,
            created_at=target.created_at,
            updated_at=target.updated_at,
            dependencies=[] if target.name == nvim.name else target.dependencies,
        )
        for target in external.targets
    ]
    ConfigManager().save(external)

    reopened = TargetManager(
        targets.database, targets.repository, targets.config_manager
    )

    assert reopened.dependency_manifest_changed


def test_check_does_not_fail_for_only_missing_optional_dependencies(
    dependency_environment, monkeypatch
):
    targets, dependencies, _ = dependency_environment
    dependencies.add(
        "nvim", "pacman", ["ripgrep"], optional=True, validate=False
    )
    monkeypatch.setattr("concord.cli.app.manager", lambda: targets)
    monkeypatch.setattr(
        "concord.application.dependencies.shutil.which",
        lambda executable: "/usr/bin/pacman" if executable == "pacman" else None,
    )
    monkeypatch.setattr(
        "concord.application.dependencies.subprocess.run",
        lambda command, **kwargs: completed(command, 127, "ripgrep\n"),
    )

    result = CliRunner().invoke(app, ["deps", "check", "nvim"])

    assert result.exit_code == 0, result.output
    assert "Faltante opcional" in result.output


def test_aur_dry_run_does_not_persist_detected_helper(
    dependency_environment, monkeypatch
):
    targets, dependencies, _ = dependency_environment
    dependencies.add(
        "nvim", "aur", ["visual-studio-code-bin"], validate=False
    )
    monkeypatch.setattr("concord.cli.app.manager", lambda: targets)
    monkeypatch.setattr(
        "concord.application.dependencies.shutil.which",
        lambda executable: f"/usr/bin/{executable}"
        if executable in {"pacman", "paru"}
        else None,
    )
    monkeypatch.setattr(
        "concord.application.dependencies.subprocess.run",
        lambda command, **kwargs: completed(
            command, 127, "visual-studio-code-bin\n"
        ),
    )

    result = CliRunner().invoke(
        app, ["deps", "install", "nvim", "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert "paru -S --needed -- visual-studio-code-bin" in result.output
    assert dependencies.configured_aur_helper() is None


def test_aur_helper_sources_are_fixed_to_official_https_repositories(
    dependency_environment,
):
    _, dependencies, _ = dependency_environment

    paru = dependencies.aur_helper_source("paru")
    yay = dependencies.aur_helper_source("yay")

    assert (paru.package, paru.url) == (
        "paru-bin",
        "https://aur.archlinux.org/paru-bin.git",
    )
    assert (yay.package, yay.url) == (
        "yay-bin",
        "https://aur.archlinux.org/yay-bin.git",
    )


def test_aur_helper_install_is_forbidden_without_terminal(
    dependency_environment, monkeypatch
):
    targets, _, _ = dependency_environment
    calls = []

    def dependency_factory(target_manager=None):
        return DependencyManager(
            targets.database,
            runner=lambda command, **kwargs: calls.append(command),
            which=lambda executable: (
                "/usr/bin/pacman" if executable == "pacman" else None
            ),
        )

    monkeypatch.setattr("concord.cli.app.manager", lambda: targets)
    monkeypatch.setattr("concord.cli.app.dependencies", dependency_factory)

    result = CliRunner().invoke(app, ["deps", "helper", "install"])

    assert result.exit_code == 1
    assert "prohibida en modo no interactivo" in result.output
    assert calls == []


def test_aur_dry_run_only_reports_missing_helper(
    dependency_environment, monkeypatch
):
    targets, dependencies, _ = dependency_environment
    dependencies.add("nvim", "aur", ["some-aur-package"], validate=False)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["pacman", "-T"]:
            return completed(command, 127, "some-aur-package\n")
        raise AssertionError(f"No debía ejecutarse: {command}")

    def dependency_factory(target_manager=None):
        return DependencyManager(
            targets.database,
            runner=fake_run,
            which=lambda executable: (
                "/usr/bin/pacman" if executable == "pacman" else None
            ),
        )

    monkeypatch.setattr("concord.cli.app.manager", lambda: targets)
    monkeypatch.setattr("concord.cli.app.dependencies", dependency_factory)

    result = CliRunner().invoke(
        app, ["deps", "install", "nvim", "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert "concord deps helper install" in result.output
    assert calls == [["pacman", "-T", "some-aur-package"]]


def test_interactive_aur_helper_bootstrap_installs_and_cleans_temporary_directory(
    dependency_environment, monkeypatch, capsys
):
    targets, _, _ = dependency_environment
    available = {"pacman", "sudo"}
    calls = []
    build_directories = []

    class Terminal:
        @staticmethod
        def isatty():
            return True

    class Confirmation:
        @staticmethod
        def ask():
            return True

    def which(executable):
        return f"/usr/bin/{executable}" if executable in available else None

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:2] == ["pacman", "-T"]:
            return completed(command, 127, "base-devel\ngit\n")
        if command[:3] == ["sudo", "pacman", "-S"]:
            available.update({"git", "makepkg"})
            return completed(command)
        if command[:3] == ["git", "clone", "--depth"]:
            destination = Path(command[-1])
            destination.mkdir(parents=True)
            (destination / "PKGBUILD").write_text(
                "pkgname=paru-bin\npkgver=1\n"
            )
            return completed(command)
        if len(command) >= 3 and command[:2] == ["git", "-C"]:
            return completed(
                command, stdout="https://aur.archlinux.org/paru-bin.git\n"
            )
        if command == ["makepkg", "-si"]:
            build_directories.append(Path(kwargs["cwd"]))
            available.add("paru")
            return completed(command)
        raise AssertionError(f"Comando inesperado: {command}")

    dependency_manager = DependencyManager(
        targets.database,
        runner=fake_run,
        which=which,
    )
    monkeypatch.setattr("concord.cli.app.sys.stdin", Terminal())
    monkeypatch.setattr("concord.cli.app.request_select", lambda *args: "paru")
    monkeypatch.setattr(
        "concord.cli.app.questionary.confirm", lambda *args, **kwargs: Confirmation()
    )
    monkeypatch.setattr("concord.application.dependencies.os.geteuid", lambda: 1000)

    from concord.cli.app import install_aur_helper_command

    selected = install_aur_helper_command(dependency_manager)

    assert selected == "paru"
    assert dependency_manager.configured_aur_helper() == "paru"
    assert build_directories and not build_directories[0].exists()
    makepkg_call = next(item for item in calls if item[0] == ["makepkg", "-si"])
    assert "capture_output" not in makepkg_call[1]
    assert "--noconfirm" not in makepkg_call[0]
    assert "pkgname=paru-bin" in capsys.readouterr().out


def test_failed_aur_helper_bootstrap_also_cleans_temporary_directory(
    dependency_environment, monkeypatch
):
    targets, _, _ = dependency_environment
    build_directories = []

    class Terminal:
        @staticmethod
        def isatty():
            return True

    class Confirmation:
        @staticmethod
        def ask():
            return True

    def fake_run(command, **kwargs):
        if command[:2] == ["pacman", "-T"]:
            return completed(command)
        if command[:3] == ["git", "clone", "--depth"]:
            destination = Path(command[-1])
            destination.mkdir(parents=True)
            (destination / "PKGBUILD").write_text("pkgname=yay-bin\n")
            return completed(command)
        if len(command) >= 3 and command[:2] == ["git", "-C"]:
            return completed(
                command, stdout="https://aur.archlinux.org/yay-bin.git\n"
            )
        raise AssertionError(f"Comando inesperado: {command}")

    dependency_manager = DependencyManager(
        targets.database,
        runner=fake_run,
        which=lambda executable: (
            f"/usr/bin/{executable}"
            if executable in {"pacman", "git", "makepkg"}
            else None
        ),
    )

    def fail_install(helper, source_dir):
        build_directories.append(source_dir)
        raise ValueError("fallo simulado de makepkg")

    monkeypatch.setattr("concord.cli.app.sys.stdin", Terminal())
    monkeypatch.setattr("concord.cli.app.request_select", lambda *args: "yay")
    monkeypatch.setattr(
        "concord.cli.app.questionary.confirm", lambda *args, **kwargs: Confirmation()
    )
    monkeypatch.setattr(
        dependency_manager, "install_cloned_aur_helper", fail_install
    )

    from concord.cli.app import install_aur_helper_command

    with pytest.raises(typer.Exit):
        install_aur_helper_command(dependency_manager)

    assert build_directories and not build_directories[0].exists()
    assert dependency_manager.configured_aur_helper() is None


def test_restore_can_install_dependencies_before_touching_home(
    dependency_environment, monkeypatch
):
    targets, dependencies, _ = dependency_environment
    dependencies.add("nvim", "pacman", ["neovim"], validate=False)
    local = targets.get("nvim").local_path
    local.unlink()
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["pacman", "-T"]:
            return completed(command, 127, "neovim\n")
        if command[:3] == ["sudo", "pacman", "-S"]:
            return completed(command)
        raise AssertionError(f"Comando inesperado: {command}")

    monkeypatch.setattr("concord.cli.app.manager", lambda: targets)
    monkeypatch.setattr("concord.application.dependencies.os.geteuid", lambda: 1000)
    monkeypatch.setattr("concord.application.dependencies.subprocess.run", fake_run)
    monkeypatch.setattr(
        "concord.application.dependencies.shutil.which",
        lambda executable: f"/usr/bin/{executable}"
        if executable in {"pacman", "sudo"}
        else None,
    )

    result = CliRunner().invoke(
        app, ["restore", "nvim", "--install-deps", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert local.read_text() == "nvim"
    assert any(command[:3] == ["sudo", "pacman", "-S"] for command in calls)


def test_dependency_install_failure_prevents_restore(
    dependency_environment, monkeypatch
):
    targets, dependencies, _ = dependency_environment
    dependencies.add("nvim", "pacman", ["neovim"], validate=False)
    local = targets.get("nvim").local_path
    local.unlink()

    def fake_run(command, **kwargs):
        if command[:2] == ["pacman", "-T"]:
            return completed(command, 127, "neovim\n")
        return completed(command, 1)

    monkeypatch.setattr("concord.cli.app.manager", lambda: targets)
    monkeypatch.setattr("concord.application.dependencies.os.geteuid", lambda: 0)
    monkeypatch.setattr("concord.application.dependencies.subprocess.run", fake_run)
    monkeypatch.setattr(
        "concord.application.dependencies.shutil.which",
        lambda executable: f"/usr/bin/{executable}"
        if executable in {"git", "pacman"}
        else None,
    )

    result = CliRunner().invoke(
        app, ["restore", "nvim", "--install-deps", "--yes"]
    )

    assert result.exit_code == 1
    assert not local.exists()
    assert "Pendientes" in result.output


def test_restore_dependency_dry_run_is_read_only(
    dependency_environment, monkeypatch
):
    targets, dependencies, _ = dependency_environment
    dependencies.add("nvim", "pacman", ["neovim"], validate=False)
    local = targets.get("nvim").local_path
    local.unlink()
    monkeypatch.setattr("concord.cli.app.manager", lambda: targets)
    monkeypatch.setattr("concord.application.dependencies.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "concord.application.dependencies.subprocess.run",
        lambda command, **kwargs: completed(command, 127, "neovim\n"),
    )
    monkeypatch.setattr(
        "concord.application.dependencies.shutil.which",
        lambda executable: f"/usr/bin/{executable}" if executable == "pacman" else None,
    )

    result = CliRunner().invoke(
        app,
        ["restore", "nvim", "--install-deps", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "Plan de instalación" in result.output
    assert "Simulación completada" in result.output
    assert not local.exists()


def test_restore_and_bootstrap_expose_dependency_options():
    runner = CliRunner()

    restore_help = runner.invoke(app, ["restore", "--help"]).output
    bootstrap_help = runner.invoke(app, ["bootstrap", "--help"]).output

    for output in (restore_help, bootstrap_help):
        assert "--install-deps" in output
        assert "--include-optional" in output
        assert "--yes" in output


def test_doctor_reports_required_and_optional_dependencies(
    dependency_environment, monkeypatch
):
    _, dependencies, _ = dependency_environment
    dependencies.add("nvim", "pacman", ["neovim"], validate=False)
    dependencies.add(
        "nvim", "pacman", ["ripgrep"], optional=True, validate=False
    )
    monkeypatch.setattr(
        "concord.application.doctor.shutil.which",
        lambda executable: "/usr/bin/pacman" if executable == "pacman" else None,
    )
    monkeypatch.setattr(
        "concord.application.doctor.subprocess.run",
        lambda command, **kwargs: completed(command, 127, "neovim\nripgrep\n"),
    )

    report = Doctor().run()

    required = next(
        check
        for check in report.checks
        if check.section == "Dependencias" and check.name == "Obligatorias"
    )
    optional = next(
        check
        for check in report.checks
        if check.section == "Dependencias" and check.name == "Opcionales"
    )
    assert required.state == "warning" and "neovim" in required.message
    assert optional.state == "pass" and "1 no instalada" in optional.message
    assert "Dependencias" in [timing.name for timing in report.timings]


def test_bootstrap_can_install_imported_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_home = tmp_path / "source-home"
    destination_home = tmp_path / "destination-home"
    source_home.mkdir()
    destination_home.mkdir()
    source_repository = tmp_path / "source-repository"
    destination_repository = tmp_path / "destination-repository"

    configure_concord_environment(source_home, source_repository, monkeypatch)
    Initializer().initialize(
        source_repository,
        git_identity=("Concord Test", "concord@example.com"),
    )
    source = source_home / ".bashrc"
    source.write_text("alias ll='ls -la'\n")
    TargetManager().add(source, "bash")
    DependencyManager().add("bash", "pacman", ["bash"], validate=False)
    TargetManager().sync(CONCORD_TARGET)
    GitManager(source_repository).commit([Path(".")], "bash: add dependencies")

    configure_concord_environment(
        destination_home, destination_repository, monkeypatch
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["pacman", "-T"]:
            return completed(command, 127, "bash\n")
        if command[:3] == ["pacman", "-S", "--needed"]:
            return completed(command)
        raise AssertionError(f"Comando inesperado: {command}")

    def dependency_factory(target_manager=None):
        assert target_manager is not None
        return DependencyManager(
            target_manager.database,
            target_manager.config_manager,
            runner=fake_run,
            which=lambda executable: (
                f"/usr/bin/{executable}" if executable == "pacman" else None
            ),
        )

    monkeypatch.setattr("concord.application.dependencies.os.geteuid", lambda: 0)
    monkeypatch.setattr("concord.cli.app.dependencies", dependency_factory)

    result = CliRunner().invoke(
        app,
        [
            "bootstrap",
            str(source_repository),
            "--repository",
            str(destination_repository),
            "--no-restore",
            "--install-deps",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert any(command[:3] == ["pacman", "-S", "--needed"] for command in calls)
    assert not (destination_home / ".bashrc").exists()
