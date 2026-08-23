from concord.application.target_manager import TargetManager
from concord.application.database import Database
from concord.application.repository import RepositoryManager


def test_add_file(tmp_path):
    source = tmp_path / ".bashrc"
    source.write_text("alias ll='ls -lah'\n")

    repository_path = tmp_path / "repository"
    database_path = tmp_path / "concord.db"

    database = Database(database_path)
    database.initialize()

    repository = RepositoryManager(repository_path)

    manager = TargetManager(database, repository)

    manager.add(source)

    destination = repository_path / ".bashrc" / ".bashrc"

    assert destination.exists()
    assert destination.is_file()


def test_add_file_preserves_content(tmp_path):
    source = tmp_path / ".bashrc"
    content = "alias ll='ls -lah'\n"
    source.write_text(content)

    repository_path = tmp_path / "repository"
    database_path = tmp_path / "concord.db"

    database = Database(database_path)
    database.initialize()

    repository = RepositoryManager(repository_path)

    manager = TargetManager(database, repository)

    manager.add(source)

    destination = repository_path / ".bashrc" / ".bashrc"

    assert destination.read_text() == content


def test_add_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    source = home / ".config/nvim"

    (source / "lua").mkdir(parents=True)

    (source / "init.lua").write_text("-- init")
    (source / "lua" / "options.lua").write_text("-- options")
    (source / "lua" / "keymaps.lua").write_text("-- keymaps")

    repository_path = tmp_path / "repository"
    database_path = tmp_path / "concord.db"

    database = Database(database_path)
    database.initialize()

    repository = RepositoryManager(repository_path)

    manager = TargetManager(database, repository)

    manager.add(source)

    destination = repository_path / "nvim/.config/nvim/"

    assert (destination / "init.lua").exists()
    assert (destination / "lua" / "options.lua").exists()
    assert (destination / "lua" / "keymaps.lua").exists()


def test_add_saves_target(tmp_path):
    source = tmp_path / ".bashrc"
    source.write_text("alias ll='ls -lah'\n")

    repository_path = tmp_path / "repository"
    database_path = tmp_path / "concord.db"

    database = Database(database_path)
    database.initialize()

    repository = RepositoryManager(repository_path)

    manager = TargetManager(database, repository)

    manager.add(source)

    with database.connect() as connection:
        row = connection.execute("""
            SELECT name, local_path, type
            FROM targets
            """).fetchone()

    assert row == (
        ".bashrc",
        str(source),
        "file",
    )


def test_add_directory_saves_target(tmp_path):
    source = tmp_path / "nvim"
    source.mkdir()

    repository_path = tmp_path / "repository"
    database_path = tmp_path / "concord.db"

    database = Database(database_path)
    database.initialize()

    repository = RepositoryManager(repository_path)

    manager = TargetManager(database, repository)

    manager.add(source)

    with database.connect() as connection:
        row = connection.execute("""
            SELECT name, local_path, type
            FROM targets
            """).fetchone()

    assert row == (
        "nvim",
        str(source),
        "directory",
    )
