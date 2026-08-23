from concord.application.file import File
from pathlib import Path
import pytest


def test_target_id(tmp_path):
    target_path = tmp_path / "nvim"
    target_path.mkdir()

    file_path = target_path / "init.lua"
    file_path.touch()
    file = File(
        target_id="123",
        local_path=file_path,
        target_path=target_path,
    )
    assert file.target_id == "123"


def test_local_path(tmp_path):
    target_path = tmp_path / "nvim"
    target_path.mkdir()
    file_path = target_path / "init.lua"
    file_path.touch()
    file = File(
        target_id="123",
        local_path=file_path,
        target_path=target_path,
    )
    assert file.local_path == file_path


def test_target_path(tmp_path):
    target_path = tmp_path / "nvim"
    target_path.mkdir()
    file_path = target_path / "init.lua"
    file_path.touch()
    file = File(
        target_id="123",
        local_path=file_path,
        target_path=target_path,
    )
    assert file.target_path == target_path


def test_relative_path(tmp_path, monkeypatch):
    target_path = tmp_path / "nvim"
    file_path = target_path / "lua" / "options.lua"
    file_path.parent.mkdir(parents=True)
    file_path.touch()
    monkeypatch.setenv("HOME", str(target_path))
    file = File(
        target_id="123",
        local_path=file_path,
        target_path=target_path,
    )
    assert file.relative_path == Path("lua/options.lua")
