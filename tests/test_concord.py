from pathlib import Path

import pytest
from typer.testing import CliRunner

from concord.application.config import Config, ConfigManager, GitConfig
from concord.application.database import Database
from concord.application.git import GitManager
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
    original = TargetManager().get("bash")

    configure_environment(second_home, monkeypatch)
    Initializer().initialize(repository)
    restored = TargetManager().restore_all()

    assert [target.name for target in restored] == ["bash"]
    assert (second_home / ".bashrc").read_text() == "alias ll='ls -lah'\n"
    assert ConfigManager().load().repository_path == repository.resolve()
    imported = TargetManager().get("bash")
    assert imported.created_at == original.created_at
    assert imported.updated_at == original.updated_at


def test_sync_updates_last_modification_without_changing_creation(manager):
    instance, home, _ = manager
    source = home / ".bashrc"
    source.write_text("one")
    created = instance.add(source, "bash")
    source.write_text("two")

    instance.sync("bash")
    synchronized = instance.get("bash")

    assert synchronized.created_at == created.created_at
    assert synchronized.updated_at > created.updated_at


def test_database_migrates_updated_at_column(tmp_path):
    database = Database(tmp_path / "legacy.db")
    with database.connect() as connection:
        connection.execute(
            """
            CREATE TABLE targets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                local_path TEXT NOT NULL,
                type TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO targets VALUES ('1', 'bash', '/tmp/.bashrc', 'file', '2026-01-01T00:00:00+00:00')"
        )

    database.initialize()

    with database.connect() as connection:
        row = connection.execute(
            "SELECT created_at, updated_at FROM targets WHERE id = '1'"
        ).fetchone()
    assert row == ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00")


def test_diff_reports_added_modified_and_deleted_files(manager):
    instance, home, _ = manager
    source = home / ".config/nvim"
    source.mkdir(parents=True)
    (source / "modified.lua").write_text("before")
    (source / "deleted.lua").write_text("delete me")
    instance.add(source, "nvim")

    (source / "modified.lua").write_text("after")
    (source / "deleted.lua").unlink()
    (source / "added.lua").write_text("new")

    result = instance.diff("nvim")[0]

    assert {(entry.state, entry.relative_path.as_posix()) for entry in result.entries} == {
        ("added", ".config/nvim/added.lua"),
        ("modified", ".config/nvim/modified.lua"),
        ("deleted", ".config/nvim/deleted.lua"),
    }


def test_diff_is_read_only_and_clean_after_sync(manager):
    instance, home, _ = manager
    source = home / ".bashrc"
    source.write_text("one")
    instance.add(source, "bash")
    before = instance.get("bash").updated_at

    assert instance.diff("bash")[0].clean
    assert instance.get("bash").updated_at == before
    source.write_text("two")
    assert not instance.diff("bash")[0].clean
    assert instance.get("bash").updated_at == before
    instance.sync("bash")
    assert instance.diff("bash")[0].clean


def test_diff_detects_changed_symbolic_link(manager):
    instance, home, _ = manager
    source = home / ".config/example"
    source.mkdir(parents=True)
    link = source / "current"
    link.symlink_to("first")
    instance.add(source, "example")

    link.unlink()
    link.symlink_to("second")

    entry = instance.diff("example")[0].entries[0]
    assert entry.state == "modified"
    assert entry.relative_path == Path(".config/example/current")


def test_sync_preview_is_read_only(manager):
    instance, home, repository = manager
    source = home / ".bashrc"
    source.write_text("before")
    instance.add(source, "bash")
    source.write_text("after")
    before_timestamp = instance.get("bash").updated_at
    before_copy = (repository / "bash/.bashrc").read_text()

    preview = instance.preview_sync("bash")[0]

    assert [(entry.state, entry.relative_path) for entry in preview.entries] == [
        ("modified", Path(".bashrc"))
    ]
    assert (repository / "bash/.bashrc").read_text() == before_copy
    assert instance.get("bash").updated_at == before_timestamp


def test_restore_preview_reverses_changes_and_is_read_only(manager):
    instance, home, repository = manager
    source = home / ".config/nvim"
    source.mkdir(parents=True)
    (source / "modified.lua").write_text("before")
    (source / "repo-only.lua").write_text("stored")
    instance.add(source, "nvim")
    (source / "modified.lua").write_text("after")
    (source / "repo-only.lua").unlink()
    (source / "local-only.lua").write_text("local")
    before_timestamp = instance.get("nvim").updated_at

    preview = instance.preview_restore("nvim")[0]

    assert {(entry.state, entry.relative_path.as_posix()) for entry in preview.entries} == {
        ("deleted", ".config/nvim/local-only.lua"),
        ("modified", ".config/nvim/modified.lua"),
        ("added", ".config/nvim/repo-only.lua"),
    }
    assert (source / "modified.lua").read_text() == "after"
    assert (repository / "nvim/.config/nvim/modified.lua").read_text() == "before"
    assert instance.get("nvim").updated_at == before_timestamp


def test_restore_all_preview_excludes_concord(manager):
    instance, home, _ = manager
    source = home / ".bashrc"
    source.write_text("one")
    instance.add(source, "bash")

    assert [item.name for item in instance.preview_restore()] == ["bash"]


def test_sync_and_restore_help_include_dry_run():
    runner = CliRunner()

    assert "--dry-run" in runner.invoke(app, ["sync", "--help"]).output
    assert "--dry-run" in runner.invoke(app, ["restore", "--help"]).output


def test_git_configuration_round_trip(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    configure_environment(home, monkeypatch)
    expected = Config(
        repository_path=home / "dotfiles",
        git=GitConfig(enabled=True, auto_commit=False, auto_push=True, remote="upstream"),
    )

    ConfigManager().save(expected)
    actual = ConfigManager().load()

    assert actual.git == expected.git


def test_initializer_creates_git_repository_and_initial_commit(tmp_path, monkeypatch):
    from concord import application as concord

    home = tmp_path / "home"
    home.mkdir()
    configure_environment(home, monkeypatch)
    repository = tmp_path / "repository"

    Initializer().initialize(
        repository,
        git_identity=("Concord Test", "concord@example.com"),
    )

    git = GitManager(repository)
    status = git.status()
    assert git.initialized
    assert status.branch == "main"
    assert status.message == "concord: initialize repository"
    assert status.clean
    assert (repository / ".gitignore").exists()
    assert concord.config_file.exists()


def test_git_commit_only_stages_paths_from_current_operation(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git = GitManager(repository)
    git.initialize()
    git.set_identity("Concord Test", "concord@example.com")
    (repository / "bash").mkdir()
    (repository / "nvim").mkdir()
    (repository / "bash/config").write_text("one")
    (repository / "nvim/config").write_text("one")
    git.commit([Path(".")], "initial")
    (repository / "bash/config").write_text("pending")
    (repository / "nvim/config").write_text("committed")

    commit = git.commit([Path("nvim")], "concord: sync nvim")

    assert commit is not None
    changed = git._run("show", "--name-only", "--pretty=", "HEAD").stdout.splitlines()
    assert changed == ["nvim/config"]
    assert git._run("status", "--porcelain", "--", "bash").stdout.startswith(" M")


def test_sync_does_not_create_metadata_changes_when_target_is_clean(manager):
    instance, home, _ = manager
    source = home / ".bashrc"
    source.write_text("one")
    instance.add(source, "bash")
    before = instance.get("bash").updated_at

    synchronized = instance.sync("bash")

    assert synchronized == []
    assert instance.get("bash").updated_at == before


def test_repo_commands_and_bootstrap_are_exposed():
    runner = CliRunner()
    root_help = runner.invoke(app, ["--help"]).output
    repo_help = runner.invoke(app, ["repo", "--help"]).output

    assert "bootstrap" in root_help
    for command in ("status", "log", "diff", "commit", "push", "pull", "remote", "init"):
        assert command in repo_help


def test_cli_init_and_add_create_automatic_commits(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    configure_environment(home, monkeypatch)
    git_config = tmp_path / "gitconfig"
    git_config.write_text(
        "[user]\n\tname = Concord Test\n\temail = concord@example.com\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    repository = tmp_path / "repository"
    runner = CliRunner()

    initialized = runner.invoke(app, ["init", "--repository", str(repository)])
    source = home / ".bashrc"
    source.write_text("alias ll='ls -la'\n")
    added = runner.invoke(app, ["add", str(source), "--name", "bash", "--yes"])

    assert initialized.exit_code == 0, initialized.output
    assert added.exit_code == 0, added.output
    assert [message for _, _, message in GitManager(repository).log()] == [
        "concord: add bash",
        "concord: initialize repository",
    ]


def test_sensitive_files_are_detected_before_first_push(tmp_path):
    repository = tmp_path / "repository"
    (repository / "app").mkdir(parents=True)
    (repository / "app/.env").write_text("TOKEN=secret")
    (repository / "app/settings.toml").write_text("theme = 'nord'")
    git = GitManager(repository)

    assert git.sensitive_files() == [Path("app/.env")]
