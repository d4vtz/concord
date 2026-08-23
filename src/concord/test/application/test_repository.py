from concord.application.repository import RepositoryManager


def test_create_directory(tmp_path):
    repository = RepositoryManager(tmp_path)

    path = tmp_path / "nvim"

    repository.create(path)

    assert path.exists()
    assert path.is_dir()


def test_create_nested_directory(tmp_path):
    repository = RepositoryManager(tmp_path)

    path = tmp_path / "nvim" / ".config" / "nvim"

    repository.create(path)

    assert path.is_dir()


def test_create_existing_directory(tmp_path):
    repository = RepositoryManager(tmp_path)

    path = tmp_path / "nvim"
    path.mkdir()

    repository.create(path)

    assert path.is_dir()


def test_repository_path(tmp_path):
    repository = RepositoryManager(tmp_path)

    assert repository.repository_path == tmp_path
