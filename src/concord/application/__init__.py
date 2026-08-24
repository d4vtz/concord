from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

APP = "concord"


config_dir = Path(user_config_dir(APP))
data_dir = Path(user_data_dir(APP))
config_file = config_dir / f"{APP}.toml"
database_file = data_dir / f"{APP}.db"
default_repository_dir = data_dir / "repository"


def is_initialized() -> bool:
    return config_file.exists()
