from pathlib import Path

import pytest

from concord.application.config import Config
from concord.application.database import Database
from concord.application.initializer import Initializer
from concord.application.repository import RepositoryManager
from concord.application.target_manager import TargetManager


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TargetManager, Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repository = tmp_path / "repository"
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
    assert instance.status()[0].state == "clean"
    source.write_text("two")
    assert instance.status()[0].state == "modified"
    instance.sync("bash")
    assert instance.status()[0].state == "clean"
    source.unlink()
    assert instance.status()[0].state == "missing"
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
    assert instance.list() == []


def test_init_does_not_load_configuration_before_creating_it(tmp_path, monkeypatch):
    from concord import application as concord

    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    repository = data_dir / "repository"
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
