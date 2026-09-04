from pathlib import Path

import pytest

from concord import application as concord
from concord.application.config import Config, ConfigManager
from concord.application.database import Database
from concord.application.repository import RepositoryManager
from concord.application.secret_manager import (PARTIAL_SUFFIX, Age,
                                                SecretManager)
from concord.application.target_manager import TargetManager


@pytest.fixture
def secret_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    monkeypatch.setattr(Age, "available", staticmethod(lambda: True))
    monkeypatch.setattr(Age, "generate_identity", classmethod(lambda cls: ("AGE-SECRET-KEY-TEST", "age1test")))
    monkeypatch.setattr(Age, "encrypt_passphrase", classmethod(lambda cls, data, password: b"age-encryption.org/v1\n" + password.encode() + b":" + data))
    monkeypatch.setattr(Age, "decrypt_passphrase", classmethod(lambda cls, data, password: data.split(b":", 1)[1] if data.startswith(b"age-encryption.org/v1\n" + password.encode() + b":") else (_ for _ in ()).throw(ValueError("bad password"))))
    monkeypatch.setattr(Age, "encrypt", classmethod(lambda cls, data, recipient: b"age-encryption.org/v1\n" + data))
    monkeypatch.setattr(Age, "decrypt", classmethod(lambda cls, data, identity: data.split(b"\n", 1)[1]))
    ConfigManager().save(Config(repository_path=repository))
    database = Database(data_dir / "concord.db")
    secrets = SecretManager(database)
    targets = TargetManager(database, RepositoryManager(repository), secret_manager=secrets)
    backup = tmp_path / "recovery.age"
    secrets.initialize("master", "recovery", backup)
    return targets, secrets, home, repository, backup


def test_complete_file_is_only_stored_encrypted(secret_environment):
    targets, secrets, home, repository, backup = secret_environment
    source = home / ".env"
    source.write_text("TOKEN=very-secret\n")
    target = targets.add(source, "environment")
    secrets.protect(source, targets.list())
    targets.sync(target.name)

    stored = repository / "environment/.env"
    assert not stored.exists()
    assert stored.with_name(".env.age").read_bytes().startswith(b"age-encryption.org/v1")
    assert "very-secret" not in concord.config_file.read_text()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert targets.sync(target.name) == []

    source.unlink()
    targets.restore(target.name)
    assert source.read_text() == "TOKEN=very-secret\n"


def test_partial_secret_creates_template_and_restores(secret_environment):
    targets, secrets, home, repository, _ = secret_environment
    source = home / ".config/app.conf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("user=dav\ntoken=abc123\nagain=abc123\n")
    target = targets.add(source, "app")
    secret = secrets.set_value(source, "token", "abc123", targets.list())
    targets.sync(target.name)

    stored = repository / "app/.config/app.conf"
    assert stored.read_text().count("{{ concord_secret: token }}") == 2
    assert stored.with_name(stored.name + PARTIAL_SUFFIX).is_file()
    assert secret.names == ["token"]
    assert targets.sync(target.name) == []

    source.unlink()
    targets.restore(target.name)
    assert source.read_text().count("abc123") == 2


def test_unprotect_withdraws_file_without_publishing_plaintext(secret_environment):
    targets, secrets, home, repository, _ = secret_environment
    source = home / ".password"
    source.write_text("do-not-publish")
    target = targets.add(source, "password")
    secrets.protect(source, targets.list())
    targets.sync(target.name)
    secrets.unprotect(source, targets.list())
    targets.sync(target.name)

    assert source.read_text() == "do-not-publish"
    assert not (repository / "password/.password").exists()
    assert not (repository / "password/.password.age").exists()
    assert secrets.for_target(target.id)[0].kind == "excluded"


def test_manifest_contains_wrappers_but_no_password_or_plaintext(secret_environment):
    targets, secrets, home, _, _ = secret_environment
    source = home / ".api"
    source.write_text("secret-value")
    targets.add(source, "api")
    secrets.protect(source, targets.list())
    targets._persist_manifest()

    manifest = concord.config_file.read_text()
    assert "secret_group" in manifest
    assert "master =" not in manifest
    assert "recovery =" not in manifest
    assert "secret-value" not in manifest
    assert ConfigManager().load().minimum_concord_version == "2.8.0"


def test_partial_sync_failure_keeps_previous_repository_copy(secret_environment):
    targets, secrets, home, repository, _ = secret_environment
    source = home / ".service"
    source.write_text("token=initial")
    target = targets.add(source, "service")
    secrets.set_value(source, "token", "initial", targets.list())
    targets.sync(target.name)
    stored = repository / "service/.service"
    previous = stored.read_bytes()

    source.write_text("token=changed-outside-concord")
    with pytest.raises(ValueError, match="No se encontró"):
        targets.sync(target.name)

    assert stored.read_bytes() == previous


def test_wrong_password_does_not_unlock(secret_environment):
    _, secrets, *_ = secret_environment
    secrets._identity = None
    with pytest.raises(ValueError, match="bad password"):
        secrets.unlock("wrong")
    assert not secrets.unlocked


def test_external_recovery_restores_identity(secret_environment):
    _, secrets, _, _, backup = secret_environment
    secrets._identity = None
    secrets.recover_from_backup(backup, "recovery")
    assert secrets.unlocked
