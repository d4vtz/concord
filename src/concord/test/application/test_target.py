from datetime import datetime, timezone
from pathlib import Path

import pytest

from concord.application.file import File
from concord.application.target import Target, TargetType


def test_resolve_path(tmp_path):
    path = tmp_path / "concord"
    path.mkdir()
    target = Target(path)
    assert target.local_path == path.resolve()


def test_expanduser_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    concord = home / ".config/concord"
    concord.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    target = Target(Path("~/.config/concord"))
    assert target.local_path == concord.resolve()


def test_reject_noexistent_path(tmp_path):
    path = tmp_path / "noexistent_path"
    with pytest.raises(FileNotFoundError):
        Target(path)


def test_detect_file(tmp_path):
    path = tmp_path / "concord"
    path.mkdir(parents=True)
    file = path / "file"
    file.touch()
    target = Target(file)
    assert target.type == TargetType.FILE


def test_detect_directory(tmp_path):
    path = tmp_path / "concord"
    path.mkdir(parents=True)
    target = Target(path)
    assert target.type == TargetType.DIRECTORY


def test_generate_id(tmp_path):
    path = tmp_path / "concord"
    path.mkdir(parents=True)
    target = Target(path)
    assert target.id
    assert isinstance(target.id, str)


def test_generates_unique_id(tmp_path):
    path = tmp_path / "file"
    path.touch()
    target1 = Target(path)
    target2 = Target(path)
    assert target1.id != target2.id


def test_registers_created_at(tmp_path):
    path = tmp_path / "file"
    path.touch()
    target = Target(path)
    assert isinstance(target.created_at, datetime)


def test_created_at_is_current_time(tmp_path):
    path = tmp_path / "file"
    path.touch()
    before = datetime.now(timezone.utc)
    target = Target(path)
    after = datetime.now(timezone.utc)
    assert before <= target.created_at <= after


def test_generate_name(tmp_path):
    path = tmp_path / "file"
    path.touch()
    target = Target(path)
    assert target.name == "file"


def test_get_files_empty_directory(tmp_path):
    path = tmp_path / "concord"
    path.mkdir()
    target = Target(path)
    files = target.get_files()
    assert files == []


def test_get_files_single_file(tmp_path):
    path = tmp_path / "concord"
    path.mkdir()
    file_path = path / "file"
    file_path.touch()
    target = Target(path)
    files = target.get_files()
    assert len(files) == 1
    assert isinstance(files[0], File)


def test_target_file_id(tmp_path):
    path = tmp_path / "concord"
    path.mkdir()
    file_path = path / "file"
    file_path.touch()
    target = Target(path)
    files = target.get_files()
    assert files[0].target_id == target.id


def test_target_file_path(tmp_path):
    path = tmp_path / "concord"
    path.mkdir()
    file_path = path / "file"
    file_path.touch()
    target = Target(path)
    files = target.get_files()
    assert files[0].target_path == target.local_path
