from pathlib import Path

import pytest
from typer.testing import CliRunner

from concord import application as concord
from concord.application.config import Config, ConfigManager
from concord.application.database import Database
from concord.application.profile_manager import ProfileManager
from concord.application.repository import RepositoryManager
from concord.application.target_manager import TargetManager
from concord.cli.app import app, request_checkbox, request_order


@pytest.fixture
def profile_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    profiles = ProfileManager(database)

    def add_target(name: str):
        source = home / f".{name}"
        source.write_text(name)
        return targets.add(source, name)

    return targets, profiles, add_target


def test_profiles_are_normalized_and_exported(profile_environment):
    targets, profiles, add_target = profile_environment
    bash = add_target("bash")
    profile = profiles.create("LiNuX", description="Linux desktop")
    profiles.update("linux", targets=["bash"])

    config = ConfigManager().load()

    assert profile.name == "linux"
    assert profiles.get("LINUX").description == "Linux desktop"
    assert config.minimum_concord_version == "2.3.1"
    assert "tags" not in concord.config_file.read_text()
    assert config.profiles[0].targets[0].id == bash.id
    assert next(target for target in config.targets if target.name == "bash").id == bash.id


def test_profile_resolution_preserves_order_and_protects_primary(profile_environment):
    _, profiles, add_target = profile_environment
    for name in ("shell", "editor", "git", "qtile"):
        add_target(name)
    profiles.create("base")
    profiles.update("base", targets=["shell", "editor", "git"])
    profiles.create("linux")
    profiles.update("linux", includes=["base"], targets=["qtile"])
    profiles.create("remove-git")
    profiles.update("remove-git", excludes=["git"])
    profiles.create("extra")
    profiles.update("extra", targets=["git"])

    profiles.activate("linux", ["remove-git", "extra"])
    resolution = profiles.resolve_active()

    assert resolution is not None
    assert resolution.target_names == ["shell", "editor", "git", "qtile"]
    assert "protegido 'git'" in resolution.warnings[0]


def test_later_complement_readds_excluded_target_at_end(profile_environment):
    _, profiles, add_target = profile_environment
    for name in ("one", "two", "three"):
        add_target(name)
    profiles.create("primary")
    profiles.update("primary", targets=["one"])
    profiles.create("first")
    profiles.update("first", targets=["two", "three"])
    profiles.create("second")
    profiles.update("second", excludes=["two"])
    profiles.create("third")
    profiles.update("third", targets=["two"])

    profiles.activate("primary", ["first", "second", "third"])

    assert profiles.resolve_active().target_names == ["one", "three", "two"]


def test_cycle_is_rejected_without_changing_profile(profile_environment):
    _, profiles, _ = profile_environment
    profiles.create("one")
    profiles.create("two")
    profiles.update("one", includes=["two"])

    with pytest.raises(ValueError, match="ciclo"):
        profiles.update("two", includes=["one"])

    assert profiles.get("two").includes == []


def test_active_profiles_filter_target_operations(profile_environment):
    targets, profiles, add_target = profile_environment
    add_target("one")
    add_target("two")
    profiles.create("selected")
    profiles.update("selected", targets=["two"])
    profiles.activate("selected")

    assert [target.name for target in targets.selected()] == ["two"]
    assert [status.name for status in targets.status()] == ["two"]
    assert [difference.name for difference in targets.diff()] == ["two"]
    assert targets.get("one").name == "one"


def test_active_profile_cannot_be_deleted_and_repeated_complement_moves_last(
    profile_environment,
):
    _, profiles, _ = profile_environment
    for name in ("main", "first", "second"):
        profiles.create(name)
    active = profiles.activate("main", ["first", "second", "first"])

    assert active.complements == ["second", "first"]
    with pytest.raises(ValueError, match="activo"):
        profiles.delete("first")


def test_manifest_import_replaces_profiles_and_keeps_stable_references(profile_environment):
    targets, profiles, add_target = profile_environment
    target = add_target("bash")
    profile = profiles.create("linux")
    profiles.update("linux", targets=["bash"])
    profiles.suggest("linux")
    manifest = ConfigManager().load()

    profiles.create("temporary")
    ConfigManager().save(manifest)
    targets.import_manifest(replace=True)

    assert [item.name for item in profiles.list()] == ["linux"]
    assert profiles.get("linux").id == profile.id
    assert profiles.resolve("linux").target_ids == [target.id]
    assert profiles.suggestion().primary == "linux"
    assert manifest.profiles[0].id == profile.id


def test_deactivate_primary_requires_replacement_or_all(profile_environment):
    _, profiles, _ = profile_environment
    profiles.create("one")
    profiles.create("two")
    profiles.activate("one", ["two"])

    with pytest.raises(ValueError, match="replace-with"):
        profiles.deactivate("one")

    assert profiles.deactivate("one", replace_with="two").primary == "two"
    profiles.deactivate_all()
    assert profiles.activation() is None


def test_declined_suggestion_is_offered_again_only_after_it_changes(profile_environment):
    _, profiles, _ = profile_environment
    profiles.create("one")
    profiles.create("two")
    profiles.suggest("one")

    assert profiles.should_offer_suggestion()
    profiles.decline_suggestion()
    assert not profiles.should_offer_suggestion()

    profiles.suggest("two")
    assert profiles.should_offer_suggestion()


def test_profile_cli_lifecycle_and_filtered_list(profile_environment, monkeypatch):
    targets, _, add_target = profile_environment
    add_target("bash")
    add_target("nvim")
    config = ConfigManager().load()
    config.git.enabled = False
    ConfigManager().save(config)
    monkeypatch.setattr("concord.cli.app.manager", lambda: targets)
    runner = CliRunner()

    created = runner.invoke(app, ["profile", "create", "BASE"])
    edited = runner.invoke(
        app, ["profile", "edit", "base", "--target", "bash"]
    )
    activated = runner.invoke(
        app, ["profile", "activate", "--primary", "base"]
    )
    listed = runner.invoke(app, ["list"])
    listed_all = runner.invoke(app, ["list", "--all"])

    assert created.exit_code == 0, created.output
    assert edited.exit_code == 0, edited.output
    assert activated.exit_code == 0, activated.output
    assert "bash" in listed.output
    assert "nvim" not in listed.output
    assert "bash" in listed_all.output and "nvim" in listed_all.output


def test_external_profile_manifest_change_is_detected(profile_environment):
    targets, profiles, _ = profile_environment
    profiles.create("local")
    external = ConfigManager().load()
    external.profiles = []
    external.minimum_concord_version = None
    ConfigManager().save(external)

    reopened = TargetManager(targets.database, targets.repository, targets.config_manager)

    assert reopened.profile_manifest_changed


def test_import_that_removes_active_profile_blocks_until_activation_is_repaired(
    profile_environment,
):
    targets, profiles, _ = profile_environment
    profiles.create("active")
    profiles.activate("active")
    config = ConfigManager().load()
    config.profiles = []
    config.minimum_concord_version = None
    ConfigManager().save(config)

    targets.import_manifest(replace=True)

    with pytest.raises(ValueError, match="activación local"):
        profiles.activation()
    profiles.deactivate_all()
    assert profiles.activation() is None


def test_empty_interactive_checkbox_returns_empty_selection(monkeypatch):
    def unexpected_checkbox(*args, **kwargs):
        raise AssertionError("Questionary no debe recibir choices=[]")

    monkeypatch.setattr("concord.cli.app.questionary.checkbox", unexpected_checkbox)

    assert request_checkbox("Sin opciones:", []) == []


def test_complements_can_be_ordered_interactively(monkeypatch):
    answers = iter(["third", "first"])

    class Prompt:
        def ask(self):
            return next(answers)

    monkeypatch.setattr("concord.cli.app.questionary.select", lambda *args, **kwargs: Prompt())

    assert request_order(["first", "second", "third"]) == ["third", "first", "second"]


def test_internal_concord_target_cannot_be_assigned_to_a_profile(profile_environment):
    _, profiles, _ = profile_environment
    profiles.create("base")

    with pytest.raises(ValueError, match="interno 'concord'"):
        profiles.update("base", targets=["concord"])

    assert profiles.get("base").targets == []
