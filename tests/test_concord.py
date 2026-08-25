from pathlib import Path

import pytest
from typer.testing import CliRunner

from concord.application.config import Config, ConfigManager
from concord.application.database import Database
from concord.application.initializer import Initializer
from concord.application.repository import RepositoryManager
from concord.application.target_manager import TargetManager
from concord.cli.app import app


def configure_environment(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from concord import application as concord

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(concord, "config_dir", home / ".config/concord")
    monkeypatch.setattr(concord, "config_file", home / ".config/concord/concord.toml")
    monkeypatch.setattr(concord, "database_file", home / ".local/share/concord/concord.db")
    monkeypatch.setattr(concord, "default_repository_dir", home / ".local/share/concord/repository")


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TargetManager, Path, Path]:
    from concord import application as concord

    home = tmp_path / "home"
    home.mkdir()
    configure_environment(home, monkeypatch)
    repository = tmp_path / "repository"
    config_dir = home / ".config/concord"
    data_dir = home / ".local/share/concord"
    monkeypatch.setattr(concord, "config_dir", config_dir)
    monkeypatch.setattr(concord, "config_file", config_dir / "concord.toml")
    monkeypatch.setattr(concord, "database_file", data_dir / "concord.db")
    ConfigManager().save(Config(repository_path=repository))
    instance = TargetManager(Database(tmp_path / "concord.db"), RepositoryManager(repository))
    return instance, home, repository


def test_add_file_uses_home_relative_path(manager):
    instance, home, repository = manager
    source = home / ".bashrc"
    source.write_text("alias ll='ls -lah'\n")
    target = instance.add(source)
    assert target.name == "dot_bashrc"
    assert (repository / "dot_bashrc/.bashrc").read_text() == source.read_text()


def test_add_directory_and_custom_name(manager):
    instance, home, repository = manager
    source = home / ".config/nvim"
    source.mkdir(parents=True)
    (source / "init.lua").write_text("-- init")
    instance.add(source, "editor")
    assert (repository / "editor/.config/nvim/init.lua").exists()


def test_duplicate_path_is_rejected(manager):
    instance, home, _ = manager
    source = home / ".bashrc"
    source.touch()
    instance.add(source, "bash")
    with pytest.raises(ValueError):
        instance.add(source, "shell")


def test_status_sync_and_restore(manager):
    instance, home, _ = manager
    source = home / ".bashrc"
    source.write_text("one")
    instance.add(source, "bash")
    assert next(item for item in instance.status() if item.name == "bash").state == "clean"
    source.write_text("two")
    assert next(item for item in instance.status() if item.name == "bash").state == "modified"
    instance.sync("bash")
    assert next(item for item in instance.status() if item.name == "bash").state == "clean"
    source.unlink()
    assert next(item for item in instance.status() if item.name == "bash").state == "missing"
    instance.restore("bash")
    assert source.read_text() == "two"


def test_restore_requires_force(manager):
    instance, home, _ = manager
    source = home / ".bashrc"
    source.write_text("one")
    instance.add(source, "bash")
    with pytest.raises(FileExistsError):
        instance.restore("bash")


def test_remove_keeps_local_file(manager):
    instance, home, repository = manager
    source = home / ".bashrc"
    source.touch()
    instance.add(source, "bash")
    instance.remove("bash")
    assert source.exists()
    assert not (repository / "bash").exists()
    assert [target.name for target in instance.list()] == ["concord"]


def test_init_does_not_load_configuration_before_creating_it(tmp_path, monkeypatch):
    from concord import application as concord

    home = tmp_path / "home"
    config_dir = home / ".config/concord"
    data_dir = home / ".local/share/concord"
    repository = data_dir / "repository"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(concord, "config_dir", config_dir)
    monkeypatch.setattr(concord, "config_file", config_dir / "concord.toml")
    monkeypatch.setattr(concord, "database_file", data_dir / "concord.db")
    monkeypatch.setattr(
        "concord.application.config.ConfigManager.request_configuration",
        lambda self: Config(repository_path=repository),
    )

    Initializer().initialize()

    assert concord.config_file.exists()
    assert concord.database_file.exists()
    assert repository.is_dir()


def test_uninitialized_command_shows_helpful_error_without_traceback(tmp_path, monkeypatch):
    from concord import application as concord

    monkeypatch.setattr(concord, "config_file", tmp_path / "missing.toml")
    result = CliRunner().invoke(app, ["list"])

    assert result.exit_code == 1
    assert "todavía no está inicializado" in result.output
    assert "concord init" in result.output
    assert "Traceback" not in result.output


def test_manifest_is_automatically_synchronized(manager):
    instance, home, repository = manager
    source = home / ".bashrc"
    source.touch()

    instance.add(source, "bash")

    local = ConfigManager().load()
    remote = ConfigManager().load(repository / "concord/.config/concord/concord.toml")
    assert [target.name for target in local.targets] == ["concord", "bash"]
    assert remote.targets == local.targets


def test_concord_target_cannot_be_removed(manager):
    instance, _, _ = manager
    with pytest.raises(ValueError, match="reservado"):
        instance.remove("concord")


def test_repository_bootstraps_a_new_home(tmp_path, monkeypatch):
    first_home = tmp_path / "first-home"
    second_home = tmp_path / "second-home"
    repository = tmp_path / "repository"
    first_home.mkdir()
    second_home.mkdir()

    configure_environment(first_home, monkeypatch)
    Initializer().initialize(repository)
    source = first_home / ".bashrc"
    source.write_text("alias ll='ls -lah'\n")
    TargetManager().add(source, "bash")

    configure_environment(second_home, monkeypatch)
    Initializer().initialize(repository)
    restored = TargetManager().restore_all()

    assert [target.name for target in restored] == ["bash"]
    assert (second_home / ".bashrc").read_text() == "alias ll='ls -lah'\n"
    assert ConfigManager().load().repository_path == repository.resolve()
