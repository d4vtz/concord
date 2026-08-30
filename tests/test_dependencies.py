import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from concord import application as concord
from concord.application.config import Config, ConfigManager
from concord.application.database import Database
from concord.application.dependencies import (DependencyInstallError,
                                              DependencyManager)
from concord.application.profile_manager import ProfileManager
from concord.application.repository import RepositoryManager
from concord.application.target_manager import TargetManager
from concord.cli.app import app


@pytest.fixture
def dependency_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    config_dir = home / ".config/concord"
    data_dir = home / ".local/share/concord"
    repository = tmp_path / "repository"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(concord, "config_dir", config_dir)
    monkeypatch.setattr(concord, "config_file", config_dir / "concord.toml")
    monkeypatch.setattr(concord, "database_file", data_dir / "concord.db")
    monkeypatch.setattr(concord, "default_repository_dir", repository)
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

    def fake_run(command, **kwargs):
        if command[:2] == ["pacman", "-T"]:
            return completed(command, 127, "neovim\nripgrep\n")
        raise AssertionError(f"No debía ejecutarse: {command}")

    monkeypatch.setattr("concord.application.dependencies.subprocess.run", fake_run)
    monkeypatch.setattr(
        "concord.application.dependencies.shutil.which",
        lambda executable: f"/usr/bin/{executable}" if executable == "pacman" else None,
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
