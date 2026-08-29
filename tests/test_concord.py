import os
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from concord.application.config import Config, ConfigManager, GitConfig
from concord.application.database import Database
from concord.application.doctor import Doctor
from concord.application.git import GitManager
from concord.application.initializer import Initializer
from concord.application.repository import RepositoryManager
from concord.application.target_manager import TargetManager
from concord.cli.app import app, editor_command, sync_commit_message
from concord.cli.completion import (complete_editables,
                                    complete_removable_targets,
                                    complete_targets)


def configure_environment(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from concord import application as concord

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(concord, "config_dir", home / ".config/concord")
    monkeypatch.setattr(concord, "data_dir", home / ".local/share/concord")
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


def test_target_can_add_multiple_paths(manager):
    instance, home, repository = manager
    directory = home / ".config/zsh"
    directory.mkdir(parents=True)
    (directory / ".zshrc").write_text("source plugins\n")
    environment = home / ".zshenv"
    environment.write_text("ZDOTDIR=$HOME/.config/zsh\n")
    instance.add(directory, "zsh")
    (directory / ".zshrc").write_text("local change not synchronized\n")

    target = instance.add_path("zsh", environment)

    assert [path.local_path for path in target.paths] == [directory, environment]
    assert (repository / "zsh/.config/zsh/.zshrc").exists()
    assert (repository / "zsh/.config/zsh/.zshrc").read_text() == "source plugins\n"
    assert (repository / "zsh/.zshenv").read_text() == environment.read_text()
    manifest = ConfigManager().load()
    zsh = next(item for item in manifest.targets if item.name == "zsh")
    assert [path.relative_path for path in zsh.paths] == [Path(".config/zsh"), Path(".zshenv")]


def test_overlapping_paths_are_rejected_even_within_same_target(manager):
    instance, home, _ = manager
    directory = home / ".config/nvim"
    nested = directory / "lua"
    nested.mkdir(parents=True)
    instance.add(directory, "nvim")

    with pytest.raises(ValueError, match="solapa"):
        instance.add_path("nvim", nested)
    with pytest.raises(ValueError, match="solapa"):
        instance.add(nested, "lua")


def test_existing_target_requires_add_path_command(manager):
    instance, home, _ = manager
    first = home / ".zshenv"
    second = home / ".zprofile"
    first.touch()
    second.touch()
    instance.add(first, "zsh")

    with pytest.raises(ValueError, match="add-path"):
        instance.add(second, "zsh")


def test_remove_path_keeps_local_file_and_rejects_empty_target(manager):
    instance, home, repository = manager
    first = home / ".zshenv"
    second = home / ".zprofile"
    first.write_text("one")
    second.write_text("two")
    instance.add(first, "zsh")
    instance.add_path("zsh", second)

    target = instance.remove_path("zsh", second)

    assert second.exists()
    assert not (repository / "zsh/.zprofile").exists()
    assert [path.local_path for path in target.paths] == [first]
    with pytest.raises(ValueError, match="concord remove zsh"):
        instance.remove_path("zsh", first)


def test_sync_all_validates_every_path_before_writing(manager):
    instance, home, repository = manager
    first = home / ".first"
    second = home / ".second"
    first.write_text("stored first")
    second.write_text("stored second")
    instance.add(first, "first")
    instance.add(second, "second")
    first.write_text("changed first")
    second.unlink()

    with pytest.raises(FileNotFoundError):
        instance.sync()

    assert (repository / "first/.first").read_text() == "stored first"


def test_restore_multi_path_aborts_before_writing_on_collision(manager):
    instance, home, _ = manager
    first = home / ".zshenv"
    second = home / ".zprofile"
    first.write_text("stored first")
    second.write_text("stored second")
    instance.add(first, "zsh")
    instance.add_path("zsh", second)
    first.unlink()
    second.write_text("local collision")

    with pytest.raises(FileExistsError):
        instance.restore("zsh")

    assert not first.exists()
    assert second.read_text() == "local collision"


def test_status_counts_changed_paths_and_prioritizes_missing(manager):
    instance, home, _ = manager
    first = home / ".zshenv"
    second = home / ".zprofile"
    first.write_text("one")
    second.write_text("two")
    instance.add(first, "zsh")
    instance.add_path("zsh", second)
    first.write_text("changed")
    second.unlink()

    status = next(item for item in instance.status() if item.name == "zsh")

    assert status.state == "missing"
    assert (status.changed_paths, status.total_paths) == (2, 2)


def test_list_groups_relative_paths_and_status_hides_path_counts(manager, monkeypatch):
    instance, home, _ = manager
    first = home / ".zshenv"
    second = home / ".config/zsh"
    first.write_text("one")
    second.mkdir(parents=True)
    (second / ".zshrc").write_text("two")
    instance.add(first, "zsh")
    instance.add_path("zsh", second)
    monkeypatch.setattr("concord.cli.app.manager", lambda: instance)

    listed = CliRunner().invoke(app, ["list"])

    assert listed.exit_code == 0, listed.output
    assert "~/.zshenv" in listed.output
    assert "~/.config/zsh" in listed.output
    assert str(home) not in listed.output
    assert listed.output.count("├") >= len(instance.list())

    first.write_text("changed")
    status_result = CliRunner().invoke(app, ["status"])

    assert status_result.exit_code == 0, status_result.output
    assert "Modificado" in status_result.output
    assert "1/2 rutas" not in status_result.output


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


def test_first_command_migrates_manifest_and_database_to_v2(tmp_path, monkeypatch):
    from concord import application as concord

    home = tmp_path / "home"
    home.mkdir()
    configure_environment(home, monkeypatch)
    repository = tmp_path / "repository"
    repository.mkdir()
    concord.config_dir.mkdir(parents=True)
    concord.config_file.write_text(
        """
version = 1
repository_path = "REPOSITORY"

[git]
enabled = true
auto_commit = true
auto_push = false
remote = "origin"

[[targets]]
name = "concord"
relative_path = ".config/concord"
type = "directory"
created_at = "2026-01-01T00:00:00+00:00"
updated_at = "2026-01-01T00:00:00+00:00"
""".replace("REPOSITORY", str(repository))
    )
    database = Database(concord.database_file)
    with database.connect() as connection:
        connection.execute(
            """
            CREATE TABLE targets (
                id TEXT PRIMARY KEY, name TEXT UNIQUE, local_path TEXT,
                type TEXT, created_at TEXT, updated_at TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO targets VALUES (?, ?, ?, ?, ?, ?)",
            (
                "1",
                "concord",
                str(concord.config_dir),
                "directory",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
    git = GitManager(repository)
    git.initialize()
    git.set_identity("Concord Test", "concord@example.com")
    (repository / "old").write_text("fixture")
    git.commit([Path(".")], "initial")

    result = CliRunner().invoke(app, ["list"])

    assert result.exit_code == 0, result.output
    assert ConfigManager().load().source_version == 2
    assert "paths" in concord.config_file.read_text()
    assert list((concord.data_dir / "backups").glob("concord-v1-*.toml"))
    with database.connect() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(targets)")}
        count = connection.execute("SELECT COUNT(*) FROM target_paths").fetchone()[0]
    assert "local_path" not in columns
    assert count == 1
    assert git.log()[0][2] == "concord: migrate manifest v2"


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


def test_sync_commit_message_highlights_single_changed_target():
    assert sync_commit_message(["nvim"]) == "nvim: sync target"


def test_sync_commit_message_stays_general_for_multiple_targets():
    assert sync_commit_message(["nvim", "zsh"]) == "concord: sync all targets"


def test_sync_commit_message_rejects_empty_changes():
    with pytest.raises(ValueError, match="No hay targets modificados"):
        sync_commit_message([])


def test_editor_command_prefers_visual_and_preserves_arguments(monkeypatch):
    monkeypatch.setenv("VISUAL", "nvim -f")
    monkeypatch.setenv("EDITOR", "vim")
    monkeypatch.setattr("concord.cli.app.shutil.which", lambda name: f"/usr/bin/{name}")

    assert editor_command() == ["nvim", "-f"]


def test_reserved_target_names_are_rejected(manager):
    instance, home, _ = manager
    source = home / ".config/example"
    source.mkdir(parents=True)

    for name in ("ignore", "manifest", "config"):
        with pytest.raises(ValueError, match="reservado"):
            instance.add(source, name)


def test_edit_target_opens_local_path_without_syncing(manager, monkeypatch):
    instance, home, repository = manager
    source = home / ".bashrc"
    source.write_text("before")
    instance.add(source, "bash")

    def fake_editor(path: Path) -> int:
        assert path == source.resolve()
        path.write_text("after")
        return 0

    monkeypatch.setattr("concord.cli.app.open_in_editor", fake_editor)
    monkeypatch.setattr("concord.cli.app.manager", lambda: instance)
    result = CliRunner().invoke(app, ["edit", "bash"])

    assert result.exit_code == 0, result.output
    assert source.read_text() == "after"
    assert (repository / "bash/.bashrc").read_text() == "before"


def test_edit_multi_path_requires_or_accepts_explicit_path(manager, monkeypatch):
    instance, home, _ = manager
    first = home / ".zshenv"
    second = home / ".zprofile"
    first.write_text("one")
    second.write_text("two")
    instance.add(first, "zsh")
    instance.add_path("zsh", second)
    opened = []
    monkeypatch.setattr("concord.cli.app.manager", lambda: instance)
    monkeypatch.setattr(
        "concord.cli.app.open_in_editor", lambda path: opened.append(path) or 0
    )

    ambiguous = CliRunner().invoke(app, ["edit", "zsh"])
    selected = CliRunner().invoke(app, ["edit", "zsh", "--path", str(second)])

    assert ambiguous.exit_code == 1
    assert "contiene varias rutas" in ambiguous.output
    assert selected.exit_code == 0, selected.output
    assert opened == [second.resolve()]


def test_edit_ignore_untracks_commits_and_pushes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    configure_environment(home, monkeypatch)
    repository = tmp_path / "repository"
    remote = tmp_path / "remote.git"
    remote.mkdir()
    GitManager(remote)._run("init", "--bare")
    Initializer().initialize(
        repository,
        git_identity=("Concord Test", "concord@example.com"),
    )
    git = GitManager(repository)
    git.set_remote(str(remote))
    secret = repository / "private.env"
    secret.write_text("TOKEN=test\n")
    git.commit([Path("private.env")], "add fixture")

    def fake_editor(path: Path) -> int:
        path.write_text(path.read_text() + "private.env\n")
        return 0

    monkeypatch.setattr("concord.cli.app.open_in_editor", fake_editor)
    result = CliRunner().invoke(app, ["edit", "ignore"])

    assert result.exit_code == 0, result.output
    assert secret.exists()
    assert "private.env" not in git._run("ls-files").stdout.splitlines()
    assert git.log()[0][2] == "concord: update ignore rules"
    remote_head = git._run(
        "--git-dir", str(remote), "log", "--all", "-1", "--pretty=%s"
    ).stdout.strip()
    assert remote_head == "concord: update ignore rules"


def test_target_completion_reads_manifest_and_includes_paths(manager):
    instance, home, _ = manager
    source = home / ".config/nvim"
    source.mkdir(parents=True)
    instance.add(source, "nvim")

    assert ("nvim", str(source)) in complete_targets("nv")
    assert complete_targets("missing") == []


def test_target_completion_falls_back_to_read_only_database(tmp_path, monkeypatch):
    from concord import application as concord

    home = tmp_path / "home"
    home.mkdir()
    configure_environment(home, monkeypatch)
    database = Database(concord.database_file)
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO targets VALUES (?, ?, ?, ?)", ("1", "zsh", "now", "now"))
        connection.execute(
            "INSERT INTO target_paths VALUES (?, ?, ?, ?, ?)",
            ("p1", "1", str(home / ".config/zsh"), "directory", 0),
        )

    assert complete_targets("zs") == [("zsh", str(home / ".config/zsh"))]
    assert not concord.config_dir.exists()


def test_target_completion_is_silent_without_manifest_or_database(tmp_path, monkeypatch):
    from concord import application as concord

    home = tmp_path / "home"
    home.mkdir()
    configure_environment(home, monkeypatch)

    assert complete_targets("") == []
    assert complete_editables("") == []
    assert not concord.config_dir.exists()
    assert not concord.database_file.parent.exists()


def test_completion_filters_internal_target_and_adds_ignore(manager):
    instance, home, _ = manager
    source = home / ".bashrc"
    source.write_text("test")
    instance.add(source, "bash")

    assert any(name == "concord" for name, _ in complete_targets(""))
    assert all(name != "concord" for name, _ in complete_removable_targets(""))
    assert any(name == "ignore" for name, _ in complete_editables(""))


def test_zsh_completion_protocol_returns_dynamic_target(manager):
    instance, home, _ = manager
    source = home / ".config/nvim"
    source.mkdir(parents=True)
    instance.add(source, "nvim")

    result = CliRunner().invoke(
        app,
        [],
        prog_name="concord",
        env={
            "_CONCORD_COMPLETE": "complete_zsh",
            "_TYPER_COMPLETE_ARGS": "concord sync nv",
        },
    )

    assert result.exit_code == 0, result.output
    assert "nvim" in result.output
    assert str(source) in result.output.replace("\n", "")


def test_repo_commands_and_bootstrap_are_exposed():
    runner = CliRunner()
    root_help = runner.invoke(app, ["--help"]).output
    repo_help = runner.invoke(app, ["repo", "--help"]).output

    assert "bootstrap" in root_help
    assert "add-path" in root_help
    assert "remove-path" in root_help
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


def test_cli_global_sync_highlights_only_changed_target(tmp_path, monkeypatch):
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

    runner.invoke(app, ["init", "--repository", str(repository)])
    source = home / ".bashrc"
    source.write_text("one\n")
    runner.invoke(app, ["add", str(source), "--name", "bash", "--yes"])
    source.write_text("two\n")

    synchronized = runner.invoke(app, ["sync", "--yes"])

    assert synchronized.exit_code == 0, synchronized.output
    assert GitManager(repository).log()[0][2] == "bash: sync target"


def test_cli_add_and_remove_path_create_specific_commits(tmp_path, monkeypatch):
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
    runner.invoke(app, ["init", "--repository", str(repository)])
    first = home / ".zshenv"
    second = home / ".zprofile"
    first.write_text("one")
    second.write_text("two")
    runner.invoke(app, ["add", str(first), "--name", "zsh", "--yes"])

    added = runner.invoke(app, ["add-path", "zsh", str(second), "--yes"])
    removed = runner.invoke(app, ["remove-path", "zsh", str(second), "--yes"])

    assert added.exit_code == 0, added.output
    assert removed.exit_code == 0, removed.output
    messages = [message for _, _, message in GitManager(repository).log()[:2]]
    assert messages == ["zsh: remove .zprofile", "zsh: add .zprofile"]


def test_cli_sync_dry_run_previews_message_for_changed_targets(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    configure_environment(home, monkeypatch)
    repository = tmp_path / "repository"
    runner = CliRunner()

    initialized = runner.invoke(app, ["init", "--repository", str(repository)])
    assert initialized.exit_code == 0, initialized.output
    source = home / ".bashrc"
    source.write_text("one\n")
    runner.invoke(app, ["add", str(source), "--name", "bash"])
    config = ConfigManager().load()
    config.git = GitConfig(enabled=True, auto_commit=True)
    ConfigManager().save(config)
    source.write_text("two\n")

    preview = runner.invoke(app, ["sync", "--dry-run"])

    assert preview.exit_code == 0, preview.output
    assert "bash: sync target" in preview.output


def test_sensitive_files_are_detected_before_first_push(tmp_path):
    repository = tmp_path / "repository"
    (repository / "app").mkdir(parents=True)
    (repository / "app/.env").write_text("TOKEN=secret")
    (repository / "app/settings.toml").write_text("theme = 'nord'")
    git = GitManager(repository)

    assert git.sensitive_files() == [Path("app/.env")]


def test_sensitive_files_use_precise_names_and_ignore_resources(tmp_path):
    repository = tmp_path / "repository"
    files = {
        "app/.env.production": "TOKEN=secret",
        "app/credentials.json": '{"token": "secret"}',
        "ssh/id_ed25519": "private key",
        "certificates/server.key": "private key",
        "certificates/client.p12": "certificate",
        "icons/credentials-preferences.svg": "<svg />",
        "docs/token-guide.txt": "documentation",
        "themes/secrets-dark.png": "image",
    }
    for relative, content in files.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git = GitManager(repository)

    assert git.sensitive_files() == [
        Path("app/.env.production"),
        Path("app/credentials.json"),
        Path("certificates/client.p12"),
        Path("certificates/server.key"),
        Path("ssh/id_ed25519"),
    ]


def test_sensitive_files_use_git_candidates_and_respect_gitignore(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git = GitManager(repository)
    git.initialize()
    (repository / ".gitignore").write_text("ignored/\n")
    (repository / "tracked").mkdir()
    (repository / "tracked/.env").write_text("TOKEN=tracked")
    (repository / "ignored").mkdir()
    (repository / "ignored/.env").write_text("TOKEN=ignored")

    assert git.sensitive_files() == [Path("tracked/.env")]


def test_doctor_file_comparison_uses_metadata_fast_path(tmp_path, monkeypatch):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    left_file = left / "nested.conf"
    right_file = right / "nested.conf"
    left_file.write_text("same content")
    right_file.write_text("same content")
    timestamp = 1_700_000_000_000_000_000
    os.utime(left_file, ns=(timestamp, timestamp))
    os.utime(right_file, ns=(timestamp, timestamp))
    monkeypatch.setattr(
        "concord.application.doctor.filecmp.cmp",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("deep comparison")),
    )
    monkeypatch.setattr(
        Path,
        "rglob",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Path.rglob")),
    )

    assert Doctor()._paths_equal(left, right)


def test_doctor_file_comparison_reads_changed_metadata_candidates(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.write_text("first")
    right.write_text("other")
    os.utime(left, ns=(1_700_000_000_000_000_000,) * 2)
    os.utime(right, ns=(1_700_000_001_000_000_000,) * 2)

    assert not Doctor()._paths_equal(left, right)


def test_doctor_reports_uninitialized_installation_without_creating_files(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    configure_environment(home, monkeypatch)

    report = Doctor().run()

    assert report.failures == 1
    assert report.checks[0].name == "Configuración"
    assert [timing.name for timing in report.timings] == ["Configuración"]
    assert report.elapsed >= 0
    assert not (home / ".config/concord").exists()
    assert not (home / ".local/share/concord").exists()


def test_doctor_accepts_a_healthy_local_installation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    configure_environment(home, monkeypatch)
    repository = tmp_path / "repository"
    Initializer().initialize(
        repository,
        git_identity=("Concord Test", "concord@example.com"),
    )

    report = Doctor().run()

    assert report.failures == 0
    assert report.passed >= 10
    assert any(check.name == "Remoto" and check.state == "warning" for check in report.checks)


def test_doctor_detects_database_manifest_mismatch(tmp_path, monkeypatch):
    from concord import application as concord

    home = tmp_path / "home"
    home.mkdir()
    configure_environment(home, monkeypatch)
    Initializer().initialize(
        tmp_path / "repository",
        git_identity=("Concord Test", "concord@example.com"),
    )
    with Database(concord.database_file).connect() as connection:
        connection.execute("DELETE FROM targets WHERE name = 'concord'")

    report = Doctor().run()

    check = next(item for item in report.checks if item.name == "Índice local")
    assert check.state == "failure"
    assert "concord import --replace" in check.hint


def test_doctor_accepts_multiple_paths_and_detects_path_mismatch(tmp_path, monkeypatch):
    from concord import application as concord

    home = tmp_path / "home"
    home.mkdir()
    configure_environment(home, monkeypatch)
    Initializer().initialize(
        tmp_path / "repository",
        git_identity=("Concord Test", "concord@example.com"),
    )
    first = home / ".zshenv"
    second = home / ".zprofile"
    first.write_text("one")
    second.write_text("two")
    TargetManager().add(first, "zsh")
    TargetManager().add_path("zsh", second)

    assert Doctor().run().failures == 0

    with Database(concord.database_file).connect() as connection:
        connection.execute("DELETE FROM target_paths WHERE local_path = ?", (str(second),))
    report = Doctor().run()
    check = next(item for item in report.checks if item.name == "Índice local")
    assert check.state == "failure"


def test_doctor_command_returns_success_with_only_warnings(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    configure_environment(home, monkeypatch)
    Initializer().initialize(
        tmp_path / "repository",
        git_identity=("Concord Test", "concord@example.com"),
    )

    result = CliRunner().invoke(app, ["doctor"])
    strict = CliRunner().invoke(app, ["doctor", "--strict"])
    timed = CliRunner().invoke(app, ["doctor", "--timings"])

    assert result.exit_code == 0
    assert "Resumen del diagnóstico" in result.output
    assert "Tiempos" not in result.output
    assert "Concord funciona" in result.output
    assert strict.exit_code == 1
    assert timed.exit_code == 0
    assert "Tiempos" in timed.output
    assert "Targets" in timed.output
    assert "Total" in timed.output


def test_reset_dry_run_and_reset_remove_only_concord_state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    configure_environment(home, monkeypatch)
    repository = home / ".local/share/concord/repository"
    Initializer().initialize(
        repository,
        git_identity=("Concord Test", "concord@example.com"),
    )
    target = home / ".bashrc"
    target.write_text("alias ll='ls -la'\n")
    TargetManager().add(target, "bash")
    runner = CliRunner()

    preview = runner.invoke(app, ["reset", "--dry-run"])

    assert preview.exit_code == 0, preview.output
    assert "simulación" in preview.output
    assert repository.exists()
    assert target.exists()

    result = runner.invoke(app, ["reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert not (home / ".config/concord").exists()
    assert not (home / ".local/share/concord").exists()
    assert target.read_text() == "alias ll='ls -la'\n"


def test_reset_rejects_unsafe_repository_before_deleting(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    configure_environment(home, monkeypatch)
    ConfigManager().save(Config(repository_path=home))

    result = CliRunner().invoke(app, ["reset", "--yes"])

    assert result.exit_code == 1
    assert "Ruta insegura" in result.output
    assert (home / ".config/concord/concord.toml").exists()


def test_reset_removes_a_verified_custom_repository(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    configure_environment(home, monkeypatch)
    repository = home / "dotfiles"
    Initializer().initialize(
        repository,
        git_identity=("Concord Test", "concord@example.com"),
    )

    result = CliRunner().invoke(app, ["reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert not repository.exists()
    assert not (home / ".config/concord").exists()
    assert not (home / ".local/share/concord").exists()


def test_pkgbuild_matches_project_metadata():
    root = Path(__file__).parents[1]
    with (root / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)["project"]
    pkgbuild = (root / "PKGBUILD").read_text()
    srcinfo = (root / ".SRCINFO").read_text()

    assert f"pkgver={project['version']}" in pkgbuild
    assert "license=('MIT')" in pkgbuild
    assert "'python-questionary'" in pkgbuild
    assert "'github-cli: crear y autenticar repositorios remotos en GitHub'" in pkgbuild
    assert "PYTHONPATH=src python -m pytest" in pkgbuild
    assert "/usr/share/bash-completion/completions/concord" in pkgbuild
    assert "/usr/share/zsh/site-functions/_concord" in pkgbuild
    assert "/usr/share/fish/vendor_completions.d/concord.fish" in pkgbuild
    assert f"\tpkgver = {project['version']}" in srcinfo
    assert "\tdepends = python-questionary" in srcinfo
